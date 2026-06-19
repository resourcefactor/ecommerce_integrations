# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import get_datetime
from shopify.collection import PaginatedIterator
from shopify.resources import Location

from ecommerce_integrations.controllers.setting import (
	ERPNextWarehouse,
	IntegrationWarehouse,
	SettingController,
)
from ecommerce_integrations.shopify import connection
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	FULLFILLMENT_ID_FIELD,
	ITEM_PUBLISH_FIELD,
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SUPPLIER_ID_FIELD,
)
from ecommerce_integrations.shopify.oauth import validate_oauth_credentials
from ecommerce_integrations.shopify.utils import (
	ensure_old_connector_is_disabled,
	migrate_from_old_connector,
)


class ShopifySetting(SettingController):
	def is_enabled(self) -> bool:
		return bool(self.enable_shopify)

	def _get_password_safe(self, fieldname: str) -> str:
		"""Safely get password field value without raising exceptions."""
		try:
			if not self.name or self.is_new():
				return ""
			password = self.get_password(fieldname, raise_exception=False)
			return password if password else ""
		except Exception:
			return ""

	def validate(self):
		ensure_old_connector_is_disabled()

		if self.shopify_url:
			self.shopify_url = self.shopify_url.replace("https://", "").replace("http://", "")

		self._set_default_authentication_method()
		self._validate_authentication_fields()
		self._validate_oauth_credentials_if_needed()
		self._handle_webhooks()
		self._validate_warehouse_links()
		self._initalize_default_values()

		if self.is_enabled():
			setup_custom_fields()

	def before_save(self):
		"""Pre-generate OAuth token on save and cache it in memory for this request."""
		if not self.is_enabled():
			return

		if self.authentication_method == "OAuth 2.0 Client Credentials":
			current_token = self._get_password_safe("oauth_access_token")
			token_expiry = self.token_expires_at
			from ecommerce_integrations.shopify.oauth import is_token_valid

			token_ok = bool(current_token) and is_token_valid(token_expiry)
			if (
				self.has_value_changed("client_id")
				or self.has_value_changed("client_secret")
				or not token_ok
			):
				try:
					token = self._get_or_generate_oauth_token()
					# Cache in memory so _handle_webhooks reuses it without re-fetching
					self._oauth_token_cache = token
				except Exception:
					pass

	def _set_default_authentication_method(self):
		"""Set default authentication method for existing documents."""
		if not self.authentication_method:
			self.authentication_method = "Static Token"

	def _validate_authentication_fields(self):
		"""Validate that required fields are present based on authentication method."""
		if not self.is_enabled():
			return

		if self.authentication_method == "Static Token":
			if not self._get_password_safe("password"):
				frappe.throw(_("Password / Access Token is required for Static Token authentication"))
			if not self.shared_secret:
				frappe.throw(_("Shared secret / API Secret is required for Static Token authentication"))

		elif self.authentication_method == "OAuth 2.0 Client Credentials":
			if not self.client_id:
				frappe.throw(_("Client ID is required for OAuth 2.0 authentication"))
			if not self._get_password_safe("client_secret"):
				frappe.throw(_("Client Secret is required for OAuth 2.0 authentication"))

	def _validate_oauth_credentials_if_needed(self):
		"""Validate OAuth credentials by generating a test token if credentials changed."""
		if not self.is_enabled():
			return
		if self.authentication_method != "OAuth 2.0 Client Credentials":
			return

		if self.has_value_changed("client_id") or self.has_value_changed("client_secret"):
			client_secret = self._get_password_safe("client_secret")
			if not client_secret:
				return
			try:
				validate_oauth_credentials(self.shopify_url, self.client_id, client_secret)
				frappe.msgprint(
					_("OAuth credentials validated successfully."),
					indicator="green",
					alert=True,
				)
			except Exception:
				raise

	def _get_or_generate_oauth_token(self) -> str:
		"""Get OAuth token if valid, or generate a new one if expired/missing."""
		from ecommerce_integrations.shopify.oauth import is_token_valid, refresh_oauth_token

		current_token = self._get_password_safe("oauth_access_token")
		token_expiry = self.token_expires_at

		if current_token and is_token_valid(token_expiry):
			return current_token

		# Token missing or expired — generate fresh
		try:
			token = refresh_oauth_token(self)
			# Keep in-memory expiry in sync so subsequent calls don't re-generate
			self.token_expires_at = frappe.db.get_value(
				"Shopify Setting", "Shopify Setting", "token_expires_at"
			)
			return token
		except Exception as e:
			frappe.throw(
				_("Failed to generate OAuth token: {0}").format(str(e)),
				title=_("OAuth Authentication Error"),
			)

	def on_update(self):
		if self.is_enabled() and not self.is_old_data_migrated:
			migrate_from_old_connector()

	def _handle_webhooks(self):
		if self.is_enabled() and not self.webhooks:
			if self.authentication_method == "OAuth 2.0 Client Credentials":
				# Use in-memory cached token from before_save if available
				password = getattr(self, "_oauth_token_cache", None) or self._get_or_generate_oauth_token()
			else:
				password = self.get_password("password")

			new_webhooks = connection.register_webhooks(self.shopify_url, password)

			if not new_webhooks:
				msg = _("Shopify webhooks could not be registered.") + "<br>"
				msg += _("This is usually caused by missing webhook scopes on your Shopify app.") + "<br>"
				msg += _(
					"Go to <b>dev.shopify.com</b> → your app → Configuration → add "
					"<b>write_webhook_subscriptions</b> scope → release a new version → "
					"reinstall the app on your store, then disable and re-enable this integration."
				)
				frappe.msgprint(msg, title=_("Webhook Registration Failed"), indicator="orange")
			else:
				for webhook in new_webhooks:
					webhook_id = webhook.get("id") if isinstance(webhook, dict) else webhook.id
					webhook_topic = webhook.get("topic") if isinstance(webhook, dict) else webhook.topic
					self.append("webhooks", {"webhook_id": webhook_id, "method": webhook_topic})

		elif not self.is_enabled():
			if self.authentication_method == "OAuth 2.0 Client Credentials":
				password = self._get_password_safe("oauth_access_token")
			else:
				password = self._get_password_safe("password")

			if password:
				try:
					connection.unregister_webhooks(self.shopify_url, password)
				except Exception:
					pass  # ignore errors when disabling

			self.webhooks = list()  # remove all webhooks

	def _validate_warehouse_links(self):
		for wh_map in self.shopify_warehouse_mapping:
			if not wh_map.erpnext_warehouse:
				frappe.throw(_("ERPNext warehouse required in warehouse map table."))

	def _initalize_default_values(self):
		if not self.last_inventory_sync:
			self.last_inventory_sync = get_datetime("1970-01-01")
		if not self.last_item_sync:
			self.last_item_sync = get_datetime("1970-01-01")
		if not self.shopify_field_mapping:
			for erpnext_field, shopify_field in [
				("item_name", "title"),
				("description", "body_html"),
				("item_group", "product_type"),
				("brand", "vendor"),
			]:
				self.append("shopify_field_mapping", {
					"erpnext_field": erpnext_field,
					"shopify_field": shopify_field,
				})

	@frappe.whitelist()
	@connection.temp_shopify_session
	def update_location_table(self):
		"""Fetch locations from shopify and add it to child table so user can
		map it with correct ERPNext warehouse."""

		self.shopify_warehouse_mapping = []
		for locations in PaginatedIterator(Location.find()):
			for location in locations:
				self.append(
					"shopify_warehouse_mapping",
					{"shopify_location_id": location.id, "shopify_location_name": location.name},
				)

	def get_erpnext_warehouses(self) -> list[ERPNextWarehouse]:
		return [wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping]

	def get_erpnext_to_integration_wh_mapping(self) -> dict[ERPNextWarehouse, IntegrationWarehouse]:
		return {
			wh_map.erpnext_warehouse: wh_map.shopify_location_id for wh_map in self.shopify_warehouse_mapping
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[IntegrationWarehouse, ERPNextWarehouse]:
		return {
			wh_map.shopify_location_id: wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping
		}

	def get_item_price(self, item_code):
		if not self.price_list:
			return None
		return frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": self.price_list, "selling": 1},
			"price_list_rate",
		)

	@frappe.whitelist()
	def sync_items_to_shopify(self):
		from ecommerce_integrations.shopify.utils import create_shopify_log

		items = frappe.get_all(
			"Item",
			filters={ITEM_PUBLISH_FIELD: 1, "disabled": 0, "has_variants": 0},
			pluck="name",
		)
		enqueued = 0
		errors = 0
		for item_code in items:
			try:
				if frappe.db.exists("Ecommerce Item", {"erpnext_item_code": item_code, "integration": MODULE_NAME}):
					continue
				frappe.enqueue(
					"ecommerce_integrations.shopify.product.upload_erpnext_item",
					doc=frappe.get_doc("Item", item_code),
					queue="long",
				)
				enqueued += 1
			except Exception as e:
				errors += 1
				create_shopify_log(
					status="Error",
					message=f"Failed to queue {item_code} for Shopify sync: {e}",
					method="sync_items_to_shopify",
				)
		msg = _("{0} item(s) queued for Shopify sync.").format(enqueued)
		if errors:
			msg += " " + _("{0} error(s) — check Ecommerce Integration Log.").format(errors)
		return msg

	@frappe.whitelist()
	@connection.temp_shopify_session
	def sync_price_to_shopify(self):
		from shopify.resources import Variant

		from ecommerce_integrations.shopify.utils import create_shopify_log

		if not self.price_list:
			frappe.throw(_("Please set a Price List in Shopify Setting first."))
		synced_items = frappe.get_all(
			"Ecommerce Item",
			filters={"integration": MODULE_NAME, "has_variants": 0},
			fields=["erpnext_item_code", "variant_id"],
		)
		updated = 0
		errors = 0
		for ecom in synced_items:
			try:
				price = self.get_item_price(ecom.erpnext_item_code)
				if not price or not ecom.variant_id:
					continue
				variant = Variant.find(ecom.variant_id)
				if not variant:
					continue
				variant.price = price
				variant.save()
				updated += 1
			except Exception as e:
				errors += 1
				create_shopify_log(
					status="Error",
					message=f"Price sync failed for {ecom.erpnext_item_code}: {e}",
					method="sync_price_to_shopify",
				)
		msg = _("{0} item price(s) synced to Shopify.").format(updated)
		if errors:
			msg += " " + _("{0} error(s) — check Ecommerce Integration Log.").format(errors)
		return msg


def setup_custom_fields():
	custom_fields = {
		"Item": [
			dict(
				fieldname=ITEM_PUBLISH_FIELD,
				label="Publish on Website",
				fieldtype="Check",
				insert_after="standard_rate",
				default=0,
			)
		],
		"Customer": [
			dict(
				fieldname=CUSTOMER_ID_FIELD,
				label="Shopify Customer Id",
				fieldtype="Data",
				insert_after="series",
				read_only=1,
				print_hide=1,
			)
		],
		"Supplier": [
			dict(
				fieldname=SUPPLIER_ID_FIELD,
				label="Shopify Supplier Id",
				fieldtype="Data",
				insert_after="supplier_name",
				read_only=1,
				print_hide=1,
			)
		],
		"Address": [
			dict(
				fieldname=ADDRESS_ID_FIELD,
				label="Shopify Address Id",
				fieldtype="Data",
				insert_after="fax",
				read_only=1,
				print_hide=1,
			)
		],
		"Sales Order": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Order Item": [
			dict(
				fieldname=ORDER_ITEM_DISCOUNT_FIELD,
				label="Shopify Discount per unit",
				fieldtype="Float",
				insert_after="discount_and_margin",
				read_only=1,
			),
		],
		"Delivery Note": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=FULLFILLMENT_ID_FIELD,
				label="Shopify Fulfillment Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Invoice": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
	}

	create_custom_fields(custom_fields)
