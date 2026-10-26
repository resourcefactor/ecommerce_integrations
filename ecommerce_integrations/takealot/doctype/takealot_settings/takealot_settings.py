# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, cstr
from frappe.utils.background_jobs import is_job_enqueued

from ecommerce_integrations.controllers.setting import (
	ERPNextWarehouse,
	IntegrationWarehouse,
	SettingController,
)
from ecommerce_integrations.takealot.api_client import TakealotAPIError, TakealotClient
from ecommerce_integrations.takealot.constants import (
	API_URL,
	FEES,
	OFFER_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_ID_FIELD,
	PACK_QTY_FIELD,
	RETURN_ID_FIELD,
	SALE_STATUS_FIELD,
)


class TakealotSettings(SettingController):
	def is_enabled(self) -> bool:
		return bool(self.enable_takealot)

	def validate(self):
		if not self.is_enabled():
			return

		self.api_url = (self.api_url or API_URL).strip().rstrip("/")
		self._validate_credentials()
		self._set_defaults()
		self._validate_company_links()
		self._validate_mappings()

		if not self.flags.ignore_custom_fields:
			setup_custom_fields()

	def _validate_credentials(self):
		api_key = self.get_password("api_key", raise_exception=False)
		if not api_key:
			# mandatory check reports the missing key
			return

		key_changed = not self.is_dummy_password(self.api_key or "")
		if not (key_changed or not self.seller_id or self.has_value_changed("api_url")):
			return

		seller = _get_seller(TakealotClient(api_key, self.api_url), expands=())
		self.seller_id = cstr(seller.get("seller_id"))
		self.seller_name = seller.get("display_name") or seller.get("legal_name")

	def _set_defaults(self):
		self.return_warehouse = self.return_warehouse or self.warehouse
		self.lookback_days = max(cint(self.lookback_days), 1)
		self.returns_lookback_days = max(cint(self.returns_lookback_days), 1)

	def _validate_company_links(self):
		for fieldname in ("warehouse", "leadtime_warehouse", "return_warehouse", "removal_warehouse"):
			self._check_company("Warehouse", self.get(fieldname))
		for row in self.region_warehouses:
			self._check_company("Warehouse", row.warehouse)
		for row in self.seller_warehouses:
			self._check_company("Warehouse", row.erpnext_warehouse)

		account_fields = ["vat_account", "fee_vat_account", *(fee[2] for fee in FEES)]
		for fieldname in account_fields:
			self._check_company("Account", self.get(fieldname))

		self._check_company("Cost Center", self.cost_center)

	def _check_company(self, doctype, name):
		if not name:
			return
		company, is_group = frappe.db.get_value(doctype, name, ["company", "is_group"]) or (None, 0)
		if company != self.company:
			frappe.throw(_("{0} {1} does not belong to Company {2}").format(doctype, name, self.company))
		if is_group:
			frappe.throw(_("{0} {1} is a group. Please select a ledger.").format(doctype, name))

	def _validate_mappings(self):
		regions = [row.region for row in self.region_warehouses]
		if len(regions) != len(set(regions)):
			frappe.throw(_("Each Takealot region can only be mapped to one warehouse."))

		if self.push_stock and not self.get_seller_warehouse_map():
			frappe.throw(
				_("Map at least one Seller Warehouse to an ERPNext Warehouse to update leadtime stock.")
			)

	def get_erpnext_warehouses(self) -> list[ERPNextWarehouse]:
		warehouses = [self.warehouse, self.leadtime_warehouse, self.return_warehouse, self.removal_warehouse]
		warehouses += [row.warehouse for row in self.region_warehouses]
		warehouses += [row.erpnext_warehouse for row in self.seller_warehouses]
		return list(dict.fromkeys(wh for wh in warehouses if wh))

	def get_erpnext_to_integration_wh_mapping(self) -> dict[ERPNextWarehouse, IntegrationWarehouse]:
		return {
			row.erpnext_warehouse: cstr(row.takealot_warehouse_id)
			for row in self.seller_warehouses
			if row.erpnext_warehouse
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[IntegrationWarehouse, ERPNextWarehouse]:
		mapping = {}
		for row in self.seller_warehouses:
			if row.erpnext_warehouse:
				mapping.setdefault(cstr(row.takealot_warehouse_id), row.erpnext_warehouse)
		return mapping

	def get_seller_warehouse_map(self) -> dict[int, list[str]]:
		"""Takealot seller warehouse id -> ERPNext warehouses whose stock is pushed to it."""
		mapping = {}
		for row in self.seller_warehouses:
			if row.erpnext_warehouse:
				mapping.setdefault(cint(row.takealot_warehouse_id), []).append(row.erpnext_warehouse)
		return mapping

	def get_region_warehouse(self, region: str | None) -> str | None:
		for row in self.region_warehouses:
			if row.region == (region or "").upper():
				return row.warehouse

	@frappe.whitelist()
	def test_connection(self):
		seller = _get_seller(TakealotClient(), expands=())
		return _("Connected to Takealot seller {0} ({1}).").format(
			seller.get("display_name") or seller.get("legal_name"), seller.get("seller_id")
		)

	@frappe.whitelist()
	def fetch_seller_warehouses(self):
		"""Add the seller's leadtime warehouses to the mapping table, keeping existing mappings."""
		seller = _get_seller(TakealotClient(), expands=("warehouses",))
		rows = {cint(row.takealot_warehouse_id): row for row in self.seller_warehouses}

		fetched = added = 0
		for warehouse in seller.get("warehouses") or []:
			warehouse_id = cint(
				warehouse.get("merchant_warehouse_id") or warehouse.get("seller_warehouse_id")
			)
			if not warehouse_id:
				continue
			fetched += 1
			row = rows.get(warehouse_id)
			if not row:
				row = self.append("seller_warehouses", {"takealot_warehouse_id": warehouse_id})
				added += 1
			row.takealot_warehouse_name = warehouse.get("name")

		return {"fetched": fetched, "added": added}

	@frappe.whitelist()
	def import_offers(self):
		from ecommerce_integrations.takealot.product import import_offers

		_enqueue("takealot_import_offers", "Import Takealot Offers", import_offers)

	@frappe.whitelist()
	def export_unmapped_offers(self):
		from ecommerce_integrations.takealot.product import get_unmapped_offers

		return get_unmapped_offers()

	@frappe.whitelist()
	def import_offer_mapping(self, csv_data):
		from ecommerce_integrations.takealot.product import import_offer_mapping

		return import_offer_mapping(csv_data)

	@frappe.whitelist()
	def sync_orders_now(self):
		from ecommerce_integrations.takealot.order import sync_orders_now

		self._validate_old_orders_range()
		_enqueue(
			"takealot_sync_orders_now",
			"Sync Takealot Orders Now",
			sync_orders_now,
			from_date=self.old_orders_from,
			to_date=self.old_orders_to,
		)

	@frappe.whitelist()
	def sync_returns_now(self):
		from ecommerce_integrations.takealot.returns import sync_returns_now

		self._validate_old_orders_range()
		_enqueue(
			"takealot_sync_returns_now",
			"Sync Takealot Returns Now",
			sync_returns_now,
			from_date=self.old_orders_from,
			to_date=self.old_orders_to,
		)

	@frappe.whitelist()
	def push_offers_now(self):
		from ecommerce_integrations.takealot.offer_sync import push_changed_offers

		if not (self.push_price or self.push_stock):
			frappe.throw(_("Enable Update Price or Update Leadtime Stock first."))
		_enqueue("takealot_push_offers", "Push Takealot Offers", push_changed_offers)

	def _validate_old_orders_range(self):
		if not self.old_orders_from or not self.old_orders_to:
			frappe.throw(_("Please set Old Orders From and Old Orders To first."))
		if self.old_orders_from > self.old_orders_to:
			frappe.throw(_("Old Orders From must be on or before Old Orders To."))


def _get_seller(client: TakealotClient, expands) -> dict:
	try:
		return client.get_seller(expands=expands)
	except TakealotAPIError as e:
		if e.status_code in (401, 403):
			frappe.throw(
				_(
					"Takealot rejected the API Key. Generate a new key in Seller Portal › API Access and save it here."
				)
			)
		raise


def _enqueue(job_id, job_name, method, **kwargs):
	# v15's RQ Job list ignores a job_name filter, so check by job_id instead
	if is_job_enqueued(job_id):
		frappe.msgprint(_("{0} is already running in the background.").format(job_name))
		return

	frappe.enqueue(method, queue="long", timeout=4000, job_id=job_id, job_name=job_name, **kwargs)
	frappe.msgprint(_("{0} has started in the background. Check View Logs for the result.").format(job_name))


def setup_custom_fields():
	def header_field(insert_after="customer_name"):
		return dict(
			fieldname=ORDER_ID_FIELD,
			label="Takealot Order ID",
			fieldtype="Data",
			insert_after=insert_after,
			read_only=1,
			search_index=1,
			in_standard_filter=1,
			print_hide=1,
		)

	def item_fields(with_fees=True):
		fields = [
			dict(
				fieldname="takealot_section",
				label="Takealot",
				fieldtype="Section Break",
				insert_after="page_break",
				collapsible=1,
				depends_on=f"eval:doc.{ORDER_ITEM_ID_FIELD}",
			),
			dict(
				fieldname=ORDER_ITEM_ID_FIELD,
				label="Takealot Order Item ID",
				fieldtype="Data",
				insert_after="takealot_section",
				read_only=1,
				search_index=1,
				print_hide=1,
			),
			dict(
				fieldname=OFFER_ID_FIELD,
				label="Takealot Offer ID",
				fieldtype="Data",
				insert_after=ORDER_ITEM_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=SALE_STATUS_FIELD,
				label="Takealot Sale Status",
				fieldtype="Data",
				insert_after=OFFER_ID_FIELD,
				read_only=1,
				allow_on_submit=1,
				print_hide=1,
			),
		]
		if with_fees:
			fields.append(
				dict(
					fieldname="takealot_fees_column",
					fieldtype="Column Break",
					insert_after=SALE_STATUS_FIELD,
				)
			)
			insert_after = "takealot_fees_column"
			for _sale_field, item_field, _account_field, label in FEES:
				fields.append(
					dict(
						fieldname=item_field,
						label=label,
						fieldtype="Currency",
						insert_after=insert_after,
						read_only=1,
						allow_on_submit=1,
						print_hide=1,
					)
				)
				insert_after = item_field
		return fields

	custom_fields = {
		"Sales Order": [header_field()],
		"Sales Order Item": item_fields(),
		"Sales Invoice": [
			header_field(),
			dict(
				fieldname=RETURN_ID_FIELD,
				label="Takealot Return ID",
				fieldtype="Data",
				insert_after="return_against",
				read_only=1,
				search_index=1,
				print_hide=1,
				no_copy=1,
			),
		],
		"Sales Invoice Item": item_fields(),
		"Delivery Note": [header_field()],
		"Delivery Note Item": item_fields(with_fees=False),
		"Ecommerce Item": [
			dict(
				fieldname=PACK_QTY_FIELD,
				label="Takealot Pack Qty",
				fieldtype="Int",
				default="1",
				insert_after="sku",
				depends_on="eval:doc.integration == 'Takealot'",
				description="Units of the ERPNext item in one Takealot offer, e.g. 2 for an 'x 2' bundle.",
			),
		],
	}

	create_custom_fields(custom_fields)
