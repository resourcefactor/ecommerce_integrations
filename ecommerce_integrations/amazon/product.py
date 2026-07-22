# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _

from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository import AmazonRepository
from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api import SPAPIError
from ecommerce_integrations.amazon.utils import MODULE_NAME, SETTING_DOCTYPE, create_amazon_log

ITEM_PUBLISH_FIELD = "publish_on_amazon"
ITEM_PRODUCT_TYPE_FIELD = "amazon_product_type"


def upload_erpnext_item(doc, method=None):
	"""`Item` doc_event hook (after_insert / on_update).

	Publishes/updates a single Item on Amazon as a listing, gated by the
	`Upload ERPNext Items to Amazon` setting and the item's own
	`publish_on_amazon` checkbox + `amazon_product_type`.
	"""
	item = doc

	if item.flags.from_integration:
		return

	if frappe.flags.in_import or frappe.flags.in_patch:
		return

	if item.has_variants:
		return

	if not item.get(ITEM_PUBLISH_FIELD):
		return

	setting = frappe.get_single(SETTING_DOCTYPE)
	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return

	_publish_item(setting, item)


def publish_items_to_amazon(amz_setting_name: str) -> None:
	"""Publish/update every eligible Item (publish_on_amazon=1, amazon_product_type set).

	Called by the "Publish Items Now" button and can also be run manually
	from bench console.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	if not setting.is_enabled():
		return

	items = frappe.get_all(
		"Item",
		filters={
			ITEM_PUBLISH_FIELD: 1,
			"has_variants": 0,
			"disabled": 0,
		},
		fields=["name"],
	)

	for item_row in items:
		item = frappe.get_doc("Item", item_row.name)
		if not item.get(ITEM_PRODUCT_TYPE_FIELD):
			continue
		_publish_item(setting, item)
		frappe.db.commit()


def _publish_item(setting, item) -> None:
	if not item.get(ITEM_PRODUCT_TYPE_FIELD):
		frappe.log_error(
			title="Amazon publish skipped: missing Amazon Product Type",
			message=f"Item {item.name} has publish_on_amazon checked but no amazon_product_type set.",
		)
		return

	repo = AmazonRepository(setting)
	sku = item.item_code
	product_type = item.get(ITEM_PRODUCT_TYPE_FIELD)
	price = setting.get_item_price(sku)

	listings_api = repo.get_listings_items_instance()
	currency = frappe.db.get_value("Price List", setting.price_list, "currency") or "USD"
	attributes = _build_listing_attributes(item, price, listings_api.marketplace_id, currency)

	try:
		result = listings_api.put_listing_item(sku=sku, product_type=product_type, attributes=attributes)
		status = result.get("status") or "ACCEPTED"
		submission_id = result.get("submissionId")
		issues = result.get("issues") or []

		# Amazon's submission response never includes the ASIN (only sku/status/
		# submissionId/issues) — it's assigned async and only readable later via
		# ListingsItems.get_listing_item(). Mark as pending until backfill_asins runs.
		if issues and status == "INVALID":
			_upsert_ecommerce_item(item, sku=sku, status="Error", error="; ".join(i.get("message", "") for i in issues))
		else:
			_upsert_ecommerce_item(item, sku=sku, status="Pending ASIN")

		create_amazon_log(
			status="Success",
			method="ecommerce_integrations.amazon.product.upload_erpnext_item",
			message=f"Listing submitted for SKU {sku}. status={status} submissionId={submission_id} issues={issues}",
			response_data=result,
		)
	except SPAPIError as e:
		_upsert_ecommerce_item(item, sku=sku, status="Error", error=f"{e.error}: {e.error_description}")
		create_amazon_log(
			status="Error",
			method="ecommerce_integrations.amazon.product.upload_erpnext_item",
			message=f"Failed to publish SKU {sku}: {e.error} - {e.error_description}",
		)


def backfill_asins(amz_setting_name: str | None = None) -> dict:
	"""Fetch and store the real ASIN for listings still marked 'Pending ASIN'.

	Amazon assigns the ASIN asynchronously after a listing submission is
	accepted; it is not present in the putListingsItem/patchListingsItem
	response. This polls GET listings/2021-08-01/items/{sellerId}/{sku}
	(summaries[].asin) for each pending Ecommerce Item and updates
	`integration_item_code` once Amazon reports one.

	Safe to call repeatedly (e.g. hourly via scheduler, or manually from
	console/button) — items are skipped once integration_item_code is a
	real ASIN and sync_status is no longer 'Pending ASIN'.
	"""
	filters = {"integration": MODULE_NAME, "sync_status": "Pending ASIN"}
	if amz_setting_name:
		setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	else:
		setting = frappe.get_single(SETTING_DOCTYPE)

	repo = AmazonRepository(setting)
	listings_api = repo.get_listings_items_instance()

	pending = frappe.get_all("Ecommerce Item", filters=filters, fields=["name", "sku", "erpnext_item_code"])
	results = {"checked": len(pending), "asin_found": 0, "still_pending": 0, "errors": 0}

	for row in pending:
		try:
			item_data = listings_api.get_listing_item(sku=row.sku)
			summaries = item_data.get("summaries") or []
			asin = summaries[0].get("asin") if summaries else None

			if asin:
				frappe.db.set_value(
					"Ecommerce Item",
					row.name,
					{"integration_item_code": asin, "sync_status": "Synced", "sync_error": ""},
					update_modified=False,
				)
				results["asin_found"] += 1
			else:
				results["still_pending"] += 1
		except SPAPIError as e:
			frappe.db.set_value(
				"Ecommerce Item",
				row.name,
				{"sync_error": f"{e.error}: {e.error_description}"},
				update_modified=False,
			)
			results["errors"] += 1

		frappe.db.commit()

	create_amazon_log(
		method="ecommerce_integrations.amazon.product.backfill_asins",
		status="Success" if results["errors"] == 0 else "Partial Success",
		message=str(results),
	)
	return results


def _build_listing_attributes(item, price, marketplace_id, currency="USD") -> dict:
	"""Minimal attribute set built only from fields already mandatory/available on Item.

	Amazon validates this server-side against the item's product_type schema;
	categories that require more attributes (bullet points, brand, images, ...)
	will come back with issues in the response — check Ecommerce Item.sync_error
	or re-fetch via ListingsItems.get_listing_item().
	"""
	condition_type = "new_new"

	attributes = {
		"condition_type": [{"value": condition_type}],
		"item_name": [{"value": item.item_name or item.item_code}],
	}

	if item.description:
		attributes["product_description"] = [{"value": frappe.utils.strip_html(item.description)[:2000]}]

	if price:
		attributes["purchasable_offer"] = [
			{
				"currency": currency,
				"our_price": [{"schedule": [{"value_with_tax": price}]}],
			}
		]

	barcode = frappe.db.get_value("Item Barcode", {"parent": item.name}, "barcode")
	if barcode:
		attributes["externally_assigned_product_identifier"] = [
			{"type": "ean", "value": barcode, "marketplace_id": marketplace_id}
		]

	return attributes


def _upsert_ecommerce_item(item, sku: str, status: str, error: str | None = None) -> None:
	name = frappe.db.get_value(
		"Ecommerce Item", {"erpnext_item_code": item.name, "integration": MODULE_NAME}, "name"
	)

	values = {
		"item_synced_on": frappe.utils.now(),
		"sync_status": status,
		"sync_error": error or "",
	}

	if name:
		frappe.db.set_value("Ecommerce Item", name, values, update_modified=False)
	else:
		ecom_item = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"erpnext_item_code": item.name,
				"integration": MODULE_NAME,
				"integration_item_code": sku,
				"sku": sku,
				"has_variants": 0,
				**values,
			}
		)
		ecom_item.insert(ignore_permissions=True)
