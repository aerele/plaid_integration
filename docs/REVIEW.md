# Plaid Integration — Functional & Technical Review

**Reviewed:** `plaid_integration` @ `c019c49`
**Targets:** Frappe/ERPNext `version-16`, `plaid-python ~=39.2.0`, Plaid `transactions/sync` flow
**Scope:** Full app (api layer, connector, doctypes, client JS, install/uninstall, tests)

This review pairs with:
- [`FUNCTIONAL_FLOW.md`](./FUNCTIONAL_FLOW.md) — the intended clean end-to-end flow.
- The rewritten test suite (`test_plaid_settings.py`, `test_plaid_item.py`, `plaid_test_utils.py`).

Severity legend: **🔴 High** (correctness/security/dead feature) · **🟠 Medium** (UX/robustness) · **🟡 Low** (polish/edge case).

---

## Executive summary

The integration is **well-structured and readable**. The cursor-based `transactions/sync` design is correct, the reconciliation-protection logic (never auto-mutating a matched Bank Transaction, flagging it for review instead) is genuinely good, and the disconnect/re-auth lifecycle is thoughtfully handled. The client JS cleanly shares one `PlaidLink` utility across linking and re-auth.

The main gaps are: **two settings that do nothing** (`transaction_history_days`, `automatic_sync`), **an unauthenticated public webhook**, **a custom field that is never populated**, and **zero real test coverage** (both test files are empty stubs). None of these are architectural — they are finishable items.

---

## 🔴 High severity

### H1 — `transaction_history_days` setting has no effect (dead feature)
`PlaidSettings.validate` enforces `90 ≤ transaction_history_days ≤ 730`, the field has a helpful description, and the JS defaults it to 90 — but **the value is never sent to Plaid**.

Plaid controls the initial history window via `transactions: { days_requested: N }` on the **`link_token_create`** request, not on `transactions/sync`. `PlaidConnector.get_link_token()` never sets it, so the user's choice is silently ignored and Plaid uses its own default.

- **Where:** `plaid_settings.py:validate`, `plaid_connector.py:get_link_token`
- **Fix:** In `get_link_token`, when products include `transactions`, pass
  `transactions=LinkTokenTransactions(days_requested=settings.transaction_history_days or 90)`.

### H2 — `automatic_sync` setting is never read (dead feature, contradicts scheduler)
The settings form exposes an **Automatic Sync** checkbox, but nothing reads it. `hooks.py` unconditionally registers a `daily` scheduler job, and `sync_all_transactions()` only checks `enabled`. So:
- Turning `automatic_sync` **off** does *not* stop the daily sync.
- The checkbox implies a choice that the system ignores.

- **Where:** `hooks.py:scheduler_events`, `api.py:sync_all_transactions`
- **Fix:** Gate the scheduled (not the manual) run on the flag, e.g. in `sync_all_transactions` add an `only_if_auto` path, or have the daily hook call a wrapper that returns early unless `automatic_sync` is set. The manual **Sync Now** button should still work regardless.

### H3 — Public webhook has no Plaid signature verification (security)
`plaid_webhook` is `@frappe.whitelist(allow_guest=True)` and trusts the JSON body completely. Plaid signs every webhook with a JWT in the **`Plaid-Verification`** header (verifiable via `/webhook_verification_key/get` with JWK + body SHA-256). Without verification, **any anonymous caller** can POST:
- `ITEM_LOGIN_REQUIRED` for a known `item_id` → flips an Item to *Needs Re-auth* and spams notifications (nuisance/DoS), or
- `SYNC_UPDATES_AVAILABLE` → enqueues `long`-queue sync jobs on demand (resource exhaustion).

- **Where:** `api.py:plaid_webhook`
- **Fix:** Verify the `Plaid-Verification` JWT signature before acting (cache the JWK by `kid`; reject if the body hash doesn't match or the timestamp is stale). Reject unverified requests with HTTP 401/400. This is the one item I'd block a production go-live on.

### H4 — No test coverage (both test files are empty `pass` stubs)
`test_plaid_item.py` and `test_plaid_settings.py` contain only docstrings and `pass`. For an integration that creates GL accounts, submits Bank Transactions, and mutates financial records, this is the highest-risk gap. **Addressed by this PR** — see the new suites below.

---

## 🟠 Medium severity

### M1 — `last_integration_date` ("Last Sync Date") custom field is never populated
`custom_fields.py` adds a `last_integration_date` field to **Bank Account**, but no code ever writes it. Per-Item `last_sync_date` (on Plaid Item) *is* updated; the Bank-Account-level one is dead.
- **Fix:** either populate it per account during sync, or drop the field.

### M2 — No account-type filtering when creating Bank Accounts
`add_bank_accounts` turns **every** account returned by `accounts/get` into an ERPNext **Bank Account** with a `Bank`-type GL account — including `credit`, `loan`, and `investment` accounts. A credit card booked as a Bank-type ledger is usually wrong for accounting.
- **Where:** `api.py:add_bank_accounts` / `_sync_bank_account`
- **Fix:** filter to `type == "depository"` (optionally `credit`) or make the allowed types configurable.

### M3 — `validate_credentials()` makes a live Plaid call on every settings save
`PlaidSettings.validate` calls `validate_credentials()` whenever `enabled` + creds are present. So a transient Plaid outage (or rate-limit) **blocks saving unrelated changes** like notification roles. It also adds network latency to every save.
- **Fix:** only validate when `plaid_client_id`/`plaid_secret`/`plaid_environment` actually changed (`self.has_value_changed(...)`), or move it behind an explicit **Test Connection** button.

### M4 — `development` environment is deprecated/retired by Plaid
`plaid_connector.py` maps `development` to a raw URL string `https://development.plaid.com`. Plaid **retired the Development environment** and `plaid-python` 39.x no longer ships `Environment.Development`. Selecting it points at a dead host.
- **Fix:** remove `development` from the Select options (keep `sandbox`/`production`), or map it to `production` with a deprecation warning.

### M5 — Limited webhook code coverage
Only `ITEM_LOGIN_REQUIRED` and `SYNC_UPDATES_AVAILABLE` are handled. Notably missing:
- `ITEM / PENDING_EXPIRATION` (consent/PSD2 expiry) → should pre-emptively mark *Needs Re-auth*.
- `ITEM / USER_PERMISSION_REVOKED` & `ITEM / ERROR` → should mark *Disconnected* / surface the error.
- **Fix:** extend the `webhook_type/webhook_code` routing with these cases.

---

## 🟡 Low severity

- **L1 — Sync notification overcounts "added".** The completion alert reports `len(added)` from Plaid, but `_create_bank_transaction` skips duplicates and batches large sets, so the *actual* number of new Bank Transactions can be lower. Report the real created count for accuracy. *(`api.py:_sync_item_transactions`)*
- **L2 — Bank keyed by institution display name.** `get_or_create_bank` uses `institution_name` as the `Bank` primary key; two institutions sharing a display name collide. Consider keying on Plaid `institution_id`. *(`api.py:get_or_create_bank`)*
- **L3 — Currency mismatch risk.** The GL account currency is fixed at first-account creation, but each Bank Transaction's `currency` comes from the txn's `iso_currency_code`. A later transaction in a different currency would carry a currency differing from the GL account and may fail on submit. Rare (multi-currency account), but worth a guard. *(`api.py:_create_bank_transaction`)*
- **L4 — Insert race on `transaction_id`.** `_create_bank_transaction` does `exists()`-then-`insert()` with no unique DB constraint, so concurrent batch jobs *could* double-insert. Per-Item sync is effectively serialized today, so low risk; a unique index on `Bank Transaction.transaction_id` would make it bulletproof.
- **L5 — `complete_reauth` trusts the client.** It flips status to *Active* on the client's say-so without a server-side confirmation. Plaid Link only fires `on_success` after a real success, so practically fine, but a crafted call could re-activate an Item. Low.
- **L6 — `requires-python = ">=3.14"`** is aggressive; confirm it matches the bench's actual Python (Frappe v16 commonly runs 3.11–3.13). Mismatch will block `pip install` of the app.
- **L7 — Magic number `10`** for the background-vs-inline account threshold and `500` batch size are undocumented constants; fine, but a module-level named constant would read better.

---

## What's done well (keep)

- **Reconciliation safety:** `_is_reconciled` + flag-don't-mutate on modified/removed transactions is the right call and protects financial integrity. Excellent.
- **Cursor persistence** (`plaid_sync_cursor`) with the `has_more` pagination loop is implemented correctly.
- **Disconnect lifecycle:** revokes at Plaid, removes the encrypted token + cursor, keeps history, and tolerates `ITEM_NOT_FOUND`. Reconnect re-links existing Bank Accounts by `integration_id`. Clean.
- **Notification routing** with role fallback to *System Manager* is sensible.
- **Shared `PlaidLink` JS** utility avoids duplication across link and re-auth flows.
- **Idempotency** on Bank Account (`integration_id`) and Bank Transaction (`transaction_id`) lookups.

---

## Suggested fix priority

1. **H3** (webhook signature verification) — security gate for production.
2. **H1 + H2** (wire up the two dead settings) — they actively mislead users.
3. **M2 / M3 / M4** — robustness & correctness.
4. **M1 / M5 / L-series** — polish.

I have **not** modified app source in this PR (it's a review). Say the word and I'll apply H1–H3 + M-series as a follow-up changeset with tests.
