# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import csv
import io
import re
from collections import Counter

import frappe
from frappe import _
from frappe.utils import cint, cstr, now

from ecommerce_integrations.takealot.api_client import TakealotClient
from ecommerce_integrations.takealot.constants import MODULE_NAME, PACK_QTY_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.takealot.utils import create_takealot_log

IMPORT_OFFERS_METHOD = "ecommerce_integrations.takealot.product.import_offers"

# Takealot SKUs of bundles, e.g. "I-04212 x 2" -> 2 units of item I-04212
BUNDLE_SKU = re.compile(r"^(?P<item>.+?)\s+x\s*(?P<qty>\d+)$", re.IGNORECASE)

# suggest an item by item name prefix only for SKUs at least this long (Takealot cuts SKUs short)
MIN_PREFIX_LENGTH = 8

MAPPING_COLUMNS = (
	"offer_id",
	"sku",
	"barcode",
	"title",
	"status",
	"sold_30_days",
	"suggested_item_code",
	"item_code",
	"pack_qty",
)


def get_item_code(offer_id, sku, barcode=None) -> tuple[str | None, int]:
	"""ERPNext item and pack quantity of a Takealot offer.

	A mapped Ecommerce Item wins. Otherwise the SKU is matched to an Item, and the match
	is saved as an Ecommerce Item so it's resolved directly next time."""
	mapped = get_mapped_item(offer_id, sku)
	if mapped:
		return mapped

	item_code, pack_qty = match_item(sku, barcode)
	if item_code:
		link_offer(offer_id, sku, item_code, pack_qty)
	return item_code, pack_qty


def get_mapped_item(offer_id, sku) -> tuple[str, int] | None:
	for filters in ({"sku": cstr(sku)}, {"integration_item_code": cstr(offer_id)}):
		if not next(iter(filters.values())):
			continue
		row = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, **filters},
			["erpnext_item_code", PACK_QTY_FIELD],
			as_dict=True,
		)
		if row:
			return row.erpnext_item_code, max(cint(row.get(PACK_QTY_FIELD)), 1)


def match_item(sku, barcode=None) -> tuple[str | None, int]:
	"""Find the ERPNext item a Takealot SKU refers to.

	Tries the SKU, then the SKU without the configured tag words (e.g. "DW"), as an item
	code, a bundle ("I-04212 x 2") or an exact item name. Then a single item code
	contained in the SKU ("DW I-02770"), then the barcode."""
	sku = cstr(sku).strip()
	if not sku:
		return None, 1

	stripped = _strip_ignored_words(sku)
	for candidate in dict.fromkeys((sku, stripped)):
		if not candidate:
			continue
		if _is_sellable_item(candidate):
			return candidate, 1

		bundle = BUNDLE_SKU.match(candidate)
		if bundle and _is_sellable_item(bundle.group("item").strip()):
			return bundle.group("item").strip(), max(cint(bundle.group("qty")), 1)

		# database collation is case and trailing-space insensitive
		by_name = frappe.get_all(
			"Item", filters={"item_name": candidate, "disabled": 0, "has_variants": 0}, pluck="name", limit=2
		)
		if len(by_name) == 1:
			return by_name[0], 1

	codes = {token for token in re.split(r"[\s()]+", sku) if token and _is_sellable_item(token)}
	if len(codes) == 1:
		return codes.pop(), 1

	for code in (cstr(barcode).strip(), sku):
		if code:
			item_code = frappe.db.get_value("Item Barcode", {"barcode": code}, "parent")
			if item_code and _is_sellable_item(item_code):
				return item_code, 1

	return None, 1


def _strip_ignored_words(sku: str) -> str:
	"""Remove the configured tag words from the start and end of a SKU, with the
	separators around them: "DW - Hc1.5M Hdmi Cable" / "Cable 2M (DW)" -> the bare name."""
	words = [
		word.strip()
		for word in cstr(frappe.db.get_single_value(SETTING_DOCTYPE, "sku_ignore_words", cache=True)).split(
			","
		)
		if word.strip()
	]
	if not words:
		return sku

	tags = "|".join(re.escape(word) for word in words)
	pattern = re.compile(rf"^[\W_]*\b(?:{tags})\b[\W_]*|[\W_]*\b(?:{tags})\b[\W_]*$", re.IGNORECASE)
	previous = None
	while previous != sku:
		previous, sku = sku, pattern.sub("", sku).strip()
	return sku


def _is_sellable_item(item_code) -> bool:
	return bool(frappe.db.exists("Item", {"name": item_code, "disabled": 0, "has_variants": 0}))


def link_offer(offer_id, sku, item_code, pack_qty=1):
	"""Create or update the Ecommerce Item mapping a Takealot offer to an ERPNext item."""
	name = frappe.db.get_value(
		"Ecommerce Item", {"integration": MODULE_NAME, "integration_item_code": cstr(offer_id)}
	) or frappe.db.get_value("Ecommerce Item", {"integration": MODULE_NAME, "sku": cstr(sku)})

	if name:
		frappe.db.set_value(
			"Ecommerce Item",
			name,
			{"erpnext_item_code": item_code, "sku": cstr(sku), PACK_QTY_FIELD: max(cint(pack_qty), 1)},
		)
		return name

	doc = frappe.get_doc(
		{
			"doctype": "Ecommerce Item",
			"integration": MODULE_NAME,
			"erpnext_item_code": item_code,
			"integration_item_code": cstr(offer_id),
			"sku": cstr(sku),
			PACK_QTY_FIELD: max(cint(pack_qty), 1),
		}
	).insert(ignore_permissions=True)
	return doc.name


def import_offers():
	"""Link every Takealot offer that can be matched to an ERPNext item."""
	counts = Counter()
	unmatched = []
	try:
		for offer in TakealotClient().iter_offers():
			if get_mapped_item(offer["offer_id"], offer.get("sku")):
				counts["already linked"] += 1
				continue

			item_code, pack_qty = match_item(offer.get("sku"), offer.get("barcode"))
			if item_code:
				link_offer(offer["offer_id"], offer.get("sku"), item_code, pack_qty)
				counts["linked"] += 1
				if counts["linked"] % 100 == 0:
					frappe.db.commit()
			else:
				counts["unmatched"] += 1
				unmatched.append({key: offer.get(key) for key in ("offer_id", "sku", "status", "title")})
	except Exception as e:
		create_takealot_log(
			status="Error",
			exception=e,
			rollback=True,
			method=IMPORT_OFFERS_METHOD,
			message=_("Takealot offer import failed: {0}").format(e),
			make_new=True,
		)
		return

	frappe.db.set_single_value(SETTING_DOCTYPE, "last_offer_import", now())
	create_takealot_log(
		status="Success",
		method=IMPORT_OFFERS_METHOD,
		message=_("Takealot offers imported: {0} newly linked, {1} already linked, {2} unmatched").format(
			counts["linked"], counts["already linked"], counts["unmatched"]
		),
		response_data={"counts": counts, "unmatched_offers": unmatched},
		make_new=True,
	)


def get_unmapped_offers() -> list[dict]:
	"""Takealot offers without an Ecommerce Item, with a suggested ERPNext item where possible.

	Buyable and best-selling offers come first, as they are the ones holding up order sync."""
	mapped_offer_ids = set(
		frappe.get_all("Ecommerce Item", filters={"integration": MODULE_NAME}, pluck="integration_item_code")
	)
	mapped_skus = set(frappe.get_all("Ecommerce Item", filters={"integration": MODULE_NAME}, pluck="sku"))
	item_names = frappe.get_all(
		"Item", filters={"disabled": 0, "has_variants": 0}, fields=["name", "item_name"]
	)

	rows = []
	for offer in TakealotClient().iter_offers(expands=["takealot_warehouse_stock"]):
		if cstr(offer["offer_id"]) in mapped_offer_ids or offer.get("sku") in mapped_skus:
			continue

		item_code, pack_qty = match_item(offer.get("sku"), offer.get("barcode"))
		rows.append(
			{
				"offer_id": offer["offer_id"],
				"sku": offer.get("sku"),
				"barcode": offer.get("barcode"),
				"title": offer.get("title"),
				"status": offer.get("status"),
				"sold_30_days": sum(
					cint(stock.get("quantity_sold_30_days"))
					for stock in offer.get("takealot_warehouse_stock") or []
				),
				"suggested_item_code": item_code or _suggest_by_name_prefix(offer.get("sku"), item_names),
				"item_code": "",
				"pack_qty": pack_qty,
			}
		)

	rows.sort(key=lambda row: (row["status"] != "buyable", -row["sold_30_days"]))
	return rows


def _suggest_by_name_prefix(sku, item_names) -> str:
	"""Takealot truncates SKUs, so an SKU is often the start of the item name."""
	sku = cstr(sku).strip().lower()
	if len(sku) < MIN_PREFIX_LENGTH:
		return ""
	matches = [item.name for item in item_names if cstr(item.item_name).strip().lower().startswith(sku)]
	return matches[0] if len(matches) == 1 else ""


def import_offer_mapping(csv_data: str) -> dict:
	"""Create or update Ecommerce Items from a filled-in unmapped offers CSV."""
	created, updated, skipped, errors = 0, 0, 0, []

	for line_no, row in enumerate(csv.DictReader(io.StringIO(csv_data)), start=2):
		row = {cstr(key).strip(): cstr(value).strip() for key, value in row.items() if key}
		item_code, offer_id, sku = row.get("item_code"), row.get("offer_id"), row.get("sku")
		if not item_code:
			skipped += 1
			continue
		if not offer_id or not sku:
			errors.append(_("Row {0}: offer_id and sku are required").format(line_no))
			continue
		if not _is_sellable_item(item_code):
			errors.append(_("Row {0}: Item {1} not found, disabled or a template").format(line_no, item_code))
			continue

		existed = bool(get_mapped_item(offer_id, sku))
		try:
			link_offer(offer_id, sku, item_code, cint(row.get("pack_qty")) or 1)
		except Exception as e:
			errors.append(_("Row {0}: {1}").format(line_no, e))
			continue

		if existed:
			updated += 1
		else:
			created += 1

	frappe.db.commit()
	return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
