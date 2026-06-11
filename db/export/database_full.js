// =============================================================================
// TradeGain Capital — Full MongoDB Database Dump
// Generated: 2026-05-27T09:42:05.912487+00:00
// Source DB: tradegain
//
// This single file contains EVERYTHING needed to recreate the database:
//   • All collections
//   • All indexes (matches backend/server.py on_startup)
//   • All current documents from every collection
//
// Run:
//   mongosh "$MONGO_URL/$DB_NAME" db/database_full.js
//
// WARNING: this DROPS the existing collections before re-creating them.
// Comment out the `_drop_collections` block at the bottom if you want
// to merge instead of replace.
// =============================================================================

// Helper: parse {$date: "..."} → Date so dates round-trip correctly.
function _reviveDates(o) {
  if (o === null || typeof o !== 'object') return o;
  if (Array.isArray(o)) return o.map(_reviveDates);
  if (Object.keys(o).length === 1 && '$date' in o) return new Date(o['$date']);
  const out = {};
  for (const k of Object.keys(o)) out[k] = _reviveDates(o[k]);
  return out;
}

// ---- Drop existing collections (clean slate) ----------------------------
try { db['users'].drop(); } catch (e) {}
try { db['user_sessions'].drop(); } catch (e) {}
try { db['plans'].drop(); } catch (e) {}
try { db['investments'].drop(); } catch (e) {}
try { db['earnings'].drop(); } catch (e) {}
try { db['weekly_payouts'].drop(); } catch (e) {}
try { db['leads'].drop(); } catch (e) {}
try { db['deposit_networks'].drop(); } catch (e) {}
try { db['audit_log'].drop(); } catch (e) {}
try { db['push_subscriptions'].drop(); } catch (e) {}
try { db['push_campaigns'].drop(); } catch (e) {}
try { db['app_config'].drop(); } catch (e) {}

// ---- Insert all documents ----------------------------------------------
// users — 2 document(s)
db['users'].insertMany(_reviveDates([
  {
    "user_id": "user_e13fe583224d",
    "email": "admin@tradegain.app",
    "name": "Trade Gain Admin",
    "password_hash": "$2b$12$7i9nOKzVIkxekhhSfxG56OyalAt78/xS66WnBjiL7RPPuGzwR/Z06",
    "role": "admin",
    "auth_provider": "password",
    "wallet_address": null,
    "phone": null,
    "country": null,
    "referral_code": "TGHZXFJ9",
    "referred_by": null,
    "picture": null,
    "is_active": true,
    "force_password_change": false,
    "failed_login_attempts": 0,
    "lockout_until": null,
    "created_at": "2026-05-27T08:48:40.629454+00:00"
  },
  {
    "user_id": "user_d1798304e062",
    "email": "abc@abc.com",
    "name": "ABC",
    "password_hash": "$2b$12$OwKUaTgpRKJVRuOh4aX9Z.Q4zF3VFcuE3/peP/rr4Z69F8i.pzRL2",
    "role": "investor",
    "auth_provider": "password",
    "wallet_address": "abc",
    "phone": "+91 9999999999",
    "country": "IN",
    "referral_code": "TGMOUWI0",
    "referred_by": null,
    "picture": null,
    "created_at": "2026-05-27T09:37:57.490741+00:00"
  }
]));

// user_sessions — 0 document(s)
db.createCollection('user_sessions');

// plans — 6 document(s)
db['plans'].insertMany(_reviveDates([
  {
    "name": "Starter",
    "min_amount": 100,
    "max_amount": 499,
    "daily_roi": 0.005,
    "working_days": 400,
    "badge": null,
    "blurb": "Test the waters with a disciplined first position.",
    "sort_order": 1,
    "plan_id": "plan_213099282c",
    "is_active": true,
    "created_at": "2026-05-27T08:48:09.178452+00:00"
  },
  {
    "name": "Bronze",
    "min_amount": 500,
    "max_amount": 999,
    "daily_roi": 0.005,
    "working_days": 400,
    "badge": null,
    "blurb": "A measured step up — same formula, larger output.",
    "sort_order": 2,
    "plan_id": "plan_c377213869",
    "is_active": true,
    "created_at": "2026-05-27T08:48:09.178464+00:00"
  },
  {
    "name": "Silver",
    "min_amount": 1000,
    "max_amount": 4999,
    "daily_roi": 0.005,
    "working_days": 400,
    "badge": "Most Popular",
    "blurb": "The sweet spot most investors land on.",
    "sort_order": 3,
    "plan_id": "plan_71580e4c54",
    "is_active": true,
    "created_at": "2026-05-27T08:48:09.178474+00:00"
  },
  {
    "name": "Gold",
    "min_amount": 5000,
    "max_amount": 9999,
    "daily_roi": 0.005,
    "working_days": 400,
    "badge": null,
    "blurb": "Serious capital, serious weekly payouts.",
    "sort_order": 4,
    "plan_id": "plan_1100907ead",
    "is_active": true,
    "created_at": "2026-05-27T08:48:09.178480+00:00"
  },
  {
    "name": "Platinum",
    "min_amount": 10000,
    "max_amount": 99999,
    "daily_roi": 0.005,
    "working_days": 400,
    "badge": null,
    "blurb": "Built for committed allocators.",
    "sort_order": 5,
    "plan_id": "plan_46001fa44d",
    "is_active": true,
    "created_at": "2026-05-27T08:48:09.178485+00:00"
  },
  {
    "name": "Sovereign",
    "min_amount": 100000,
    "max_amount": 100000,
    "daily_roi": 0.005,
    "working_days": 400,
    "badge": "Institutional",
    "blurb": "Institutional tier. White-glove onboarding.",
    "sort_order": 6,
    "plan_id": "plan_c3ce09b899",
    "is_active": true,
    "created_at": "2026-05-27T08:48:09.178495+00:00"
  }
]));

// investments — 0 document(s)
db.createCollection('investments');

// earnings — 0 document(s)
db.createCollection('earnings');

// weekly_payouts — 0 document(s)
db.createCollection('weekly_payouts');

// leads — 0 document(s)
db.createCollection('leads');

// deposit_networks — 1 document(s)
db['deposit_networks'].insertMany(_reviveDates([
  {
    "network_id": "tgcoin",
    "address": "TGxxxx...your-tgcoin-address",
    "created_at": "2026-05-27T09:39:43.286873+00:00",
    "is_active": true,
    "label": "USDT · TGcoin",
    "sort_order": 0
  }
]));

// audit_log — 0 document(s)
db.createCollection('audit_log');

// push_subscriptions — 0 document(s)
db.createCollection('push_subscriptions');

// push_campaigns — 0 document(s)
db.createCollection('push_campaigns');

// app_config — 1 document(s)
db['app_config'].insertMany(_reviveDates([
  {
    "created_at": "2026-05-27T09:39:38.978713+00:00",
    "private": "_21wN5u7faC5s6WX5uN-W8-ennwLnBljloHQ6T2X4Vs",
    "public": "BAwqh01mLi-MCgk6SaHqdNV-DUiRJTCsxFOFrOgGgbBUXpMdCH9WWcBv5r157bYJTMwBPD0P-bfDXo7bjf9fZ2k"
  }
]));

// ---- Create indexes -----------------------------------------------------
db['users'].createIndex({"email": 1}, {"unique": true});
db['users'].createIndex({"user_id": 1}, {"unique": true});
db['users'].createIndex({"referral_code": 1}, {"unique": true, "name": "referral_code_unique_str", "partialFilterExpression": {"referral_code": {"$type": "string"}}});
db['users'].createIndex({"referred_by": 1}, {});
db['user_sessions'].createIndex({"session_token": 1}, {"unique": true});
db['plans'].createIndex({"plan_id": 1}, {"unique": true});
db['investments'].createIndex({"investment_id": 1}, {"unique": true});
db['investments'].createIndex({"user_id": 1, "created_at": -1}, {});
db['earnings'].createIndex({"earning_id": 1}, {"unique": true});
db['earnings'].createIndex({"user_id": 1, "period_date": -1}, {});
db['earnings'].createIndex({"user_id": 1, "type": 1, "period_date": 1}, {});
db['earnings'].createIndex({"status": 1, "period_date": 1}, {});
db['weekly_payouts'].createIndex({"payout_id": 1}, {"unique": true});
db['weekly_payouts'].createIndex({"user_id": 1, "week_end_date": -1}, {});
db['leads'].createIndex({"lead_id": 1}, {"unique": true});
db['deposit_networks'].createIndex({"network_id": 1}, {"unique": true});
db['audit_log'].createIndex({"ts": -1}, {});
db['audit_log'].createIndex({"target_user_id": 1, "ts": -1}, {});
db['push_subscriptions'].createIndex({"endpoint": 1}, {"unique": true});
db['push_subscriptions'].createIndex({"user_id": 1}, {});
db['push_campaigns'].createIndex({"campaign_id": 1}, {"unique": true});
db['push_campaigns'].createIndex({"status": 1, "scheduled_at": 1}, {});

print('✅ Database restored — collections, indexes, and all data are in place.');