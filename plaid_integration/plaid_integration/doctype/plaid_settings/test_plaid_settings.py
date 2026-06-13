# Copyright (c) 2026, Aerele Technologies and Contributors
# See license.txt

"""Tests for Plaid Settings and the PlaidConnector wrapper.

Run with:
    bench --site <site> run-tests --app plaid_integration \
        --module plaid_integration.plaid_integration.doctype.plaid_settings.test_plaid_settings

The PlaidConnector tests never hit the network: the connector is built from a
``FakeSettings`` object and its ``.client`` is replaced with a MagicMock, so we
exercise *our* logic (pagination, request building, error wrapping, language /
country mapping) rather than the Plaid SDK.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from plaid_integration.plaid_integration.plaid_connector import PlaidConnector
from plaid_integration.plaid_integration.plaid_test_utils import (
	FakeSettings,
	fake_response,
	make_account,
	make_sync_payload,
	make_transaction,
)


class TestPlaidConnector(IntegrationTestCase):
	"""Unit tests for PlaidConnector — Plaid SDK client fully mocked."""

	def _connector(self, **kwargs) -> PlaidConnector:
		conn = PlaidConnector(settings=FakeSettings(**kwargs))
		conn.client = MagicMock()
		return conn

	# --- construction -----------------------------------------------------

	def test_disabled_settings_raise_on_construct(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			PlaidConnector(settings=FakeSettings(enabled=0))

	# --- country codes ----------------------------------------------------

	def test_country_codes_default_us_ca(self):
		self.assertEqual(self._connector(european=0).get_country_codes(), ["US", "CA"])

	def test_country_codes_include_europe_when_enabled(self):
		codes = self._connector(european=1).get_country_codes()
		self.assertIn("US", codes)
		self.assertIn("GB", codes)
		self.assertIn("DE", codes)
		# European list should be a strict superset of the default.
		self.assertGreater(len(codes), 2)

	# --- language mapping -------------------------------------------------

	def test_language_maps_norwegian_bokmal_to_no(self):
		conn = self._connector()
		with patch.object(frappe.local, "lang", "nb"):
			self.assertEqual(conn.get_plaid_language(), "no")

	def test_language_strips_region_suffix(self):
		conn = self._connector()
		with patch.object(frappe.local, "lang", "fr-CA"):
			self.assertEqual(conn.get_plaid_language(), "fr")

	def test_language_falls_back_to_english_when_unsupported(self):
		conn = self._connector()
		with patch.object(frappe.local, "lang", "zz"):
			self.assertEqual(conn.get_plaid_language(), "en")

	# --- sync pagination (the critical loop) ------------------------------

	def test_sync_transactions_follows_has_more_and_merges_pages(self):
		conn = self._connector()
		page1 = fake_response(
			make_sync_payload(added=[make_transaction("t1")], has_more=True, next_cursor="c1")
		)
		page2 = fake_response(
			make_sync_payload(
				added=[make_transaction("t2")],
				modified=[make_transaction("t3")],
				removed=[{"transaction_id": "t0"}],
				has_more=False,
				next_cursor="c2",
			)
		)
		conn.client.transactions_sync.side_effect = [page1, page2]

		result = conn.sync_transactions("access-token", cursor=None)

		self.assertEqual(conn.client.transactions_sync.call_count, 2)
		self.assertEqual({t["transaction_id"] for t in result["added"]}, {"t1", "t2"})
		self.assertEqual(len(result["modified"]), 1)
		self.assertEqual(len(result["removed"]), 1)
		self.assertEqual(result["next_cursor"], "c2")

	def test_sync_transactions_single_page(self):
		conn = self._connector()
		conn.client.transactions_sync.return_value = fake_response(
			make_sync_payload(added=[make_transaction("t1")], has_more=False, next_cursor="cZ")
		)
		result = conn.sync_transactions("access-token", cursor="prev")
		self.assertEqual(conn.client.transactions_sync.call_count, 1)
		self.assertEqual(result["next_cursor"], "cZ")

	# --- request building -------------------------------------------------

	def test_get_link_token_returns_token(self):
		conn = self._connector()
		conn.client.link_token_create.return_value = fake_response({"link_token": "link-sandbox-xyz"})
		out = conn.get_link_token(user_name="Administrator")
		self.assertEqual(out["link_token"], "link-sandbox-xyz")
		self.assertTrue(conn.client.link_token_create.called)

	def test_reauth_link_token_passes_access_token(self):
		conn = self._connector()
		conn.client.link_token_create.return_value = fake_response({"link_token": "link-reauth"})
		conn.get_link_token(user_name="Administrator", access_token="existing-access-token")
		# The request object is the first positional arg to link_token_create.
		request = conn.client.link_token_create.call_args[0][0]
		self.assertEqual(request.access_token, "existing-access-token")

	def test_exchange_public_token_passthrough(self):
		conn = self._connector()
		conn.client.item_public_token_exchange.return_value = fake_response(
			{"access_token": "acc-tok", "item_id": "item-1"}
		)
		out = conn.exchange_public_token("public-token")
		self.assertEqual(out["item_id"], "item-1")

	def test_get_accounts_passthrough(self):
		conn = self._connector()
		conn.client.accounts_get.return_value = fake_response({"accounts": [make_account()]})
		self.assertEqual(len(conn.get_accounts("tok")["accounts"]), 1)

	# --- error wrapping ---------------------------------------------------

	def test_exchange_public_token_wraps_errors(self):
		conn = self._connector()
		conn.client.item_public_token_exchange.side_effect = Exception("plaid boom")
		with self.assertRaises(frappe.exceptions.ValidationError):
			conn.exchange_public_token("public-token")

	def test_sync_transactions_wraps_errors(self):
		conn = self._connector()
		conn.client.transactions_sync.side_effect = Exception("plaid boom")
		with self.assertRaises(frappe.exceptions.ValidationError):
			conn.sync_transactions("access-token")

	def test_remove_item_wraps_errors(self):
		conn = self._connector()
		conn.client.item_remove.side_effect = Exception("plaid boom")
		with self.assertRaises(frappe.exceptions.ValidationError):
			conn.remove_item("access-token")

	def test_remove_item_success(self):
		conn = self._connector()
		conn.client.item_remove.return_value = fake_response({"request_id": "r1"})
		conn.remove_item("access-token")  # should not raise
		self.assertTrue(conn.client.item_remove.called)


class IntegrationTestPlaidSettings(IntegrationTestCase):
	"""Validation behaviour of the Plaid Settings Single."""

	def _settings(self):
		settings = frappe.get_single("Plaid Settings")
		settings.enabled = 1
		settings.plaid_client_id = "cid"
		settings.plaid_secret = "secret"
		settings.plaid_environment = "sandbox"
		return settings

	def test_history_days_below_minimum_rejected(self):
		settings = self._settings()
		settings.transaction_history_days = 30
		with patch.object(PlaidConnector, "validate_credentials", return_value=None):
			with self.assertRaises(frappe.exceptions.ValidationError):
				settings.validate()

	def test_history_days_above_maximum_rejected(self):
		settings = self._settings()
		settings.transaction_history_days = 800
		with patch.object(PlaidConnector, "validate_credentials", return_value=None):
			with self.assertRaises(frappe.exceptions.ValidationError):
				settings.validate()

	def test_history_days_within_range_accepted(self):
		settings = self._settings()
		settings.transaction_history_days = 180
		with patch.object(PlaidConnector, "validate_credentials", return_value=None):
			settings.validate()  # must not raise

	def test_disabled_settings_skip_validation(self):
		settings = frappe.get_single("Plaid Settings")
		settings.enabled = 0
		settings.transaction_history_days = 5  # out of range, but ignored when disabled
		settings.validate()  # must not raise (no credential call either)

	def test_validate_credentials_invoked_when_enabled_with_creds(self):
		settings = self._settings()
		settings.transaction_history_days = 90
		with patch.object(PlaidConnector, "validate_credentials", return_value=None) as mock_validate:
			settings.validate()
		mock_validate.assert_called_once()
