# Plaid Integration — Functional Flow

End-to-end description of how the app links banks and keeps ERPNext **Bank Transactions** in sync with Plaid. This is the *intended clean flow*; deviations from current code are flagged as **⚠ Gap** and tracked in [`REVIEW.md`](./REVIEW.md).

---

## Actors & objects

| Object | Role |
|---|---|
| **Plaid Settings** (Single) | Credentials, environment, sync/notification config. |
| **Plaid Item** | One linked bank login (Plaid "Item"). Holds `access_token`, `plaid_sync_cursor`, `status`. Named by Plaid `item_id`. |
| **Bank** | ERPNext master, one per institution. |
| **Bank Account** | One per Plaid account. Linked to a `Plaid Item` + GL `Account`; keyed to Plaid by `integration_id`. |
| **Bank Transaction** | Submitted ledger entry created from each Plaid transaction. Keyed by `transaction_id`. |
| **PlaidConnector** | Thin wrapper over `plaid-python` (link token, token exchange, accounts, sync, item remove). |

**Plaid Item status machine:** `Active → Needs Re-auth → Active` (via re-auth) and `Active/Needs Re-auth → Disconnected` (terminal until a fresh link creates a *new* Item).

---

## 1. Setup (admin, once)

```
Plaid Settings: enable → enter Client ID + Secret → choose environment
   → validate() pings Plaid (institutions_get) to confirm credentials
   → set Automatic Sync, European Access, Transaction History Days, Notification Roles
```
- ⚠ Gap: `Transaction History Days` is validated but never sent to Plaid (**H1**).
- ⚠ Gap: credential validation runs on *every* save (**M3**).

## 2. Link a bank (admin)

```
Plaid Settings ▸ "Link Bank Account"
   → pick Company (dialog)
   → get_link_token()            [server]  → Plaid link_token
   → PlaidLink popup (Plaid.create)         [browser, Plaid-hosted]
   → user authenticates with their bank
   → onSuccess(public_token, metadata)
   → add_bank_accounts(public_token, institution_name, company)   [server]
```

`add_bank_accounts`:
1. `exchange_public_token` → `access_token`, `item_id`.
2. `get_accounts(access_token)` → list of accounts.
3. `get_or_create_bank(institution_name, item_id)`.
4. `get_or_create_plaid_item(token_data, bank, company)` → status **Active**.
5. For each account → `_sync_bank_account`:
   - dedupe by `integration_id`; if it exists, just re-point `plaid_item` (handles reconnect).
   - else create the `Bank`-type GL **Account** (currency matched to the bank account) and the **Bank Account**.
6. **> 10 accounts** → offloaded to a `long` background job, user notified on completion.

- ⚠ Gap: every account type becomes a Bank Account (no `depository` filter) (**M2**).

## 3. Transaction sync

Two entry points converge on `_sync_item_transactions(plaid_item)`:

```
A. Webhook   : Plaid → plaid_webhook (TRANSACTIONS/SYNC_UPDATES_AVAILABLE) → enqueue per Item
B. Manual    : "Sync Now" → sync_all_transactions() → enqueue every Active Item
C. Scheduled : daily hook → sync_all_transactions()
```
- ⚠ Gap: scheduled run ignores `Automatic Sync` (**H2**); webhook is unauthenticated (**H3**).

`_sync_item_transactions` (per Item):
```
skip if Disconnected / no token
cursor = plaid_item.plaid_sync_cursor
result = PlaidConnector.sync_transactions(access_token, cursor)   # loops while has_more
  → added[], modified[], removed[], next_cursor

added    → _create_bank_transaction   (insert + submit; skip if transaction_id exists)
            > 500 → batched into long-queue jobs
modified → _update_bank_transaction   (update in place IF unreconciled, else FLAG)
removed  → _cancel_bank_transaction   (cancel IF unreconciled, else FLAG)

persist next_cursor + last_sync_date
notify: "N added, M updated, R removed" (+ any flagged-for-review)
```

### Amount convention (Plaid → ERPNext)
Plaid: **positive = money out**, **negative = money in**.
```
deposit    = abs(amount) if amount < 0 else 0
withdrawal = amount      if amount > 0 else 0
```

### Reconciliation protection (the important rule)
A Bank Transaction is **protected** once `allocated_amount > 0` or `status == "Reconciled"`.
Protected transactions are **never auto-modified or auto-cancelled** — they're left intact and a
review notification is raised, so manual reconciliation is never silently broken. ✅

## 4. Re-authentication

```
Plaid (ITEM/ITEM_LOGIN_REQUIRED webhook) → status = Needs Re-auth → notify roles
Plaid Item ▸ "Re-connect Bank"
   → get_reauth_link_token(plaid_item)   (link token bound to existing access_token)
   → PlaidLink popup (update mode) → onSuccess
   → complete_reauth(plaid_item)         → status = Active
```
- ⚠ Gap: `PENDING_EXPIRATION` / `USER_PERMISSION_REVOKED` not handled (**M5**); `complete_reauth` trusts client (**L5**).

## 5. Disconnect

```
Plaid Item ▸ "Disconnect Bank" (confirm)
   → disconnect_bank(plaid_item)
       → PlaidConnector.remove_item(access_token)   (tolerates ITEM_NOT_FOUND)
       → status = Disconnected, plaid_sync_cursor = None
       → remove_encrypted_password(access_token)
```
Bank Accounts & transaction history are **retained**; no further sync occurs. Reconnecting later
is a fresh link that creates a **new** Plaid Item and re-points the existing Bank Accounts by
`integration_id`. ✅

---

## One-glance lifecycle

```
        ┌─────────────┐  link_token + public_token   ┌──────────────┐
        │ Plaid Settings├────────────────────────────►│  Plaid Item  │ status=Active
        └─────────────┘   add_bank_accounts           └──────┬───────┘
                                                              │ sync (webhook/manual/daily)
                                                              ▼
                                                     ┌──────────────────┐
                                                     │ Bank Transactions │ (submitted)
                                                     └──────────────────┘
   ITEM_LOGIN_REQUIRED ──► Needs Re-auth ──(Re-connect)──► Active
   Disconnect ──► Disconnected (history kept; new link ⇒ new Item)
```
