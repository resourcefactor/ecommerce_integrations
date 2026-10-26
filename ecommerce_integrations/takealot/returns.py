# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from collections import Counter

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import add_days, cint, cstr, flt, getdate, nowdate, strip_html
from frappe.utils.synchronization import LockTimeoutError, filelock

from ecommerce_integrations.takealot.api_client import TakealotAPIError, TakealotClient
from ecommerce_integrations.takealot.constants import (
	FEES,
	OFFER_ID_FIELD,
	ORDER_ID_FIELD,
	REMOVAL_OUTCOMES,
	RETURN_FEE_TRANSACTIONS,
	RETURN_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.takealot.invoice import apply_fee_rows
from ecommerce_integrations.takealot.order import get_sales_order
from ecommerce_integrations.takealot.product import get_item_code
from ecommerce_integrations.takealot.utils import create_takealot_log, date_windows, get_open_log

SYNC_RETURN_METHOD = "ecommerce_integrations.takealot.returns.sync_return"


def sync_new_returns():
	"""Sync returns concluded since the last run, re-reading `returns_lookback_days`
	so returns of orders invoiced in the meantime are picked up."""
	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.sync_returns:
		return

	today = getdate(nowdate())
	synced_till = getdate(settings.returns_synced_till or settings.sync_orders_from or today)
	from_date = add_days(synced_till, -cint(settings.returns_lookback_days))

	if _sync_returns_in_range(from_date, today):
		frappe.db.set_single_value(SETTING_DOCTYPE, "returns_synced_till", today)


def sync_returns_now(from_date, to_date):
	"""Sync returns concluded in a date range on demand."""
	_sync_returns_in_range(from_date, to_date)


def _sync_returns_in_range(from_date, to_date) -> bool:
	counts = Counter()
	try:
		with filelock("takealot_returns_sync", timeout=10):
			client = TakealotClient()
			for window_start, window_end in date_windows(from_date, to_date, days=31):
				returns = list(client.iter_returns(window_start, window_end))
				synced = _get_synced_returns([cstr(ret["seller_return_id"]) for ret in returns])

				for ret in returns:
					return_id = cstr(ret["seller_return_id"])
					if return_id in synced:
						counts["already synced"] += 1
						continue
					if not get_sales_order(ret.get("order_id")):
						# placed before order sync started, or still waiting for SKU mapping
						counts["order not synced"] += 1
						continue

					request_id = get_open_log(SYNC_RETURN_METHOD, return_reference(return_id))
					if request_id and not find_invoice_item(ret):
						# already logged; retried once the order is invoiced
						counts["waiting for invoice"] += 1
						continue

					counts[_sync_return(ret, request_id)] += 1
	except LockTimeoutError:
		return False
	except TakealotAPIError as e:
		create_takealot_log(
			status="Error",
			exception=e,
			message=_("Takealot returns sync {0} to {1} failed: {2}").format(from_date, to_date, e),
			make_new=True,
		)
		return False

	if counts["created"] or counts["error"]:
		create_takealot_log(
			status="Partial Success" if counts["error"] else "Success",
			message=_("Takealot returns {0} to {1}: {2}").format(
				from_date, to_date, ", ".join(f"{count} {status}" for status, count in counts.most_common())
			),
			response_data={"counts": counts},
			make_new=True,
		)
	return True


def sync_return(payload, request_id=None):
	"""Create the credit note of one Takealot return. Also the Retry target of its log."""
	_sync_return(payload, request_id)


def _sync_return(payload, request_id=None) -> str:
	return_id = cstr(payload["seller_return_id"])
	frappe.flags.request_id = request_id
	try:
		settings = frappe.get_doc(SETTING_DOCTYPE)
		if _get_synced_returns([return_id]):
			status, message = "already synced", _("Credit note already exists")
		else:
			credit_note = create_credit_note(payload, settings)
			status, message = "created", _("Credit note {0} created").format(credit_note.name)
	except Exception as e:
		create_takealot_log(
			status="Error",
			exception=e,
			rollback=True,
			method=SYNC_RETURN_METHOD,
			request_data=payload,
			message=f"{return_reference(return_id)}: {strip_html(cstr(e))}",
		)
		return "error"
	else:
		if request_id:
			create_takealot_log(status="Success", message=f"{return_reference(return_id)}: {message}")
		frappe.db.commit()
		return status
	finally:
		frappe.flags.request_id = None


def return_reference(return_id) -> str:
	return f"Takealot return {return_id}"


def create_credit_note(ret, settings):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	invoice_item = find_invoice_item(ret)
	if not invoice_item:
		_throw_missing_invoice(ret)

	_item_code, pack_qty = get_item_code(ret.get("offer_id"), ret.get("sku"))
	qty = cint(ret.get("quantity")) * pack_qty

	credit_note = make_return_doc("Sales Invoice", invoice_item.parent)
	row = next((item for item in credit_note.items if item.sales_invoice_item == invoice_item.name), None)
	if not row:
		frappe.throw(
			_("{0} {1} of Sales Invoice {2} has already been returned in full.").format(
				invoice_item.item_code, invoice_item.name, invoice_item.parent
			)
		)
	_set_return_qty(row, qty)
	if credit_note.update_stock:
		row.warehouse = _get_return_warehouse(ret, settings)

	# fee fields hold the fees of the line; a reversal is a negative fee
	reversals = _get_fee_reversals(ret)
	for sale_field, item_field, _account, _label in FEES:
		row.set(item_field, -reversals.get(sale_field, 0))
	credit_note.set("items", [row])
	apply_fee_rows(credit_note, settings)

	credit_note.set_posting_time = 1
	credit_note.posting_date = getdate(ret.get("return_date")) or getdate(nowdate())
	credit_note.set(RETURN_ID_FIELD, cstr(ret["seller_return_id"]))
	credit_note.set(ORDER_ID_FIELD, cstr(ret.get("order_id")))
	credit_note.remarks = _get_remarks(ret)
	credit_note.insert(ignore_permissions=True)

	if not credit_note.update_stock:
		_create_delivery_return(invoice_item, qty, _get_return_warehouse(ret, settings), credit_note)

	if settings.submit_returns:
		credit_note.submit()
	return credit_note


def find_invoice_item(ret) -> frappe._dict | None:
	"""The submitted Sales Invoice line that billed the returned Takealot order line."""
	SalesInvoice = DocType("Sales Invoice")
	SalesInvoiceItem = DocType("Sales Invoice Item")
	SalesOrder = DocType("Sales Order")
	items = (
		frappe.qb.from_(SalesInvoiceItem)
		.join(SalesInvoice)
		.on(SalesInvoice.name == SalesInvoiceItem.parent)
		.join(SalesOrder)
		.on(SalesOrder.name == SalesInvoiceItem.sales_order)
		.select(
			SalesInvoiceItem.name,
			SalesInvoiceItem.parent,
			SalesInvoiceItem.item_code,
			SalesInvoiceItem.so_detail,
			SalesInvoiceItem.dn_detail,
			SalesInvoiceItem[OFFER_ID_FIELD].as_("offer_id"),
		)
		.where(
			(SalesOrder[ORDER_ID_FIELD] == cstr(ret.get("order_id")))
			& (SalesInvoice.docstatus == 1)
			& (SalesInvoice.is_return == 0)
		)
		.orderby(SalesInvoice.posting_date)
	).run(as_dict=True)
	if not items:
		return None

	offer_id = cstr(ret.get("offer_id"))
	matches = [item for item in items if item.offer_id == offer_id]
	if not matches:
		item_code, _pack_qty = get_item_code(ret.get("offer_id"), ret.get("sku"))
		matches = [item for item in items if item.item_code == item_code] or (
			items if len(items) == 1 else []
		)
	return matches[0] if matches else None


def _throw_missing_invoice(ret):
	sales_order = frappe.db.get_value(
		"Sales Order", {ORDER_ID_FIELD: cstr(ret.get("order_id")), "docstatus": ("<", 2)}, "name"
	)
	if sales_order:
		frappe.throw(
			_("Sales Order {0} has not been invoiced yet. Invoice it, then Retry.").format(sales_order)
		)
	frappe.throw(
		_(
			"No Sales Order found for Takealot order {0}. Sync the order (Sync Orders Now), then Retry."
		).format(ret.get("order_id"))
	)


def _set_return_qty(row, qty):
	row.qty = -qty
	row.stock_qty = -qty * flt(row.conversion_factor or 1)


def _get_return_warehouse(ret, settings) -> str:
	outcomes = [outcome.get("status") for outcome in ret.get("outcomes") or []]
	if settings.removal_warehouse and any(outcome in REMOVAL_OUTCOMES for outcome in outcomes):
		return settings.removal_warehouse
	return settings.return_warehouse or settings.warehouse


def _get_fee_reversals(ret) -> dict:
	"""Fees Takealot refunded on the return, VAT inclusive, by sale fee field."""
	reversals = {}
	for transaction in ret.get("transactions") or []:
		sale_field = RETURN_FEE_TRANSACTIONS.get(transaction.get("transaction_type"))
		if sale_field:
			reversals[sale_field] = reversals.get(sale_field, 0) + abs(
				flt(transaction.get("amount_incl_vat"))
			)
	return reversals


def _get_remarks(ret) -> str:
	outcomes = ", ".join(cstr(outcome.get("status")) for outcome in ret.get("outcomes") or [])
	remarks = [
		_("Takealot return {0} ({1}) of order {2}").format(
			ret.get("seller_return_id"), ret.get("return_reference_number"), ret.get("order_id")
		),
		_("Reason: {0}").format(cstr(ret.get("return_reason")).replace("_", " ")),
		_("Outcome: {0}").format(outcomes.replace("_", " ") or "-"),
	]
	if ret.get("customer_comment"):
		remarks.append(_("Customer comment: {0}").format(ret["customer_comment"]))
	return "\n".join(remarks)


def _create_delivery_return(invoice_item, qty, warehouse, credit_note):
	"""The order shipped on a Delivery Note, so the stock comes back on a draft Delivery Note return."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

	dn_detail = invoice_item.dn_detail or frappe.db.get_value(
		"Delivery Note Item", {"so_detail": invoice_item.so_detail, "docstatus": 1}, "name"
	)
	if not dn_detail:
		return

	delivery_return = make_sales_return(frappe.db.get_value("Delivery Note Item", dn_detail, "parent"))
	row = next((item for item in delivery_return.items if item.dn_detail == dn_detail), None)
	if not row:
		return

	_set_return_qty(row, qty)
	row.warehouse = warehouse
	delivery_return.set("items", [row])
	delivery_return.set(ORDER_ID_FIELD, credit_note.get(ORDER_ID_FIELD))
	delivery_return.posting_date = credit_note.posting_date
	delivery_return.set_posting_time = 1
	delivery_return.remarks = credit_note.remarks
	delivery_return.insert(ignore_permissions=True)
	credit_note.add_comment(
		"Comment", _("Stock return: draft Delivery Note {0}").format(delivery_return.name)
	)


def _get_synced_returns(return_ids) -> set:
	if not return_ids:
		return set()
	return set(
		frappe.get_all(
			"Sales Invoice",
			filters={RETURN_ID_FIELD: ("in", return_ids), "docstatus": ("<", 2)},
			pluck=RETURN_ID_FIELD,
		)
	)
