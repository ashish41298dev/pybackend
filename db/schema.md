# TradeGain Capital — MongoDB Schema

> Auto-generated from `backend/server.py`, `backend/earnings.py`, `backend/push.py`.
> All datetimes are **ISO-8601 UTC strings** unless otherwise noted.
> Money values are stored as **floats** (USDT, 6 dp precision for earnings, 2 dp for payouts).

Database name (env): **`DB_NAME`** — usually `tradegain` or `tradegain_prod`.

---

## Collections at a glance

| # | Collection | Purpose | Owner |
|---|---|---|---|
| 1 | `users` | Investors + admins | auth/account |
| 2 | `user_sessions` | Server-side Google OAuth sessions | auth |
| 3 | `plans` | Investment plans (Starter → Sovereign) | catalog |
| 4 | `investments` | Each deposit / position | investor |
| 5 | `earnings` | Daily ROI + level + referral ledger | earnings engine |
| 6 | `weekly_payouts` | Saturday sweep — settled withdrawals | payout engine |
| 7 | `leads` | Marketing inquiries from Allocator form | public |
| 8 | `deposit_networks` | TRC20 / BEP20 wallet addresses | admin |
| 9 | `audit_log` | Tamper-evident system event log | system |
| 10 | `push_subscriptions` | Web-Push browser endpoints (RFC 8030) | notifications |
| 11 | `push_campaigns` | Admin-authored push campaigns | notifications |
| 12 | `app_config` | Singleton settings — VAPID keypair, etc. | system |

---

## 1. `users`

| Field | Type | Notes |
|---|---|---|
| `user_id` | string (PK) | `user_<12-hex>` |
| `email` | string (unique) | lowercased |
| `name` | string | display name |
| `password_hash` | string \| null | bcrypt; null for Google-only users |
| `role` | enum | `"investor"` \| `"admin"` |
| `auth_provider` | enum | `"password"` \| `"google"` \| `"both"` |
| `wallet_address` | string \| null | USDT payout address |
| `phone` | string \| null | E.164 |
| `country` | string \| null | ISO-2 |
| `referral_code` | string \| null | `TGXXXXXX`, unique when non-null |
| `referred_by` | string \| null | upline `user_id` |
| `picture` | string \| null | Google avatar URL |
| `display_id` | string \| null | UUIDv7, public-safe ID |
| `is_active` | bool | default `true` |
| `force_password_change` | bool | first-login enforcement |
| `failed_login_attempts` | int | brute-force counter |
| `lockout_until` | iso-string \| null | 1-hour lockout after 3 fails |
| `cap_push_sent` | bool | 2× ROI cap notification flag |
| `created_at` | iso-string | UTC |

**Indexes**

```
{ email: 1 } UNIQUE
{ user_id: 1 } UNIQUE
{ referral_code: 1 } UNIQUE  partial: { referral_code: { $type: "string" } }
{ referred_by: 1 }
```

---

## 2. `user_sessions`  (Google OAuth)

| Field | Type | Notes |
|---|---|---|
| `user_id` | string | FK → users.user_id |
| `session_token` | string (unique) | provided by Emergent OAuth |
| `expires_at` | datetime | UTC |
| `created_at` | datetime | UTC |

**Index:** `{ session_token: 1 } UNIQUE`

---

## 3. `plans`

| Field | Type | Notes |
|---|---|---|
| `plan_id` | string (PK) | `plan_<10-hex>` |
| `name` | string | "Starter", "Bronze", … |
| `min_amount` | float | 100 ≤ x ≤ 100000 |
| `max_amount` | float | 100 ≤ x ≤ 100000 |
| `daily_roi` | float | e.g. `0.005` = 0.5 %/day |
| `working_days` | int | total payout days (e.g. 400) |
| `badge` | string \| null | "Most Popular", "Institutional" |
| `blurb` | string \| null | marketing copy |
| `is_active` | bool | |
| `sort_order` | int | display order |
| `created_at` | iso-string | |

**Index:** `{ plan_id: 1 } UNIQUE`

**Seed (6 default plans)** — see `seed_data.json`.

---

## 4. `investments`

| Field | Type | Notes |
|---|---|---|
| `investment_id` | string (PK) | `inv_<10-hex>` |
| `user_id` | string | FK → users |
| `plan_id` | string | FK → plans |
| `plan_name` | string | denormalised |
| `amount` | float | USDT, 100..100000 |
| `daily_roi` | float | copied from plan at creation |
| `working_days` | int | copied from plan |
| `network` | enum | `"TGcoin"` |
| `tx_hash` | string | min 6 chars |
| `screenshot` | data-url \| null | base64 PNG/JPG/WEBP, ≤ 6 MB |
| `status` | enum | `"pending"` \| `"active"` \| `"rejected"` \| `"completed"` |
| `admin_note` | string \| null | rejection reason / note |
| `created_at` | iso-string | |
| `activated_at` | iso-string \| null | set when admin approves |

**Indexes**

```
{ investment_id: 1 } UNIQUE
{ user_id: 1, created_at: -1 }
```

---

## 5. `earnings`

Append-only ledger written by the daily payout engine.

| Field | Type | Notes |
|---|---|---|
| `earning_id` | string (PK) | `earn_<12-hex>` |
| `user_id` | string | FK → users |
| `type` | enum | `"roi"` \| `"level"` \| `"referral"` |
| `amount` | float (6 dp) | USDT |
| `period_date` | string `YYYY-MM-DD` | accrual day |
| `source_user_id` | string \| null | downline that triggered the income |
| `source_investment_id` | string \| null | which deposit generated this |
| `source_level` | int | 0 (self ROI) / 1..10 (downline level) |
| `capped` | bool | true if 2× cap clipped the amount |
| `status` | enum | `"locked"` → `"paid"` |
| `payout_id` | string \| null | set when swept by Saturday job |
| `meta` | object | free-form |
| `created_at` | iso-string | |
| `paid_at` | iso-string \| null | |

**Indexes**

```
{ earning_id: 1 } UNIQUE
{ user_id: 1, period_date: -1 }
{ user_id: 1, type: 1, period_date: 1 }
{ status: 1, period_date: 1 }
```

---

## 6. `weekly_payouts`

| Field | Type | Notes |
|---|---|---|
| `payout_id` | string (PK) | `po_<12-hex>` |
| `user_id` | string | FK → users |
| `week_start_date` | `YYYY-MM-DD` | Monday |
| `week_end_date` | `YYYY-MM-DD` | Saturday |
| `total` | float (2 dp) | sum of breakdown |
| `breakdown` | object | `{ roi, level, matching, referral }` |
| `earning_count` | int | rows folded into this payout |
| `status` | enum | `"paid"` |
| `created_at` | iso-string | |
| `paid_at` | iso-string | |

**Indexes**

```
{ payout_id: 1 } UNIQUE
{ user_id: 1, week_end_date: -1 }
```

---

## 7. `leads`

| Field | Type | Notes |
|---|---|---|
| `lead_id` | string (PK) | `lead_<10-hex>` |
| `email` | string | |
| `ticket_size` | enum | `"1k+ USDT"` \| `"10k+ USDT"` \| `"50k+ USDT"` \| `"Institutional"` |
| `note` | string \| null | |
| `created_at` | iso-string | |

**Index:** `{ lead_id: 1 } UNIQUE`

---

## 8. `deposit_networks`

| Field | Type | Notes |
|---|---|---|
| `network_id` | string (PK) | `"tgcoin"` |
| `label` | string | "USDT · TGcoin" |
| `address` | string | on-chain receiving address |
| `is_active` | bool | at least one must stay true |
| `sort_order` | int | |
| `created_at` | iso-string | |

---

## 9. `audit_log`

| Field | Type | Notes |
|---|---|---|
| `audit_id` | string (PK) | `aud_<12-hex>` |
| `ts` | iso-string | UTC |
| `actor` | string \| null | acting user_id / "system" |
| `action` | string | e.g. `auth.login`, `investment.active` |
| `target_user_id` | string \| null | |
| `amount` | float | |
| `meta` | object | free-form |

**Indexes**

```
{ ts: -1 }
{ target_user_id: 1, ts: -1 }
```

---

## 10. `push_subscriptions`

| Field | Type | Notes |
|---|---|---|
| `sub_id` | string | `sub_<14-hex>` |
| `user_id` | string | FK → users |
| `endpoint` | string (unique) | browser push endpoint |
| `p256dh` | string | b64url |
| `auth` | string | b64url |
| `user_agent` | string \| null | |
| `created_at` | iso-string | |

---

## 11. `push_campaigns`

| Field | Type | Notes |
|---|---|---|
| `campaign_id` | string (PK) | `camp_<12-hex>` |
| `title` | string ≤ 80 | |
| `body` | string ≤ 240 | |
| `icon` | string \| null | |
| `cta_url` | string | rewritten to tracking URL on send |
| `cta_url_real` | string | original CTA destination |
| `target_type` | enum | `"all"` \| `"user"` |
| `target_user_id` | string \| null | |
| `status` | enum | `"draft"` / `"scheduled"` / `"sent"` |
| `scheduled_at` | iso-string \| null | |
| `created_by` | string | admin user_id |
| `created_at` | iso-string | |
| `sent_count` / `failed_count` / `click_count` / `total_targets` | int | counters |

---

## 12. `app_config`

Singleton documents keyed by `_id`. Currently holds the VAPID keypair:

```json
{ "_id": "vapid", "public": "<b64url>", "private": "<b64url-raw-32B>", "created_at": "..." }
```

---

## Relationships

```
users 1 ─< investments         (user_id)
users 1 ─< earnings            (user_id)
users 1 ─< weekly_payouts      (user_id)
users 1 ─< push_subscriptions  (user_id)
users 1 ─< user_sessions       (user_id)
users 1 ─< users               (referred_by, MLM tree, max 10 levels)

plans 1 ─< investments         (plan_id)

investments 1 ─< earnings      (source_investment_id)
weekly_payouts 1 ─< earnings   (payout_id)
```

---

## How to provision

* **Auto** – just start the FastAPI backend; `on_startup` creates indexes and seeds admin + plans + deposit networks.
* **Manual** – run `mongosh "$MONGO_URL/$DB_NAME" db/init_mongo.js` (creates collections, indexes, validators, seed data — same as auto, but explicit and inspectable).
* **Python** – `python db/init_db.py` (uses `motor`, reads `backend/.env`).
