# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from collections import Counter, defaultdict

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import add_days, cint, cstr, flt, getdate, nowdate, strip_html
from frappe.utils.synchronization import LockTimeoutError, filelock

from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.takealot.api_client import TakealotAPIError, TakealotClient
from ecommerce_integrations.takealot.constants import (
	FEES,
	LEADTIME_STATUSES,
	OFFER_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_ID_FIELD,
	SALE_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.takealot.invoice import apply_fee_rows
from ecommerce_integrations.takealot.product import get_item_code
from ecommerce_integrations.takealot.utils import (
	create_takealot_log,
	date_windows,
	get_open_log,
	get_takealot_settings,
	is_cancelled,
	to_system_datetime,
)
from ecommerce_integrations.utils.price_list import get_dummy_price_list
from ecommerce_integrations.utils.taxation import get_dummy_tax_category

SYNC_ORDER_METHOD = "ecommerce_integrations.takealot.order.sync_sales_order"
FEE_ITEM_FIELDS = [fee[1] for fee in FEES]


class UnmappedSKUError(frappe.ValidationError):
	def __init__(self, lines):
		self.skus = [cstr(line.get("sku")) for line in lines]
		super().__init__(
			_(
				"SKU {0} not mapped to an ERPNext Item. Map it on an Ecommerce Item (Integration: Takealot) "
				"or with Takealot Settings › Import Offer Mapping, then Retry."
			).format(", ".join(f"'{line.get('sku')}' (offer {line.get('offer_id')})" for line in lines))
		)


def sync_orders():
	"""Scheduled every few minutes; syncs at the configured order sync frequency."""
	settings = get_takealot_settings()
	if not settings.is_enabled() or not settings.sync_orders:
		return
	if not need_to_run(SETTING_DOCTYPE, "order_sync_frequency", "last_order_sync"):
		return

	frappe.enqueue(
		sync_new_orders, queue="long", timeout=4000, job_id="takealot_sync_orders", deduplicate=True
	)


def sync_new_orders():
	"""Sync orders placed since the last run, re-reading `lookback_days` so fees, status
	changes and cancellations reach orders that were already synced. Then sync returns."""
	settings = frappe.get_doc(SETTING_DOCTYPE)
	today = getdate(nowdate())
	sync_from = getdate(settings.sync_orders_from or today)
	synced_till = getdate(settings.orders_synced_till or sync_from)
	from_date = max(sync_from, add_days(synced_till, -cint(settings.lookback_days)))

	if _sync_sales_in_range(from_date, today):
		frappe.db.set_single_value(SETTING_DOCTYPE, "orders_synced_till", today)

	if settings.sync_returns:
		from ecommerce_integrations.takealot.returns import sync_new_returns

		sync_new_returns()


def sync_orders_now(from_date, to_date):
	"""Sync orders placed in a date range on demand, e.g. old orders."""
	_sync_sales_in_range(from_date, to_date)


def _sync_sales_in_range(from_date, to_date) -> bool:
	"""Create or refresh the Sales Orders of all Takealot orders placed in the range.

	Returns False if Takealot couldn't be read or another sync is running."""
	counts, unmapped_skus, notes = Counter(), Counter(), []
	try:
		with filelock("takealot_sales_sync", timeout=10):
			client = TakealotClient()
			for window_start, window_end in date_windows(from_date, to_date):
				orders = defaultdict(list)
				for line in client.iter_sales(window_start, window_end):
					orders[cstr(line["order_id"])].append(line)

				synced = _get_synced_orders(list(orders))
				for order_id, lines in orders.items():
					status = _sync_order(order_id, lines, synced.get(order_id), unmapped_skus, notes)
					counts[status] += 1
	except LockTimeoutError:
		return False
	except TakealotAPIError as e:
		create_takealot_log(
			status="Error",
			exception=e,
			message=_("Takealot sales sync {0} to {1} failed: {2}").format(from_date, to_date, e),
			make_new=True,
		)
		return False

	_log_summary(from_date, to_date, counts, unmapped_skus, notes)
	return True


def _sync_order(order_id, lines, synced, unmapped_skus, notes) -> str:
	if synced and not _has_changes(synced, lines):
		return "unchanged"
	if not synced and all(is_cancelled(line.get("sale_status")) for line in lines):
		return "cancelled before sync"

	request_id = get_open_log(SYNC_ORDER_METHOD, order_reference(order_id))
	if request_id and not synced and not _skus_mapped(lines):
		# already logged; retried once the SKUs are mapped
		unmapped_skus.update(cstr(line.get("sku")) for line in lines)
		return "waiting for SKU mapping"

	status, message = _sync_sales_order({"order_id": order_id, "sales": lines}, request_id)
	if status == "waiting for SKU mapping":
		unmapped_skus.update(cstr(line.get("sku")) for line in lines)
	if message and status != "created":
		notes.append(f"{order_reference(order_id)}: {message}")
	return status


def sync_sales_order(payload, request_id=None):
	"""Create or refresh the Sales Order of one Takealot order.

	payload: {"order_id": ..., "sales": [sale lines of the order]}
	Also the Retry target of the order's Ecommerce Integration Log."""
	_sync_sales_order(payload, request_id)


def _sync_sales_order(payload, request_id=None) -> tuple[str, str | None]:
	order_id = cstr(payload["order_id"])
	frappe.flags.request_id = request_id
	try:
		settings = frappe.get_doc(SETTING_DOCTYPE)
		sales_order = get_sales_order(order_id)
		if sales_order:
			status, message = refresh_sales_order(sales_order, payload["sales"], settings)
		elif all(is_cancelled(line.get("sale_status")) for line in payload["sales"]):
			status, message = "cancelled before sync", _("All lines were cancelled on Takealot")
		else:
			so = create_sales_order(payload, settings)
			status, message = "created", _("Sales Order {0} created").format(so.name)
	except Exception as e:
		create_takealot_log(
			status="Error",
			exception=e,
			rollback=True,
			method=SYNC_ORDER_METHOD,
			request_data=payload,
			message=f"{order_reference(order_id)}: {strip_html(cstr(e))}",
		)
		status = "waiting for SKU mapping" if isinstance(e, UnmappedSKUError) else "error"
		return status, None
	else:
		if request_id:
			create_takealot_log(status="Success", message=f"{order_reference(order_id)}: {message}")
		frappe.db.commit()
		return status, message
	finally:
		frappe.flags.request_id = None


def order_reference(order_id) -> str:
	return f"Takealot order {order_id}"


def get_sales_order(order_id) -> str | None:
	"""Sales Order of a Takealot order. A cancelled one counts, so it's never re-created."""
	orders = frappe.get_all(
		"Sales Order",
		filters={ORDER_ID_FIELD: cstr(order_id)},
		fields=["name", "docstatus"],
		order_by="docstatus asc, creation desc",
	)
	active = [order.name for order in orders if order.docstatus < 2]
	return (active or [order.name for order in orders] or [None])[0]


def create_sales_order(payload, settings):
	lines = [line for line in payload["sales"] if not is_cancelled(line.get("sale_status"))]
	order_date = to_system_datetime(lines[0]["order_date"]).date()

	items, unmapped = [], []
	for line in lines:
		item_code, pack_qty = get_item_code(line.get("offer_id"), line.get("sku"))
		if item_code:
			items.append(_get_item_row(line, item_code, pack_qty, order_date, settings))
		else:
			unmapped.append(line)
	if unmapped:
		raise UnmappedSKUError(unmapped)

	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": settings.customer,
			"company": settings.company,
			"transaction_date": order_date,
			"delivery_date": order_date,
			ORDER_ID_FIELD: cstr(payload["order_id"]),
			"selling_price_list": get_dummy_price_list(),
			"tax_category": get_dummy_tax_category(),
			"ignore_pricing_rule": 1,
			"disable_rounded_total": 1,
			"cost_center": settings.cost_center,
			"items": items,
		}
	)
	if settings.sales_order_series:
		so.naming_series = settings.sales_order_series

	apply_fee_rows(so, settings)
	so.insert(ignore_permissions=True)
	if settings.submit_sales_order:
		so.submit()
	return so


def _get_item_row(line, item_code, pack_qty, order_date, settings) -> dict:
	# selling_price is the VAT inclusive total of the line, in whole Rand
	qty = cint(line.get("quantity")) * pack_qty
	rate = flt(line.get("selling_price")) / qty
	row = {
		"item_code": item_code,
		"qty": qty,
		"rate": rate,
		"price_list_rate": rate,
		"delivery_date": order_date,
		"warehouse": _get_warehouse(line, settings),
		ORDER_ITEM_ID_FIELD: cstr(line.get("order_item_id")),
		OFFER_ID_FIELD: cstr(line.get("offer_id")),
		SALE_STATUS_FIELD: line.get("sale_status"),
	}
	for sale_field, item_field, _account, _label in FEES:
		row[item_field] = flt(line.get(sale_field))
	return row


def _get_warehouse(line, settings) -> str:
	if settings.leadtime_warehouse and line.get("sale_status") in LEADTIME_STATUSES:
		return settings.leadtime_warehouse
	return settings.get_region_warehouse(line.get("stock_source_region")) or settings.warehouse


def refresh_sales_order(name, lines, settings) -> tuple[str, str | None]:
	"""Bring a synced order's line status and fees up to date, and act on cancellations."""
	so = frappe.get_doc("Sales Order", name)
	if so.docstatus == 2:
		return "unchanged", None

	lines_by_id = {cstr(line.get("order_item_id")): line for line in lines}
	changed_rows, cancelled_rows, newly_cancelled = [], [], []
	for row in so.items:
		line = lines_by_id.get(row.get(ORDER_ITEM_ID_FIELD))
		if not line:
			continue
		if is_cancelled(line.get("sale_status")):
			cancelled_rows.append(row)
			if not is_cancelled(row.get(SALE_STATUS_FIELD)):
				newly_cancelled.append(row)

		values = _get_line_values(line)
		if _differs(row, values):
			row.update(values)
			changed_rows.append((row, values))

	takealot_rows = [row for row in so.items if row.get(ORDER_ITEM_ID_FIELD)]
	all_cancelled = bool(takealot_rows) and len(cancelled_rows) == len(takealot_rows)

	if so.docstatus == 0:
		if all_cancelled:
			frappe.delete_doc("Sales Order", so.name, ignore_permissions=True)
			return "cancelled", _("Cancelled on Takealot; draft Sales Order {0} deleted").format(so.name)
		if not changed_rows and not cancelled_rows:
			return "unchanged", None

		so.set("items", [row for row in so.items if row not in cancelled_rows])
		apply_fee_rows(so, settings)
		so.save(ignore_permissions=True)
		return "refreshed", None

	for row, values in changed_rows:
		frappe.db.set_value("Sales Order Item", row.name, values, update_modified=False)
	_update_draft_invoices([row for row, _values in changed_rows])

	if all_cancelled and not flt(so.per_delivered) and not flt(so.per_billed):
		so.reload()
		so.cancel()
		so.add_comment("Comment", _("Cancelled automatically: the order was cancelled on Takealot."))
		return "cancelled", _("Cancelled on Takealot; Sales Order {0} cancelled").format(so.name)

	if newly_cancelled:
		message = _(
			"Lines {0} were cancelled on Takealot. Remove them from Sales Order {1} with Update Items."
		).format(", ".join(row.item_code for row in newly_cancelled), so.name)
		so.add_comment("Comment", message)
		return "needs attention", message

	return ("refreshed" if changed_rows else "unchanged"), None


def _get_line_values(line) -> dict:
	values = {SALE_STATUS_FIELD: line.get("sale_status")}
	for sale_field, item_field, _account, _label in FEES:
		values[item_field] = flt(line.get(sale_field))
	return values


def _differs(row, values) -> bool:
	if cstr(row.get(SALE_STATUS_FIELD)) != cstr(values[SALE_STATUS_FIELD]):
		return True
	return any(flt(row.get(field), 2) != flt(values[field], 2) for field in FEE_ITEM_FIELDS)


def _update_draft_invoices(so_rows):
	"""Carry new fees and statuses to draft invoices made from the order."""
	invoices = set()
	for row in so_rows:
		values = {field: row.get(field) for field in [SALE_STATUS_FIELD, *FEE_ITEM_FIELDS]}
		for item in frappe.get_all(
			"Sales Invoice Item", filters={"so_detail": row.name, "docstatus": 0}, fields=["name", "parent"]
		):
			frappe.db.set_value("Sales Invoice Item", item.name, values, update_modified=False)
			invoices.add(item.parent)

	for invoice in invoices:
		# saving rebuilds the fee rows through the before_validate hook
		frappe.get_doc("Sales Invoice", invoice).save(ignore_permissions=True)


def _get_synced_orders(order_ids) -> dict:
	"""Synced state of the given Takealot orders: {order_id: {"docstatus", "items": {order_item_id: row}}}."""
	if not order_ids:
		return {}

	SalesOrder = DocType("Sales Order")
	SalesOrderItem = DocType("Sales Order Item")
	rows = (
		frappe.qb.from_(SalesOrder)
		.left_join(SalesOrderItem)
		.on(SalesOrderItem.parent == SalesOrder.name)
		.select(
			SalesOrder[ORDER_ID_FIELD].as_("order_id"),
			SalesOrder.docstatus,
			SalesOrderItem[ORDER_ITEM_ID_FIELD].as_("order_item_id"),
			SalesOrderItem[SALE_STATUS_FIELD],
			*(SalesOrderItem[field] for field in FEE_ITEM_FIELDS),
		)
		.where(SalesOrder[ORDER_ID_FIELD].isin(order_ids))
		.orderby(SalesOrder.docstatus)
	).run(as_dict=True)

	# an active order comes before a cancelled one, which is then ignored
	synced = {}
	for row in rows:
		order = synced.setdefault(row.order_id, {"docstatus": row.docstatus, "items": {}})
		if row.docstatus == order["docstatus"] and row.order_item_id:
			order["items"][row.order_item_id] = row
	return synced


def _has_changes(synced, lines) -> bool:
	if synced["docstatus"] == 2:
		return False
	for line in lines:
		row = synced["items"].get(cstr(line.get("order_item_id")))
		if row and _differs(row, _get_line_values(line)):
			return True
	return False


def _skus_mapped(lines) -> bool:
	return all(
		get_item_code(line.get("offer_id"), line.get("sku"))[0]
		for line in lines
		if not is_cancelled(line.get("sale_status"))
	)


def _log_summary(from_date, to_date, counts, unmapped_skus, notes):
	if not any(counts[key] for key in ("created", "refreshed", "cancelled", "error", "needs attention")):
		return

	message = _("Takealot sales {0} to {1}: {2}").format(
		from_date, to_date, ", ".join(f"{count} {status}" for status, count in counts.most_common())
	)
	create_takealot_log(
		status="Partial Success" if counts["error"] or counts["waiting for SKU mapping"] else "Success",
		message=message,
		response_data={"counts": counts, "unmapped_skus": unmapped_skus, "notes": notes},
		make_new=True,
	)
