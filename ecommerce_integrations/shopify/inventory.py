import time
from collections import Counter

import frappe
from frappe.utils import cint, create_batch, now
from pyactiveresource.connection import ClientError, ResourceNotFound
from shopify.resources import InventoryLevel, Variant

from ecommerce_integrations.controllers.inventory import (
	get_inventory_levels_aggregated,
	update_inventory_sync_status,
)
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log


def update_inventory_on_shopify() -> None:
	"""Upload stock levels from ERPNext to Shopify.

	Called by scheduler on configured interval.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopify:
		return

	if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return

	location_warehouse_map = setting.get_location_warehouse_map()
	inventory_levels = get_inventory_levels_aggregated(location_warehouse_map, MODULE_NAME)

	if inventory_levels:
		upload_inventory_data_to_shopify(inventory_levels)


@temp_shopify_session
def upload_inventory_data_to_shopify(inventory_levels) -> None:
	synced_on = now()

	for inventory_sync_batch in create_batch(inventory_levels, 50):
		for d in inventory_sync_batch:
			d.shopify_location_id = d.warehouse  # warehouse field carries location_id from aggregated query

			try:
				variant = _find_variant_with_retry(d.variant_id)
				inventory_id = variant.inventory_item_id

				_set_inventory_level_with_retry(
					location_id=d.shopify_location_id,
					inventory_item_id=inventory_id,
					# shopify doesn't support fractional quantity
					available=cint(d.actual_qty) - cint(d.reserved_qty),
				)
				update_inventory_sync_status(d.ecom_item, time=synced_on, status="Synced")
				d.status = "Success"
			except ResourceNotFound:
				# Variant or location deleted in Shopify — mark and skip.
				update_inventory_sync_status(d.ecom_item, time=synced_on, status="Not Found")
				d.status = "Not Found"
			except ClientError as e:
				if e.response.code == 422:
					# Inventory tracking disabled for this item in Shopify.
					update_inventory_sync_status(d.ecom_item, time=synced_on, status="Tracking Disabled")
					d.status = "Tracking Disabled"
				else:
					frappe.db.set_value(
						"Ecommerce Item", d.ecom_item,
						{"sync_status": "Error", "sync_error": str(e)},
						update_modified=False,
					)
					d.status = "Failed"
					d.failure_reason = str(e)
			except Exception as e:
				frappe.db.set_value(
					"Ecommerce Item", d.ecom_item,
					{"sync_status": "Error", "sync_error": str(e)},
					update_modified=False,
				)
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status(inventory_sync_batch)


def _log_inventory_update_status(inventory_levels) -> None:
	"""Create log of inventory update."""
	log_message = "variant_id,location_id,status,failure_reason\n"

	log_message += "\n".join(
		f"{d.variant_id},{d.shopify_location_id},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])

	percent_successful = stats["Success"] / len(inventory_levels)

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	log_message = f"Updated {percent_successful * 100}% items\n\n" + log_message

	create_shopify_log(method="update_inventory_on_shopify", status=status, message=log_message)


def _find_variant_with_retry(variant_id: str, max_retries: int = 3):
	for attempt in range(max_retries):
		try:
			return Variant.find(variant_id)
		except ResourceNotFound:
			raise
		except ClientError as e:
			if e.response.code == 429 and attempt < max_retries - 1:
				retry_after = float(e.response.headers.get("retry-after", 2.0))
				time.sleep(retry_after)
			else:
				raise


def _set_inventory_level_with_retry(location_id, inventory_item_id, available, max_retries: int = 3) -> None:
	for attempt in range(max_retries):
		try:
			InventoryLevel.set(
				location_id=location_id,
				inventory_item_id=inventory_item_id,
				available=available,
			)
			return
		except ClientError as e:
			if e.response.code == 429 and attempt < max_retries - 1:
				retry_after = float(e.response.headers.get("retry-after", 2.0))
				time.sleep(retry_after)
			else:
				raise
