# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

MODULE_NAME = "Takealot"
SETTING_DOCTYPE = "Takealot Settings"

API_URL = "https://marketplace-api.takealot.com/v1"
# Takealot's WAF rejects some default library user agents (e.g. Python-urllib)
USER_AGENT = "ERPNext-Takealot/1.0 (Frappe ecommerce_integrations)"
# Socket timeout (seconds) for every Takealot call, so a stalled connection can't block a worker
REQUEST_TIMEOUT = 30

# page sizes are the API maximums
SALES_PAGE_SIZE = 100
RETURNS_PAGE_SIZE = 100
OFFERS_PAGE_SIZE = 1000

# custom fields
ORDER_ID_FIELD = "takealot_order_id"
ORDER_ITEM_ID_FIELD = "takealot_order_item_id"
OFFER_ID_FIELD = "takealot_offer_id"
SALE_STATUS_FIELD = "takealot_sale_status"
RETURN_ID_FIELD = "takealot_return_id"
PACK_QTY_FIELD = "takealot_pack_qty"

# Fees reported on every sale line (VAT inclusive, only filled once the line ships):
# (sale field, item custom field, settings account field, tax row description)
FEES = (
	("success_fee", "takealot_success_fee", "success_fee_account", "Takealot Success Fee"),
	("fulfillment_fee", "takealot_fulfillment_fee", "fulfillment_fee_account", "Takealot Fulfilment Fee"),
	(
		"courier_collection_fee",
		"takealot_courier_collection_fee",
		"courier_collection_fee_account",
		"Takealot Courier Collection Fee",
	),
	(
		"stock_transfer_fee",
		"takealot_stock_transfer_fee",
		"stock_transfer_fee_account",
		"Takealot Stock Transfer Fee",
	),
)
FEE_VAT_DESCRIPTION = "Takealot VAT on Fees"

# Fee reversals reported on a return (transaction type -> sale fee field)
RETURN_FEE_TRANSACTIONS = {
	"reversal-success-fee": "success_fee",
	"reversal-fulfilment-fee": "fulfillment_fee",
	"reversal-courier-collection-fee": "courier_collection_fee",
}
RETURN_ORDER_TRANSACTION = "reversal-customer-order"

# sale statuses of orders shipped from the seller's own (leadtime) warehouse
LEADTIME_STATUSES = ("New Lead Time Order", "Draft Shipment", "Shipped Shipment")

# return outcomes where Takealot sends the stock back to the seller
REMOVAL_OUTCOMES = ("removal_order", "pending_removal_order")
