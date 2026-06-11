"""
TradeGain Capital — MongoDB initialiser (Python / motor).

Run:
    python db/init_db.py

Reads MONGO_URL + DB_NAME + ADMIN_EMAIL + ADMIN_PASSWORD from backend/.env.
Idempotent — safe to re-run. Creates collections, attaches indexes (mirrors
backend/server.py on_startup), and seeds admin + plans + deposit networks.

This is functionally equivalent to letting the FastAPI backend run its
on_startup hook, but is useful for:
    • initialising a fresh Atlas cluster before the app boots,
    • CI / staging seeding,
    • inspecting the schema independently of the backend.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "backend" / ".env")

MONGO_URL      = os.environ.get("MONGO_URL")
DB_NAME        = os.environ.get("DB_NAME")
ADMIN_EMAIL    = (os.environ.get("ADMIN_EMAIL") or "admin@tradegain.local").lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "Admin@1234"

if not MONGO_URL or not DB_NAME:
    raise SystemExit("✗ MONGO_URL and DB_NAME must be set in backend/.env")

NOW = lambda: datetime.now(timezone.utc).isoformat()


def _ref_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "TG" + "".join(secrets.choice(alphabet) for _ in range(6))


def _bcrypt(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


DEFAULT_PLANS = [
    {"name": "Starter",   "min_amount": 100,    "max_amount": 499,    "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "Test the waters with a disciplined first position.", "sort_order": 1},
    {"name": "Bronze",    "min_amount": 500,    "max_amount": 999,    "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "A measured step up — same formula, larger output.",  "sort_order": 2},
    {"name": "Silver",    "min_amount": 1000,   "max_amount": 4999,   "daily_roi": 0.005, "working_days": 400, "badge": "Most Popular",  "blurb": "The sweet spot most investors land on.",             "sort_order": 3},
    {"name": "Gold",      "min_amount": 5000,   "max_amount": 9999,   "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "Serious capital, serious weekly payouts.",            "sort_order": 4},
    {"name": "Platinum",  "min_amount": 10000,  "max_amount": 99999,  "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "Built for committed allocators.",                    "sort_order": 5},
    {"name": "Sovereign", "min_amount": 100000, "max_amount": 100000, "daily_roi": 0.005, "working_days": 400, "badge": "Institutional", "blurb": "Institutional tier. White-glove onboarding.",        "sort_order": 6},
]

DEFAULT_NETWORKS = [
    {"network_id": "tgcoin", "label": "USDT · TGcoin", "address": "TGxxxx...your-tgcoin-address", "is_active": True, "sort_order": 0},
]

COLLECTIONS = [
    "users", "user_sessions", "plans", "investments", "earnings",
    "weekly_payouts", "leads", "deposit_networks", "audit_log",
    "push_subscriptions", "push_campaigns", "app_config",
]


async def main() -> None:
    print(f"→ Connecting to {MONGO_URL}  db={DB_NAME}")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]

    # 1) Collections
    existing = set(await db.list_collection_names())
    for name in COLLECTIONS:
        if name not in existing:
            await db.create_collection(name)
            print(f"  ✓ created collection: {name}")
        else:
            print(f"  · exists: {name}")

    # 2) Indexes
    async def idx(coll, keys, **opts):
        try:
            await db[coll].create_index(keys, **opts)
            print(f"    idx {coll}  {keys}")
        except Exception as e:
            print(f"    ! skip {coll} {keys}: {e}")

    await idx("users", "email", unique=True)
    await idx("users", "user_id", unique=True)
    await idx("users", "referral_code", unique=True,
              partialFilterExpression={"referral_code": {"$type": "string"}},
              name="referral_code_unique_str")
    await idx("users", "referred_by")
    await idx("user_sessions", "session_token", unique=True)
    await idx("plans", "plan_id", unique=True)
    await idx("investments", "investment_id", unique=True)
    await idx("investments", [("user_id", 1), ("created_at", -1)])
    await idx("earnings", "earning_id", unique=True)
    await idx("earnings", [("user_id", 1), ("period_date", -1)])
    await idx("earnings", [("user_id", 1), ("type", 1), ("period_date", 1)])
    await idx("earnings", [("status", 1), ("period_date", 1)])
    await idx("weekly_payouts", "payout_id", unique=True)
    await idx("weekly_payouts", [("user_id", 1), ("week_end_date", -1)])
    await idx("leads", "lead_id", unique=True)
    await idx("deposit_networks", "network_id", unique=True)
    await idx("audit_log", [("ts", -1)])
    await idx("audit_log", [("target_user_id", 1), ("ts", -1)])
    await idx("push_subscriptions", "endpoint", unique=True)
    await idx("push_subscriptions", "user_id")
    await idx("push_campaigns", "campaign_id", unique=True)
    await idx("push_campaigns", [("status", 1), ("scheduled_at", 1)])

    # 3) Seed deposit_networks
    for n in DEFAULT_NETWORKS:
        await db.deposit_networks.update_one(
            {"network_id": n["network_id"]},
            {"$setOnInsert": {**n, "created_at": NOW()}},
            upsert=True,
        )
    print("  ✓ deposit_networks seeded")

    # 4) Seed plans
    if await db.plans.count_documents({}) == 0:
        await db.plans.insert_many([
            {**p, "plan_id": f"plan_{uuid.uuid4().hex[:10]}", "is_active": True, "created_at": NOW()}
            for p in DEFAULT_PLANS
        ])
        print(f"  ✓ seeded {len(DEFAULT_PLANS)} default plans")
    else:
        print("  · plans already present — skip")

    # 5) Seed admin user (with bcrypt — backend would also do this, but we own it here)
    existing_admin = await db.users.find_one({"email": ADMIN_EMAIL})
    if not existing_admin:
        await db.users.insert_one({
            "user_id": f"user_{uuid.uuid4().hex[:12]}",
            "email": ADMIN_EMAIL,
            "name": "Trade Gain Admin",
            "password_hash": _bcrypt(ADMIN_PASSWORD),
            "role": "admin",
            "auth_provider": "password",
            "wallet_address": None,
            "phone": None,
            "country": None,
            "referral_code": _ref_code(),
            "referred_by": None,
            "picture": None,
            "is_active": True,
            "force_password_change": False,
            "failed_login_attempts": 0,
            "lockout_until": None,
            "created_at": NOW(),
        })
        print(f"  ✓ admin created: {ADMIN_EMAIL}")
    else:
        # Re-sync hash so dev env always works
        if not bcrypt.checkpw(ADMIN_PASSWORD.encode(), existing_admin.get("password_hash", "").encode() or b""):
            await db.users.update_one(
                {"email": ADMIN_EMAIL},
                {"$set": {"password_hash": _bcrypt(ADMIN_PASSWORD), "role": "admin"}},
            )
            print(f"  ↻ admin password re-synced from env: {ADMIN_EMAIL}")
        else:
            print(f"  · admin already configured: {ADMIN_EMAIL}")

    print("\n✅ MongoDB initialised. You can now start the FastAPI backend.\n")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
