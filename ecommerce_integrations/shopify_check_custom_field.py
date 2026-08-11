"""
bench --site lpdemo execute ecommerce_integrations.shopify_check_custom_field.run
"""
import frappe


def run():
	fields = frappe.get_all(
		"Custom Field",
		filters={"dt": "Item", "options": "Ecommerce Attribute"},
		fields=["name", "fieldname", "fieldtype", "options"],
	)
	print("Custom Fields on Item with options='Ecommerce Attribute':")
	for f in fields:
		print(f"  {f.name} | {f.fieldname} | {f.fieldtype} | {f.options}")

	if not fields:
		print("  None found — the custom field is not installed on this site")
	else:
		print("\nThis custom field exists but the DocType it references is missing.")
		print("Fix: either create the Ecommerce Attribute DocType or remove this custom field.")
