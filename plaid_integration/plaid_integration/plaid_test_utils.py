# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Shared test helpers and Plaid mock builders.

This module is intentionally NOT named ``test_*`` so the Frappe test runner does
not collect it as a test case. It provides:

* ``FakeSettings``       — a stand-in for the Plaid Settings Single so a
                           ``PlaidConnector`` can be built without touching the DB
                           or the network.
* ``fake_response``      — wraps a dict in an object exposing ``.to_dict()``,
                           matching what the ``plaid-python`` SDK returns.
* ``make_account`` /     — minimal but realistic Plaid payload builders.
  ``make_transaction`` /
  ``make_sync_payload``

All builders accept overrides so individual tests stay terse and readable.
"""

from unittest.mock import MagicMock


class FakeSettings:
	"""Duck-typed Plaid Settings for constructing PlaidConnector offline."""

	def __init__(
		self,
		enabled=1,
		environment="sandbox",
		client_id="test-client-id",
		secret="test-secret",
		european=0,
		transaction_history_days=90,
	):
		self.enabled = enabled
		self.plaid_environment = environment
		self.plaid_client_id = client_id
		self._secret = secret
		self.enable_european_access = european
		self.transaction_history_days = transaction_history_days

	def get_password(self, fieldname=None, raise_exception=False):
		return self._secret


def fake_response(data: dict) -> MagicMock:
	"""Mimic a plaid-python response object: it has a ``.to_dict()`` method."""
	resp = MagicMock()
	resp.to_dict.return_value = data
	return resp


def make_account(
	account_id="acc-1",
	name="Plaid Checking",
	mask="0000",
	iso_currency_code="USD",
	type="depository",
	subtype="checking",
	current=1000.0,
) -> dict:
	return {
		"account_id": account_id,
		"name": name,
		"mask": mask,
		"type": type,
		"subtype": subtype,
		"balances": {
			"iso_currency_code": iso_currency_code,
			"current": current,
			"available": current,
		},
	}


def make_transaction(
	transaction_id="txn-1",
	account_id="acc-1",
	amount=25.0,
	date="2026-06-01",
	name="Coffee Shop",
	merchant_name=None,
	iso_currency_code="USD",
) -> dict:
	"""A Plaid transaction. Plaid sign convention: positive = money OUT."""
	return {
		"transaction_id": transaction_id,
		"account_id": account_id,
		"amount": amount,
		"date": date,
		"name": name,
		"merchant_name": merchant_name,
		"iso_currency_code": iso_currency_code,
	}


def make_sync_payload(added=None, modified=None, removed=None, has_more=False, next_cursor="cursor-1"):
	"""Build one page of a transactions/sync response (pre-``.to_dict()``)."""
	return {
		"added": added or [],
		"modified": modified or [],
		"removed": removed or [],
		"has_more": has_more,
		"next_cursor": next_cursor,
	}
