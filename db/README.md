# TradeGain Capital — Database (MongoDB)

This folder contains everything needed to **provision the MongoDB database**
that backs the React frontend + FastAPI backend.

## Files

| File | Purpose |
|---|---|
| `schema.md` | Human-readable schema for every collection + indexes + relationships |
| **`database_full.js`** | **🔥 Single self-contained dump — schema + indexes + ALL current data. One-command restore.** |
| `init_mongo.js` | `mongosh` script — creates collections, indexes, seeds plans / networks / admin (no historical data) |
| `init_db.py` | Python equivalent using `motor` — also bcrypts the admin password |
| `generate_dump.py` | Regenerates `database_full.js` from the live DB whenever you want a fresh snapshot |

### Restore from `database_full.js`

```bash
mongosh "$MONGO_URL/$DB_NAME" db/database_full.js
```

This drops every collection, re-creates them with indexes, and inserts every
document that was in the DB at dump time. Use it for backups, staging seeding,
or moving between environments.

### Regenerate `database_full.js` (after data changes)

```bash
python db/generate_dump.py
```

## How the DB is wired to the frontend

```
React (frontend/src/lib/api.js)
   │  axios → ${BACKEND_URL}/api/*
   ▼
FastAPI (backend/server.py)
   │  motor (async MongoDB driver)
   ▼
MongoDB  ◄── env: MONGO_URL + DB_NAME  (set in backend/.env)
```

The frontend never talks to MongoDB directly — every request goes through the
`/api` routes defined in `backend/server.py`. So "connecting the DB to the
frontend" means:

1. **Configure** `backend/.env` with `MONGO_URL` + `DB_NAME`.
2. **Provision** the DB (run `init_db.py` or just start the backend — it
   auto-creates indexes + seeds plans / admin on startup).
3. **Start** backend + frontend (already done by supervisor in this env).

## Quick start (already done for you)

```bash
# 1. Make sure backend/.env exists with MONGO_URL + DB_NAME + JWT_SECRET + ADMIN_* + CORS_ORIGINS
# 2. Provision (idempotent — safe to re-run)
python db/init_db.py
# 3. (Re)start backend
sudo supervisorctl restart backend
```

Admin login: see `memory/test_credentials.md`.
