# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _


class PlaidConnector:
	def __init__(self, settings=None):
		import plaid
		from plaid.api import plaid_api

		settings = settings or frappe.get_single("Plaid Settings")
		if not settings.enabled:
			frappe.throw(_("Plaid integration is not enabled."))

		env_map = {
			"sandbox": plaid.Environment.Sandbox,
			"development": "https://development.plaid.com",
			"production": plaid.Environment.Production,
		}

		configuration = plaid.Configuration(
			host=env_map.get(settings.plaid_environment, plaid.Environment.Sandbox),
			api_key={
				"clientId": settings.plaid_client_id,
				"secret": settings.get_password(fieldname="plaid_secret", raise_exception=False),
			},
		)

		api_client = plaid.ApiClient(configuration)
		self.client = plaid_api.PlaidApi(api_client)
		self.settings = settings

	def get_country_codes(self):
		"""Return Plaid country codes based on settings."""
		codes = ["US", "CA"]
		if self.settings.enable_european_access:
			codes += ["GB", "IE", "ES", "NL", "FR", "DE", "IT", "PL", "DK", "NO", "SE", "EE", "LT", "LV", "PT", "BE", "AT", "FI"]
		return codes

	def get_plaid_language(self):
		"""Map Frappe site language to a Plaid-supported language code."""
		supported = {"da", "nl", "en", "et", "fr", "de", "hi", "it", "lv", "lt", "no", "pl", "pt", "ro", "es", "sv", "vi"}
		# Frappe uses 'nb' (Norsk Bokmål) but Plaid expects 'no' for Norwegian
		frappe_to_plaid = {"nb": "no"}
		lang = (frappe.local.lang or "en").split("-")[0].lower()
		lang = frappe_to_plaid.get(lang, lang)
		return lang if lang in supported else "en"

	def validate_credentials(self):
		"""Test credentials by fetching one institution. Throws on invalid credentials."""
		try:
			from plaid.model.institutions_get_request import InstitutionsGetRequest
			from plaid.model.country_code import CountryCode

			self.client.institutions_get(
				InstitutionsGetRequest(count=1, offset=0, country_codes=[CountryCode(c) for c in self.get_country_codes()])
			)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plaid Credential Validation Failed")
			frappe.throw(_("Invalid Plaid Client ID or Secret. Please check your credentials."))

	def get_link_token(self, user_name, products=None, access_token=None):
		"""Create a link token to initialize Plaid Link in the browser."""
		try:
			from plaid.model.link_token_create_request import LinkTokenCreateRequest
			from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
			from plaid.model.products import Products
			from plaid.model.country_code import CountryCode

			products = products or ["transactions"]

			webhook_url = frappe.utils.get_url("/api/method/plaid_integration.plaid_integration.api.plaid_webhook")

			request_params = {
				"user": LinkTokenCreateRequestUser(client_user_id=user_name),
				"client_name": (frappe.db.get_single_value("System Settings", "app_name") or frappe.local.site)[:30],
				"language": self.get_plaid_language(),
				"country_codes": [CountryCode(c) for c in self.get_country_codes()],
				"webhook": webhook_url,
			}

			if access_token:
				# re-authentication flow (ITEM_LOGIN_REQUIRED)
				request_params["access_token"] = access_token
			else:
				request_params["products"] = [Products(p) for p in products]

			response = self.client.link_token_create(LinkTokenCreateRequest(**request_params))
			return response.to_dict()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plaid Get Link Token Failed")
			frappe.throw(_("Failed to create Plaid link token. Please check the error log."))

	def exchange_public_token(self, public_token):
		"""Exchange a public_token from Plaid Link for a permanent access_token and item_id."""
		try:
			from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

			response = self.client.item_public_token_exchange(
				ItemPublicTokenExchangeRequest(public_token=public_token)
			)
			return response.to_dict()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plaid Exchange Public Token Failed")
			frappe.throw(_("Failed to exchange Plaid token. Please check the error log."))

	def get_accounts(self, access_token):
		"""Fetch all accounts linked under an access_token."""
		try:
			from plaid.model.accounts_get_request import AccountsGetRequest

			response = self.client.accounts_get(AccountsGetRequest(access_token=access_token))
			return response.to_dict()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plaid Get Accounts Failed")
			frappe.throw(_("Failed to fetch Plaid accounts. Please check the error log."))

	def sync_transactions(self, access_token, cursor=None):
		"""
		Incrementally fetch added/modified/removed transactions using cursor.
		Returns dict with keys: added, modified, removed, next_cursor.
		"""
		try:
			from plaid.model.transactions_sync_request import TransactionsSyncRequest

			params = {"access_token": access_token}
			if cursor:
				params["cursor"] = cursor

			added, modified, removed = [], [], []
			has_more = True
			next_cursor = cursor

			while has_more:
				response = self.client.transactions_sync(TransactionsSyncRequest(**params))
				data = response.to_dict()
				added += data.get("added", [])
				modified += data.get("modified", [])
				removed += data.get("removed", [])
				has_more = data.get("has_more", False)
				next_cursor = data.get("next_cursor")
				params["cursor"] = next_cursor

			return {
				"added": added,
				"modified": modified,
				"removed": removed,
				"next_cursor": next_cursor,
			}
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plaid Sync Transactions Failed")
			frappe.throw(_("Failed to sync Plaid transactions. Please check the error log."))

	def remove_item(self, access_token):
		"""Remove a Plaid Item (unlink a bank). Called when user disconnects a bank account."""
		try:
			from plaid.model.item_remove_request import ItemRemoveRequest

			self.client.item_remove(ItemRemoveRequest(access_token=access_token))
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plaid Remove Item Failed")
			frappe.throw(_("Failed to remove Plaid bank link. Please check the error log."))
