# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log import (
	create_log,
)

MODULE_NAME = "Amazon"
SETTING_DOCTYPE = "Amazon SP API Settings"


def create_amazon_log(**kwargs):
	return create_log(module_def=MODULE_NAME, **kwargs)
