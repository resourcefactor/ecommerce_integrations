# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import time
from collections.abc import Iterator
from typing import Any

import requests

import frappe
from frappe import _

from ecommerce_integrations.takealot.constants import (
	API_URL,
	OFFERS_PAGE_SIZE,
	REQUEST_TIMEOUT,
	RETURNS_PAGE_SIZE,
	SALES_PAGE_SIZE,
	SETTING_DOCTYPE,
	USER_AGENT,
)
from ecommerce_integrations.takealot.utils import format_date

MAX_ATTEMPTS = 4
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


class TakealotAPIError(frappe.ValidationError):
	def __init__(self, message, status_code=None, response_body=None):
		super().__init__(message)
		self.message = message
		self.status_code = status_code
		self.response_body = response_body


class TakealotClient:
	"""Wrapper around the Takealot Marketplace API.

	API docs: https://marketplace-api.takealot.com/v1/docs
	"""

	def __init__(self, api_key: str | None = None, url: str | None = None):
		if not api_key or not url:
			settings = frappe.get_doc(SETTING_DOCTYPE)
			api_key = api_key or settings.get_password("api_key", raise_exception=False)
			url = url or settings.api_url

		if not api_key:
			frappe.throw(_("Takealot API Key is not set in {0}").format(SETTING_DOCTYPE))

		self.base_url = (url or API_URL).rstrip("/")
		self.session = requests.Session()
		self.session.headers.update(
			{"X-API-Key": api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
		)
		# rate limit state from the last response (200 requests per ~60s window)
		self._remaining = None
		self._reset_at = None

	def request(
		self,
		method: str,
		path: str,
		params: dict[str, Any] | None = None,
		body: dict[str, Any] | None = None,
	) -> dict:
		url = self.base_url + path

		for attempt in range(1, MAX_ATTEMPTS + 1):
			self._wait_for_rate_limit()
			try:
				response = self.session.request(
					method, url, params=params, json=body, timeout=REQUEST_TIMEOUT
				)
			except (requests.ConnectionError, requests.Timeout) as e:
				if attempt == MAX_ATTEMPTS:
					raise TakealotAPIError(
						_("Takealot API {0} {1} failed: {2}").format(method, path, e)
					) from e
				time.sleep(2**attempt)
				continue

			self._update_rate_limit(response)

			if response.status_code in RETRY_STATUS_CODES and attempt < MAX_ATTEMPTS:
				time.sleep(self._retry_delay(response, attempt))
				continue

			if not response.ok:
				raise TakealotAPIError(
					_("Takealot API {0} {1} failed with HTTP {2}: {3}").format(
						method, path, response.status_code, response.text[:1000] or response.reason
					),
					status_code=response.status_code,
					response_body=response.text,
				)

			return response.json() if response.content else {}

	def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
		"""Yield every item of a collection endpoint, following `continuation_token`."""
		params = dict(params or {})
		while True:
			data = self.request("GET", path, params=params)
			yield from data.get("items") or []

			token = data.get("continuation_token")
			if not token:
				return
			# Takealot ignores all other parameters once a continuation token is passed
			params = {"continuation_token": token}

	def get_status(self) -> dict:
		return self.request("GET", "/status")

	def get_seller(self, expands=("warehouses",)) -> dict:
		return self.request("GET", "/seller", params={"expands": list(expands)})

	def iter_sales(self, from_date, to_date) -> Iterator[dict]:
		"""Sales (one row per order item) placed between the two dates, inclusive, in SAST."""
		params = {
			"order_date__gte": format_date(from_date),
			"order_date__lte": format_date(to_date),
			"limit": SALES_PAGE_SIZE,
		}
		return self.paginate("/sales", params)

	def iter_offers(self, **filters) -> Iterator[dict]:
		params = {"limit": OFFERS_PAGE_SIZE, **filters}
		return self.paginate("/offers", params)

	def get_offer(self, offer_id, expands=()) -> dict:
		params = {"expands": list(expands)} if expands else None
		return self.request("GET", f"/offers/{offer_id}", params=params)

	def update_offer(self, offer_id, body: dict[str, Any]) -> dict:
		return self.request("PATCH", f"/offers/{offer_id}", body=body)

	def iter_returns(self, from_date, to_date) -> Iterator[dict]:
		"""Returns concluded between the two dates, inclusive, with outcomes and fee reversals."""
		params = {
			"return_date__gte": format_date(from_date),
			"return_date__lte": format_date(to_date),
			"expands": ["outcomes", "transactions"],
			"limit": RETURNS_PAGE_SIZE,
		}
		return self.paginate("/returns", params)

	def _update_rate_limit(self, response):
		try:
			self._remaining = int(response.headers["x-ratelimit-remaining"])
			self._reset_at = float(response.headers["x-ratelimit-reset"])
		except (KeyError, TypeError, ValueError):
			self._remaining = self._reset_at = None

	def _wait_for_rate_limit(self):
		if self._remaining is None or self._remaining > 1 or not self._reset_at:
			return
		wait = self._reset_at - time.time()
		if wait > 0:
			time.sleep(min(wait, 60))

	def _retry_delay(self, response, attempt: int) -> float:
		retry_after = response.headers.get("Retry-After")
		if retry_after:
			try:
				return min(float(retry_after), 60)
			except ValueError:
				pass
		if response.status_code == 429 and self._reset_at:
			return min(max(self._reset_at - time.time(), 1), 60)
		return 2**attempt
