# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _

from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository import AmazonRepository
from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api import SPAPIError
from ecommerce_integrations.amazon.utils import MODULE_NAME, SETTING_DOCTYPE, create_amazon_log

ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD = "amazon_product_type"


@frappe.whitelist()
def get_active_amazon_setting() -> dict:
	"""Resolve the active `Amazon SP API Settings` record name for use by
	client-side actions triggered by users who may not have read access to
	the settings doctype itself — it holds API credentials and is restricted
	to System Manager. Only the `name`/`is_active` lookup is done with
	elevated permission here; nothing sensitive is returned to the client.
	"""
	settings = frappe.get_all(SETTING_DOCTYPE, filters={"is_active": 1}, fields=["name"], limit=2, ignore_permissions=True)

	if not settings:
		frappe.throw(_("No active Amazon SP API Settings found."))

	if len(settings) > 1:
		frappe.msgprint(
			_("Multiple active Amazon SP API Settings found — using {0}.").format(frappe.bold(settings[0].name)),
			alert=True,
		)

	return {"name": settings[0].name}


@frappe.whitelist()
def get_item_publish_status(item_code: str) -> dict:
	"""Latest Amazon sync status/error for one Item, for display on the Item
	form's read-only "Item Publish Error" field — so a user can see what went
	wrong (and when it was last attempted) without opening Ecommerce Item.

	Returns {status, error, synced_on} — `synced_on` is whichever of
	item_synced_on/inventory_synced_on is more recent, or None if this item
	has never been synced to Amazon.
	"""
	row = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": item_code, "integration": MODULE_NAME},
		["sync_status", "sync_error", "item_synced_on", "inventory_synced_on"],
		as_dict=True,
	)

	if not row:
		return {"status": None, "error": None, "synced_on": None}

	synced_on = max(filter(None, [row.item_synced_on, row.inventory_synced_on]), default=None)

	return {
		"status": row.sync_status,
		"error": row.sync_error,
		"synced_on": synced_on,
	}


@frappe.whitelist()
def sync_item_to_amazon(ecommerce_item: str) -> dict:
	"""Manually push one already-listed item's current stock, price, and
	images (if 'Sync Images to Amazon' is enabled) to Amazon right now,
	instead of waiting for the hourly scheduled inventory job — the entry
	point for the "Sync Now" button on Ecommerce Item.

	This app does not create new Amazon listings — `ecommerce_item` must
	already be mapped to a real SKU (via CSV import or a prior manual
	mapping), since Amazon's API requires a product type on every patch call
	even for an existing listing. If `amazon_product_type` isn't set yet,
	it's auto-fetched from Amazon's Listings Items API and saved onto the
	record before syncing (see `push_single_item_to_amazon`).
	"""
	from ecommerce_integrations.amazon.inventory import push_single_item_to_amazon

	ecom_doc = frappe.get_doc("Ecommerce Item", ecommerce_item)
	if ecom_doc.integration != MODULE_NAME:
		frappe.throw(_("Ecommerce Item {0} is not an Amazon integration record.").format(ecommerce_item))

	settings = frappe.get_all(SETTING_DOCTYPE, filters={"is_active": 1}, pluck="name")
	if not settings:
		frappe.throw(_("No active Amazon SP API Settings found."))
	setting = frappe.get_doc(SETTING_DOCTYPE, settings[0])

	result = push_single_item_to_amazon(setting, ecom_doc)

	ecom_doc.reload()
	return {
		"sync_status": ecom_doc.sync_status,
		"sync_error": ecom_doc.sync_error,
		"stock_result": result,
	}


@frappe.whitelist()
def import_existing_amazon_mappings(mappings: list | str) -> dict:
	"""Register items already published on Amazon, so they can be kept in
	sync (stock/price/images) going forward. This app does not create
	listings — it maps ERPNext Items to SKUs/ASINs that already exist on
	Amazon (published via Seller Central or any other means).

	`mappings` is a list of dicts (or a JSON string of the same):
	    [{"item_code": "ITEM-001", "sku": "ITEM-001", "asin": "B0EXAMPLE1",
	      "product_type": "WEARABLE_COMPUTER"}, ...]

	For each row, creates or updates an `Ecommerce Item` with sync_status=Synced
	and the given ASIN/product type — no Amazon API calls are made.
	`item_code` must reference an existing Item; `asin` and `sku` are required.
	`product_type` is required for stock/price sync to work afterward — set
	via this import or manually on the Ecommerce Item record.
	"""
	import json as json_module

	if isinstance(mappings, str):
		mappings = json_module.loads(mappings)

	results = {"created": 0, "updated": 0, "skipped": []}

	for row in mappings:
		item_code = row.get("item_code")
		sku = row.get("sku") or item_code
		asin = row.get("asin")

		if not item_code or not asin:
			results["skipped"].append({**row, "reason": "item_code and asin are required"})
			continue

		if not frappe.db.exists("Item", item_code):
			results["skipped"].append({**row, "reason": "Item not found"})
			continue

		existing_name = frappe.db.get_value(
			"Ecommerce Item", {"erpnext_item_code": item_code, "integration": MODULE_NAME}, "name"
		)

		values = {
			"integration_item_code": asin,
			"sku": sku,
			"sync_status": "Synced",
			"sync_error": "",
			"item_synced_on": frappe.utils.now(),
		}
		product_type = row.get("product_type")
		if product_type:
			values[ECOMMERCE_ITEM_PRODUCT_TYPE_FIELD] = product_type

		if existing_name:
			frappe.db.set_value("Ecommerce Item", existing_name, values, update_modified=False)
			results["updated"] += 1
		else:
			ecom_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"erpnext_item_code": item_code,
					"integration": MODULE_NAME,
					"has_variants": 0,
					**values,
				}
			)
			ecom_item.insert(ignore_permissions=True)
			results["created"] += 1

	frappe.db.commit()
	create_amazon_log(
		method="ecommerce_integrations.amazon.product.import_existing_amazon_mappings",
		status="Success" if not results["skipped"] else "Partial Success",
		message=str(results),
	)
	return results


@frappe.whitelist()
def import_existing_amazon_mappings_from_csv(file_path: str) -> dict:
	"""Same as `import_existing_amazon_mappings`, reading rows from a CSV file.

	Expected header row: item_code,sku,asin,product_type
	`product_type` column is optional and may be left blank per row.

	`file_path` may be a path on disk (bench console) or a Frappe File URL
	such as `/private/files/amazon_mappings.csv` (when called from a File
	uploaded via the UI/API).
	"""
	import csv
	import os

	if file_path.startswith("/private/files/") or file_path.startswith("/files/"):
		file_path = frappe.utils.get_site_path(file_path.lstrip("/"))

	if not os.path.exists(file_path):
		frappe.throw(_("File not found: {0}").format(file_path))

	mappings = []
	with open(file_path, newline="", encoding="utf-8-sig") as f:
		for row in csv.DictReader(f):
			mappings.append(
				{
					"item_code": (row.get("item_code") or "").strip(),
					"sku": (row.get("sku") or "").strip(),
					"asin": (row.get("asin") or "").strip(),
					"product_type": (row.get("product_type") or "").strip(),
				}
			)

	return import_existing_amazon_mappings(mappings)


@frappe.whitelist()
def check_amazon_listing_status(amz_setting_name: str, item_codes: list | str | None = None) -> list:
	"""Validate mapped listings against live Amazon data — not just ERP's own
	sync_status. A listing can show sync_status=Synced in ERP while Amazon
	itself reports blocking issues (e.g. suppressed for missing GPSR info)
	that only surface via a live GET call.

	`item_codes`: optional list (or JSON string) of Item codes to check; if
	omitted, checks every mapped Ecommerce Item for this integration.

	Returns a list of dicts: item_code, sku, asin, status (Amazon's DISCOVERABLE/
	SEARCHABLE/BUYABLE flags), errors (blocking issues), warnings, or a top-level
	`error` key if the SP-API call itself failed for that SKU.
	"""
	import json as json_module

	if isinstance(item_codes, str):
		item_codes = json_module.loads(item_codes)

	filters = {"integration": MODULE_NAME}
	if item_codes:
		filters["erpnext_item_code"] = ["in", item_codes]

	ecom_items = frappe.get_all(
		"Ecommerce Item", filters=filters, fields=["erpnext_item_code", "sku", "integration_item_code"]
	)

	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	repo = AmazonRepository(setting)
	listings_api = repo.get_listings_items_instance()

	results = []
	for row in ecom_items:
		entry = {"item_code": row.erpnext_item_code, "sku": row.sku, "asin": row.integration_item_code}
		try:
			data = listings_api.get_listing_item(sku=row.sku, included_data=["summaries", "issues"])
			summaries = data.get("summaries") or []
			issues = data.get("issues") or []

			entry["status"] = summaries[0].get("status") if summaries else []
			entry["asin"] = summaries[0].get("asin") if summaries else row.integration_item_code
			entry["errors"] = [i["message"] for i in issues if i.get("severity") == "ERROR"]
			entry["warnings"] = [i["message"] for i in issues if i.get("severity") == "WARNING"]
			entry["suppressed"] = any(
				a.get("action") == "LISTING_SUPPRESSED"
				for i in issues
				for a in (i.get("enforcements", {}).get("actions") or [])
			)
		except SPAPIError as e:
			entry["error"] = f"{e.error}: {e.error_description}"

		results.append(entry)

	create_amazon_log(
		method="ecommerce_integrations.amazon.product.check_amazon_listing_status",
		status="Success",
		message=f"Checked {len(results)} listing(s).",
	)
	return results
