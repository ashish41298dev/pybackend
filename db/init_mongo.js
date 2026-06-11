// =============================================================================
// TradeGain Capital — MongoDB Initialiser  (mongosh)
// =============================================================================
// Usage:
//   mongosh "$MONGO_URL/$DB_NAME" db/init_mongo.js
//
// Idempotent — safe to re-run. Creates every collection used by the FastAPI
// backend, attaches indexes (matching backend/server.py on_startup), and seeds
// admin + plans + deposit networks.
//
// Reads optional env vars at the top of the file; defaults are safe for dev.
// =============================================================================

// ---------- Config -----------------------------------------------------------
const ADMIN_EMAIL    = (typeof process !== "undefined" && process.env.ADMIN_EMAIL)    || "admin@tradegain.local";
const ADMIN_NAME     = "Trade Gain Admin";
// NOTE: This script does NOT hash the password. Run the Python init script or
// let the FastAPI backend handle the bcrypt seed. For a quick dev-only admin
// we set a placeholder hash; backend re-syncs it from ADMIN_PASSWORD env on boot.
const ADMIN_HASH_PLACEHOLDER = "REPLACE_WITH_BCRYPT_OR_LET_BACKEND_SEED";

const COLLECTIONS = [
  "users",
  "user_sessions",
  "plans",
  "investments",
  "earnings",
  "weekly_payouts",
  "leads",
  "deposit_networks",
  "audit_log",
  "push_subscriptions",
  "push_campaigns",
  "app_config",
];

// ---------- 1. Create collections -------------------------------------------
const existing = new Set(db.getCollectionNames());
for (const name of COLLECTIONS) {
  if (!existing.has(name)) {
    db.createCollection(name);
    print(`✓ created collection: ${name}`);
  } else {
    print(`· exists: ${name}`);
  }
}

// ---------- 2. Indexes (mirrors backend/server.py on_startup) ---------------
function idx(coll, spec, opts) {
  try { db[coll].createIndex(spec, opts || {}); print(`  idx ${coll} ${JSON.stringify(spec)}`); }
  catch (e) { print(`  ! idx skip on ${coll}: ${e.message}`); }
}

idx("users", { email: 1 }, { unique: true });
idx("users", { user_id: 1 }, { unique: true });
idx("users", { referral_code: 1 }, {
  unique: true,
  name: "referral_code_unique_str",
  partialFilterExpression: { referral_code: { $type: "string" } },
});
idx("users", { referred_by: 1 });

idx("user_sessions", { session_token: 1 }, { unique: true });

idx("plans", { plan_id: 1 }, { unique: true });

idx("investments", { investment_id: 1 }, { unique: true });
idx("investments", { user_id: 1, created_at: -1 });

idx("earnings", { earning_id: 1 }, { unique: true });
idx("earnings", { user_id: 1, period_date: -1 });
idx("earnings", { user_id: 1, type: 1, period_date: 1 });
idx("earnings", { status: 1, period_date: 1 });

idx("weekly_payouts", { payout_id: 1 }, { unique: true });
idx("weekly_payouts", { user_id: 1, week_end_date: -1 });

idx("leads", { lead_id: 1 }, { unique: true });

idx("deposit_networks", { network_id: 1 }, { unique: true });

idx("audit_log", { ts: -1 });
idx("audit_log", { target_user_id: 1, ts: -1 });

idx("push_subscriptions", { endpoint: 1 }, { unique: true });
idx("push_subscriptions", { user_id: 1 });

idx("push_campaigns", { campaign_id: 1 }, { unique: true });
idx("push_campaigns", { status: 1, scheduled_at: 1 });

// ---------- 3. Seed: deposit_networks ---------------------------------------
const networkSeed = [
  { network_id: "tgcoin", label: "USDT · TGcoin", address: "TGxxxx...your-tgcoin-address", is_active: true, sort_order: 0 },
];
for (const n of networkSeed) {
  const r = db.deposit_networks.updateOne(
    { network_id: n.network_id },
    { $setOnInsert: { ...n, created_at: new Date().toISOString() } },
    { upsert: true },
  );
  if (r.upsertedCount) print(`✓ seeded network: ${n.network_id}`);
}

// ---------- 4. Seed: plans (only when collection is empty) ------------------
if (db.plans.estimatedDocumentCount() === 0) {
  const now = new Date().toISOString();
  const rid = () => Math.random().toString(16).slice(2, 12);
  const PLANS = [
    { name: "Starter",   min_amount: 100,    max_amount: 499,    daily_roi: 0.005, working_days: 400, badge: null,             blurb: "Test the waters with a disciplined first position.", sort_order: 1 },
    { name: "Bronze",    min_amount: 500,    max_amount: 999,    daily_roi: 0.005, working_days: 400, badge: null,             blurb: "A measured step up — same formula, larger output.",  sort_order: 2 },
    { name: "Silver",    min_amount: 1000,   max_amount: 4999,   daily_roi: 0.005, working_days: 400, badge: "Most Popular",   blurb: "The sweet spot most investors land on.",             sort_order: 3 },
    { name: "Gold",      min_amount: 5000,   max_amount: 9999,   daily_roi: 0.005, working_days: 400, badge: null,             blurb: "Serious capital, serious weekly payouts.",            sort_order: 4 },
    { name: "Platinum",  min_amount: 10000,  max_amount: 99999,  daily_roi: 0.005, working_days: 400, badge: null,             blurb: "Built for committed allocators.",                    sort_order: 5 },
    { name: "Sovereign", min_amount: 100000, max_amount: 100000, daily_roi: 0.005, working_days: 400, badge: "Institutional",  blurb: "Institutional tier. White-glove onboarding.",        sort_order: 6 },
  ].map(p => ({ ...p, plan_id: `plan_${rid()}`, is_active: true, created_at: now }));
  db.plans.insertMany(PLANS);
  print(`✓ seeded ${PLANS.length} default plans`);
} else {
  print("· plans already present — skip");
}

// ---------- 5. Seed: admin user --------------------------------------------
// Idempotent. The FastAPI backend will overwrite password_hash from
// ADMIN_PASSWORD env on first boot if it doesn't match — so this row simply
// reserves the email + role.
if (!db.users.findOne({ email: ADMIN_EMAIL })) {
  const rid = () => Math.random().toString(16).slice(2, 14);
  const refCode = "TG" + Math.random().toString(36).toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 6).padEnd(6, "0");
  db.users.insertOne({
    user_id: `user_${rid()}`,
    email: ADMIN_EMAIL,
    name: ADMIN_NAME,
    password_hash: ADMIN_HASH_PLACEHOLDER, // backend re-syncs on boot
    role: "admin",
    auth_provider: "password",
    wallet_address: null,
    phone: null,
    country: null,
    referral_code: refCode,
    referred_by: null,
    picture: null,
    is_active: true,
    force_password_change: false,
    failed_login_attempts: 0,
    lockout_until: null,
    created_at: new Date().toISOString(),
  });
  print(`✓ seeded admin shell: ${ADMIN_EMAIL}  (backend will set bcrypt hash on boot)`);
} else {
  print(`· admin already exists: ${ADMIN_EMAIL}`);
}

print("\n✅ MongoDB initialised.");
print("   Now start the FastAPI backend — it will bcrypt the admin password,");
print("   refresh indexes, and the React frontend will connect via /api.\n");
