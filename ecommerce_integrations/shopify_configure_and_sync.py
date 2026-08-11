"""
Configure Shopify Setting tax accounts and trigger old order sync for a test date range.
bench --site lpdemo execute ecommerce_integrations.shopify_configure_and_sync.run
"""
import frappe
from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE


def run():
	setting = frappe.get_doc(SETTING_DOCTYPE)

	# Set tax accounts — VAT - LP for both sales tax and shipping
	setting.default_sales_tax_account = "VAT - LP"
	setting.default_shipping_charges_account = "VAT - LP"

	# Add FR TVA mapping so order #1003 tax line resolves to VAT - LP
	# First check if it already exists
	existing_taxes = [t.shopify_tax for t in setting.taxes]
	if "FR TVA" not in existing_taxes:
		setting.append("taxes", {
			"shopify_tax": "FR TVA",
			"tax_account": "VAT - LP",
		})
		print("Added FR TVA -> VAT - LP tax mapping")
	else:
		print("FR TVA mapping already exists")

	# Set date range: pull just order #1003 first (Aug 2026) and earlier orders
	# Use a narrow range to test: just pull 3 recent orders
	setting.old_orders_from = "2025-01-01 00:00:00"
	setting.old_orders_to = "2026-08-10 23:59:59"
	setting.sync_old_orders = 1

	print(f"Saving setting with sync_old_orders=1, range: {setting.old_orders_from} to {setting.old_orders_to}")
	setting.save(ignore_permissions=True)
	frappe.db.commit()
	print("Setting saved. Now running sync_old_orders directly...")

	from ecommerce_integrations.shopify.order import sync_old_orders
	sync_old_orders()
	frappe.db.commit()
	print("Done — check Shopify Log for results.")
