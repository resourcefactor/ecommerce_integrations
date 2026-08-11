"""
bench --site lpdemo execute ecommerce_integrations.shopify_setup_check.run
"""
import frappe
from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE


def run():
	setting = frappe.get_doc(SETTING_DOCTYPE)
	print("=== Shopify Setting ===")
	print(f"default_sales_tax_account: {setting.default_sales_tax_account}")
	print(f"default_shipping_charges_account: {setting.default_shipping_charges_account}")
	print(f"sync_old_orders: {setting.sync_old_orders}")
	print(f"old_orders_from: {setting.old_orders_from}")
	print(f"old_orders_to: {setting.old_orders_to}")
	print(f"warehouse: {setting.warehouse}")
	print(f"company: {setting.company}")
	print(f"default_customer: {setting.default_customer}")
	print(f"customer_group: {setting.customer_group}")
	print("\nShopify Tax Account mappings:")
	for t in setting.tax_account:
		print(f"  '{t.shopify_tax}' -> {t.tax_account}")
