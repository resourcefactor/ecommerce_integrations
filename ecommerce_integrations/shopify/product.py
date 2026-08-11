import time

from pyactiveresource.connection import ClientError, ResourceNotFound
from shopify.resources import Product, Variant

import frappe
from frappe import _, msgprint
from frappe.utils import cint, cstr
from frappe.utils.nestedset import get_root_of

from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
	SUPPLIER_ID_FIELD,
	WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


class ShopifyProduct:
	def __init__(
		self,
		product_id: str,
		variant_id: str | None = None,
		sku: str | None = None,
		has_variants: int | None = 0,
	):
		self.product_id = str(product_id)
		self.variant_id = str(variant_id) if variant_id else None
		self.sku = str(sku) if sku else None
		self.has_variants = has_variants
		self.setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
			has_variants=self.has_variants,
		)

	@temp_shopify_session
	def sync_product(self, product_dict=None):
		if not self.is_synced():
			if product_dict is None:
				shopify_product = Product.find(self.product_id)
				product_dict = shopify_product.to_dict()
			self._make_item(product_dict)

	def _make_item(self, product_dict):
		_add_weight_details(product_dict)

		warehouse = self.setting.warehouse

		if _has_variants(product_dict):
			self.has_variants = 1
			attributes = self._create_attribute(product_dict)
			self._create_item(product_dict, warehouse, 1, attributes)
			self._create_item_variants(product_dict, warehouse, attributes)

		else:
			product_dict["variant_id"] = product_dict["variants"][0]["id"]
			self._create_item(product_dict, warehouse)

	def _create_attribute(self, product_dict):
		attribute = []
		for attr in product_dict.get("options"):
			if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attr.get("name"),
						"item_attribute_values": [
							{"attribute_value": attr_value, "abbr": attr_value}
							for attr_value in attr.get("values")
						],
					}
				).insert()
				attribute.append({"attribute": attr.get("name")})

			else:
				# check for attribute values
				item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
				if not item_attr.numeric_values:
					self._set_new_attribute_values(item_attr, attr.get("values"))
					item_attr.save()
					attribute.append({"attribute": attr.get("name")})

				else:
					attribute.append(
						{
							"attribute": attr.get("name"),
							"from_range": item_attr.get("from_range"),
							"to_range": item_attr.get("to_range"),
							"increment": item_attr.get("increment"),
							"numeric_values": item_attr.get("numeric_values"),
						}
					)

		return attribute

	def _set_new_attribute_values(self, item_attr, values):
		for attr_value in values:
			if not any(
				(d.abbr.lower() == attr_value.lower() or d.attribute_value.lower() == attr_value.lower())
				for d in item_attr.item_attribute_values
			):
				item_attr.append("item_attribute_values", {"attribute_value": attr_value, "abbr": attr_value})

	def _create_item(self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None):
		item_dict = {
			"variant_of": variant_of,
			"is_stock_item": 1,
			"item_code": cstr(product_dict.get("item_code")) or cstr(product_dict.get("id")),
			"item_name": product_dict.get("title", "").strip(),
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": has_variant,
			"attributes": attributes or [],
			"stock_uom": product_dict.get("uom") or _("Nos"),
			"sku": product_dict.get("sku") or _get_sku(product_dict),
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[product_dict.get("weight_unit")],
			"weight_per_unit": product_dict.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]  # shopify product_id
		variant_id = product_dict.get("variant_id", "")  # shopify variant_id if has variants
		sku = item_dict["sku"]

		if not _match_sku_and_link_item(
			item_dict, integration_item_code, variant_id, variant_of=variant_of, has_variant=has_variant
		):
			ecommerce_item.create_ecommerce_item(
				MODULE_NAME,
				integration_item_code,
				item_dict,
				variant_id=variant_id,
				sku=sku,
				variant_of=variant_of,
				has_variants=has_variant,
			)

	def _create_item_variants(self, product_dict, warehouse, attributes):
		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=product_dict.get("id"), has_variants=1
		)

		if template_item:
			for variant in product_dict.get("variants"):
				shopify_item_variant = {
					"id": product_dict.get("id"),
					"variant_id": variant.get("id"),
					"item_code": variant.get("id"),
					"title": product_dict.get("title", "").strip() + "-" + variant.get("title"),
					"product_type": product_dict.get("product_type"),
					"sku": variant.get("sku"),
					"uom": template_item.stock_uom or _("Nos"),
					"item_price": variant.get("price"),
					"weight_unit": variant.get("weight_unit"),
					"weight": variant.get("weight"),
				}

				for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
					if variant.get(variant_attr):
						attributes[i].update(
							{
								"attribute_value": self._get_attribute_value(
									variant.get(variant_attr), attributes[i]
								)
							}
						)
				self._create_item(shopify_item_variant, warehouse, 0, attributes, template_item.name)

	def _get_attribute_value(self, variant_attr_val, attribute):
		attribute_value = frappe.db.sql(
			"""select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
			(attribute["attribute"], variant_attr_val, variant_attr_val),
			as_list=1,
		)
		return attribute_value[0][0] if len(attribute_value) > 0 else cint(variant_attr_val)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if not product_type:
			return parent_item_group

		if frappe.db.get_value("Item Group", product_type, "name"):
			return product_type
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": product_type,
				"parent_item_group": parent_item_group,
				"is_group": "No",
			}
		).insert()
		return item_group.name

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if supplier:
				return product_dict.get("vendor")
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": product_dict.get("vendor"),
					SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
					"supplier_group": self._get_supplier_group(),
				}
			).insert()
			return supplier.name
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def _match_sku_and_link_item(item_dict, product_id, variant_id, variant_of=None, has_variant=False) -> bool:
	"""Tries to match new item with existing item using Shopify SKU == item_code.

	Returns true if matched and linked.
	"""
	sku = item_dict["sku"]
	if not sku or variant_of or has_variant:
		return False

	item_name = frappe.db.get_value("Item", {"item_code": sku})
	if item_name:
		try:
			ecommerce_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": MODULE_NAME,
					"erpnext_item_code": item_name,
					"integration_item_code": product_id,
					"has_variants": 0,
					"variant_id": cstr(variant_id),
					"sku": sku,
				}
			)

			ecommerce_item.insert()
			return True
		except Exception:
			create_shopify_log(
				status="Error",
				message=f"Failed to link item by SKU: {sku}",
				method="_match_sku_and_link_item",
			)
			return False


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):
		product_id = item["product_id"]
		variant_id = item.get("variant_id")
		sku = item.get("sku")
		product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

		if not product.is_synced():
			product.sync_product()


def get_item_code(shopify_item):
	"""Get item code using shopify_item dict.

	Item should contain both product_id and variant_id."""

	item = ecommerce_item.get_erpnext_item(
		integration=MODULE_NAME,
		integration_item_code=shopify_item.get("product_id"),
		variant_id=shopify_item.get("variant_id"),
		sku=shopify_item.get("sku"),
	)
	if item:
		return item.item_code


@temp_shopify_session
def map_erpnext_variant_to_shopify_variant(shopify_product: Product, erpnext_item, variant_attributes):
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		for variant in shopify_product.variants:
			if (
				variant.option1 == variant_attributes.get("option1")
				and variant.option2 == variant_attributes.get("option2")
				and variant.option3 == variant_attributes.get("option3")
			):
				variant_product_id = str(variant.id)
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.id),
							"variant_id": variant_product_id,
							"sku": str(variant.sku),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			msgprint(_("Shopify: Couldn't sync item variant."))
	return variant_product_id


def map_erpnext_item_to_shopify(shopify_product: Product, erpnext_item, setting=None):
	"""Map erpnext fields to shopify, called both when updating and creating new products.

	Returns (extra_variant_fields, metafields) collected from the field mapping table.
	Weight mapping is kept hardcoded because it requires UOM conversion.
	"""
	extra_variant_fields = {}
	metafields = []
	for row in (setting.get("shopify_field_mapping") if setting else []):
		if not row.erpnext_field or not row.shopify_field:
			continue
		value = erpnext_item.get(row.erpnext_field)
		if value is None or value == "":
			continue
		sf = row.shopify_field.strip()
		if sf.startswith("variant:"):
			extra_variant_fields[sf[len("variant:"):]] = value
		elif sf.startswith("metafield:"):
			ns_key = sf[len("metafield:"):]
			if "." in ns_key:
				ns, key = ns_key.split(".", 1)
				metafields.append({
					"namespace": ns,
					"key": key,
					"value": str(value),
					"type": "single_line_text_field",
				})
		else:
			setattr(shopify_product, sf, value)

	# custom_shopify_title takes priority over item_name → title mapping
	if erpnext_item.get("custom_shopify_title"):
		shopify_product.title = erpnext_item.get("custom_shopify_title")

	if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
		shopify_product.weight = erpnext_item.weight_per_unit
		shopify_product.weight_unit = uom

	if erpnext_item.disabled:
		shopify_product.status = "draft"
		shopify_product.published = False
		msgprint(_("Status of linked Shopify product is changed to Draft."))

	return extra_variant_fields, metafields


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def _apply_metafields(product_id, metafields: list) -> None:
	if not metafields:
		return
	from shopify.resources import Metafield

	for mf in metafields:
		try:
			Metafield.create({
				"namespace": mf["namespace"],
				"key": mf["key"],
				"value": mf["value"],
				"type": mf["type"],
				"owner_resource": "product",
				"owner_id": product_id,
			})
		except Exception as e:
			create_shopify_log(
				status="Error",
				message=f"Failed to write metafield {mf.get('namespace')}.{mf.get('key')} on product {product_id}: {e}",
				method="_apply_metafields",
			)


def sync_items_and_price_to_shopify() -> None:
	"""Scheduled trigger: enqueues the heavy sync work on the long queue (1500s timeout)."""
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled():
		return

	if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_item_sync"):
		return

	frappe.enqueue(
		_do_sync_items_and_price,
		setting=setting,
		queue="long",
		timeout=1500,
		job_id="shopify_sync_items_and_price",
		deduplicate=True,
	)


@frappe.whitelist()
def sync_item_to_shopify(ecommerce_item: str) -> dict:
	"""Manually push one already-mapped item's current stock and price to
	Shopify right now, instead of waiting for the scheduled job — the entry
	point for the "Sync Now" button on Ecommerce Item.

	This app does not create new Shopify products from ERPNext — `ecommerce_item`
	must already be mapped to a real Shopify product (via Export/Import Item
	Mapping or a prior sync).
	"""
	from ecommerce_integrations.shopify.inventory import push_single_item_to_shopify

	ecom_doc = frappe.get_doc("Ecommerce Item", ecommerce_item)
	if ecom_doc.integration != MODULE_NAME:
		frappe.throw(_("Ecommerce Item {0} is not a Shopify integration record.").format(ecommerce_item))

	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is not enabled."))

	result = push_single_item_to_shopify(setting, ecom_doc)

	ecom_doc.reload()
	return {
		"sync_status": ecom_doc.sync_status,
		"sync_error": ecom_doc.sync_error,
		"stock_result": result,
	}


def _fetch_product_with_retry(product_id: str, max_retries: int = 3):
	for attempt in range(max_retries):
		try:
			return Product.find(product_id)
		except ResourceNotFound:
			return None
		except ClientError as e:
			if e.response.code == 429 and attempt < max_retries - 1:
				retry_after = float(e.response.headers.get("retry-after", 2.0))
				time.sleep(retry_after)
			else:
				raise


def _sync_product_with_retry(shopify_product, max_retries: int = 3) -> None:
	for attempt in range(max_retries):
		try:
			shopify_product.save()
			return
		except ClientError as e:
			if e.response.code == 429 and attempt < max_retries - 1:
				retry_after = float(e.response.headers.get("retry-after", 2.0))
				time.sleep(retry_after)
			else:
				raise


@temp_shopify_session
def _do_sync_items_and_price(setting) -> None:
	synced_items = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": MODULE_NAME, "has_variants": 0},
		fields=["erpnext_item_code", "integration_item_code", "variant_id"],
	)

	updated = 0
	errors = 0
	for ecom in synced_items:
		try:
			item = frappe.get_doc("Item", ecom.erpnext_item_code)
			shopify_product = _fetch_product_with_retry(ecom.integration_item_code)
			if not shopify_product:
				frappe.db.set_value("Ecommerce Item", ecom.name, "sync_status", "Not Found", update_modified=False)
				continue

			extra_variant_fields, metafields = map_erpnext_item_to_shopify(
				shopify_product=shopify_product, erpnext_item=item, setting=setting
			)

			price = setting.get_item_price(item.item_code)
			if (price is not None or extra_variant_fields) and ecom.variant_id:
				default_variant = shopify_product.variants[0] if shopify_product.variants else None
				if default_variant:
					if price is not None:
						default_variant.price = price
					for field, val in (extra_variant_fields or {}).items():
						setattr(default_variant, field, val)

			_sync_product_with_retry(shopify_product)
			_apply_metafields(shopify_product.id, metafields)
			frappe.db.set_value(
				"Ecommerce Item", ecom.name,
				{"sync_status": "Synced", "sync_error": ""},
				update_modified=False,
			)
			updated += 1
		except Exception as e:
			errors += 1
			frappe.db.set_value(
				"Ecommerce Item", ecom.name,
				{"sync_status": "Error", "sync_error": str(e)[:500]},
				update_modified=False,
			)
			create_shopify_log(
				status="Error",
				message=f"Scheduled sync failed for {ecom.erpnext_item_code}: {e}",
				method="sync_items_and_price_to_shopify",
			)

	create_shopify_log(
		status="Success" if errors == 0 else "Partial Success",
		message=f"Scheduled item sync: {updated} updated, {errors} errors",
		method="sync_items_and_price_to_shopify",
	)
