# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import math
from collections import Counter

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Coalesce, Max, Sum
from frappe.utils import cint, cstr, flt, now

from ecommerce_integrations.controllers.inventory import update_inventory_sync_status
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.takealot.api_client import TakealotAPIError, TakealotClient
from ecommerce_integrations.takealot.constants import MODULE_NAME, PACK_QTY_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.takealot.utils import create_takealot_log, get_takealot_settings

NEVER_SYNCED = "1970-01-01 00:00:00"
# offers read back from Takealot per request before pushing
OFFER_BATCH_SIZE = 100


def push_offers():
	"""Scheduled every few minutes; pushes at the configured price & stock sync frequency."""
	settings = get_takealot_settings()
	if not settings.is_enabled() or not (settings.push_price or settings.push_stock):
		return
	if not need_to_run(SETTING_DOCTYPE, "offer_sync_frequency", "last_offer_sync"):
		return

	frappe.enqueue(
		push_changed_offers, queue="long", timeout=4000, job_id="takealot_push_offers", deduplicate=True
	)


def push_changed_offers():
	"""Push price and leadtime stock of mapped items whose Item Price or Bin changed since
	their last push. Takealot is read first, so only values that differ are written."""
	settings = frappe.get_doc(SETTING_DOCTYPE)
	run_start = now()
	items = _get_changed_items(settings)
	if not items:
		return

	counts, errors = Counter(), []
	try:
		client = TakealotClient()
		for start in range(0, len(items), OFFER_BATCH_SIZE):
			batch = items[start : start + OFFER_BATCH_SIZE]
			offers = _get_offers(client, [item.offer_id for item in batch])
			for item in batch:
				result = push_offer(client, settings, item, offers.get(item.offer_id), run_start)
				counts[result["sync_status"]] += 1
				if result.get("sync_error"):
					errors.append(f"{item.item_code} (offer {item.offer_id}): {result['sync_error']}")
			frappe.db.commit()
	except TakealotAPIError as e:
		errors.append(str(e))

	create_takealot_log(
		status="Partial Success" if errors else "Success",
		message=_("Takealot price & stock push: {0}").format(
			", ".join(f"{count} {status}" for status, count in counts.most_common()) or _("nothing pushed")
		),
		response_data={"counts": counts, "errors": errors},
		make_new=True,
	)


@frappe.whitelist()
def sync_item_to_takealot(ecommerce_item):
	"""Push one mapped item's price and stock now (Ecommerce Item > Sync Now)."""
	frappe.has_permission("Ecommerce Item", "write", doc=ecommerce_item, throw=True)

	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.is_enabled():
		frappe.throw(_("Takealot is not enabled in {0}").format(SETTING_DOCTYPE))
	if not (settings.push_price or settings.push_stock):
		frappe.throw(
			_("Enable Update Price or Update Leadtime Stock on Takealot in {0} first.").format(
				SETTING_DOCTYPE
			)
		)

	ecom = frappe.get_doc("Ecommerce Item", ecommerce_item)
	if ecom.integration != MODULE_NAME:
		frappe.throw(_("{0} is not a Takealot item").format(ecommerce_item))

	# only what is switched on is pushed and stamped as synced
	item = frappe._dict(
		ecom_item=ecom.name,
		item_code=ecom.erpnext_item_code,
		offer_id=cint(ecom.integration_item_code),
		pack_qty=max(cint(ecom.get(PACK_QTY_FIELD)), 1),
		price_changed=cint(settings.push_price),
		stock_changed=cint(settings.push_stock),
	)
	client = TakealotClient()
	try:
		offer = client.get_offer(item.offer_id, expands=("seller_warehouse_stock",))
	except TakealotAPIError as e:
		if e.status_code != 404:
			raise
		offer = None

	result = push_offer(client, settings, item, offer, now())
	frappe.db.commit()
	return result


def push_offer(client, settings, item, offer, run_start) -> dict:
	"""Update one Takealot offer's price and/or leadtime stock if they differ from ERPNext.

	Returns {sync_status, sync_error, stock_result} for Ecommerce Item > Sync Now."""
	if not offer:
		_set_sync_status(item, run_start, "Not Found", _("Offer not found on Takealot"))
		return {"sync_status": "Not Found", "sync_error": _("Offer not found on Takealot")}

	body, results = {}, []
	if settings.push_price and item.price_changed:
		price = _get_selling_price(item, settings.price_list)
		if price is None:
			results.append(_("no price in {0}").format(settings.price_list))
		elif cint(offer.get("selling_price")) != price:
			body["selling_price"] = price
			results.append(_("price {0} → {1}").format(offer.get("selling_price"), price))
		else:
			results.append(_("price unchanged"))

	if settings.push_stock and item.stock_changed:
		if cint(offer.get("minimum_leadtime_days")) <= 0:
			results.append(_("stock skipped, leadtime is off for this offer"))
		else:
			stock = _get_leadtime_stock(item, settings)
			current = {
				cint(row.get("seller_warehouse_id")): cint(row.get("quantity_available"))
				for row in offer.get("seller_warehouse_stock") or []
			}
			if any(current.get(row["seller_warehouse_id"]) != row["quantity_available"] for row in stock):
				body["seller_warehouse_stock"] = stock
				results.append(
					_("stock {0}").format(
						", ".join(
							f"{row['seller_warehouse_id']}: {row['quantity_available']}" for row in stock
						)
					)
				)
			else:
				results.append(_("stock unchanged"))

	stock_result = "; ".join(results) or _("nothing to push")
	try:
		if body:
			client.update_offer(item.offer_id, body)
	except TakealotAPIError as e:
		status = "Not Found" if e.status_code == 404 else "Error"
		_set_sync_status(item, run_start, status, e.message)
		return {"sync_status": status, "sync_error": e.message, "stock_result": stock_result}

	_set_sync_status(item, run_start, "Synced")
	return {"sync_status": "Synced", "sync_error": "", "stock_result": stock_result}


def _set_sync_status(item, run_start, status, error=None):
	# stamp only what was considered, so a skipped change is pushed on the next run
	if item.stock_changed:
		update_inventory_sync_status(item.ecom_item, time=run_start, status=status)
	values = {"sync_status": status, "sync_error": cstr(error)[:1000]}
	if item.price_changed:
		values["item_synced_on"] = run_start
	frappe.db.set_value("Ecommerce Item", item.ecom_item, values, update_modified=False)


def _get_offers(client, offer_ids) -> dict:
	offers = client.iter_offers(offer_id__in=offer_ids, expands=["seller_warehouse_stock"])
	return {cint(offer["offer_id"]): offer for offer in offers}


def _get_selling_price(item, price_list) -> int | None:
	price = frappe.db.get_value(
		"Item Price",
		{"item_code": item.item_code, "price_list": price_list, "selling": 1},
		"price_list_rate",
		order_by="valid_from desc",
	)
	if price is None:
		return None
	# Takealot prices are whole Rand
	return max(math.floor(flt(price) * item.pack_qty + 0.5), 1)


def _get_leadtime_stock(item, settings) -> list[dict]:
	Bin = DocType("Bin")
	stock = []
	for warehouse_id, warehouses in settings.get_seller_warehouse_map().items():
		qty = (
			frappe.qb.from_(Bin)
			.select(Sum(Bin.actual_qty - Bin.reserved_qty))
			.where((Bin.item_code == item.item_code) & Bin.warehouse.isin(warehouses))
		).run()[0][0]
		stock.append(
			{
				"seller_warehouse_id": warehouse_id,
				"quantity_available": max(math.floor(flt(qty) / item.pack_qty), 0),
			}
		)
	return stock


def _get_changed_items(settings) -> list[frappe._dict]:
	"""Mapped items whose price or leadtime stock changed since their last push."""
	EcommerceItem = DocType("Ecommerce Item")
	items = {}

	def add(rows, flag):
		for row in rows:
			item = items.setdefault(
				row.ecom_item,
				frappe._dict(
					ecom_item=row.ecom_item,
					item_code=row.item_code,
					offer_id=cint(row.offer_id),
					pack_qty=max(cint(row.pack_qty), 1),
					price_changed=0,
					stock_changed=0,
				),
			)
			item[flag] = 1

	base_fields = (
		EcommerceItem.name.as_("ecom_item"),
		EcommerceItem.erpnext_item_code.as_("item_code"),
		EcommerceItem.integration_item_code.as_("offer_id"),
		EcommerceItem[PACK_QTY_FIELD].as_("pack_qty"),
	)

	if settings.push_price and settings.price_list:
		ItemPrice = DocType("Item Price")
		add(
			(
				frappe.qb.from_(EcommerceItem)
				.join(ItemPrice)
				.on(
					(ItemPrice.item_code == EcommerceItem.erpnext_item_code)
					& (ItemPrice.price_list == settings.price_list)
				)
				.select(*base_fields)
				.where(EcommerceItem.integration == MODULE_NAME)
				.groupby(EcommerceItem.name)
				.having(Max(ItemPrice.modified) > Coalesce(Max(EcommerceItem.item_synced_on), NEVER_SYNCED))
			).run(as_dict=True),
			"price_changed",
		)

	warehouses = [wh for whs in settings.get_seller_warehouse_map().values() for wh in whs]
	if settings.push_stock and warehouses:
		Bin = DocType("Bin")
		add(
			(
				frappe.qb.from_(EcommerceItem)
				.left_join(Bin)
				.on((Bin.item_code == EcommerceItem.erpnext_item_code) & Bin.warehouse.isin(warehouses))
				.select(*base_fields)
				.where(EcommerceItem.integration == MODULE_NAME)
				.groupby(EcommerceItem.name)
				.having(
					# never pushed (stock may be wrong on Takealot even without a Bin here) or Bin changed
					(Coalesce(Max(EcommerceItem.inventory_synced_on), NEVER_SYNCED) <= NEVER_SYNCED)
					| (Max(Bin.modified) > Coalesce(Max(EcommerceItem.inventory_synced_on), NEVER_SYNCED))
				)
			).run(as_dict=True),
			"stock_changed",
		)

	return list(items.values())
