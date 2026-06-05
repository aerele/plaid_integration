# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import create_batch, now_datetime
from frappe.utils.user import get_users_with_role

from plaid_integration.plaid_integration.plaid_connector import PlaidConnector


def _check_plaid_enabled(silent: bool = False) -> bool:
	if not frappe.db.get_single_value("Plaid Settings", "enabled"):
		if silent:
			frappe.log_error(_("Plaid Integration is not enabled."), _("Plaid Integration Disabled"))
		else:
			frappe.throw(_("Plaid Integration is not enabled."))
		return False
	return True


@frappe.whitelist()
def get_link_token() -> dict:
	return PlaidConnector().get_link_token(user_name=frappe.session.user)


@frappe.whitelist()
def get_reauth_link_token(plaid_item: str) -> dict:
	access_token = frappe.get_doc("Plaid Item", plaid_item).get_password("access_token")
	return PlaidConnector().get_link_token(user_name=frappe.session.user, access_token=access_token)


@frappe.whitelist()
def complete_reauth(plaid_item: str):
	_check_plaid_enabled()
	frappe.db.set_value("Plaid Item", plaid_item, "status", "Active")


@frappe.whitelist()
def disconnect_bank(plaid_item: str) -> dict:
	"""Disconnect a bank: revoke the Item at Plaid and stop syncing it locally.

	Linked Bank Accounts and their transaction history are retained — only the
	live Plaid connection (access token and sync cursor) is removed. Reconnecting
	later is a fresh link from Plaid Settings, which creates a new Plaid Item.
	"""
	from frappe.utils.password import remove_encrypted_password

	_check_plaid_enabled()

	doc = frappe.get_doc("Plaid Item", plaid_item)
	access_token = doc.get_password("access_token")

	if access_token:
		try:
			PlaidConnector().remove_item(access_token)
		except Exception:
			# The Item may already be gone at Plaid (e.g. ITEM_NOT_FOUND). The
			# connector logs the details; continue with local cleanup so the
			# connection is not left dangling.
			pass

	frappe.db.set_value("Plaid Item", plaid_item, {
		"status": "Disconnected",
		"plaid_sync_cursor": None,
	})
	remove_encrypted_password("Plaid Item", plaid_item, "access_token")

	return {"disconnected": True}


@frappe.whitelist()
def add_bank_accounts(public_token: str, institution_name: str, company: str) -> dict:
	"""Exchange public_token, create Bank + Plaid Item if needed, then create Bank Account records."""
	connector = PlaidConnector()

	token_data = connector.exchange_public_token(public_token)
	accounts = connector.get_accounts(token_data["access_token"]).get("accounts", [])
	bank = get_or_create_bank(institution_name, token_data["item_id"])
	plaid_item = get_or_create_plaid_item(token_data, bank, company)

	if len(accounts) > 10:
		frappe.enqueue(
			method="plaid_integration.plaid_integration.api._sync_accounts_in_background",
			queue="long",
			enqueue_after_commit=True,
			accounts=accounts,
			bank=bank,
			plaid_item=plaid_item,
			company=company,
			institution_name=institution_name,
		)
		return {"queued": True, "total": len(accounts)}

	added = [
		account.get("name")
		for account in accounts
		if _sync_bank_account(account, bank, plaid_item, company, institution_name)
	]

	return {"queued": False, "added": added, "total": len(accounts)}


def _sync_accounts_in_background(accounts, bank, plaid_item, company, institution_name):
	"""Background job — create Bank Account records for each Plaid account."""
	added_count = sum(
		1 for account in accounts
		if _sync_bank_account(account, bank, plaid_item, company, institution_name)
	)

	enqueue_create_notification(
		users=_get_notification_users(),
		doc={
			"type": "Alert",
			"document_type": "Plaid Item",
			"document_name": plaid_item,
			"subject": _("{0} bank account(s) linked successfully from {1}.").format(added_count, bank),
			"from_user": "Administrator",
		},
	)


def get_or_create_bank(institution_name, item_id):
	# If a Plaid Item already exists for this item_id, its bank is already created
	existing_bank = frappe.db.get_value("Plaid Item", item_id, "bank")
	if existing_bank:
		return existing_bank

	if not frappe.db.exists("Bank", institution_name):
		frappe.get_doc({
			"doctype": "Bank",
			"bank_name": institution_name,
		}).insert(ignore_permissions=True)

	return institution_name


def get_or_create_plaid_item(token_data, bank, company):
	item_id = token_data["item_id"]
	access_token = token_data["access_token"]

	if frappe.db.exists("Plaid Item", item_id):
		frappe.db.set_value("Plaid Item", item_id, {
			"access_token": access_token,
			"status": "Active",
		})
		return item_id

	frappe.get_doc({
		"doctype": "Plaid Item",
		"item_id": item_id,
		"access_token": access_token,
		"bank": bank,
		"company": company,
		"status": "Active",
	}).insert(ignore_permissions=True)

	return item_id


@frappe.whitelist(allow_guest=True)
def plaid_webhook() :
	"""Receive and handle incoming webhook events from Plaid."""
	if not _check_plaid_enabled(silent=True):
		return

	payload = frappe.request.get_json(silent=True) or {}
	webhook_type = payload.get("webhook_type")
	webhook_code = payload.get("webhook_code")
	item_id = payload.get("item_id")

	if not item_id:
		return

	item = frappe.db.get_value("Plaid Item", item_id, ["bank", "status"], as_dict=True)

	# Ignore webhooks for unknown or already disconnected items.
	if not item or item.status == "Disconnected":
		return

	if webhook_type == "ITEM" and webhook_code == "ITEM_LOGIN_REQUIRED":
		frappe.db.set_value("Plaid Item", item_id, "status", "Needs Re-auth")
		_notify_reauth_required(item_id, item.bank)

	elif webhook_type == "TRANSACTIONS" and webhook_code == "SYNC_UPDATES_AVAILABLE":
		frappe.enqueue(
			method="plaid_integration.plaid_integration.api._sync_item_transactions",
			queue="long",
			enqueue_after_commit=True,
			plaid_item=item_id,
		)


def _get_notification_users() -> list[str]:
	settings = frappe.get_single("Plaid Settings")
	roles = [row.role for row in settings.notification_roles]

	users = []
	for role in roles:
		users.extend(get_users_with_role(role))

	if not users:
		users = get_users_with_role("System Manager")

	return list(set(users))


def _notify_reauth_required(item_id, bank):
	enqueue_create_notification(
		users=_get_notification_users(),
		doc={
			"type": "Alert",
			"document_type": "Plaid Item",
			"document_name": item_id,
			"subject": _("Bank Re-authentication Required: {0}. Please re-connect the bank account.").format(bank),
			"from_user": "Administrator",
		},
	)


def _get_or_create_gl_account(account_name, institution_name, company, account_currency=None):
	parent_gl_accounts = frappe.db.get_all(
		"Account",
		{"company": company, "account_type": "Bank", "is_group": 1, "disabled": 0},
	)

	if not parent_gl_accounts:
		frappe.throw(
			_("Please setup and enable a group account with the Account Type - {0} for the company {1}").format(
				frappe.bold(_("Bank")), company
			)
		)

	gl_account = frappe.get_doc({
		"doctype": "Account",
		"account_name": f"{account_name} - {institution_name}",
		"parent_account": parent_gl_accounts[0].name,
		"account_type": "Bank",
		"company": company,
		# Match the bank's own currency (e.g. USD) so transactions in that
		# currency are accepted even when the company currency differs.
		"account_currency": account_currency,
	})
	gl_account.insert(ignore_if_duplicate=True)

	return gl_account.name


def _sync_bank_account(account, bank, plaid_item, company, institution_name):
	"""Create or update a Bank Account. Returns True if newly created."""
	account_id = account.get("account_id")

	existing = frappe.db.get_value("Bank Account", {"integration_id": account_id})

	if existing:
		frappe.db.set_value("Bank Account", existing, "plaid_item", plaid_item)
		return False

	account_currency = (account.get("balances") or {}).get("iso_currency_code")
	gl_account = _get_or_create_gl_account(account.get("name"), institution_name, company, account_currency)
	company_abbr = frappe.get_cached_value("Company", company, "abbr")
	account_name = " - ".join([account.get("name"), company_abbr])

	frappe.get_doc({
		"doctype": "Bank Account",
		"account_name": account_name,
		"bank": bank,
		"is_company_account": 1,
		"company": company,
		"account": gl_account,
		"bank_account_no": account.get("mask"),
		"integration_id": account_id,
		"plaid_item": plaid_item,
	}).insert(ignore_permissions=True)

	return True



@frappe.whitelist()
def sync_all_transactions() -> dict:
	"""Sync transactions for all active Plaid Items. Called by Sync Now button or scheduler."""
	_check_plaid_enabled()

	active_items = frappe.get_all("Plaid Item", {"status": "Active"}, pluck="name")

	for item_id in active_items:
		frappe.enqueue(
			method="plaid_integration.plaid_integration.api._sync_item_transactions",
			queue="long",
			enqueue_after_commit=True,
			plaid_item=item_id,
		)

	return {"queued": len(active_items)}


def _sync_item_transactions(plaid_item: str):
	"""Background job — sync all transactions for one Plaid Item."""
	doc = frappe.get_doc("Plaid Item", plaid_item)

	# A disconnected item has no valid access token — nothing to sync.
	if doc.status == "Disconnected":
		return

	access_token = doc.get_password("access_token")
	if not access_token:
		return

	cursor = doc.plaid_sync_cursor or None

	result = PlaidConnector().sync_transactions(access_token, cursor)

	added = result["added"]
	modified = result["modified"]
	removed = result["removed"]
	next_cursor = result["next_cursor"]

	# Build account_id → Bank Account map for this Item
	bank_accounts = frappe.get_all(
		"Bank Account",
		filters={"plaid_item": plaid_item},
		fields=["name", "integration_id", "company"],
	)
	account_map = {ba.integration_id: ba for ba in bank_accounts}

	# Process modified and removed directly — usually small.
	# Reconciled transactions are never auto-changed; they are left intact and
	# collected so the user can review them manually.
	modified_applied = 0
	flagged_modified, flagged_removed = [], []

	for txn in modified:
		res = _update_bank_transaction(txn, account_map)
		if res["action"] == "flagged":
			flagged_modified.append(res["name"])
		else:
			modified_applied += 1

	removed_applied = 0
	for txn in removed:
		res = _cancel_bank_transaction(txn.get("transaction_id"))
		if res["action"] == "flagged":
			flagged_removed.append(res["name"])
		elif res["action"] == "cancelled":
			removed_applied += 1

	# Process added in batches of 500 — can be large on first sync
	if len(added) > 500:
		for batch in create_batch(added, 500):
			frappe.enqueue(
				method="plaid_integration.plaid_integration.api._add_transaction_batch",
				queue="long",
				enqueue_after_commit=True,
				transactions=list(batch),
				account_map={k: v.as_dict() for k, v in account_map.items()},
			)
	else:
		for txn in added:
			_create_bank_transaction(txn, account_map)

	# Save cursor and last sync timestamp
	frappe.db.set_value("Plaid Item", plaid_item, {
		"plaid_sync_cursor": next_cursor,
		"last_sync_date": now_datetime(),
	})

	# Alert the user, per transaction, for each reconciled entry that needs review.
	for bt_name in flagged_modified:
		_notify_review_required(bt_name, "modified")
	for bt_name in flagged_removed:
		_notify_review_required(bt_name, "removed")

	subject = _("Transaction sync complete for {0}: {1} added, {2} updated, {3} removed.").format(
		doc.bank, len(added), modified_applied, removed_applied
	)
	flagged_count = len(flagged_modified) + len(flagged_removed)
	if flagged_count:
		subject += " " + _("{0} reconciled transaction(s) were left unchanged and need review.").format(
			flagged_count
		)

	enqueue_create_notification(
		users=_get_notification_users(),
		doc={
			"type": "Alert",
			"document_type": "Plaid Item",
			"document_name": plaid_item,
			"subject": subject,
			"from_user": "Administrator",
		},
	)


def _add_transaction_batch(transactions: list, account_map: dict):
	"""Background job — create Bank Transactions for one batch."""
	for txn in transactions:
		_create_bank_transaction(txn, account_map)


def _create_bank_transaction(txn: dict, account_map: dict):
	"""Create a submitted Bank Transaction from a Plaid transaction. Skips if already exists."""
	transaction_id = txn.get("transaction_id")

	if frappe.db.exists("Bank Transaction", {"transaction_id": transaction_id}):
		return

	bank_account = account_map.get(txn.get("account_id"))
	if not bank_account:
		return

	amount = txn.get("amount") or 0

	doc = frappe.get_doc({
		"doctype": "Bank Transaction",
		"bank_account": bank_account.get("name"),
		"company": bank_account.get("company"),
		"date": txn.get("date"),
		"deposit": abs(amount) if amount < 0 else 0,
		"withdrawal": amount if amount > 0 else 0,
		"description": txn.get("merchant_name") or txn.get("name"),
		"currency": txn.get("iso_currency_code"),
		"transaction_id": transaction_id,
	})
	doc.insert(ignore_permissions=True)
	doc.submit()


def _is_reconciled(bt: dict) -> bool:
	"""A Bank Transaction is protected once any amount is matched to a voucher."""
	return (bt.get("allocated_amount") or 0) > 0 or bt.get("status") == "Reconciled"


def _update_bank_transaction(txn: dict, account_map: dict) -> dict:
	"""Apply a modified Plaid transaction.

	Returns {"action": "created" | "updated" | "flagged", "name": <bt or None>}.
	A reconciled (or partially matched) Bank Transaction is never changed — it is
	left intact and flagged for manual review so reconciliation is not broken.
	"""
	transaction_id = txn.get("transaction_id")
	existing = frappe.db.get_value(
		"Bank Transaction",
		{"transaction_id": transaction_id},
		["name", "docstatus", "allocated_amount", "status"],
		as_dict=True,
	)

	if not existing:
		_create_bank_transaction(txn, account_map)
		return {"action": "created", "name": None}

	if _is_reconciled(existing):
		return {"action": "flagged", "name": existing.name}

	# Unreconciled — update the same record in place so the id stays stable.
	amount = txn.get("amount") or 0
	deposit = abs(amount) if amount < 0 else 0
	withdrawal = amount if amount > 0 else 0

	frappe.db.set_value(
		"Bank Transaction",
		existing.name,
		{
			"date": txn.get("date"),
			"deposit": deposit,
			"withdrawal": withdrawal,
			"description": txn.get("merchant_name") or txn.get("name"),
			"currency": txn.get("iso_currency_code"),
			"unallocated_amount": deposit or withdrawal,
		},
	)
	return {"action": "updated", "name": existing.name}


def _cancel_bank_transaction(transaction_id: str) -> dict:
	"""Cancel a Bank Transaction that Plaid removed.

	Returns {"action": "cancelled" | "flagged" | None, "name": <bt or None>}.
	A reconciled transaction is left intact and flagged for manual review.
	"""
	existing = frappe.db.get_value(
		"Bank Transaction",
		{"transaction_id": transaction_id},
		["name", "docstatus", "allocated_amount", "status"],
		as_dict=True,
	)

	if not existing:
		return {"action": None, "name": None}

	if _is_reconciled(existing):
		return {"action": "flagged", "name": existing.name}

	if existing.docstatus == 1:
		frappe.get_doc("Bank Transaction", existing.name).cancel()
	return {"action": "cancelled", "name": existing.name}


def _notify_review_required(bank_transaction: str, change: str):
	"""Alert that a reconciled Bank Transaction was changed/removed at the bank."""
	messages = {
		"modified": _(
			"Plaid modified an already-reconciled Bank Transaction {0}. "
			"It was left unchanged — please review the new bank details and re-reconcile if needed."
		),
		"removed": _(
			"Plaid removed an already-reconciled Bank Transaction {0}. "
			"It was left intact — please review whether the bank reversed this entry."
		),
	}
	enqueue_create_notification(
		users=_get_notification_users(),
		doc={
			"type": "Alert",
			"document_type": "Bank Transaction",
			"document_name": bank_transaction,
			"subject": messages[change].format(bank_transaction),
			"from_user": "Administrator",
		},
	)
