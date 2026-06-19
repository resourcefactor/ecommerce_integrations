# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""
OAuth 2.0 Client Credentials Flow for Shopify Apps
Implements token generation and refresh for apps created via Shopify Dev Dashboard
"""

import json
import time
from datetime import timedelta

import frappe
import requests
from frappe import _
from frappe.utils import get_datetime, get_datetime_str, now_datetime
from frappe.utils.password import set_encrypted_password

from ecommerce_integrations.shopify.utils import create_shopify_log


def get_oauth_token_endpoint(shopify_url: str) -> str:
	"""Construct the OAuth token endpoint URL for a given shop."""
	shop_url = shopify_url.replace("https://", "").replace("http://", "").rstrip("/")
	return f"https://{shop_url}/admin/oauth/access_token"


def generate_oauth_token(shopify_url: str, client_id: str, client_secret: str) -> dict:
	"""
	Generate a new OAuth 2.0 access token using client credentials flow.

	Returns dict with access_token, expires_in, scope.
	Raises frappe.ValidationError if token generation fails.
	"""
	token_endpoint = get_oauth_token_endpoint(shopify_url)

	payload = {
		"grant_type": "client_credentials",
		"client_id": client_id,
		"client_secret": client_secret,
	}

	headers = {
		"Content-Type": "application/x-www-form-urlencoded",
	}

	try:
		response = requests.post(token_endpoint, data=payload, headers=headers, timeout=30)
		response.raise_for_status()

		token_data = response.json()

		create_shopify_log(
			status="Success",
			method="ecommerce_integrations.shopify.oauth.generate_oauth_token",
			message=_("OAuth token generated successfully"),
		)

		return token_data

	except requests.exceptions.RequestException as e:
		error_message = str(e)
		error_response = None

		if hasattr(e, "response") and e.response is not None:
			try:
				error_response = e.response.json()
				error_message = error_response.get(
					"error_description", error_response.get("error", str(e))
				)
			except (json.JSONDecodeError, Exception):
				error_message = e.response.text or str(e)

		sanitized_payload = payload.copy()
		sanitized_payload["client_secret"] = "REDACTED"

		create_shopify_log(
			status="Error",
			method="ecommerce_integrations.shopify.oauth.generate_oauth_token",
			message=_("Failed to generate OAuth token"),
			exception=error_message,
			request_data=sanitized_payload,
			response_data=error_response,
		)

		frappe.throw(
			_("Failed to generate OAuth token: {0}").format(error_message),
			title=_("OAuth Authentication Error"),
		)


def is_token_valid(token_expires_at, buffer_minutes: int = 5) -> bool:
	"""Check if the OAuth token is still valid with a buffer period."""
	if not token_expires_at:
		return False

	expiry_datetime = get_datetime(token_expires_at)
	buffer_time = now_datetime() + timedelta(minutes=buffer_minutes)

	return expiry_datetime > buffer_time


def calculate_token_expiry(expires_in_seconds: int):
	"""Calculate the exact expiry datetime for a token."""
	return now_datetime() + timedelta(seconds=expires_in_seconds)


def refresh_oauth_token(setting) -> str:
	"""
	Refresh the OAuth token and update the setting document.
	Called when the token is expired or about to expire.
	"""
	if setting.authentication_method != "OAuth 2.0 Client Credentials":
		frappe.throw(
			_("Token refresh is only applicable for OAuth 2.0 authentication"),
			title=_("Invalid Authentication Method"),
		)

	setting.reload()

	token_data = generate_oauth_token(
		setting.shopify_url,
		setting.client_id,
		setting.get_password("client_secret"),
	)

	expires_at = calculate_token_expiry(token_data.get("expires_in", 86399))

	set_encrypted_password(
		"Shopify Setting",
		setting.name,
		token_data["access_token"],
		fieldname="oauth_access_token",
	)

	frappe.db.set_value(
		"Shopify Setting",
		setting.name,
		"token_expires_at",
		get_datetime_str(expires_at),
		update_modified=False,
	)

	setting.reload()

	return token_data["access_token"]


def get_valid_access_token(setting) -> str:
	"""
	Get a valid OAuth access token, refreshing if necessary.
	Returns a valid token ready to use.
	"""
	if setting.authentication_method != "OAuth 2.0 Client Credentials":
		frappe.throw(
			_("This method is only for OAuth 2.0 authentication"),
			title=_("Invalid Authentication Method"),
		)

	if is_token_valid(setting.token_expires_at):
		current_token = setting.get_password("oauth_access_token", raise_exception=False)
		if current_token:
			return current_token

	try:
		return refresh_oauth_token(setting)
	except Exception as e:
		create_shopify_log(
			status="Warning",
			method="ecommerce_integrations.shopify.oauth.get_valid_access_token",
			message=_("Token refresh failed, retrying once..."),
			exception=str(e),
		)
		time.sleep(1)
		return refresh_oauth_token(setting)


def validate_oauth_credentials(shopify_url: str, client_id: str, client_secret: str) -> bool:
	"""
	Validate OAuth credentials by attempting to generate a token.
	Used during setup to verify credentials are correct.
	"""
	try:
		token_data = generate_oauth_token(shopify_url, client_id, client_secret)
		return bool(token_data.get("access_token"))
	except Exception:
		raise
