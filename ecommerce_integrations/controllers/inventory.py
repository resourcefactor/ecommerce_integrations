import frappe
from frappe import _dict
from frappe.query_builder import DocType
from frappe.query_builder.functions import Coalesce, Max, Sum
from frappe.utils import now
from frappe.utils.nestedset import get_descendants_of


def get_inventory_levels(warehouses: tuple[str], integration: str) -> list[_dict]:
	"""
	Get list of dict containing items for which the inventory needs to be updated on Integeration.

	New inventory levels are identified by checking Bin modification timestamp,
	so ensure that if you sync the inventory with integration, you have also
	updated `inventory_synced_on` field in related Ecommerce Item.

	returns: list of _dict containing ecom_item, item_code, integration_item_code, variant_id, actual_qty, warehouse, reserved_qty
	"""
	bin = frappe.qb.DocType("Bin")
	ecommerce_item = frappe.qb.DocType("Ecommerce Item")

	return (
		frappe.qb.from_(ecommerce_item)
		.join(bin)
		.on(ecommerce_item.erpnext_item_code == bin.item_code)
		.select(
			ecommerce_item.name.as_("ecom_item"),
			bin.item_code,
			ecommerce_item.integration_item_code,
			ecommerce_item.variant_id,
			bin.actual_qty,
			bin.warehouse,
			bin.reserved_qty,
		)
		.where(bin.warehouse.isin(warehouses))
		.where(bin.modified > ecommerce_item.inventory_synced_on)
		.where(ecommerce_item.integration == integration)
	).run(as_dict=True)


def get_inventory_levels_of_group_warehouse(warehouse: str, integration: str):
	"""Get updated inventory for a single group warehouse.

	If warehouse mapping is done to a group warehouse then consolidation of all
	leaf warehouses is required"""

	child_warehouse = get_descendants_of("Warehouse", warehouse)
	all_warehouses = (*tuple(child_warehouse), warehouse)

	bin = frappe.qb.DocType("Bin")
	ecommerce_item = frappe.qb.DocType("Ecommerce Item")

	data = (
		frappe.qb.from_(ecommerce_item)
		.join(bin)
		.on(ecommerce_item.erpnext_item_code == bin.item_code)
		.select(
			ecommerce_item.name.as_("ecom_item"),
			bin.item_code,
			ecommerce_item.integration_item_code,
			ecommerce_item.variant_id,
			Sum(bin.actual_qty).as_("actual_qty"),
			Sum(bin.reserved_qty).as_("reserved_qty"),
			Max(bin.modified).as_("last_updated"),
			Max(ecommerce_item.inventory_synced_on).as_("last_synced"),
		)
		.where(bin.warehouse.isin(all_warehouses))
		.where(ecommerce_item.integration == integration)
		.groupby(ecommerce_item.erpnext_item_code)
		.having(Max(bin.modified) > Max(ecommerce_item.inventory_synced_on))
	).run(as_dict=True)

	# add warehouse as group warehouse for sending to integrations
	for item in data:
		item.warehouse = warehouse

	return data


def get_inventory_levels_aggregated(location_warehouse_map: dict, integration: str) -> list[_dict]:
	"""Aggregate stock across multiple ERPNext warehouses per Shopify location.

	Args:
		location_warehouse_map: {shopify_location_id: [warehouse1, warehouse2, ...]}
		integration: integration name (e.g. "shopify")

	Returns list of _dict with ecom_item, item_code, integration_item_code,
	variant_id, actual_qty (summed), reserved_qty (summed), warehouse (= location_id).
	"""
	results = []

	for location_id, warehouses in location_warehouse_map.items():
		if not warehouses:
			continue

		EcommerceItem = DocType("Ecommerce Item")
		Bin = DocType("Bin")

		query = (
			frappe.qb.from_(EcommerceItem)
			.join(Bin)
			.on(EcommerceItem.erpnext_item_code == Bin.item_code)
			.select(
				EcommerceItem.name.as_("ecom_item"),
				Bin.item_code.as_("item_code"),
				EcommerceItem.integration_item_code,
				EcommerceItem.variant_id,
				Sum(Bin.actual_qty).as_("actual_qty"),
				Sum(Bin.reserved_qty).as_("reserved_qty"),
			)
			.where(
				(Bin.warehouse.isin(warehouses))
				& (EcommerceItem.integration == integration)
			)
			.groupby(EcommerceItem.erpnext_item_code)
			.having(Max(Bin.modified) > Coalesce(Max(EcommerceItem.inventory_synced_on), "1970-01-01 00:00:00"))
		)

		rows = query.run(as_dict=1)
		for row in rows:
			# reuse warehouse field to carry the shopify location_id
			row.warehouse = location_id
		results.extend(rows)

	return results


def update_inventory_sync_status(ecommerce_item, time=None, status="Synced"):
	"""Update `inventory_synced_on` timestamp and `sync_status` on Ecommerce Item."""
	if time is None:
		time = now()

	values = {"inventory_synced_on": time, "sync_status": status}
	if status == "Synced":
		values["sync_error"] = ""
	frappe.db.set_value("Ecommerce Item", ecommerce_item, values, update_modified=False)
