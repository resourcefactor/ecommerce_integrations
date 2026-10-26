# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.utils import flt

from ecommerce_integrations.takealot.constants import (
	FEE_VAT_DESCRIPTION,
	FEES,
	ORDER_ITEM_ID_FIELD,
	SALE_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.takealot.utils import get_takealot_settings, is_cancelled


def set_fee_rows(doc, method=None):
	"""Sales Invoice before_validate hook.

	Takealot charges its fees only once an order line ships, usually after the Sales Order
	was created. The sync keeps the fee fields on the order lines current, and an invoice
	made from the order gets its fee rows rebuilt from them here.
	Returns are skipped: their fee reversals are set by the returns sync."""
	if doc.docstatus != 0 or doc.get("is_return"):
		return
	if not any(item.get(ORDER_ITEM_ID_FIELD) for item in doc.items):
		return
	if not frappe.db.get_single_value(SETTING_DOCTYPE, "enable_takealot"):
		return

	apply_fee_rows(doc)

	unshipped = [
		item.item_code
		for item in doc.items
		if item.get(ORDER_ITEM_ID_FIELD)
		and not is_cancelled(item.get(SALE_STATUS_FIELD))
		and not any(flt(item.get(fee[1])) for fee in FEES)
	]
	if unshipped:
		frappe.msgprint(
			_(
				"Takealot has not charged fees yet for {0}. Fee rows will be added when the order syncs again."
			).format(", ".join(unshipped)),
			indicator="orange",
			alert=True,
		)


def apply_fee_rows(doc, settings=None):
	"""Rebuild the Takealot VAT and fee rows of a Sales Order / Sales Invoice.

	Takealot prices include VAT, so VAT is an inclusive On Net Total row. Each fee type
	becomes an Actual row of minus the fees on the lines (fee fields of reversed fees
	on credit notes are negative, which turns the row positive). Fees include VAT too:
	with an input VAT account set, they are booked net and the VAT goes to that account."""
	settings = settings or get_takealot_settings()
	managed_accounts = {
		account
		for account in (
			settings.vat_account,
			settings.fee_vat_account,
			*(settings.get(fee[2]) for fee in FEES),
		)
		if account
	}
	other_rows = [row for row in doc.get("taxes") if row.account_head not in managed_accounts]

	vat_rows, fee_rows = [], []
	vat_rate = flt(settings.vat_rate)
	if settings.vat_account:
		vat_rows.append(
			{
				"charge_type": "On Net Total",
				"account_head": settings.vat_account,
				"description": _("VAT {0}% (included in price)").format(f"{vat_rate:g}"),
				"rate": vat_rate,
				"included_in_print_rate": 1,
				"cost_center": settings.cost_center,
			}
		)

	fee_vat = 0
	for _sale_field, item_field, account_field, label in FEES:
		total = sum(flt(item.get(item_field)) for item in doc.items)
		if not total:
			continue

		account = settings.get(account_field)
		if not account:
			frappe.throw(_("Please set the {0} account in {1}").format(label, SETTING_DOCTYPE))

		amount = total
		if settings.fee_vat_account and vat_rate:
			amount = flt(total * 100 / (100 + vat_rate), 2)
			fee_vat += total - amount

		fee_rows.append(_actual_row(account, label, -amount, settings))

	fee_vat = flt(fee_vat, 2)
	if fee_vat:
		fee_rows.append(_actual_row(settings.fee_vat_account, FEE_VAT_DESCRIPTION, -fee_vat, settings))

	doc.set("taxes", [])
	for row in vat_rows + other_rows + fee_rows:
		doc.append("taxes", row)


def _actual_row(account, description, amount, settings):
	return {
		"charge_type": "Actual",
		"account_head": account,
		"description": description,
		"tax_amount": amount,
		"cost_center": settings.cost_center,
	}
