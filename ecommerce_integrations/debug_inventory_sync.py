"""
Diagnostic: run with
  bench --site <site> execute ecommerce_integrations.debug_inventory_sync.run
"""
import frappe
from frappe.query_builder import DocType
from frappe.query_builder.functions import Max, Sum


def run():
	setting = frappe.get_doc("Shopify Setting")
	location_warehouse_map = setting.get_location_warehouse_map()

	print("\n=== Location → Warehouse Map ===")
	for loc, whs in location_warehouse_map.items():
		print(f"  Location {loc}: {whs}")

	EcommerceItem = DocType("Ecommerce Item")
	Bin = DocType("Bin")

	for location_id, warehouses in location_warehouse_map.items():
		print(f"\n=== Query for location {location_id}, warehouses {warehouses} ===")

		# Raw bin stock per warehouse (no HAVING filter)
		raw = (
			frappe.qb.from_(EcommerceItem)
			.join(Bin)
			.on(EcommerceItem.erpnext_item_code == Bin.item_code)
			.select(
				EcommerceItem.erpnext_item_code,
				Bin.warehouse,
				Bin.actual_qty,
				Bin.modified,
				EcommerceItem.inventory_synced_on,
			)
			.where(
				(Bin.warehouse.isin(warehouses))
				& (EcommerceItem.integration == "shopify")
			)
			.limit(10)
		).run(as_dict=1)

		print(f"  Raw bin rows (first 10, no HAVING):")
		for r in raw:
			print(f"    {r.erpnext_item_code} | {r.warehouse} | qty={r.actual_qty} | bin_modified={r.modified} | synced_on={r.inventory_synced_on}")

		# Aggregated with HAVING
		aggregated = (
			frappe.qb.from_(EcommerceItem)
			.join(Bin)
			.on(EcommerceItem.erpnext_item_code == Bin.item_code)
			.select(
				EcommerceItem.erpnext_item_code,
				Sum(Bin.actual_qty).as_("total_qty"),
				Max(Bin.modified).as_("last_bin_modified"),
				Max(EcommerceItem.inventory_synced_on).as_("last_synced"),
			)
			.where(
				(Bin.warehouse.isin(warehouses))
				& (EcommerceItem.integration == "shopify")
			)
			.groupby(EcommerceItem.erpnext_item_code)
			.having(Max(Bin.modified) > Max(EcommerceItem.inventory_synced_on))
			.limit(10)
		).run(as_dict=1)

		print(f"\n  Aggregated rows pending sync (first 10, with HAVING):")
		if not aggregated:
			print("  → 0 rows. Either all items are up-to-date (no bin changes since last sync), or inventory_synced_on is NULL blocking the comparison.")
		for r in aggregated:
			print(f"    {r.erpnext_item_code} | total_qty={r.total_qty} | bin_modified={r.last_bin_modified} | synced_on={r.last_synced}")

		# Check for NULL inventory_synced_on
		null_synced = frappe.db.count("Ecommerce Item", {"integration": "shopify", "inventory_synced_on": ["is", "not set"]})
		print(f"\n  Ecommerce Items with NULL inventory_synced_on: {null_synced}")
