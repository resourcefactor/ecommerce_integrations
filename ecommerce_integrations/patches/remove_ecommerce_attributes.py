import frappe

# Item buttons that worked on the Ecommerce Attributes table
ATTRIBUTE_BUTTONS = (
	"fetch_amazon_attributes",
	"copy_ecommerce_attributes_btn",
	"sync_attributes_from_amazon_btn",
)


def execute():
	"""Remove the Ecommerce Attributes table and its buttons from Item.

	The Ecommerce Attribute child doctype and the Item form script behind these buttons are no longer
	part of the app, and an Item custom field still pointing to the doctype stops every Item from
	loading. Syncing item.json never deletes fields, so existing sites need this patch.
	"""
	fields = frappe.get_all(
		"Custom Field", filters={"dt": "Item", "options": "Ecommerce Attribute"}, pluck="name"
	)
	fields += frappe.get_all(
		"Custom Field", filters={"dt": "Item", "fieldname": ("in", ATTRIBUTE_BUTTONS)}, pluck="name"
	)
	for name in fields:
		frappe.delete_doc("Custom Field", name, ignore_permissions=True)

	if frappe.db.exists("DocType", "Ecommerce Attribute"):
		frappe.delete_doc("DocType", "Ecommerce Attribute", force=True, ignore_permissions=True)

	frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabEcommerce Attribute`")
