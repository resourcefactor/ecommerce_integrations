"""
Peek at a couple of Shopify orders to check structure.
bench --site lpdemo execute ecommerce_integrations.shopify_peek.run
"""
import frappe
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE


@temp_shopify_session
def run():
	orders = Order.find(limit=3, status="any")
	for o in orders:
		d = o.to_dict()
		print(f"\nOrder: {d.get('name')} ({d.get('id')}) | status: {d.get('financial_status')} | taxes_included: {d.get('taxes_included')}")
		print(f"  customer: {d.get('customer', {}).get('first_name')} {d.get('customer', {}).get('last_name')} | email: {d.get('customer', {}).get('email')}")
		print(f"  total_price: {d.get('total_price')} | subtotal: {d.get('subtotal_price')}")
		print(f"  shipping_lines: {len(d.get('shipping_lines', []))}")
		for sl in d.get("shipping_lines", []):
			print(f"    shipping: title={sl.get('title')} price={sl.get('price')} tax_lines={sl.get('tax_lines')}")
		print(f"  order tax_lines: {d.get('tax_lines', [])}")
		for item in d.get("line_items", [])[:3]:
			print(f"  item: {item.get('title')[:40]} | qty={item.get('quantity')} | price={item.get('price')} | product_exists={item.get('product_exists')} | variant_id={item.get('variant_id')}")
			if item.get("tax_lines"):
				print(f"    item tax_lines: {item.get('tax_lines')}")
