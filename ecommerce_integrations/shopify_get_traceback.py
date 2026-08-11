"""
bench --site lpdemo execute ecommerce_integrations.shopify_get_traceback.run
"""
import frappe


def run():
	logs = frappe.get_all(
		"Ecommerce Integration Log",
		filters={"status": "Error"},
		fields=["name", "method", "creation"],
		order_by="creation desc",
		limit=1,
	)
	if not logs:
		print("No error logs found")
		return
	doc = frappe.get_doc("Ecommerce Integration Log", logs[0].name)
	print(f"Log: {doc.name} | {doc.method} | {doc.creation}")
	print(f"\nMessage: {doc.message}")
	print(f"\nTraceback:\n{doc.traceback}")
