"""
Delete the broken Ecommerce Attribute custom field on Item (DocType doesn't exist).
bench --site lpdemo execute ecommerce_integrations.shopify_fix_custom_field.run
"""
import frappe


def run():
	cf_name = "Item-ecommerce_attributes"
	if frappe.db.exists("Custom Field", cf_name):
		frappe.delete_doc("Custom Field", cf_name, ignore_missing=True)
		frappe.db.commit()
		print(f"Deleted Custom Field: {cf_name}")
	else:
		print("Custom Field not found — nothing to delete")

	# Verify
	remaining = frappe.get_all(
		"Custom Field",
		filters={"dt": "Item", "options": "Ecommerce Attribute"},
		fields=["name"],
	)
	print(f"Remaining 'Ecommerce Attribute' custom fields on Item: {len(remaining)}")
