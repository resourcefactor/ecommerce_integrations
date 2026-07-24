# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _

from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository import AmazonRepository
from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api import SPAPIError
from ecommerce_integrations.amazon.utils import MODULE_NAME, SETTING_DOCTYPE, create_amazon_log

ITEM_PUBLISH_FIELD = "publish_on_amazon"
ITEM_PRODUCT_TYPE_FIELD = "amazon_product_type"
ITEM_ATTRIBUTES_FIELD = "ecommerce_attributes"


@frappe.whitelist()
def get_active_amazon_setting() -> dict:
	"""Resolve the active `Amazon SP API Settings` record name for use by
	Item-form actions (Fetch Required Fields, Search Product Type) triggered
	by users who may not have read access to the settings doctype itself —
	it holds API credentials and is restricted to System Manager. Only the
	`name`/`is_active` lookup is done with elevated permission here; nothing
	sensitive is returned to the client.
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
	item_synced_on (publish attempts)/inventory_synced_on (stock/price sync)
	is more recent, or None if this item has never been synced to Amazon.
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
	"""Manually push one item's full current state to Amazon right now —
	attributes (incl. images, if 'Sync Images to Amazon' is enabled) via a
	full republish, plus current stock/price via a patch — instead of waiting
	for the after_insert/on_update hook or the hourly scheduled inventory job.

	`ecommerce_item` is the name of an `Ecommerce Item` record with
	integration=Amazon — this is the entry point for the "Sync Now" button
	on that doctype.
	"""
	from ecommerce_integrations.amazon.inventory import upload_inventory_data_to_amazon

	ecom_doc = frappe.get_doc("Ecommerce Item", ecommerce_item)
	if ecom_doc.integration != MODULE_NAME:
		frappe.throw(_("Ecommerce Item {0} is not an Amazon integration record.").format(ecommerce_item))

	item = frappe.get_doc("Item", ecom_doc.erpnext_item_code)
	if not item.get(ITEM_PUBLISH_FIELD):
		frappe.throw(_("Item {0} does not have 'Publish on Amazon' checked.").format(item.name))

	settings = frappe.get_all(SETTING_DOCTYPE, filters={"is_active": 1}, pluck="name")
	if not settings:
		frappe.throw(_("No active Amazon SP API Settings found."))
	setting = frappe.get_doc(SETTING_DOCTYPE, settings[0])

	# 1. full republish: attributes, ecommerce_attributes table, images (if enabled)
	_publish_item(setting, item)

	# 2. current stock/price, regardless of the "did it change since last sync"
	# watermark used by the scheduled job — a manual Sync Now always pushes
	# the current numbers.
	warehouses = setting.get_merged_warehouses()
	stock_result = "skipped (no warehouses configured)"
	if warehouses:
		from frappe.query_builder import DocType
		from frappe.query_builder.functions import Sum

		Bin = DocType("Bin")
		bin_totals = (
			frappe.qb.from_(Bin)
			.select(Sum(Bin.actual_qty).as_("actual_qty"), Sum(Bin.reserved_qty).as_("reserved_qty"))
			.where((Bin.item_code == item.item_code) & (Bin.warehouse.isin(warehouses)))
			.run(as_dict=True)
		)
		actual_qty = (bin_totals[0].actual_qty if bin_totals else 0) or 0
		reserved_qty = (bin_totals[0].reserved_qty if bin_totals else 0) or 0

		ecom_doc.reload()
		inventory_row = frappe._dict(
			ecom_item=ecom_doc.name,
			item_code=item.item_code,
			integration_item_code=ecom_doc.integration_item_code,
			actual_qty=actual_qty,
			reserved_qty=reserved_qty,
		)
		upload_inventory_data_to_amazon(setting, [inventory_row])
		stock_result = f"pushed (actual_qty={actual_qty}, reserved_qty={reserved_qty})"

	ecom_doc.reload()
	return {
		"sync_status": ecom_doc.sync_status,
		"sync_error": ecom_doc.sync_error,
		"stock_result": stock_result,
	}


@frappe.whitelist()
def copy_ecommerce_attributes(source_item_code: str, target_item_code: str, overwrite: bool = False) -> dict:
	"""Copy all Ecommerce Attribute rows from source_item_code's Item onto
	target_item_code's Item — for reusing a known-good attribute set from an
	already-published item on similar items stuck in error.

	If `overwrite` is falsy (default), existing parameters on the target are
	left untouched and only missing ones are added — mirrors the "don't
	clobber a value already filled in" behavior already used by Fetch
	Required Fields (item.js). If truthy, matching parameters are replaced
	with the source item's value.
	"""
	source = frappe.get_doc("Item", source_item_code)
	target = frappe.get_doc("Item", target_item_code)

	existing = {row.parameter: row for row in target.ecommerce_attributes}
	copied, skipped = 0, 0

	for row in source.ecommerce_attributes:
		if row.parameter in existing:
			if overwrite:
				existing[row.parameter].value = row.value
				existing[row.parameter].allowed_values = row.allowed_values
			else:
				skipped += 1
				continue
		else:
			target.append(
				"ecommerce_attributes",
				{"parameter": row.parameter, "value": row.value, "allowed_values": row.allowed_values},
			)
		copied += 1

	target.save()
	return {"copied": copied, "skipped": skipped}


@frappe.whitelist()
def sync_attributes_from_amazon(amz_setting_name: str, item_code: str) -> dict:
	"""Pull the live attribute set Amazon has on file for item_code's mapped
	SKU and populate Item.ecommerce_attributes from it — for an item that's
	already a good, live listing (e.g. filled in via Seller Central directly,
	not through this app) so it can be used as a copy-from source for
	similar items via `copy_ecommerce_attributes`.
	"""
	ecom_item = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": item_code, "integration": MODULE_NAME},
		["sku", "integration_item_code"],
		as_dict=True,
	)
	if not ecom_item:
		frappe.throw(_("No Amazon mapping found for {0} — publish or map it first.").format(item_code))

	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	repo = AmazonRepository(setting)
	listings_api = repo.get_listings_items_instance()

	result = listings_api.get_listing_item(sku=ecom_item.sku, included_data=["attributes"])
	amazon_attributes = result.get("attributes") or {}
	if not amazon_attributes:
		frappe.throw(_("Amazon returned no attributes for SKU {0}.").format(ecom_item.sku))

	reverse_alias = {v: k for k, v in _AMAZON_ATTRIBUTE_ALIASES.items()}

	item = frappe.get_doc("Item", item_code)
	existing = {row.parameter: row for row in item.ecommerce_attributes}
	updated, added = 0, 0

	for amazon_key, value_list in amazon_attributes.items():
		erp_key = reverse_alias.get(amazon_key, amazon_key)
		value_str = _amazon_value_to_string(value_list)
		if value_str is None:
			continue

		if erp_key in existing:
			existing[erp_key].value = value_str
			updated += 1
		else:
			item.append("ecommerce_attributes", {"parameter": erp_key, "value": value_str})
			added += 1

	item.save()
	return {"updated": updated, "added": added, "total_from_amazon": len(amazon_attributes)}


def _amazon_value_to_string(value_list) -> str | None:
	"""Reverse of the wrapping done in `_ecommerce_attributes_to_amazon`: a
	single-entry {"value": X, "marketplace_id": ..., "language_tag": ...}
	wrapper collapses back to plain X; anything more structured (multiple
	entries, or an entry without a bare "value" key — e.g. battery,
	item_package_dimensions) is stored as its JSON string so it round-trips
	through the Value column unchanged and re-parses correctly on next publish
	(confirmed live: Amazon's getListingsItem returns backend enum codes like
	"CN", never display labels like "China" — safe to copy through as-is).

	Always returns either a plain str (for X that's already a string, e.g.
	"CN") or a JSON-encoded string (for bool/int/float/dict/list X, e.g.
	`batteries_required`'s `False` -> "false") — never a raw bool/int/float.
	Storing a bare Python bool in the Small Text `value` column breaks
	Frappe's own version-diff formatter (expects str) and, on the way back
	via `_ecommerce_attributes_to_amazon`'s `frappe.parse_json`, a Python-
	style "False"/"True" string wouldn't parse as JSON at all — round-tripping
	through json.dumps keeps both directions correct.
	"""
	import json

	if not isinstance(value_list, list) or not value_list:
		return None

	if len(value_list) == 1 and set(value_list[0].keys()) <= {"value", "marketplace_id", "language_tag"}:
		value = value_list[0].get("value")
		return value if isinstance(value, str) else json.dumps(value)

	return json.dumps(value_list)


def upload_erpnext_item(doc, method=None):
	"""`Item` doc_event hook (after_insert / on_update).

	Publishes/updates a single Item on Amazon as a listing, gated by the
	`Upload ERPNext Items to Amazon` setting and the item's own
	`publish_on_amazon` checkbox + `amazon_product_type`.

	`Amazon SP API Settings` is a regular doctype, not a Single (an account
	can have multiple marketplace records) — publish to every record that
	has `is_active` + `upload_erpnext_items` enabled.
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

	setting_names = frappe.get_all(
		SETTING_DOCTYPE, filters={"is_active": 1, "upload_erpnext_items": 1}, pluck="name"
	)

	for setting_name in setting_names:
		setting = frappe.get_doc(SETTING_DOCTYPE, setting_name)
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
	# Amazon's product type codes are uppercase (e.g. HEADPHONES) — normalize
	# so a lowercase/mixed-case entry doesn't get rejected as an unknown type.
	product_type = item.get(ITEM_PRODUCT_TYPE_FIELD).strip().upper()
	price = setting.get_item_price(sku)

	listings_api = repo.get_listings_items_instance()
	currency = frappe.db.get_value("Price List", setting.price_list, "currency") or "USD"

	matched_asin = _find_existing_asin(repo, item)
	if matched_asin:
		# an ASIN already exists in Amazon's catalog for this product (e.g. another
		# seller already listed it) — attach a minimal offer rather than submitting
		# the full category attribute schema from scratch.
		attributes = _build_minimal_offer_attributes(price, currency)
		attributes["merchant_suggested_asin"] = [{"value": matched_asin}]
	else:
		attributes = _build_listing_attributes(item, price, listings_api.marketplace_id, currency, setting)

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


def backfill_asins(amz_setting_name: str) -> dict:
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
	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)

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


@frappe.whitelist()
def import_existing_amazon_mappings(mappings: list | str) -> dict:
	"""Register items already published on Amazon, without publishing them again.

	`mappings` is a list of dicts (or a JSON string of the same):
	    [{"item_code": "ITEM-001", "sku": "ITEM-001", "asin": "B0EXAMPLE1",
	      "product_type": "WEARABLE_COMPUTER"}, ...]

	For each row, creates or updates an `Ecommerce Item` with sync_status=Synced
	and the given ASIN — no Amazon API calls are made. Use this once to backfill
	the ERPNext<->Amazon mapping for a catalog that predates this integration.
	`item_code` must reference an existing Item; `asin` and `sku` are required.
	`product_type`, if given, is written to the Item's Amazon Product Type field
	so future stock/patch pushes for this item work without extra setup.
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

		item_updates = {ITEM_PUBLISH_FIELD: 1}
		product_type = row.get("product_type")
		if product_type:
			item_updates[ITEM_PRODUCT_TYPE_FIELD] = product_type
		frappe.db.set_value("Item", item_code, item_updates, update_modified=False)

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
	"""Validate published listings against live Amazon data — not just ERP's own
	sync_status. A listing can show sync_status=Synced/Pending ASIN in ERP while
	Amazon itself reports blocking issues (e.g. suppressed for missing GPSR info)
	that only surface via a live GET call.

	`item_codes`: optional list (or JSON string) of Item codes to check; if
	omitted, checks every Item with `publish_on_amazon` = 1.

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
	else:
		filters["erpnext_item_code"] = ["in", frappe.get_all("Item", {ITEM_PUBLISH_FIELD: 1}, pluck="name")]

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


@frappe.whitelist()
def describe_product_type_requirements(amz_setting_name: str, product_type: str) -> list:
	"""Fetch Amazon's real JSON Schema for `product_type` (via the Product Type
	Definitions API — the authoritative source, not error-message guessing) and
	return a list of attributes with title, description, allowed enum values
	(where applicable), and `always_required`.

	`always_required=True` fields are unconditionally mandatory (Amazon's
	top-level schema `required` list — a short, reliable set). The rest are
	pulled from the schema's conditional (allOf/if/then) branches — these
	commonly apply but aren't guaranteed for every specific item, since they
	depend on other attribute values (variation parenting, hazmat, etc.).
	Results are sorted always-required first. Use `check_amazon_listing_status`
	or a real publish attempt for the definitive per-item answer.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	repo = AmazonRepository(setting)
	pt_api = repo.get_product_type_definitions_instance()

	# Amazon's product type codes are uppercase (e.g. HEADPHONES) — normalize
	# so a lowercase/mixed-case typo doesn't silently fail to match.
	product_type = (product_type or "").strip().upper()

	try:
		schema = pt_api.get_schema(product_type)
	except SPAPIError as e:
		frappe.throw(
			_("Could not fetch requirements for Amazon Product Type {0}: {1}").format(
				frappe.bold(product_type), e.error_description
			)
		)

	properties = schema.get("properties") or {}

	always_required = set(schema.get("required") or [])
	conditionally_required = set()
	_collect_conditional_required(schema.get("allOf") or [], conditionally_required)
	conditionally_required -= always_required

	results = []
	for name, always in [(n, True) for n in always_required] + [(n, False) for n in conditionally_required]:
		prop = properties.get(name)
		if not prop:
			continue  # referenced only in a conditional branch that doesn't apply generally

		entry = _describe_property(name, prop)
		entry["always_required"] = always
		results.append(entry)

	results.sort(key=lambda e: (not e["always_required"], e["name"]))

	return results


def _describe_property(name: str, prop: dict) -> dict:
	"""Build the {name, title, description, allowed_values, example} shape
	shared by `describe_product_type_requirements` and
	`get_attribute_allowed_values` from one raw JSON Schema property def.
	"""
	entry = {
		"name": name,
		"title": prop.get("title") or name,
		"description": prop.get("description") or "",
	}

	# enum values usually live under items.properties.value.enum for these
	# array-wrapped attributes; fall back to a top-level enum if present.
	value_schema = ((prop.get("items") or {}).get("properties") or {}).get("value") or {}
	enum = value_schema.get("enum") or prop.get("enum")
	if enum:
		entry["allowed_values"] = enum

	# Amazon's own example text for this attribute (e.g. "24 Kilogrammes")
	# — pre-fill hint. Prefer the enum's first allowed value when one exists:
	# enum-constrained fields reject anything else, but Amazon's free-text
	# "examples" hint is sometimes a locale sample (e.g. lowercase "fr")
	# rather than a real submittable enum code.
	examples = prop.get("examples") or value_schema.get("examples")
	if enum:
		entry["example"] = enum[0]
	elif examples:
		entry["example"] = examples[0]

	return entry


@frappe.whitelist()
def get_attribute_allowed_values(amz_setting_name: str, product_type: str, parameter: str) -> dict:
	"""Look up a single Amazon attribute's allowed values / example directly
	from the product type's schema — for fixing one rejected field (e.g. after
	a publish error like "invalid value for Battery Chemical Construction")
	without re-fetching the whole category's requirement list.

	Returns {name, title, description, allowed_values, example} or an
	`error` key if the parameter isn't part of this product type's schema.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	repo = AmazonRepository(setting)
	pt_api = repo.get_product_type_definitions_instance()

	product_type = (product_type or "").strip().upper()
	parameter = (parameter or "").strip()

	try:
		schema = pt_api.get_schema(product_type)
	except SPAPIError as e:
		frappe.throw(
			_("Could not fetch schema for Amazon Product Type {0}: {1}").format(
				frappe.bold(product_type), e.error_description
			)
		)

	prop = (schema.get("properties") or {}).get(parameter)
	if not prop:
		return {"name": parameter, "error": _("No such attribute in {0}'s schema.").format(product_type)}

	return _describe_property(parameter, prop)


@frappe.whitelist()
def search_amazon_product_types(amz_setting_name: str, keywords: str) -> list:
	"""Search Amazon's product type taxonomy by keyword (e.g. "headphones",
	"smart watch") via the Product Type Definitions API's search operation —
	so a user can find the correct `amazon_product_type` code without already
	knowing Amazon's exact category name.

	`keywords` is a comma-separated string (matches how the Item form's
	search prompt collects it). Returns a simplified list of
	{"name": <code to use as amazon_product_type>, "display_name": <label>}.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE, amz_setting_name)
	repo = AmazonRepository(setting)
	pt_api = repo.get_product_type_definitions_instance()

	keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]

	try:
		result = pt_api.search_by_keywords(keyword_list)
	except SPAPIError as e:
		frappe.throw(_("Amazon product type search failed: {0}").format(e.error_description))

	return [
		{"name": pt.get("name"), "display_name": pt.get("displayName") or pt.get("name")}
		for pt in (result.get("productTypes") or [])
	]


def _collect_conditional_required(all_of: list, required_names: set) -> None:
	"""Walk a JSON Schema `allOf` list and collect field names from the
	*consequence* (`then`/`else`) of each conditional branch — i.e. fields
	that become required once some trigger condition is met.

	Every branch in Amazon's product type schemas takes one of these shapes:
	  - {"if": ..., "then": ...} / {"if": ..., "then": ..., "else": ...}
	  - {"properties": ...}  (refines an existing field, adds no requirement)
	  - {"allOf": [...]}     (nested composition — recurse)

	Critically, `if.required` is the *trigger condition* (e.g. "if
	league_name is set to NASCAR"), not something this product type actually
	requires — earlier code wrongly treated it as required, which surfaced
	irrelevant fields (e.g. league_name/team_name on a power bank) for every
	product type. Only `then`/`else` are real "becomes required" signals, and
	even those are conditional on the trigger — still not a guarantee for
	every specific item, but far more accurate than reading `if` as well.
	"""

	def collect_required(node):
		if isinstance(node, dict) and isinstance(node.get("required"), list):
			required_names.update(r for r in node["required"] if isinstance(r, str))

	def walk(branches):
		for branch in branches:
			if not isinstance(branch, dict):
				continue
			if "allOf" in branch:
				walk(branch["allOf"])
			if "then" in branch:
				collect_required(branch["then"])
			if "else" in branch:
				collect_required(branch["else"])

	walk(all_of)


def _find_existing_asin(repo, item) -> str | None:
	"""Search Amazon's catalog for an ASIN already matching this item's EAN/UPC
	barcode. Returns the ASIN string if exactly one confident match is found,
	else None (falls back to submitting the full attribute schema).
	"""
	barcode = frappe.db.get_value(
		"Item Barcode", {"parent": item.name, "barcode": ["!=", item.item_code]}, "barcode"
	)
	if not barcode:
		return None

	try:
		search_api = repo.get_catalog_items_search_instance()
		result = search_api.search_by_identifier(identifier=barcode, identifier_type="EAN")
	except SPAPIError:
		return None

	items = result.get("items") or []
	if len(items) == 1:
		return items[0].get("asin")
	return None


def _build_minimal_offer_attributes(price, currency="USD") -> dict:
	"""Attribute set for attaching an offer to an ASIN Amazon's catalog already
	has data for — mirrors the shape of an existing working listing (I-00333)
	rather than the full from-scratch category schema.
	"""
	attributes = {"condition_type": [{"value": "new_new"}]}
	if price:
		attributes["purchasable_offer"] = [
			{"currency": currency, "our_price": [{"schedule": [{"value_with_tax": price}]}]}
		]
	return attributes


# platform-agnostic key -> Amazon SP-API attribute name, for the common ones
# that repeat across most categories. Anything not listed here is still sent
# through as-is (using the JSON key directly as the Amazon attribute name),
# so category-specific attributes (e.g. headphone_form_factor, gdpr_risk)
# can be set using Amazon's own name without needing a mapping entry.
_AMAZON_ATTRIBUTE_ALIASES = {
	"brand": "brand",
	"manufacturer": "manufacturer",
	"model_number": "model_number",
	"model_name": "model_name",
	"color": "color",
	"country_of_origin": "country_of_origin",
	"warranty_description": "warranty_description",
	"connectivity_technology": "connectivity_technology",
}


def _build_listing_attributes(item, price, marketplace_id, currency="USD", setting=None) -> dict:
	"""Attribute set for publishing: mandatory/available Item fields, plus
	whatever platform-agnostic data is provided in the `Item.ecommerce_attributes`
	child table (one row per parameter: Parameter | Value).

	Amazon validates this server-side against the item's product_type schema;
	categories that require attributes beyond what's mapped here will still
	come back with issues in the response — check Ecommerce Item.sync_error
	or re-fetch via ListingsItems.get_listing_item() to see exactly what's
	still missing for that item's specific category.
	"""
	condition_type = "new_new"

	attributes = {
		"condition_type": [{"value": condition_type}],
		"item_name": [{"value": item.item_name or item.item_code}],
	}

	if item.brand:
		attributes["brand"] = [{"value": item.brand, "marketplace_id": marketplace_id}]

	if item.description:
		description = frappe.utils.strip_html(item.description)
		attributes["product_description"] = [{"value": description[:2000]}]
		attributes["bullet_point"] = [{"value": description[:500], "marketplace_id": marketplace_id}]

	if price:
		attributes["purchasable_offer"] = [
			{
				"currency": currency,
				"our_price": [{"schedule": [{"value_with_tax": price}]}],
			}
		]
		# Amazon's list_price (MSRP) has no separate ERPNext concept — reuse the
		# same selling price rather than requiring a second field to maintain.
		attributes["list_price"] = [{"currency": currency, "value_with_tax": price, "marketplace_id": marketplace_id}]

	# the `lp` app always inserts item_code as a barcode row (see lp/le_point_global/item.py);
	# skip it and use the real EAN/UPC — Amazon requires a proper 12-13 digit product identifier.
	barcode = frappe.db.get_value(
		"Item Barcode", {"parent": item.name, "barcode": ["!=", item.item_code]}, "barcode"
	)
	if barcode:
		attributes["externally_assigned_product_identifier"] = [
			{"type": "ean", "value": barcode, "marketplace_id": marketplace_id}
		]

	if setting and setting.get("sync_images_to_amazon"):
		attributes.update(_build_image_attributes(item, marketplace_id))

	attributes.update(_ecommerce_attributes_to_amazon(item, marketplace_id))

	return attributes


def _build_image_attributes(item, marketplace_id: str) -> dict:
	"""Item.image -> main_product_image_locator; every other File attachment
	on the Item (in creation order) -> other_product_image_locator_1..8.

	Amazon fetches images by URL rather than accepting uploads, so this only
	works if the site is reachable on the public internet at the URL
	`frappe.utils.get_url()` resolves to (its configured host_name) — on an
	internal/dev site (e.g. a bench dev server) these URLs will be
	unreachable from Amazon and the listing will show a missing-image issue.
	"""
	site_url = frappe.utils.get_url()
	attributes = {}

	if item.image:
		attributes["main_product_image_locator"] = [
			{"media_location": f"{site_url}{item.image}", "marketplace_id": marketplace_id}
		]

	other_files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Item",
			"attached_to_name": item.name,
			"file_url": ["!=", item.image or ""],
			"is_private": 0,
		},
		fields=["file_url"],
		order_by="creation asc",
		limit=8,
	)

	for idx, file_row in enumerate(other_files, start=1):
		attributes[f"other_product_image_locator_{idx}"] = [
			{"media_location": f"{site_url}{file_row.file_url}", "marketplace_id": marketplace_id}
		]

	return attributes


def _ecommerce_attributes_to_amazon(item, marketplace_id: str) -> dict:
	"""Read `Item.ecommerce_attributes` (the "Ecommerce Attribute" child table —
	one row per parameter: Parameter | Value, shared across marketplace
	integrations) and convert each row into Amazon's attribute wrapper shape.

	Each row's Value is plain text, entered one-per-line in the grid. It's
	parsed as JSON first (so a row can hold a bool, number, list, or a
	structured dict like {"unit": "grams", "value": 50} for attributes that
	need sub-fields) and falls back to the raw string if that fails — so a
	plain "YESIDO" or "Black" is used exactly as typed.
	"""
	rows = item.get(ITEM_ATTRIBUTES_FIELD) or []

	result = {}
	for row in rows:
		key = (row.parameter or "").strip()
		raw_value = row.value
		if not key or raw_value in (None, ""):
			continue

		try:
			value = frappe.parse_json(raw_value)
		except Exception:
			value = raw_value

		amazon_key = _AMAZON_ATTRIBUTE_ALIASES.get(key, key)
		if isinstance(value, list):
			# already in Amazon's raw attribute shape — pass through as-is
			result[amazon_key] = value
		elif isinstance(value, dict):
			# structured attribute (e.g. dimensions/weight sub-objects)
			result[amazon_key] = [{**value, "marketplace_id": marketplace_id}]
		else:
			result[amazon_key] = [{"value": value, "marketplace_id": marketplace_id}]

	return result


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
