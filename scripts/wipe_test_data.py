"""
Wipe Test Data — Production Launch Cleanup
===========================================
Deletes all dummy investor / earning / referral / push data BEFORE launch.
Preserves:
  - Admin users (role == "admin")
  - Investment plans
  - System config (app_config, deposit_networks)

Usage (preview / local):
    cd /app/backend && python3 scripts/wipe_test_data.py

Usage (production):
    Set MONGO_URL + DB_NAME env vars on the prod host and run the same command.

The script is idempotent — running it twice on an already-clean DB is a no-op.
"""
import os
import sys
from pathlib import Path

# Allow running from anywhere
sys.path.append(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

# Collections that will be COMPLETELY truncated
TRUNCATE_COLLECTIONS = [
    "investments",
    "earnings",
    "weekly_payouts",
    "push_subscriptions",
    "push_campaigns",
    "audit_log",
    "leads",
    "user_sessions",
]

# Collections preserved entirely
PRESERVE_COLLECTIONS = [
    "plans",
    "app_config",
    "deposit_networks",
]


def main():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]

    print("=" * 60)
    print(f"  Database: {DB_NAME}")
    print("=" * 60)

    # --- Snapshot BEFORE
    print("\nBEFORE:")
    for name in sorted(db.list_collection_names()):
        print(f"  {name:24s} {db[name].count_documents({}):>6} docs")

    # --- Truncate transactional collections
    print("\nTruncating transactional collections…")
    for col in TRUNCATE_COLLECTIONS:
        if col in db.list_collection_names():
            result = db[col].delete_many({})
            print(f"  {col:24s} {result.deleted_count:>6} deleted")
        else:
            print(f"  {col:24s} (does not exist — skipped)")

    # --- Users: keep ONLY admin role
    print("\nUsers: keeping role='admin' only…")
    users = db["users"]
    admin_count = users.count_documents({"role": "admin"})
    deleted = users.delete_many({"role": {"$ne": "admin"}})
    print(f"  admin preserved : {admin_count}")
    print(f"  investors wiped : {deleted.deleted_count}")

    if admin_count == 0:
        print("  WARNING: no admin user found. Re-run seed if needed.")

    # --- Snapshot AFTER
    print("\nAFTER:")
    for name in sorted(db.list_collection_names()):
        print(f"  {name:24s} {db[name].count_documents({}):>6} docs")

    print("\n✓ Cleanup complete. DB is fresh and ready for launch.")
    client.close()


if __name__ == "__main__":
    main()
