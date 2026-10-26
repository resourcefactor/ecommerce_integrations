# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import time
import unittest
from unittest.mock import MagicMock, patch

from ecommerce_integrations.takealot.api_client import TakealotAPIError, TakealotClient
from ecommerce_integrations.takealot.constants import USER_AGENT


def _response(status=200, json_data=None, headers=None):
	response = MagicMock()
	response.status_code = status
	response.ok = status < 400
	response.headers = headers or {}
	response.content = b"{}" if json_data is not None else b""
	response.json.return_value = json_data
	response.text = "" if json_data is None else str(json_data)
	response.reason = "reason"
	return response


class TestTakealotClient(unittest.TestCase):
	def setUp(self):
		self.client = TakealotClient(api_key="test-key", url="https://example.test/v1/")
		self.client.session = MagicMock()
		self.client.session.headers = {}

	def test_auth_and_user_agent_headers(self):
		client = TakealotClient(api_key="test-key", url="https://example.test/v1")
		self.assertEqual(client.session.headers["X-API-Key"], "test-key")
		# Takealot's WAF rejects default library user agents
		self.assertEqual(client.session.headers["User-Agent"], USER_AGENT)
		self.assertEqual(client.base_url, "https://example.test/v1")

	def test_paginate_sends_only_the_continuation_token(self):
		self.client.session.request.side_effect = [
			_response(json_data={"items": [1, 2], "continuation_token": "abc"}),
			_response(json_data={"items": [3]}),
		]

		items = list(self.client.paginate("/sales", {"limit": 2, "order_date__gte": "2026-10-01"}))

		self.assertEqual(items, [1, 2, 3])
		first, second = self.client.session.request.call_args_list
		self.assertEqual(first.kwargs["params"], {"limit": 2, "order_date__gte": "2026-10-01"})
		self.assertEqual(second.kwargs["params"], {"continuation_token": "abc"})

	@patch("ecommerce_integrations.takealot.api_client.time.sleep")
	def test_retries_rate_limited_request(self, sleep):
		self.client.session.request.side_effect = [
			_response(429, headers={"Retry-After": "3"}),
			_response(json_data={"status": "ok"}),
		]

		self.assertEqual(self.client.get_status(), {"status": "ok"})
		sleep.assert_called_once_with(3.0)

	@patch("ecommerce_integrations.takealot.api_client.time.sleep")
	def test_gives_up_after_max_attempts(self, sleep):
		self.client.session.request.return_value = _response(503)

		with self.assertRaises(TakealotAPIError) as error:
			self.client.get_status()
		self.assertEqual(error.exception.status_code, 503)

	def test_raises_with_status_code(self):
		self.client.session.request.return_value = _response(403, json_data={"detail": "forbidden"})

		with self.assertRaises(TakealotAPIError) as error:
			self.client.get_seller()
		self.assertEqual(error.exception.status_code, 403)

	@patch("ecommerce_integrations.takealot.api_client.time.sleep")
	def test_waits_for_exhausted_rate_limit_window(self, sleep):
		reset_at = time.time() + 5
		self.client.session.request.side_effect = [
			_response(
				json_data={}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset_at)}
			),
			_response(json_data={}),
		]

		self.client.get_status()
		sleep.assert_not_called()
		self.client.get_status()
		self.assertAlmostEqual(sleep.call_args.args[0], 5, delta=1)

	def test_iter_returns_expands_outcomes_and_transactions(self):
		self.client.session.request.return_value = _response(json_data={"items": []})

		list(self.client.iter_returns("2026-10-01", "2026-10-25"))

		params = self.client.session.request.call_args.kwargs["params"]
		self.assertEqual(params["expands"], ["outcomes", "transactions"])
		self.assertEqual(params["return_date__gte"], "2026-10-01")
