"""
Sync old Shopify orders — show full traceback on errors.
bench --site lpdemo execute ecommerce_integrations.shopify_sync_orders.run
"""
import json

import frappe
from shopify.collection import PaginatedIterator
from shopify.resources import Order
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import EVENT_MAPPER
from ecommerce_integrations.shopify.order import sync_sales_order
from ecommerce_integrations.shopify.utils import create_shopify_log


@temp_shopify_session
def run():
	frappe.set_user("Administrator")

	filtered = Order.find(
		created_at_min="2025-01-01T00:00:00Z",
		created_at_max="2026-08-11T00:00:00Z",
		limit=250,
		status="any",
	)

	count = 0
	errors = 0
	skipped = 0

	for page in PaginatedIterator(filtered):
		for order in page:
			order_dict = order.to_dict()
			order_name = order_dict.get("name")
			print(f"  Processing {order_name}...")

			log = create_shopify_log(
				method=EVENT_MAPPER["orders/create"],
				request_data=json.dumps(order_dict),
				make_new=True,
			)
			sync_sales_order(order_dict, request_id=log.name)

			log_doc = frappe.get_doc("Ecommerce Integration Log", log.name)
			status = log_doc.status
			if status == "Success":
				count += 1
				print(f"    -> Created successfully")
			elif status == "Invalid":
				skipped += 1
				print(f"    -> Skipped: {log_doc.message or 'already exists'}")
			else:
				errors += 1
				print(f"    -> {status}")
				if log_doc.message:
					print(f"       message: {log_doc.message[:300]}")
				if log_doc.traceback:
					# Print last 15 lines of traceback
					tb_lines = log_doc.traceback.strip().split("\n")
					print(f"       traceback (last 15 lines):")
					for line in tb_lines[-15:]:
						print(f"         {line}")

			frappe.db.commit()

	print(f"\nSummary: {count} created, {skipped} skipped, {errors} errors")
