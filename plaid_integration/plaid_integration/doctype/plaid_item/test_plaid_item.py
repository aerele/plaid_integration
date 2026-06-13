# Copyright (c) 2026, Aerele Technologies and Contributors
# See license.txt

"""End-to-end integration tests for the Plaid api layer.

Run with:
    bench --site <site> run-tests --app plaid_integration \
        --module plaid_integration.plaid_integration.doctype.plaid_item.test_plaid_item

These tests use a *real* Frappe/ERPNext DB (a Company, Bank Accounts, GL
Accounts and submitted Bank Transactions are created) but mock the Plaid network
layer entirely by patching ``plaid_integration.plaid_integration.api.PlaidConnector``.
Notification dispatch and ``frappe.enqueue`` are patched so jobs are asserted on
rather than actually enqueued.

Every test runs inside the IntegrationTestCase transaction and is rolled back.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from plaid_integration.plaid_integration import api
from plaid_integration.plaid_integration.plaid_test_utils import (
	make_account,
	make_transaction,
)

API = "plaid_integration.plaid_integration.api"


def _first_company() -> str:
	"""Reuse ERPNext's test company, else fall back to any company on the site."""
	name = frappe.db.get_value("Company", {"company_name": "_Test Company"}, "name")
	if name:
		return name
	companies = frappe.get_all("Company", pluck="name", limit=1)
	if companies:
		return companies[0]
	raise Exception("No Company available on the test site")


def _ensure_bank_group(company: str) -> str:
	"""Guarantee a Bank-type group Account exists (required by _get_or_create_gl_account)."""
	existing = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Bank", "is_group": 1, "disabled": 0},
	)
	if existing:
		return existing

	asset_root = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Asset", "is_group": 1}, "name"
	)
	acc = frappe.get_doc({
		"doctype": "Account",
		"account_name": "Plaid Test Bank Group",
		"company": company,
		"is_group": 1,
		"account_type": "Bank",
		"parent_account": asset_root,
	}).insert(ignore_if_duplicate=True)
	return acc.name


class IntegrationTestPlaidItem(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _first_company()
		_ensure_bank_group(cls.company)
		cls.institution = "Test Federal Bank"

	def setUp(self):
		# Enable Plaid for the api guard checks; bypass validate()'s network call.
		frappe.db.set_single_value("Plaid Settings", "enabled", 1)

	# ------------------------------------------------------------------ #
	# Helpers
	# ------------------------------------------------------------------ #

	def _make_plaid_item(self, item_id="item-test", status="Active", access_token="access-tok"):
		if frappe.db.exists("Plaid Item", item_id):
			frappe.delete_doc("Plaid Item", item_id, force=True)
		bank = api.get_or_create_bank(self.institution, item_id)
		doc = frappe.get_doc({
			"doctype": "Plaid Item",
			"item_id": item_id,
			"access_token": access_token,
			"bank": bank,
			"company": self.company,
			"status": status,
		}).insert(ignore_permissions=True)
		return doc

	def _make_bank_account(self, plaid_item, account_id="acc-1"):
		account = make_account(account_id=account_id)
		api._sync_bank_account(account, plaid_item.bank, plaid_item.name, self.company, self.institution)
		return frappe.db.get_value("Bank Account", {"integration_id": account_id})

	# ------------------------------------------------------------------ #
	# _check_plaid_enabled
	# ------------------------------------------------------------------ #

	def test_check_enabled_true_when_enabled(self):
		self.assertTrue(api._check_plaid_enabled())

	def test_check_enabled_throws_when_disabled(self):
		frappe.db.set_single_value("Plaid Settings", "enabled", 0)
		with self.assertRaises(frappe.exceptions.ValidationError):
			api._check_plaid_enabled()

	def test_check_enabled_silent_when_disabled(self):
		frappe.db.set_single_value("Plaid Settings", "enabled", 0)
		self.assertFalse(api._check_plaid_enabled(silent=True))

	# ------------------------------------------------------------------ #
	# get_or_create_bank / get_or_create_plaid_item
	# ------------------------------------------------------------------ #

	def test_get_or_create_bank_creates_once(self):
		name = f"Bank Of Test {frappe.generate_hash(length=6)}"
		bank = api.get_or_create_bank(name, "item-xyz")
		self.assertTrue(frappe.db.exists("Bank", bank))
		# Second call with same institution returns same bank (no duplicate).
		bank2 = api.get_or_create_bank(name, "item-other")
		self.assertEqual(bank, bank2)

	def test_get_or_create_bank_reuses_existing_item_bank(self):
		item = self._make_plaid_item(item_id="item-reuse")
		# Same item_id should short-circuit to the item's existing bank.
		bank = api.get_or_create_bank("Some Other Display Name", "item-reuse")
		self.assertEqual(bank, item.bank)

	def test_get_or_create_plaid_item_insert_then_update(self):
		token_data = {"item_id": "item-upsert", "access_token": "tok-1"}
		bank = api.get_or_create_bank(self.institution, "item-upsert")
		name = api.get_or_create_plaid_item(token_data, bank, self.company)
		self.assertEqual(name, "item-upsert")
		self.assertEqual(frappe.db.get_value("Plaid Item", name, "status"), "Active")

		# Re-linking the same item updates token + status, no duplicate.
		token_data2 = {"item_id": "item-upsert", "access_token": "tok-2"}
		name2 = api.get_or_create_plaid_item(token_data2, bank, self.company)
		self.assertEqual(name2, "item-upsert")
		self.assertEqual(
			frappe.get_doc("Plaid Item", name2).get_password("access_token"), "tok-2"
		)

	# ------------------------------------------------------------------ #
	# _sync_bank_account
	# ------------------------------------------------------------------ #

	def test_sync_bank_account_creates_new(self):
		item = self._make_plaid_item(item_id="item-ba")
		account = make_account(account_id="acc-new", name="Primary Checking", mask="1234")
		created = api._sync_bank_account(account, item.bank, item.name, self.company, self.institution)
		self.assertTrue(created)

		ba = frappe.db.get_value(
			"Bank Account", {"integration_id": "acc-new"}, ["name", "plaid_item", "bank_account_no"], as_dict=True
		)
		self.assertIsNotNone(ba)
		self.assertEqual(ba.plaid_item, item.name)
		self.assertEqual(ba.bank_account_no, "1234")

	def test_sync_bank_account_idempotent_relinks(self):
		item = self._make_plaid_item(item_id="item-ba2")
		account = make_account(account_id="acc-dup")
		self.assertTrue(api._sync_bank_account(account, item.bank, item.name, self.company, self.institution))

		# A second link of the same Plaid account must NOT create a duplicate,
		# but should re-point plaid_item (reconnect scenario).
		item2 = self._make_plaid_item(item_id="item-ba2-new")
		created = api._sync_bank_account(account, item2.bank, item2.name, self.company, self.institution)
		self.assertFalse(created)
		self.assertEqual(
			frappe.db.count("Bank Account", {"integration_id": "acc-dup"}), 1
		)
		self.assertEqual(
			frappe.db.get_value("Bank Account", {"integration_id": "acc-dup"}, "plaid_item"),
			item2.name,
		)

	# ------------------------------------------------------------------ #
	# add_bank_accounts (full link flow, Plaid mocked)
	# ------------------------------------------------------------------ #

	def test_add_bank_accounts_links_inline(self):
		with patch(f"{API}.PlaidConnector") as MockPC:
			inst = MockPC.return_value
			inst.exchange_public_token.return_value = {"access_token": "tok", "item_id": "item-link"}
			inst.get_accounts.return_value = {
				"accounts": [
					make_account(account_id="link-a", name="Checking A"),
					make_account(account_id="link-b", name="Savings B", mask="2222"),
				]
			}
			result = api.add_bank_accounts("public-token", self.institution, self.company)

		self.assertFalse(result["queued"])
		self.assertEqual(result["total"], 2)
		self.assertEqual(len(result["added"]), 2)
		self.assertTrue(frappe.db.exists("Plaid Item", "item-link"))
		self.assertEqual(frappe.db.count("Bank Account", {"plaid_item": "item-link"}), 2)

	def test_add_bank_accounts_idempotent_second_call(self):
		def _link():
			with patch(f"{API}.PlaidConnector") as MockPC:
				inst = MockPC.return_value
				inst.exchange_public_token.return_value = {"access_token": "tok", "item_id": "item-idem"}
				inst.get_accounts.return_value = {"accounts": [make_account(account_id="idem-a")]}
				return api.add_bank_accounts("public-token", self.institution, self.company)

		first = _link()
		second = _link()
		self.assertEqual(len(first["added"]), 1)
		self.assertEqual(len(second["added"]), 0)  # already linked
		self.assertEqual(frappe.db.count("Bank Account", {"integration_id": "idem-a"}), 1)

	def test_add_bank_accounts_queues_when_many_accounts(self):
		many = [make_account(account_id=f"bulk-{i}", name=f"Acct {i}") for i in range(11)]
		with patch(f"{API}.PlaidConnector") as MockPC, patch(f"{API}.frappe.enqueue") as mock_enqueue:
			inst = MockPC.return_value
			inst.exchange_public_token.return_value = {"access_token": "tok", "item_id": "item-bulk"}
			inst.get_accounts.return_value = {"accounts": many}
			result = api.add_bank_accounts("public-token", self.institution, self.company)

		self.assertTrue(result["queued"])
		self.assertEqual(result["total"], 11)
		mock_enqueue.assert_called_once()

	# ------------------------------------------------------------------ #
	# Bank Transaction create / update / cancel
	# ------------------------------------------------------------------ #

	def _account_map(self, plaid_item):
		ba = frappe.get_all(
			"Bank Account",
			filters={"plaid_item": plaid_item},
			fields=["name", "integration_id", "company"],
		)
		return {row.integration_id: row for row in ba}

	def test_create_bank_transaction_withdrawal(self):
		item = self._make_plaid_item(item_id="item-bt1")
		self._make_bank_account(item, account_id="acc-bt1")
		account_map = self._account_map(item.name)

		api._create_bank_transaction(make_transaction("txn-w", account_id="acc-bt1", amount=42.0), account_map)

		bt = frappe.db.get_value(
			"Bank Transaction", {"transaction_id": "txn-w"},
			["deposit", "withdrawal", "docstatus"], as_dict=True,
		)
		self.assertEqual(bt.withdrawal, 42.0)
		self.assertEqual(bt.deposit, 0)
		self.assertEqual(bt.docstatus, 1)  # submitted

	def test_create_bank_transaction_deposit(self):
		item = self._make_plaid_item(item_id="item-bt2")
		self._make_bank_account(item, account_id="acc-bt2")
		account_map = self._account_map(item.name)

		# Negative Plaid amount == money in == deposit.
		api._create_bank_transaction(make_transaction("txn-d", account_id="acc-bt2", amount=-30.0), account_map)

		bt = frappe.db.get_value(
			"Bank Transaction", {"transaction_id": "txn-d"}, ["deposit", "withdrawal"], as_dict=True
		)
		self.assertEqual(bt.deposit, 30.0)
		self.assertEqual(bt.withdrawal, 0)

	def test_create_bank_transaction_skips_duplicate(self):
		item = self._make_plaid_item(item_id="item-bt3")
		self._make_bank_account(item, account_id="acc-bt3")
		account_map = self._account_map(item.name)

		txn = make_transaction("txn-dup", account_id="acc-bt3", amount=10.0)
		api._create_bank_transaction(txn, account_map)
		api._create_bank_transaction(txn, account_map)  # second call no-ops
		self.assertEqual(frappe.db.count("Bank Transaction", {"transaction_id": "txn-dup"}), 1)

	def test_create_bank_transaction_unknown_account_ignored(self):
		api._create_bank_transaction(make_transaction("txn-orphan", account_id="nope"), {})
		self.assertFalse(frappe.db.exists("Bank Transaction", {"transaction_id": "txn-orphan"}))

	def test_update_unreconciled_transaction_in_place(self):
		item = self._make_plaid_item(item_id="item-up")
		self._make_bank_account(item, account_id="acc-up")
		account_map = self._account_map(item.name)
		api._create_bank_transaction(make_transaction("txn-up", account_id="acc-up", amount=10.0), account_map)

		modified = make_transaction("txn-up", account_id="acc-up", amount=99.0, name="Updated Desc")
		res = api._update_bank_transaction(modified, account_map)

		self.assertEqual(res["action"], "updated")
		bt = frappe.db.get_value(
			"Bank Transaction", {"transaction_id": "txn-up"}, ["withdrawal", "description"], as_dict=True
		)
		self.assertEqual(bt.withdrawal, 99.0)
		self.assertEqual(bt.description, "Updated Desc")

	def test_update_reconciled_transaction_is_flagged_not_changed(self):
		item = self._make_plaid_item(item_id="item-rec")
		self._make_bank_account(item, account_id="acc-rec")
		account_map = self._account_map(item.name)
		api._create_bank_transaction(make_transaction("txn-rec", account_id="acc-rec", amount=10.0), account_map)

		bt_name = frappe.db.get_value("Bank Transaction", {"transaction_id": "txn-rec"})
		# Simulate reconciliation (a voucher matched against it).
		frappe.db.set_value("Bank Transaction", bt_name, "allocated_amount", 10.0)

		modified = make_transaction("txn-rec", account_id="acc-rec", amount=500.0, name="Tampered")
		res = api._update_bank_transaction(modified, account_map)

		self.assertEqual(res["action"], "flagged")
		# Original financial values must be untouched.
		bt = frappe.db.get_value(
			"Bank Transaction", {"transaction_id": "txn-rec"}, ["withdrawal", "description"], as_dict=True
		)
		self.assertEqual(bt.withdrawal, 10.0)
		self.assertNotEqual(bt.description, "Tampered")

	def test_update_missing_transaction_creates_it(self):
		item = self._make_plaid_item(item_id="item-upc")
		self._make_bank_account(item, account_id="acc-upc")
		account_map = self._account_map(item.name)

		res = api._update_bank_transaction(make_transaction("txn-new", account_id="acc-upc", amount=5.0), account_map)
		self.assertEqual(res["action"], "created")
		self.assertTrue(frappe.db.exists("Bank Transaction", {"transaction_id": "txn-new"}))

	def test_cancel_unreconciled_transaction(self):
		item = self._make_plaid_item(item_id="item-can")
		self._make_bank_account(item, account_id="acc-can")
		account_map = self._account_map(item.name)
		api._create_bank_transaction(make_transaction("txn-can", account_id="acc-can", amount=10.0), account_map)

		res = api._cancel_bank_transaction("txn-can")
		self.assertEqual(res["action"], "cancelled")
		self.assertEqual(frappe.db.get_value("Bank Transaction", {"transaction_id": "txn-can"}, "docstatus"), 2)

	def test_cancel_reconciled_transaction_is_flagged(self):
		item = self._make_plaid_item(item_id="item-canr")
		self._make_bank_account(item, account_id="acc-canr")
		account_map = self._account_map(item.name)
		api._create_bank_transaction(make_transaction("txn-canr", account_id="acc-canr", amount=10.0), account_map)
		bt_name = frappe.db.get_value("Bank Transaction", {"transaction_id": "txn-canr"})
		frappe.db.set_value("Bank Transaction", bt_name, "allocated_amount", 10.0)

		res = api._cancel_bank_transaction("txn-canr")
		self.assertEqual(res["action"], "flagged")
		# Still submitted, not cancelled.
		self.assertEqual(frappe.db.get_value("Bank Transaction", bt_name, "docstatus"), 1)

	def test_cancel_unknown_transaction_noop(self):
		res = api._cancel_bank_transaction("does-not-exist")
		self.assertIsNone(res["action"])

	# ------------------------------------------------------------------ #
	# _sync_item_transactions (the full per-item pipeline)
	# ------------------------------------------------------------------ #

	def test_sync_item_transactions_end_to_end(self):
		item = self._make_plaid_item(item_id="item-sync")
		self._make_bank_account(item, account_id="acc-sync")

		sync_result = {
			"added": [
				make_transaction("s-add-1", account_id="acc-sync", amount=10.0),
				make_transaction("s-add-2", account_id="acc-sync", amount=-20.0),
			],
			"modified": [],
			"removed": [],
			"next_cursor": "cursor-final",
		}

		with patch(f"{API}.PlaidConnector") as MockPC, patch(f"{API}.enqueue_create_notification"):
			MockPC.return_value.sync_transactions.return_value = sync_result
			api._sync_item_transactions("item-sync")

		self.assertTrue(frappe.db.exists("Bank Transaction", {"transaction_id": "s-add-1"}))
		self.assertTrue(frappe.db.exists("Bank Transaction", {"transaction_id": "s-add-2"}))
		self.assertEqual(frappe.db.get_value("Plaid Item", "item-sync", "plaid_sync_cursor"), "cursor-final")
		self.assertIsNotNone(frappe.db.get_value("Plaid Item", "item-sync", "last_sync_date"))

	def test_sync_item_transactions_skips_disconnected(self):
		item = self._make_plaid_item(item_id="item-disc", status="Disconnected")
		with patch(f"{API}.PlaidConnector") as MockPC:
			api._sync_item_transactions(item.name)
			MockPC.return_value.sync_transactions.assert_not_called()

	# ------------------------------------------------------------------ #
	# sync_all_transactions (scheduler / Sync Now)
	# ------------------------------------------------------------------ #

	def test_sync_all_transactions_queues_active_items_only(self):
		self._make_plaid_item(item_id="item-act-1", status="Active")
		self._make_plaid_item(item_id="item-act-2", status="Active")
		self._make_plaid_item(item_id="item-disc-x", status="Disconnected")

		with patch(f"{API}.frappe.enqueue") as mock_enqueue:
			result = api.sync_all_transactions()

		# Only the two Active items are queued (other tests' items may add more,
		# so assert on our minimum and that disconnected ones are excluded).
		self.assertGreaterEqual(result["queued"], 2)
		queued_items = {c.kwargs.get("plaid_item") for c in mock_enqueue.call_args_list}
		self.assertIn("item-act-1", queued_items)
		self.assertIn("item-act-2", queued_items)
		self.assertNotIn("item-disc-x", queued_items)

	# ------------------------------------------------------------------ #
	# Webhook routing
	# ------------------------------------------------------------------ #

	def _fire_webhook(self, payload):
		request = MagicMock()
		request.get_json.return_value = payload
		with patch.object(frappe.local, "request", request), patch(f"{API}.frappe.enqueue") as mock_enqueue, \
			patch(f"{API}.enqueue_create_notification") as mock_notify:
			api.plaid_webhook()
		return mock_enqueue, mock_notify

	def test_webhook_login_required_marks_reauth(self):
		self._make_plaid_item(item_id="item-wh1", status="Active")
		self._fire_webhook({
			"webhook_type": "ITEM",
			"webhook_code": "ITEM_LOGIN_REQUIRED",
			"item_id": "item-wh1",
		})
		self.assertEqual(frappe.db.get_value("Plaid Item", "item-wh1", "status"), "Needs Re-auth")

	def test_webhook_sync_updates_enqueues_sync(self):
		self._make_plaid_item(item_id="item-wh2", status="Active")
		mock_enqueue, _ = self._fire_webhook({
			"webhook_type": "TRANSACTIONS",
			"webhook_code": "SYNC_UPDATES_AVAILABLE",
			"item_id": "item-wh2",
		})
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.kwargs.get("plaid_item"), "item-wh2")

	def test_webhook_unknown_item_ignored(self):
		mock_enqueue, mock_notify = self._fire_webhook({
			"webhook_type": "ITEM",
			"webhook_code": "ITEM_LOGIN_REQUIRED",
			"item_id": "item-does-not-exist",
		})
		mock_enqueue.assert_not_called()
		mock_notify.assert_not_called()

	def test_webhook_disconnected_item_ignored(self):
		self._make_plaid_item(item_id="item-wh-disc", status="Disconnected")
		mock_enqueue, mock_notify = self._fire_webhook({
			"webhook_type": "TRANSACTIONS",
			"webhook_code": "SYNC_UPDATES_AVAILABLE",
			"item_id": "item-wh-disc",
		})
		mock_enqueue.assert_not_called()

	def test_webhook_missing_item_id_ignored(self):
		mock_enqueue, _ = self._fire_webhook({"webhook_type": "ITEM", "webhook_code": "ITEM_LOGIN_REQUIRED"})
		mock_enqueue.assert_not_called()

	def test_webhook_returns_early_when_disabled(self):
		frappe.db.set_single_value("Plaid Settings", "enabled", 0)
		self._make_plaid_item(item_id="item-wh-off", status="Active")
		self._fire_webhook({
			"webhook_type": "ITEM",
			"webhook_code": "ITEM_LOGIN_REQUIRED",
			"item_id": "item-wh-off",
		})
		# Status must remain unchanged because the integration is disabled.
		self.assertEqual(frappe.db.get_value("Plaid Item", "item-wh-off", "status"), "Active")

	# ------------------------------------------------------------------ #
	# disconnect_bank / complete_reauth
	# ------------------------------------------------------------------ #

	def test_disconnect_bank_revokes_and_cleans_up(self):
		item = self._make_plaid_item(item_id="item-dc", status="Active")
		frappe.db.set_value("Plaid Item", item.name, "plaid_sync_cursor", "some-cursor")

		with patch(f"{API}.PlaidConnector") as MockPC:
			result = api.disconnect_bank(item.name)
			MockPC.return_value.remove_item.assert_called_once()

		self.assertTrue(result["disconnected"])
		fresh = frappe.db.get_value(
			"Plaid Item", item.name, ["status", "plaid_sync_cursor"], as_dict=True
		)
		self.assertEqual(fresh.status, "Disconnected")
		self.assertIsNone(fresh.plaid_sync_cursor)
		# Access token removed.
		self.assertFalse(frappe.get_doc("Plaid Item", item.name).get_password("access_token", raise_exception=False))

	def test_disconnect_bank_tolerates_plaid_failure(self):
		item = self._make_plaid_item(item_id="item-dc2", status="Active")
		with patch(f"{API}.PlaidConnector") as MockPC:
			MockPC.return_value.remove_item.side_effect = Exception("ITEM_NOT_FOUND")
			result = api.disconnect_bank(item.name)
		# Local cleanup still completes despite the Plaid error.
		self.assertTrue(result["disconnected"])
		self.assertEqual(frappe.db.get_value("Plaid Item", item.name, "status"), "Disconnected")

	def test_complete_reauth_sets_active(self):
		item = self._make_plaid_item(item_id="item-ra", status="Needs Re-auth")
		api.complete_reauth(item.name)
		self.assertEqual(frappe.db.get_value("Plaid Item", item.name, "status"), "Active")

	# ------------------------------------------------------------------ #
	# Notification recipient resolution
	# ------------------------------------------------------------------ #

	def test_notification_users_fall_back_to_system_manager(self):
		# With no notification roles configured, System Managers are notified.
		settings = frappe.get_single("Plaid Settings")
		settings.notification_roles = []
		users = api._get_notification_users()
		self.assertIsInstance(users, list)
		# Administrator is a System Manager on every site.
		self.assertIn("Administrator", users)
