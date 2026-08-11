"""
bench --site lpdemo execute ecommerce_integrations.shopify_check_logs.run
"""
import frappe


def run():
	logs = frappe.get_all(
		"Ecommerce Integration Log",
		filters={"integration": "shopify"},
		fields=["name", "method", "status", "message", "creation"],
		order_by="creation desc",
		limit=20,
	)
	print(f"\n=== Last {len(logs)} Shopify Integration Logs ===")
	for log in logs:
		print(f"\n[{log.creation}] {log.status} | {log.method}")
		if log.message:
			print(f"  {log.message[:300]}")

	print("\n=== Sales Orders (Shopify) ===")
	sos = frappe.get_all(
		"Sales Order",
		fields=["name", "customer", "transaction_date", "grand_total", "docstatus", "shopify_order_id", "shopify_order_number"],
		filters={"shopify_order_id": ["!=", ""]},
		order_by="creation desc",
		limit=10,
	)
	if not sos:
		print("  No Sales Orders with shopify_order_id found")
	for so in sos:
		print(f"  {so.name} | {so.shopify_order_number} | customer: {so.customer} | date: {so.transaction_date} | total: {so.grand_total} | docstatus: {so.docstatus}")

	print("\n=== Customers created via Shopify ===")
	customers = frappe.get_all(
		"Customer",
		filters={"shopify_customer_id": ["!=", ""]},
		fields=["name", "customer_name", "shopify_customer_id"],
		limit=10,
	)
	for c in customers:
		print(f"  {c.name} | {c.customer_name} | shopify_id: {c.shopify_customer_id}")
