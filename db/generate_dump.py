"""
Generate /app/db/database_full.js — a single self-contained MongoDB dump
that contains the FULL SCHEMA (collections + indexes) and ALL DATA from
every collection in the current database.

Run:
    python db/generate_dump.py

Output:
    db/database_full.js  → executable mongosh script
        mongosh "$MONGO_URL/$DB_NAME" db/database_full.js
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "backend" / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME   = os.environ["DB_NAME"]

OUT = ROOT / "db" / "database_full.js"

# Ordered list of every collection used by the app (must match schema.md).
COLLECTIONS = [
    "users", "user_sessions", "plans", "investments", "earnings",
    "weekly_payouts", "leads", "deposit_networks", "audit_log",
    "push_subscriptions", "push_campaigns", "app_config",
]

# Indexes per collection — mirrors backend/server.py on_startup.
INDEXES = {
    "users": [
        ({"email": 1},          {"unique": True}),
        ({"user_id": 1},        {"unique": True}),
        ({"referral_code": 1},  {"unique": True, "name": "referral_code_unique_str",
                                 "partialFilterExpression": {"referral_code": {"$type": "string"}}}),
        ({"referred_by": 1},    {}),
    ],
    "user_sessions": [
        ({"session_token": 1}, {"unique": True}),
    ],
    "plans": [
        ({"plan_id": 1}, {"unique": True}),
    ],
    "investments": [
        ({"investment_id": 1}, {"unique": True}),
        ({"user_id": 1, "created_at": -1}, {}),
    ],
    "earnings": [
        ({"earning_id": 1}, {"unique": True}),
        ({"user_id": 1, "period_date": -1}, {}),
        ({"user_id": 1, "type": 1, "period_date": 1}, {}),
        ({"status": 1, "period_date": 1}, {}),
    ],
    "weekly_payouts": [
        ({"payout_id": 1}, {"unique": True}),
        ({"user_id": 1, "week_end_date": -1}, {}),
    ],
    "leads": [
        ({"lead_id": 1}, {"unique": True}),
    ],
    "deposit_networks": [
        ({"network_id": 1}, {"unique": True}),
    ],
    "audit_log": [
        ({"ts": -1}, {}),
        ({"target_user_id": 1, "ts": -1}, {}),
    ],
    "push_subscriptions": [
        ({"endpoint": 1}, {"unique": True}),
        ({"user_id": 1}, {}),
    ],
    "push_campaigns": [
        ({"campaign_id": 1}, {"unique": True}),
        ({"status": 1, "scheduled_at": 1}, {}),
    ],
    "app_config": [],
}


def _serialise(doc: dict) -> dict:
    """ObjectIds → string; datetime → ISODate placeholder via $date."""
    out = {}
    for k, v in doc.items():
        if k == "_id":
            # Skip _id so mongosh auto-generates a new one; keeps the
            # dump portable across deployments.
            continue
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = {"$date": v.isoformat()}
        elif isinstance(v, dict):
            out[k] = _serialise(v)
        elif isinstance(v, list):
            out[k] = [_serialise(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def _dump_collection(client, name):
    coll = client[DB_NAME][name]
    docs = [_serialise(d) for d in coll.find({})]
    return docs


def main():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]

    parts = []
    parts.append(f"// =============================================================================")
    parts.append(f"// TradeGain Capital — Full MongoDB Database Dump")
    parts.append(f"// Generated: {datetime.now(timezone.utc).isoformat()}")
    parts.append(f"// Source DB: {DB_NAME}")
    parts.append(f"//")
    parts.append(f"// This single file contains EVERYTHING needed to recreate the database:")
    parts.append(f"//   • All collections")
    parts.append(f"//   • All indexes (matches backend/server.py on_startup)")
    parts.append(f"//   • All current documents from every collection")
    parts.append(f"//")
    parts.append(f"// Run:")
    parts.append(f'//   mongosh "$MONGO_URL/$DB_NAME" db/database_full.js')
    parts.append(f"//")
    parts.append(f"// WARNING: this DROPS the existing collections before re-creating them.")
    parts.append(f"// Comment out the `_drop_collections` block at the bottom if you want")
    parts.append(f"// to merge instead of replace.")
    parts.append(f"// =============================================================================")
    parts.append("")
    parts.append("// Helper: parse {$date: \"...\"} → Date so dates round-trip correctly.")
    parts.append("function _reviveDates(o) {")
    parts.append("  if (o === null || typeof o !== 'object') return o;")
    parts.append("  if (Array.isArray(o)) return o.map(_reviveDates);")
    parts.append("  if (Object.keys(o).length === 1 && '$date' in o) return new Date(o['$date']);")
    parts.append("  const out = {};")
    parts.append("  for (const k of Object.keys(o)) out[k] = _reviveDates(o[k]);")
    parts.append("  return out;")
    parts.append("}")
    parts.append("")

    # ---- Drop + recreate ----------------------------------------------------
    parts.append("// ---- Drop existing collections (clean slate) ----------------------------")
    for c in COLLECTIONS:
        parts.append(f"try {{ db['{c}'].drop(); }} catch (e) {{}}")
    parts.append("")

    # ---- Insert data --------------------------------------------------------
    parts.append("// ---- Insert all documents ----------------------------------------------")
    for c in COLLECTIONS:
        docs = _dump_collection(client, c)
        parts.append(f"// {c} — {len(docs)} document(s)")
        if not docs:
            parts.append(f"db.createCollection('{c}');")
        else:
            payload = json.dumps(docs, indent=2, ensure_ascii=False, default=str)
            parts.append(f"db['{c}'].insertMany(_reviveDates({payload}));")
        parts.append("")

    # ---- Indexes ------------------------------------------------------------
    parts.append("// ---- Create indexes -----------------------------------------------------")
    for c, ixs in INDEXES.items():
        for keys, opts in ixs:
            keys_js = json.dumps(keys)
            opts_js = json.dumps(opts) if opts else "{}"
            parts.append(f"db['{c}'].createIndex({keys_js}, {opts_js});")
    parts.append("")

    parts.append("print('✅ Database restored — collections, indexes, and all data are in place.');")

    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"✓ wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
    client.close()


if __name__ == "__main__":
    main()
