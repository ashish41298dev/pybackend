# TradeGain Capital — Portable Database Bundle

Everything you need to upload this database to **any MongoDB target**
(MongoDB Atlas, AWS DocumentDB, self-hosted, local dev, etc.).

You get three formats — pick the one your target supports best.

---

## 📦 What's inside

```
export/
├── tradegain.archive.gz          ← mongodump archive (binary, BSON, gzipped)
├── database_full.js              ← single mongosh script (schema + indexes + data)
├── json/                         ← per-collection JSON exports
│   ├── users.json
│   ├── plans.json
│   ├── deposit_networks.json
│   ├── investments.json
│   ├── earnings.json
│   ├── weekly_payouts.json
│   ├── leads.json
│   ├── audit_log.json
│   ├── user_sessions.json
│   ├── push_subscriptions.json
│   ├── push_campaigns.json
│   └── app_config.json
└── README.md                     ← this file
```

The schema (collections + indexes) is identical in all three formats; pick
whichever restore command fits your environment.

---

## ✅ Option A — MongoDB Atlas / self-hosted Mongo (RECOMMENDED)

Uses the **official `mongorestore`** tool. Preserves indexes and data types
exactly.

```bash
# Replace <CONN_URI> with your Atlas connection string
# e.g. mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/tradegain
mongorestore --uri="<CONN_URI>" --gzip --archive=tradegain.archive.gz

# To overwrite an existing DB, add --drop:
mongorestore --uri="<CONN_URI>" --gzip --archive=tradegain.archive.gz --drop
```

If your archive was dumped from DB `tradegain` and you want to restore into
a differently named DB on Atlas, add:

```bash
mongorestore --uri="<CONN_URI>" --gzip --archive=tradegain.archive.gz \
  --nsFrom='tradegain.*' --nsTo='<your-db-name>.*'
```

---

## ✅ Option B — Replay the single mongosh script

Useful when you don't have `mongorestore` installed (e.g. cloud shells).

```bash
mongosh "<CONN_URI>" database_full.js
```

This drops every collection, recreates them with indexes, and inserts every
document. Safe to re-run.

---

## ✅ Option C — Per-collection JSON (mongoimport)

Useful when you only want to migrate a subset.

```bash
for f in json/*.json; do
  coll=$(basename "$f" .json)
  mongoimport --uri="<CONN_URI>" --collection="$coll" --jsonArray --file="$f"
done
```

⚠ `mongoimport` does **not** create indexes. After import run:

```bash
mongosh "<CONN_URI>" ../init_mongo.js   # creates all indexes
```

---

## 🔧 Configure the app to use the new DB

Once restored, point the backend at the new DB by editing `backend/.env`:

```
MONGO_URL=mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net
DB_NAME=tradegain
```

…then restart the backend. The React frontend needs no changes — it talks to
the backend, not Mongo.

---

## 🔐 What's in the data right now

| Collection | Docs | Notes |
|---|---|---|
| `users` | 2 | admin (admin@tradegain.app) + 1 investor |
| `plans` | 6 | Starter / Bronze / Silver / Gold / Platinum / Sovereign |
| `deposit_networks` | 1 | TGcoin |
| `app_config` | 1 | VAPID keypair for web push |
| everything else | 0 | empty — fresh app, no deposits yet |

The **admin password is bcrypt-hashed** inside `users.password_hash`. The
backend re-syncs it from `ADMIN_PASSWORD` env on every startup, so changing
that env on the target host updates the live credentials.

---

## 🔁 Refreshing this bundle later

After more data has accumulated in the live DB, regenerate the bundle:

```bash
# from /app
mongodump --uri="$MONGO_URL/$DB_NAME" --archive=db/export/tradegain.archive.gz --gzip
python db/generate_dump.py
cp db/database_full.js db/export/database_full.js
for c in users user_sessions plans investments earnings weekly_payouts \
         leads deposit_networks audit_log push_subscriptions push_campaigns app_config; do
  mongoexport --uri="$MONGO_URL/$DB_NAME" --collection="$c" \
              --out="db/export/json/$c.json" --jsonArray
done
```
