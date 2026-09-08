# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import urllib.parse
from collections import Counter

import frappe
from frappe.query_builder import DocType
from frappe.query_builder.functions import Coalesce, Max, Sum
from frappe.utils import add_to_date, cint, create_batch, get_datetime, now

from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository import AmazonRepository
from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api import SPAPIError
from ecommerce_integrations.amazon.utils import MODULE_NAME, SETTING_DOCTYPE, create_amazon_log
from ecommerce_integrations.controllers.inventory import update_inventory_sync_status

FULFILLMENT_CHANNEL_CODE = "DEFAULT"  # seller-fulfilled; Amazon FBA listings ignore this feed
ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD = "amazon_product_type"


def _due_for_inventory_sync(setting) -> bool:
	"""Per-record equivalent of `controllers.scheduling.need_to_run` — that helper
	only works for Single DocTypes (it reads/writes via the `Singles` table), but
	Amazon SP API Settings can have multiple records, so each one needs its own
	watermark read/write against its own `last_inventory_sync` field instead.
	"""
	interval = cint(setting.inventory_sync_frequency) or 10

	if setting.last_inventory_sync and get_datetime() < get_datetime(
		add_to_date(setting.last_inventory_sync, minutes=interval)
	):
		return False

	frappe.db.set_value(SETTING_DOCTYPE, setting.name, "last_inventory_sync", now(), update_modified=False)
	return True


def update_inventory_on_amazon() -> None:
	"""Merge stock across the warehouses configured in `Amazon Warehouse Mapping`
	and push one nationwide available quantity per Item to Amazon.

	Called by scheduler on the configured interval.
	"""
	for setting_name in frappe.get_all(SETTING_DOCTYPE, {"is_active": 1}, pluck="name"):
		setting = frappe.get_doc(SETTING_DOCTYPE, setting_name)

		if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_amazon:
			continue

		if not _due_for_inventory_sync(setting):
			continue

		warehouses = setting.get_merged_warehouses()
		if not warehouses:
			continue

		# single synthetic group: Amazon has no per-location concept for
		# seller-fulfilled listings, so all configured warehouses fold into one qty.
		inventory_levels = get_amazon_inventory_and_price_changes(warehouses, setting.price_list)

		if inventory_levels:
			upload_inventory_data_to_amazon(setting, inventory_levels)


def get_amazon_inventory_and_price_changes(warehouses: list[str], price_list: str | None) -> list:
	"""Like `controllers.inventory.get_inventory_levels_aggregated`, but for Amazon
	specifically: also triggers on a price-only change (no stock movement), since
	Amazon listings also carry price and that must stay in sync too. Kept local to
	Amazon rather than changing the shared Shopify/Amazon stock-aggregation helper.
	"""
	EcommerceItem = DocType("Ecommerce Item")
	Bin = DocType("Bin")
	ItemPrice = DocType("Item Price")

	query = (
		frappe.qb.from_(EcommerceItem)
		.join(Bin)
		.on(EcommerceItem.erpnext_item_code == Bin.item_code)
		.left_join(ItemPrice)
		.on(
			(ItemPrice.item_code == EcommerceItem.erpnext_item_code)
			& (ItemPrice.price_list == price_list)
			& (ItemPrice.selling == 1)
		)
		.select(
			EcommerceItem.name.as_("ecom_item"),
			Bin.item_code.as_("item_code"),
			EcommerceItem.sku,
			EcommerceItem.integration_item_code,
			EcommerceItem.variant_id,
			Sum(Bin.actual_qty).as_("actual_qty"),
			Sum(Bin.reserved_qty).as_("reserved_qty"),
		)
		.where((Bin.warehouse.isin(warehouses)) & (EcommerceItem.integration == MODULE_NAME))
		.groupby(EcommerceItem.erpnext_item_code)
		.having(
			(Max(Bin.modified) > Coalesce(Max(EcommerceItem.inventory_synced_on), "1970-01-01 00:00:00"))
			| (Max(ItemPrice.modified) > Coalesce(Max(EcommerceItem.inventory_synced_on), "1970-01-01 00:00:00"))
		)
	)

	return query.run(as_dict=1)


def fetch_amazon_listing_data(listings_api, sku: str, need_product_type: bool, need_fba_check: bool) -> dict:
	"""One GET to the Listings Items API, requesting only the sections needed:
	`summaries` to backfill `amazon_product_type` when it wasn't set at mapping
	time (Amazon requires a product type on every listing patch), and/or
	`fulfillmentAvailability` to detect whether Amazon currently fulfills this
	SKU (FBA) rather than the seller (FBM).

	`is_fba` is re-derived on every call — never cached beyond the
	`Ecommerce Item.is_fba` field it's written into — so a SKU flipping
	between FBA and FBM on Amazon's side is picked up automatically on its
	next sync. Anything other than an explicit `DEFAULT` (seller-fulfilled)
	channel code — including an empty/missing list — is treated as FBA, so we
	default to *not* pushing stock when the response is ambiguous: a skipped
	FBM push is a real miss, but a skipped FBA push is a no-op anyway.
	"""
	included_data = []
	if need_product_type:
		included_data.append("summaries")
	if need_fba_check:
		included_data.append("fulfillmentAvailability")

	data = listings_api.get_listing_item(sku=sku, included_data=included_data)

	result = {}
	if need_product_type:
		summaries = data.get("summaries") or []
		result["product_type"] = summaries[0].get("productType") if summaries else None
	if need_fba_check:
		channels = data.get("fulfillmentAvailability") or []
		result["is_fba"] = not any(c.get("fulfillmentChannelCode") == FULFILLMENT_CHANNEL_CODE for c in channels)

	return result


def _log_fba_transition(ecom_item: str, is_fba: bool, was_fba) -> None:
	"""Record an FBA<->FBM flip as a Comment — `frappe.db.set_value`/`db_set`
	don't create a Version log entry, so without this, an auto-detected
	transition on this user-overridable field would leave no trace at all.
	"""
	if cint(is_fba) == cint(was_fba):
		return
	frappe.get_doc("Ecommerce Item", ecom_item).add_comment(
		"Info",
		f"Amazon fulfillment channel auto-detected as {'FBA' if is_fba else 'FBM'} "
		f"during sync (was {'FBA' if was_fba else 'FBM'}).",
	)


def upload_inventory_data_to_amazon(setting, inventory_levels) -> None:
	repo = AmazonRepository(setting)
	listings_api = repo.get_listings_items_instance()
	synced_on = now()
	currency = frappe.db.get_value("Price List", setting.price_list, "currency") or "USD"

	for inventory_sync_batch in create_batch(inventory_levels, 50):
		for d in inventory_sync_batch:
			available_qty = max(cint(d.actual_qty) - cint(d.reserved_qty), 0)
			sku = d.sku or d.item_code

			try:
				product_type, was_fba = frappe.db.get_value(
					"Ecommerce Item", d.ecom_item, [ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD, "is_fba"]
				)
				need_product_type = not product_type
				listing_data = fetch_amazon_listing_data(listings_api, sku, need_product_type, True)

				if need_product_type:
					product_type = listing_data.get("product_type")
					if not product_type:
						raise SPAPIError(
							error="missing_product_type",
							error_description=(
								f"Ecommerce Item {d.ecom_item} has no Amazon Product Type set, and it "
								f"could not be auto-fetched from Amazon (SKU {sku} may not be listed yet)."
							),
						)
					frappe.db.set_value(
						"Ecommerce Item",
						d.ecom_item,
						ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD,
						product_type,
						update_modified=False,
					)

				is_fba = listing_data["is_fba"]
				frappe.db.set_value("Ecommerce Item", d.ecom_item, "is_fba", cint(is_fba), update_modified=False)
				_log_fba_transition(d.ecom_item, is_fba, was_fba)

				price = setting.get_item_price(d.item_code)
				patches = _build_stock_price_patches(available_qty, price, currency, skip_stock=is_fba)
				patches += _build_image_patches(setting, d.item_code)

				if patches:
					listings_api.patch_listing_item(sku=sku, product_type=product_type, patches=patches)
				update_inventory_sync_status(d.ecom_item, time=synced_on, status="Synced")
				d.status = "Success"
			except SPAPIError as e:
				frappe.db.set_value(
					"Ecommerce Item",
					d.ecom_item,
					{"sync_status": "Error", "sync_error": f"{e.error}: {e.error_description}"},
					update_modified=False,
				)
				d.status = "Failed"
				d.failure_reason = f"{e.error}: {e.error_description}"
			except Exception as e:
				frappe.db.set_value(
					"Ecommerce Item",
					d.ecom_item,
					{"sync_status": "Error", "sync_error": str(e)},
					update_modified=False,
				)
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status(inventory_sync_batch)


def push_single_item_to_amazon(setting, ecom_doc) -> str:
	"""Push one Ecommerce Item's current stock, price, and images (if enabled)
	to Amazon right now — used by the "Sync Now" button, bypassing the "did
	it change since last sync" watermark the scheduled job uses.

	If `amazon_product_type` isn't set on the Ecommerce Item yet, it's looked
	up live from Amazon (Listings Items API) and saved back onto the record —
	Amazon requires a product type on every patch call, but sellers don't
	always set it when mapping an already-published listing.
	"""
	item_code = ecom_doc.erpnext_item_code
	sku = ecom_doc.sku or item_code

	repo = AmazonRepository(setting)
	listings_api = repo.get_listings_items_instance()

	product_type = ecom_doc.get(ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD)
	need_product_type = not product_type
	listing_data = fetch_amazon_listing_data(listings_api, sku, need_product_type, True)

	if need_product_type:
		product_type = listing_data.get("product_type")
		if not product_type:
			frappe.throw(
				f"Ecommerce Item {ecom_doc.name} has no Amazon Product Type set, and it "
				f"could not be auto-fetched from Amazon (SKU {sku} may not be listed yet)."
			)
		ecom_doc.db_set(ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD, product_type, update_modified=False)

	is_fba = listing_data["is_fba"]
	was_fba = ecom_doc.get("is_fba")
	ecom_doc.db_set("is_fba", cint(is_fba), update_modified=False)
	_log_fba_transition(ecom_doc.name, is_fba, was_fba)

	warehouses = setting.get_merged_warehouses()
	if not warehouses:
		return "skipped (no warehouses configured in Amazon SP API Settings)"

	Bin = DocType("Bin")
	bin_totals = (
		frappe.qb.from_(Bin)
		.select(Sum(Bin.actual_qty).as_("actual_qty"), Sum(Bin.reserved_qty).as_("reserved_qty"))
		.where((Bin.item_code == item_code) & (Bin.warehouse.isin(warehouses)))
		.run(as_dict=True)
	)
	actual_qty = (bin_totals[0].actual_qty if bin_totals else 0) or 0
	reserved_qty = (bin_totals[0].reserved_qty if bin_totals else 0) or 0
	available_qty = max(cint(actual_qty) - cint(reserved_qty), 0)

	currency = frappe.db.get_value("Price List", setting.price_list, "currency") or "USD"
	price = setting.get_item_price(item_code)

	patches = _build_stock_price_patches(available_qty, price, currency, skip_stock=is_fba)
	patches += _build_image_patches(setting, item_code)

	if patches:
		listings_api.patch_listing_item(sku=sku, product_type=product_type, patches=patches)
	update_inventory_sync_status(ecom_doc.name, time=now(), status="Synced")

	if is_fba and not patches:
		return "skipped (FBA item — stock not pushed; no price to sync either)"
	elif is_fba:
		return "price synced; stock skipped (FBA — Amazon manages this SKU's inventory)"
	else:
		return f"pushed (actual_qty={actual_qty}, reserved_qty={reserved_qty})"


def _build_stock_price_patches(available_qty, price, currency, skip_stock: bool = False) -> list:
	patches = []
	if not skip_stock:
		patches.append(
			{
				"op": "replace",
				"path": "/attributes/fulfillment_availability",
				"value": [{"fulfillment_channel_code": FULFILLMENT_CHANNEL_CODE, "quantity": available_qty}],
			}
		)
	if price:
		patches.append(
			{
				"op": "replace",
				"path": "/attributes/purchasable_offer",
				"value": [{"currency": currency, "our_price": [{"schedule": [{"value_with_tax": price}]}]}],
			}
		)
	return patches


def _build_image_patches(setting, item_code: str) -> list:
	"""Item.image -> main_product_image_locator; every other File attachment
	on the Item (in creation order) -> other_product_image_locator_1..8.

	Only included if 'Sync Images to Amazon' is enabled in settings. Amazon
	fetches images by URL rather than accepting uploads, so this only works
	if the site is reachable on the public internet at the URL
	`frappe.utils.get_url()` resolves to (its configured host_name).
	"""
	if not setting.get("sync_images_to_amazon"):
		return []

	image = frappe.db.get_value("Item", item_code, "image")
	site_url = frappe.utils.get_url()
	patches = []

	if image:
		patches.append(
			{
				"op": "replace",
				"path": "/attributes/main_product_image_locator",
				"value": [{"media_location": f"{site_url}{_encode_file_url(image)}"}],
			}
		)

	other_files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Item",
			"attached_to_name": item_code,
			"file_url": ["!=", image or ""],
			"is_private": 0,
		},
		fields=["file_url"],
		order_by="creation asc",
		limit=8,
	)

	for idx, file_row in enumerate(other_files, start=1):
		patches.append(
			{
				"op": "replace",
				"path": f"/attributes/other_product_image_locator_{idx}",
				"value": [{"media_location": f"{site_url}{_encode_file_url(file_row.file_url)}"}],
			}
		)

	return patches


def _encode_file_url(file_url: str) -> str:
	"""Percent-encode a Frappe file_url's path segments (e.g. spaces in the
	filename) so the resulting media URL is valid for Amazon's image crawler
	to fetch — a raw space in the URL gets rejected outright.
	"""
	return urllib.parse.quote(file_url, safe="/")


def _log_inventory_update_status(inventory_levels) -> None:
	log_message = "sku,item_code,status,failure_reason\n"
	log_message += "\n".join(
		f"{d.sku or d.item_code},{d.item_code},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])
	percent_successful = stats["Success"] / len(inventory_levels)

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	log_message = f"Updated {percent_successful * 100}% items\n\n" + log_message

	create_amazon_log(method="update_inventory_on_amazon", status=status, message=log_message)
