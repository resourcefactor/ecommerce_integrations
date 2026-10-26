# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser

import frappe
from frappe.utils import get_system_timezone, getdate

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log import (
	create_log,
)
from ecommerce_integrations.takealot.constants import MODULE_NAME, SETTING_DOCTYPE


def create_takealot_log(**kwargs):
	return create_log(module_def=MODULE_NAME, **kwargs)


def get_takealot_settings():
	return frappe.get_cached_doc(SETTING_DOCTYPE)


def get_open_log(method: str, reference: str) -> str | None:
	"""Latest unresolved log of `method` whose message starts with `reference`.

	Sync runs re-scan a lookback window, so a failing order or return is retried
	every run. Reusing its open log keeps it to one log per order instead of one per run."""
	logs = frappe.get_all(
		"Ecommerce Integration Log",
		filters={
			"integration": MODULE_NAME,
			"method": method,
			"status": ("in", ("Error", "Queued")),
			"message": ("like", f"{reference}:%"),
		},
		pluck="name",
		order_by="creation desc",
		limit=1,
	)
	return logs[0] if logs else None


def to_system_datetime(value: str) -> datetime:
	"""Convert a Takealot ISO timestamp (e.g. 2025-01-15T14:30:00+02:00) to naive system time."""
	dt = parser.isoparse(value)
	if dt.tzinfo:
		dt = dt.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	return dt


def date_windows(from_date, to_date, days: int = 7):
	"""Split an inclusive date range into consecutive windows of at most `days` days."""
	start, end = getdate(from_date), getdate(to_date)
	while start <= end:
		window_end = min(start + timedelta(days=days - 1), end)
		yield start, window_end
		start = window_end + timedelta(days=1)


def is_cancelled(status: str | None) -> bool:
	return "cancel" in (status or "").lower()


def format_date(value) -> str:
	return getdate(value).isoformat()
