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
	product_type = item.get(ITEM_PRODUCT_TYPE_FIELD)
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

	schema = pt_api.get_schema(product_type)
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

		entry = {
			"name": name,
			"title": prop.get("title") or name,
			"description": prop.get("description") or "",
			"always_required": always,
		}

		# enum values usually live under items.properties.value.enum for these
		# array-wrapped attributes; fall back to a top-level enum if present.
		value_schema = ((prop.get("items") or {}).get("properties") or {}).get("value") or {}
		enum = value_schema.get("enum") or prop.get("enum")
		if enum:
			entry["allowed_values"] = enum

		results.append(entry)

	results.sort(key=lambda e: (not e["always_required"], e["name"]))

	return results


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
	result = pt_api.search_by_keywords(keyword_list)

	return [
		{"name": pt.get("name"), "display_name": pt.get("displayName") or pt.get("name")}
		for pt in (result.get("productTypes") or [])
	]


def _collect_conditional_required(all_of: list, required_names: set) -> None:
	"""Recursively walk a JSON Schema `allOf` list, adding every `required`
	array found anywhere inside (conditional `if`/`then`/`else` branches
	included) to `required_names`. Amazon's product type schemas put most
	category-specific mandatory fields here rather than the top-level
	`required` array.
	"""

	def walk(node):
		if isinstance(node, dict):
			if isinstance(node.get("required"), list):
				required_names.update(r for r in node["required"] if isinstance(r, str))
			for value in node.values():
				walk(value)
		elif isinstance(node, list):
			for value in node:
				walk(value)

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


def _build_listing_attributes(item, price, marketplace_id, currency="USD") -> dict:
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

	attributes.update(_ecommerce_attributes_to_amazon(item, marketplace_id))

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
