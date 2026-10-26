# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, get_system_timezone

from ecommerce_integrations.takealot.invoice import apply_fee_rows
from ecommerce_integrations.takealot.product import BUNDLE_SKU, _strip_ignored_words
from ecommerce_integrations.takealot.returns import _get_fee_reversals, _get_return_warehouse
from ecommerce_integrations.takealot.utils import date_windows, is_cancelled, to_system_datetime

SETTINGS = frappe._dict(
	vat_account="Output VAT",
	vat_rate=15,
	fee_vat_account="Input VAT",
	success_fee_account="Success Fees",
	fulfillment_fee_account="Fulfilment Fees",
	courier_collection_fee_account="Courier Fees",
	stock_transfer_fee_account="Transfer Fees",
	cost_center="Main",
	warehouse="Takealot DC",
	return_warehouse="Takealot DC",
	removal_warehouse="Returns",
)


class TestHelpers(unittest.TestCase):
	def test_date_windows_cover_range_inclusively(self):
		windows = list(date_windows("2026-10-01", "2026-10-16", days=7))
		self.assertEqual(
			windows,
			[
				(date(2026, 10, 1), date(2026, 10, 7)),
				(date(2026, 10, 8), date(2026, 10, 14)),
				(date(2026, 10, 15), date(2026, 10, 16)),
			],
		)

	def test_cancelled_statuses(self):
		for status in (
			"Cancelled by Customer",
			"Cancelled by Takealot - DC Stock Inquiry",
			"Cancelled - Late Delivery",
		):
			self.assertTrue(is_cancelled(status))
		for status in ("Shipped to Customer", "Preparing for Customer", "Returned", None):
			self.assertFalse(is_cancelled(status))

	def test_order_date_converted_to_system_time(self):
		expected = (
			datetime.fromisoformat("2026-10-25T07:34:19+02:00")
			.astimezone(ZoneInfo(get_system_timezone()))
			.replace(tzinfo=None)
		)
		self.assertEqual(to_system_datetime("2026-10-25T07:34:19+02:00"), expected)

	def test_bundle_sku(self):
		self.assertEqual(BUNDLE_SKU.match("I-04212 x 2").groups(), ("I-04212", "2"))
		self.assertEqual(BUNDLE_SKU.match("I-01508  x  2").groups(), ("I-01508", "2"))
		# model numbers with an x are not bundles
		self.assertIsNone(BUNDLE_SKU.match("DW-ONIKUMA-X20"))

	@patch("frappe.db.get_single_value", return_value="DW")
	def test_strip_ignored_words(self, _get_single_value):
		self.assertEqual(_strip_ignored_words("DW - Hc1.5M Hdmi Cable 1.5M"), "Hc1.5M Hdmi Cable 1.5M")
		self.assertEqual(_strip_ignored_words("Back Drop Cloth 2X3 Meter (DW)"), "Back Drop Cloth 2X3 Meter")
		self.assertEqual(_strip_ignored_words("Hc30M Hdmi Cable Flat 30M  -DW"), "Hc30M Hdmi Cable Flat 30M")
		self.assertEqual(_strip_ignored_words("I-01132 Dw"), "I-01132")
		# only whole words at either end
		self.assertEqual(_strip_ignored_words("DWX Charger"), "DWX Charger")


class TestFeeRows(unittest.TestCase):
	def _order(self, fees):
		doc = frappe.new_doc("Sales Order")
		for success_fee, fulfillment_fee in fees:
			doc.append(
				"items",
				{
					"item_code": "_Test",
					"takealot_success_fee": success_fee,
					"takealot_fulfillment_fee": fulfillment_fee,
				},
			)
		return doc

	def _rows(self, doc):
		return [(row.charge_type, row.account_head, flt(row.rate), flt(row.tax_amount)) for row in doc.taxes]

	def test_vat_inclusive_and_fees_net_of_vat(self):
		doc = self._order([(15.46, 51.75)])
		apply_fee_rows(doc, SETTINGS)

		self.assertEqual(
			self._rows(doc),
			[
				("On Net Total", "Output VAT", 15, 0),
				("Actual", "Success Fees", 0, -13.44),
				("Actual", "Fulfilment Fees", 0, -45.0),
				("Actual", "Input VAT", 0, -8.77),
			],
		)
		self.assertEqual(doc.taxes[0].included_in_print_rate, 1)

	def test_rebuild_keeps_other_rows_and_is_idempotent(self):
		doc = self._order([(10, 20), (5, 0)])
		doc.append("taxes", {"charge_type": "Actual", "account_head": "Freight", "tax_amount": 7})
		apply_fee_rows(doc, SETTINGS)
		first = self._rows(doc)
		apply_fee_rows(doc, SETTINGS)

		self.assertEqual(self._rows(doc), first)
		self.assertIn(("Actual", "Freight", 0, 7), first)
		success = next(row for row in first if row[1] == "Success Fees")
		self.assertEqual(success[3], -13.04)  # (10 + 5) / 1.15

	def test_fees_gross_without_input_vat_account(self):
		doc = self._order([(15.46, 0)])
		apply_fee_rows(doc, frappe._dict(SETTINGS, fee_vat_account=None))

		self.assertEqual(self._rows(doc)[1:], [("Actual", "Success Fees", 0, -15.46)])

	def test_fee_reversal_on_credit_note_is_positive(self):
		doc = self._order([(-19.67, 0)])
		apply_fee_rows(doc, SETTINGS)

		self.assertEqual(
			self._rows(doc)[1:], [("Actual", "Success Fees", 0, 17.1), ("Actual", "Input VAT", 0, 2.57)]
		)


class TestReturns(unittest.TestCase):
	def test_fee_reversals_from_return_transactions(self):
		ret = {
			"transactions": [
				{"transaction_type": "reversal-customer-order", "amount_incl_vat": -999.0},
				{"transaction_type": "reversal-success-fee", "amount_incl_vat": 114.88},
				{"transaction_type": "reversal-fulfilment-fee", "amount_incl_vat": 51.75},
			]
		}
		self.assertEqual(_get_fee_reversals(ret), {"success_fee": 114.88, "fulfillment_fee": 51.75})

	def test_return_warehouse_by_outcome(self):
		sellable = {"outcomes": [{"status": "sellable_stock"}]}
		removal = {"outcomes": [{"status": "pending_removal_order"}]}

		self.assertEqual(_get_return_warehouse(sellable, SETTINGS), "Takealot DC")
		self.assertEqual(_get_return_warehouse(removal, SETTINGS), "Returns")
		self.assertEqual(
			_get_return_warehouse(removal, frappe._dict(SETTINGS, removal_warehouse=None)), "Takealot DC"
		)
