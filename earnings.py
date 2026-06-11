"""Earnings & payout engine for Trade Gain Capital.

REWRITTEN to match the official MLM Payout Engine spec:

3 PAYOUT TYPES
──────────────
1. Direct ROI   → 0.5% of own active investment / working day, until 2× cap.
                  Final day pays only the remainder (not full 0.5%).
                  Investor marked inactive AFTER final payout; removed from
                  business totals the NEXT day.

2. Level Income → % of downline's daily ROI (NOT investment).
                  L1..L10 — sliding window relative to the earner.
                  Each level has a minimum-active-business unlock.
                  If a level is locked, that commission is SKIPPED
                  (not passed up the chain).

3. Referral     → 5% one-time of the new investor's amount, paid to the
                  DIRECT REFERRER ONLY (not multi-level).

All 3 streams count against the receiver's 2× cap.

SETTLEMENT — accumulate Mon–Fri, sweep on Saturday into weekly_payouts.

──────────────────────────────────────────────────────────────────────────
DB type field on db.earnings is now: "roi" | "level" | "referral"
(Legacy rows of type="matching" continue to be summed in lifetime totals
under the "level" bucket for backward compatibility, though after a
recompute migration all old rows are deleted and only new rows remain.)
"""
from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from typing import Optional
import uuid
import logging

logger = logging.getLogger("earnings")

# ─────────────────────────────────────────────────────────────
# SECTION 1 — CONSTANTS
# ─────────────────────────────────────────────────────────────

DAILY_RATE = 0.005          # 0.5% Direct ROI per working day
TARGET_MULTIPLE = 2          # 2× cap on lifetime sum of (roi + level + referral)
REFERRAL_RATE = 0.05         # 5% one-time, direct referrer only
MAX_LEVELS = 10              # L1..L10 only (sliding window)

# Min active business required at each level + commission rate on downline ROI
LEVEL_SLABS = {
    1:  {"min": 500,    "rate": 0.50},
    2:  {"min": 1000,   "rate": 0.40},
    3:  {"min": 2000,   "rate": 0.30},
    4:  {"min": 3000,   "rate": 0.30},
    5:  {"min": 5000,   "rate": 0.25},
    6:  {"min": 10000,  "rate": 0.25},
    7:  {"min": 20000,  "rate": 0.20},
    8:  {"min": 30000,  "rate": 0.20},
    9:  {"min": 50000,  "rate": 0.15},
    10: {"min": 100000, "rate": 0.10},
}

# Public re-export — UI and other modules read this for the slab table
SLABS = [
    {"id": 0,  "label": "Slab 0",  "threshold": 0,       "pct": 0.00},
    *[
        {
            "id": k,
            "label": f"Slab {k}",
            "threshold": LEVEL_SLABS[k]["min"],
            "pct": LEVEL_SLABS[k]["rate"],
        }
        for k in range(1, MAX_LEVELS + 1)
    ],
]


def slab_for(volume: float) -> dict:
    """Return the highest slab whose threshold ≤ volume. <100 USDT → Slab 0."""
    if volume < 100:
        return SLABS[0]
    current = SLABS[0]
    for s in SLABS:
        if volume >= s["threshold"]:
            current = s
    return current


# ─────────────────────────────────────────────────────────────
# SECTION 2 — DATE HELPERS
# ─────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def is_working_day(d: date) -> bool:
    return d.weekday() < 5  # Mon=0 .. Fri=4


def is_saturday(d: date) -> bool:
    return d.weekday() == 5


def next_saturday(d: date) -> date:
    """Saturday ON or AFTER d (returns d itself if d IS Saturday)."""
    offset = (5 - d.weekday()) % 7
    return d + timedelta(days=offset)


def upcoming_saturday(d: date) -> date:
    return next_saturday(d)


def week_window(any_date: date) -> tuple[date, date, date]:
    """(Monday, Friday, Saturday) for the work-week containing any_date."""
    monday = any_date - timedelta(days=any_date.weekday())
    return monday, monday + timedelta(days=4), monday + timedelta(days=5)


# ─────────────────────────────────────────────────────────────
# SECTION 3 — DB HELPERS
# ─────────────────────────────────────────────────────────────

async def _active_amount_by_user(db) -> dict:
    """user_id -> sum(amount) of investments with status='active'."""
    out: dict = {}
    async for r in db.investments.aggregate([
        {"$match": {"status": "active"}},
        {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}},
    ]):
        out[r["_id"]] = float(r.get("total", 0.0))
    return out


async def _children_map(db) -> dict:
    """parent_user_id -> [direct child user_ids]."""
    out: dict = {}
    async for u in db.users.find(
        {"role": "investor"}, {"_id": 0, "user_id": 1, "referred_by": 1}
    ):
        parent = u.get("referred_by")
        if parent:
            out.setdefault(parent, []).append(u["user_id"])
    return out


async def _parent_of(db) -> dict:
    """user_id -> direct_parent_user_id (or None)."""
    out: dict = {}
    async for u in db.users.find(
        {"role": "investor"}, {"_id": 0, "user_id": 1, "referred_by": 1}
    ):
        out[u["user_id"]] = u.get("referred_by")
    return out


async def _payout_active_users(db) -> set:
    """Set of investor user_ids who have not yet hit their 2× cap.

    A user is payout-active when:
      • role == 'investor'
      • is_payout_active != False  (default True)
      • they have at least one investment with status='active'
    """
    out = set()
    async for u in db.users.find(
        {"role": "investor"},
        {"_id": 0, "user_id": 1, "is_payout_active": 1},
    ):
        if u.get("is_payout_active") is not False:
            out.add(u["user_id"])
    return out


def downline_levels(root_id: str, children_map: dict, max_depth: int = MAX_LEVELS) -> dict:
    """{depth: [user_ids]} for root_id's downline (BFS), depths 1..max_depth."""
    out: dict = {}
    frontier = [(c, 1) for c in children_map.get(root_id, [])]
    while frontier:
        nxt = []
        for uid, d in frontier:
            if d > max_depth:
                continue
            out.setdefault(d, []).append(uid)
            for ch in children_map.get(uid, []):
                nxt.append((ch, d + 1))
        frontier = nxt
    return out


def upline_chain(user_id: str, parent_of: dict, max_depth: int = MAX_LEVELS) -> list[tuple[str, int]]:
    """Walk upward: [(ancestor_user_id, depth_below_them)], up to max_depth."""
    chain = []
    current = user_id
    depth = 0
    while current and depth < max_depth:
        parent = parent_of.get(current)
        if not parent:
            break
        depth += 1
        chain.append((parent, depth))
        current = parent
    return chain


def get_active_business(person_id: str, level: int, children_map: dict, active_by_user: dict, payout_active: set) -> float:
    """Sum of ACTIVE investments of downline members at exactly `level` depth.
    Only counts members who are still payout-active (haven't hit 2× cap)."""
    if level < 1 or level > MAX_LEVELS:
        return 0.0
    levels = downline_levels(person_id, children_map, max_depth=level)
    members = levels.get(level, [])
    return sum(
        active_by_user.get(uid, 0.0)
        for uid in members
        if uid in payout_active
    )


def is_level_unlocked(person_id: str, level: int, children_map: dict, active_by_user: dict, payout_active: set) -> bool:
    if level < 1 or level > MAX_LEVELS:
        return False
    biz = get_active_business(person_id, level, children_map, active_by_user, payout_active)
    return biz >= LEVEL_SLABS[level]["min"]


# ─────────────────────────────────────────────────────────────
# SECTION 4 — CAP TRACKING
# ─────────────────────────────────────────────────────────────

async def cap_status(db, user_id: str) -> dict:
    """{invested, cap, used, remaining, pct, reached}"""
    invested = 0.0
    async for r in db.investments.aggregate([
        {"$match": {"user_id": user_id, "status": "active"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]):
        invested = float(r.get("total", 0.0))
    cap = invested * TARGET_MULTIPLE

    used = 0.0
    async for r in db.earnings.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]):
        used = float(r.get("total", 0.0))

    remaining = max(0.0, cap - used)
    return {
        "invested": round(invested, 2),
        "cap": round(cap, 2),
        "used": round(used, 2),
        "remaining": round(remaining, 2),
        "pct": round((used / cap * 100.0) if cap > 0 else 0.0, 2),
        "reached": cap > 0 and used >= cap,
    }


def _clip_to_cap(amount: float, status: dict) -> tuple[float, bool]:
    if status["reached"]:
        return 0.0, True
    remaining = status["remaining"]
    if amount <= remaining:
        return round(amount, 6), False
    return round(remaining, 6), True


# ─────────────────────────────────────────────────────────────
# SECTION 5 — WRITERS
# ─────────────────────────────────────────────────────────────

async def _record_earning(
    db, *,
    user_id: str,
    etype: str,                     # "roi" | "level" | "referral"
    amount: float,
    period_date: date,
    source_user_id: Optional[str] = None,
    source_investment_id: Optional[str] = None,
    source_level: int = 0,
    capped: bool = False,
    meta: Optional[dict] = None,
) -> dict:
    doc = {
        "earning_id": f"earn_{uuid.uuid4().hex[:12]}",
        "user_id": user_id,
        "type": etype,
        "amount": round(float(amount), 6),
        "period_date": period_date.isoformat(),
        "source_user_id": source_user_id,
        "source_investment_id": source_investment_id,
        "source_level": int(source_level),
        "capped": bool(capped),
        "status": "locked",
        "payout_id": None,
        "meta": meta or {},
        "created_at": iso(now_utc()),
        "paid_at": None,
    }
    await db.earnings.insert_one(doc)
    doc.pop("_id", None)
    return doc


async def _audit(db, *, actor: str, action: str, target_user_id: Optional[str], amount: float, meta: dict):
    await db.audit_log.insert_one({
        "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
        "ts": iso(now_utc()),
        "actor": actor,
        "action": action,
        "target_user_id": target_user_id,
        "amount": round(float(amount), 6),
        "meta": meta,
    })


# ─────────────────────────────────────────────────────────────
# SECTION 6 — REFERRAL (DIRECT REFERRER ONLY)
# ─────────────────────────────────────────────────────────────

async def credit_referral_for_investment(db, investment: dict) -> list[dict]:
    """5% one-time referral, paid ONLY to the direct referrer.
    Returns the list with at most one earning row.
    """
    investor_id = investment["user_id"]
    amount = float(investment["amount"])
    inv_id = investment["investment_id"]
    today = now_utc().date()

    investor = await db.users.find_one(
        {"user_id": investor_id},
        {"_id": 0, "referred_by": 1},
    )
    if not investor or not investor.get("referred_by"):
        return []
    referrer_id = investor["referred_by"]

    bonus = amount * REFERRAL_RATE
    status = await cap_status(db, referrer_id)
    clipped, was_clipped = _clip_to_cap(bonus, status)
    if clipped <= 0:
        await _audit(
            db, actor="system", action="referral.skipped_capped",
            target_user_id=referrer_id, amount=0,
            meta={"would_have_paid": bonus, "source_user": investor_id, "source_inv": inv_id},
        )
        return []

    doc = await _record_earning(
        db,
        user_id=referrer_id, etype="referral", amount=clipped, period_date=today,
        source_user_id=investor_id, source_investment_id=inv_id, source_level=1,
        capped=was_clipped,
        meta={"investment_amount": amount, "rate": REFERRAL_RATE, "was_clipped": was_clipped, "direct_only": True},
    )
    await _audit(
        db, actor="system", action="referral.credited",
        target_user_id=referrer_id, amount=clipped,
        meta={"source_user": investor_id, "source_inv": inv_id, "was_clipped": was_clipped},
    )
    return [doc]


# ─────────────────────────────────────────────────────────────
# SECTION 7 — DAILY ACCRUAL (Direct ROI + Level Income)
# ─────────────────────────────────────────────────────────────

async def accrue_daily_all(db, on_date: Optional[date] = None) -> dict:
    """The per-day processor — runs Direct ROI for every payout-active investor
    then flows Level Income up to their unlocked ancestors.

    Idempotent: if a user already has an ROI row for `on_date`, they're skipped.
    """
    on_date = on_date or now_utc().date()
    if not is_working_day(on_date):
        return {"date": on_date.isoformat(), "skipped": "non_working_day", "users": 0, "total_roi": 0, "total_level": 0}

    # Snapshot state at start of day
    active_by_user = await _active_amount_by_user(db)
    payout_active = await _payout_active_users(db)
    children_map = await _children_map(db)
    parent_of = await _parent_of(db)

    # Restrict to users who have both an active investment AND are payout-active
    candidates = [uid for uid in active_by_user.keys() if uid in payout_active]
    # Sort for determinism
    candidates.sort()

    total_roi = 0.0
    total_level = 0.0
    processed = 0
    skipped = 0
    completed_today: list[str] = []

    for uid in candidates:
        # idempotency: already accrued ROI today?
        existing = await db.earnings.count_documents({
            "user_id": uid, "type": "roi", "period_date": on_date.isoformat(),
        })
        if existing > 0:
            skipped += 1
            continue

        status = await cap_status(db, uid)
        if status["reached"] or status["invested"] <= 0:
            continue

        # ─ Direct ROI ─
        roi_raw = status["invested"] * DAILY_RATE
        roi_amount, was_clipped = _clip_to_cap(roi_raw, status)
        if roi_amount <= 0:
            continue

        await _record_earning(
            db, user_id=uid, etype="roi", amount=roi_amount, period_date=on_date,
            source_user_id=uid, source_level=0, capped=was_clipped,
            meta={"rate": DAILY_RATE, "base": status["invested"], "was_clipped": was_clipped},
        )
        total_roi += roi_amount
        processed += 1

        # ─ Level Income to unlocked ancestors ─
        ancestors = upline_chain(uid, parent_of, max_depth=MAX_LEVELS)
        for ancestor_id, level in ancestors:
            slab = LEVEL_SLABS[level]
            # Unlock check (live snapshot)
            if not is_level_unlocked(ancestor_id, level, children_map, active_by_user, payout_active):
                await _audit(
                    db, actor="system", action="level.skipped_locked",
                    target_user_id=ancestor_id, amount=0,
                    meta={
                        "date": on_date.isoformat(), "level": level,
                        "from_user": uid, "min_required": slab["min"],
                        "active_biz": get_active_business(ancestor_id, level, children_map, active_by_user, payout_active),
                    },
                )
                continue

            level_amount = roi_amount * slab["rate"]
            anc_cap = await cap_status(db, ancestor_id)
            clipped, anc_was_clipped = _clip_to_cap(level_amount, anc_cap)
            if clipped <= 0:
                await _audit(
                    db, actor="system", action="level.skipped_capped",
                    target_user_id=ancestor_id, amount=0,
                    meta={"date": on_date.isoformat(), "level": level, "from_user": uid, "would_have_paid": level_amount},
                )
                continue

            await _record_earning(
                db, user_id=ancestor_id, etype="level", amount=clipped, period_date=on_date,
                source_user_id=uid, source_level=level, capped=anc_was_clipped,
                meta={
                    "downline_daily_roi": roi_amount,
                    "rate": slab["rate"],
                    "level": level,
                    "from_user": uid,
                    "was_clipped": anc_was_clipped,
                },
            )
            total_level += clipped

        # ─ Mark inactive if just hit cap ─
        post = await cap_status(db, uid)
        if post["reached"]:
            await db.users.update_one(
                {"user_id": uid},
                {"$set": {
                    "is_payout_active": False,
                    "plan_completed_at": iso(now_utc()),
                }},
            )
            completed_today.append(uid)
            await _audit(
                db, actor="system", action="plan.completed",
                target_user_id=uid, amount=post["used"],
                meta={"date": on_date.isoformat(), "cap": post["cap"], "used": post["used"]},
            )

    return {
        "date": on_date.isoformat(),
        "users": processed,
        "skipped": skipped,
        "total_roi": round(total_roi, 2),
        "total_level": round(total_level, 2),
        "completed_today": completed_today,
    }


async def accrue_daily_for_user(db, user_id: str, on_date: Optional[date] = None) -> dict:
    """Backward-compat single-user wrapper. The new engine is fundamentally
    network-wide (level income flows up from EVERY user's ROI), so the most
    correct call is `accrue_daily_all`. This helper still exists for the
    legacy admin endpoint — it just delegates to the full network run.
    """
    return await accrue_daily_all(db, on_date)


# ─────────────────────────────────────────────────────────────
# SECTION 8 — SATURDAY SWEEP
# ─────────────────────────────────────────────────────────────

async def sweep_weekly_payouts(db, week_ending: Optional[date] = None) -> dict:
    """Collect all locked earnings on or before `week_ending` (default = next
    Saturday from today), group by user, create a weekly_payouts row, and
    mark the underlying earnings as paid.

    Idempotent — if a payout for (user, week_ending) already exists, skip.
    """
    week_ending = week_ending or upcoming_saturday(now_utc().date())
    week_start, _, _ = week_window(week_ending)

    pipeline = [
        {"$match": {"status": "locked", "period_date": {"$lte": week_ending.isoformat()}}},
        {"$group": {
            "_id": "$user_id",
            "total": {"$sum": "$amount"},
            "roi": {"$sum": {"$cond": [{"$eq": ["$type", "roi"]}, "$amount", 0]}},
            # Legacy 'matching' rows (pre-rewrite) sum into the new "level" bucket
            "level": {"$sum": {"$cond": [
                {"$in": ["$type", ["level", "matching"]]}, "$amount", 0
            ]}},
            "referral": {"$sum": {"$cond": [{"$eq": ["$type", "referral"]}, "$amount", 0]}},
            "count": {"$sum": 1},
        }},
    ]
    created = []
    async for grp in db.earnings.aggregate(pipeline):
        uid = grp["_id"]
        existing = await db.weekly_payouts.find_one({
            "user_id": uid, "week_end_date": week_ending.isoformat(),
        }, {"_id": 0})
        if existing:
            continue
        payout_id = f"po_{uuid.uuid4().hex[:12]}"
        payout = {
            "payout_id": payout_id,
            "user_id": uid,
            "week_start_date": week_start.isoformat(),
            "week_end_date": week_ending.isoformat(),
            "total": round(float(grp["total"]), 2),
            "breakdown": {
                "roi":      round(float(grp["roi"]), 2),
                "level":    round(float(grp["level"]), 2),
                # Legacy alias — UI still reads breakdown.matching in places
                "matching": round(float(grp["level"]), 2),
                "referral": round(float(grp["referral"]), 2),
            },
            "earning_count": int(grp["count"]),
            "status": "paid",
            "created_at": iso(now_utc()),
            "paid_at": iso(now_utc()),
        }
        await db.weekly_payouts.insert_one(payout)
        await db.earnings.update_many(
            {"user_id": uid, "status": "locked", "period_date": {"$lte": week_ending.isoformat()}},
            {"$set": {"status": "paid", "payout_id": payout_id, "paid_at": iso(now_utc())}},
        )
        await _audit(
            db, actor="system", action="payout.swept", target_user_id=uid,
            amount=payout["total"], meta={"week_ending": week_ending.isoformat(), "payout_id": payout_id},
        )
        payout.pop("_id", None)
        created.append(payout)
    return {"week_ending": week_ending.isoformat(), "created": len(created), "payouts": created}


# ─────────────────────────────────────────────────────────────
# SECTION 9 — RECOMPUTE FROM SCRATCH (admin migration)
# ─────────────────────────────────────────────────────────────

async def recompute_all_from_scratch(db) -> dict:
    """Wipe all earnings + weekly_payouts, reset user state, and replay every
    working day from the earliest investment approval up to today, applying
    the new spec.

    Steps:
      1. db.earnings.delete_many({})
      2. db.weekly_payouts.delete_many({})
      3. users.update_many: clear is_payout_active=True, plan_completed_at=None
      4. For each investment with status='active' sorted by decided_at:
           - On its decided_at day, fire credit_referral_for_investment
      5. For each working day from earliest decided_at to today:
           - Run accrue_daily_all(date)
      6. Run sweep_weekly_payouts for each Saturday in the window

    This is a DESTRUCTIVE operation — caller must confirm.
    """
    # 1+2 — wipe ledger and payout history
    earn_deleted = (await db.earnings.delete_many({})).deleted_count
    payout_deleted = (await db.weekly_payouts.delete_many({})).deleted_count

    # 3 — reset user payout-active state
    await db.users.update_many(
        {"role": "investor"},
        {"$set": {"is_payout_active": True, "plan_completed_at": None}},
    )

    # 4 — collect active investments in chronological approval order
    investments: list[dict] = []
    async for inv in db.investments.find(
        {"status": "active"},
        {"_id": 0},
    ).sort("decided_at", 1):
        investments.append(inv)

    if not investments:
        return {
            "ok": True,
            "wiped_earnings": earn_deleted,
            "wiped_payouts": payout_deleted,
            "investments": 0,
            "days_processed": 0,
            "sweeps": 0,
        }

    def _to_date(v) -> date:
        if isinstance(v, datetime):
            return v.date()
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
        except Exception:
            return now_utc().date()

    # Earliest date — when the first investment was activated
    investments_by_day: dict = {}
    earliest: Optional[date] = None
    for inv in investments:
        d = _to_date(inv.get("decided_at") or inv.get("created_at"))
        investments_by_day.setdefault(d.isoformat(), []).append(inv)
        if earliest is None or d < earliest:
            earliest = d

    today = now_utc().date()
    days_processed = 0
    sweeps = 0
    cursor = earliest

    while cursor <= today:
        # Fire one-time referral for any investment approved on this day
        for inv in investments_by_day.get(cursor.isoformat(), []):
            try:
                await credit_referral_for_investment(db, inv)
            except Exception as e:
                logger.exception("recompute referral failed for %s: %s", inv.get("investment_id"), e)

        # Accrue daily payouts (Mon-Fri only)
        if is_working_day(cursor):
            try:
                await accrue_daily_all(db, cursor)
                days_processed += 1
            except Exception as e:
                logger.exception("recompute daily failed for %s: %s", cursor, e)

        # Sweep on Saturday — only if it's strictly in the past (don't pre-sweep current week)
        if is_saturday(cursor) and cursor < today:
            try:
                await sweep_weekly_payouts(db, cursor)
                sweeps += 1
            except Exception as e:
                logger.exception("recompute sweep failed for %s: %s", cursor, e)

        cursor += timedelta(days=1)

    await _audit(
        db, actor="system", action="payouts.recomputed",
        target_user_id=None, amount=0,
        meta={
            "wiped_earnings": earn_deleted,
            "wiped_payouts": payout_deleted,
            "investments": len(investments),
            "days_processed": days_processed,
            "sweeps": sweeps,
            "earliest": earliest.isoformat(),
            "today": today.isoformat(),
        },
    )

    return {
        "ok": True,
        "wiped_earnings": earn_deleted,
        "wiped_payouts": payout_deleted,
        "investments": len(investments),
        "days_processed": days_processed,
        "sweeps": sweeps,
        "earliest": earliest.isoformat(),
        "today": today.isoformat(),
    }


# ─────────────────────────────────────────────────────────────
# SECTION 10 — READ-SIDE HELPERS (UI / admin)
# ─────────────────────────────────────────────────────────────

async def earnings_summary(db, user_id: str) -> dict:
    """Investor dashboard snapshot — totals, week-to-date, cap, run-rate."""
    status = await cap_status(db, user_id)
    today = now_utc().date()
    week_start, week_working_end, week_end = week_window(today)
    next_sat = upcoming_saturday(today)

    # Lifetime totals by type (legacy 'matching' rolls into 'level')
    totals = {"roi": 0.0, "level": 0.0, "referral": 0.0}
    async for r in db.earnings.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
    ]):
        bucket = "level" if r["_id"] == "matching" else r["_id"]
        totals[bucket] = totals.get(bucket, 0.0) + float(r.get("total", 0.0))

    # This-week pending (locked)
    week_pending = {"roi": 0.0, "level": 0.0, "referral": 0.0}
    async for r in db.earnings.aggregate([
        {"$match": {
            "user_id": user_id, "status": "locked",
            "period_date": {"$gte": week_start.isoformat(), "$lte": week_end.isoformat()},
        }},
        {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
    ]):
        bucket = "level" if r["_id"] == "matching" else r["_id"]
        week_pending[bucket] = week_pending.get(bucket, 0.0) + float(r.get("total", 0.0))
    week_pending_total = sum(week_pending.values())

    # All-pending and paid totals
    pending_total = 0.0
    async for r in db.earnings.aggregate([
        {"$match": {"user_id": user_id, "status": "locked"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]):
        pending_total = float(r.get("total", 0.0))

    paid_total = 0.0
    async for r in db.earnings.aggregate([
        {"$match": {"user_id": user_id, "status": "paid"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]):
        paid_total = float(r.get("total", 0.0))

    # Daily run-rate (estimate): ROI + sum over UNLOCKED levels of (downline ROI × rate)
    active_by_user = await _active_amount_by_user(db)
    payout_active = await _payout_active_users(db)
    children_map = await _children_map(db)
    roi_daily = status["invested"] * DAILY_RATE
    level_daily = 0.0
    levels = downline_levels(user_id, children_map, max_depth=MAX_LEVELS)
    for depth, uids in levels.items():
        if depth < 1 or depth > MAX_LEVELS:
            continue
        slab = LEVEL_SLABS[depth]
        active_biz = sum(active_by_user.get(u, 0.0) for u in uids if u in payout_active)
        if active_biz < slab["min"]:
            continue
        downline_roi = sum(active_by_user.get(u, 0.0) * DAILY_RATE for u in uids if u in payout_active)
        level_daily += downline_roi * slab["rate"]
    daily = roi_daily + level_daily

    if daily > 0 and status["remaining"] > 0:
        days_to_cap = max(1, int(status["remaining"] / daily) + (1 if status["remaining"] % daily else 0))
    elif status["reached"]:
        days_to_cap = 0
    else:
        days_to_cap = None

    return {
        "user_id": user_id,
        "today": today.isoformat(),
        "cap": status,
        "lifetime": {
            "roi": round(totals["roi"], 2),
            "level": round(totals["level"], 2),
            "matching": round(totals["level"], 2),  # legacy alias
            "referral": round(totals["referral"], 2),
            "total": round(sum(totals.values()), 2),
            "paid": round(paid_total, 2),
            "pending": round(pending_total, 2),
        },
        "this_week": {
            "start": week_start.isoformat(),
            "working_end": week_working_end.isoformat(),
            "end": week_end.isoformat(),
            "next_payout_date": next_sat.isoformat(),
            "roi": round(week_pending["roi"], 2),
            "level": round(week_pending["level"], 2),
            "matching": round(week_pending["level"], 2),  # legacy alias
            "referral": round(week_pending["referral"], 2),
            "total": round(week_pending_total, 2),
        },
        "daily_run_rate": {
            "roi": round(roi_daily, 2),
            "level": round(level_daily, 2),
            "matching": round(level_daily, 2),  # legacy alias
            "total": round(daily, 2),
        },
        "days_to_cap": days_to_cap,
    }


async def levels_breakdown(db, user_id: str) -> dict:
    """L1..L10 breakdown of the user's downline with unlock status,
    daily level income potential, and lifetime totals."""
    active_by_user = await _active_amount_by_user(db)
    payout_active = await _payout_active_users(db)
    children_map = await _children_map(db)
    users_by_id = {}
    async for u in db.users.find({}, {"_id": 0, "password_hash": 0}):
        users_by_id[u["user_id"]] = u

    levels = downline_levels(user_id, children_map, max_depth=MAX_LEVELS)

    # Lifetime referral / level totals from this user's earnings, grouped by source_level
    referral_by_level: dict = {}
    async for r in db.earnings.aggregate([
        {"$match": {"user_id": user_id, "type": "referral"}},
        {"$group": {"_id": "$source_level", "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]):
        referral_by_level[int(r["_id"])] = {"total": float(r.get("total", 0.0)), "count": int(r.get("count", 0))}

    level_by_level: dict = {}
    async for r in db.earnings.aggregate([
        {"$match": {"user_id": user_id, "type": {"$in": ["level", "matching"]}}},
        {"$group": {"_id": "$source_level", "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]):
        level_by_level[int(r["_id"])] = {"total": float(r.get("total", 0.0)), "count": int(r.get("count", 0))}

    out = []
    for depth in range(1, MAX_LEVELS + 1):
        member_ids = levels.get(depth, [])
        # Active business — only payout-active members
        active_biz = sum(
            active_by_user.get(uid, 0.0)
            for uid in member_ids
            if uid in payout_active
        )
        # Total volume (for display)
        volume = sum(active_by_user.get(uid, 0.0) for uid in member_ids)
        slab_meta = LEVEL_SLABS[depth]
        unlocked = active_biz >= slab_meta["min"]
        # Potential daily level income — only if unlocked
        downline_roi = sum(active_by_user.get(uid, 0.0) * DAILY_RATE for uid in member_ids if uid in payout_active)
        level_daily = downline_roi * slab_meta["rate"] if unlocked else 0.0

        ref = referral_by_level.get(depth, {"total": 0.0, "count": 0})
        lvl = level_by_level.get(depth, {"total": 0.0, "count": 0})
        members = []
        for uid in member_ids:
            u = users_by_id.get(uid, {})
            members.append({
                "user_id": uid,
                "name": u.get("name"),
                "email": u.get("email"),
                "active_capital": round(active_by_user.get(uid, 0.0), 2),
                "is_payout_active": uid in payout_active,
            })
        out.append({
            "level": depth,
            "name": f"L{depth}",
            "member_count": len(member_ids),
            "active_member_count": sum(1 for u in member_ids if u in payout_active and active_by_user.get(u, 0.0) > 0),
            "volume": round(volume, 2),
            "active_business": round(active_biz, 2),
            "min_business_required": slab_meta["min"],
            "rate": slab_meta["rate"],
            "unlocked": unlocked,
            "slab": {"id": depth, "label": f"Slab {depth}", "threshold": slab_meta["min"], "pct": slab_meta["rate"]},
            "level_daily_potential": round(level_daily, 2),
            "matching_daily": round(level_daily, 2),  # legacy alias
            "referral_lifetime": round(ref["total"], 2),
            "referral_count": ref["count"],
            "level_lifetime": round(lvl["total"], 2),
            "matching_lifetime": round(lvl["total"], 2),  # legacy alias
            "members": members,
        })
    return {
        "user_id": user_id,
        "levels": out,
        "total_team_volume": round(sum(lvl["volume"] for lvl in out), 2),
        "total_team_size": sum(lvl["member_count"] for lvl in out),
    }


async def admin_overview_earnings(db) -> dict:
    """System-wide payout obligations and cap utilization for the admin dashboard."""
    today = now_utc().date()
    week_start, week_working_end, week_end = week_window(today)

    target = today if is_working_day(today) else today - timedelta(days=(today.weekday() - 4) % 7 or 1)

    today_totals = {"roi": 0.0, "level": 0.0, "referral": 0.0}
    async for r in db.earnings.aggregate([
        {"$match": {"period_date": target.isoformat()}},
        {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
    ]):
        bucket = "level" if r["_id"] == "matching" else r["_id"]
        today_totals[bucket] = today_totals.get(bucket, 0.0) + float(r.get("total", 0.0))

    week_pending = {"roi": 0.0, "level": 0.0, "referral": 0.0}
    async for r in db.earnings.aggregate([
        {"$match": {"status": "locked",
                    "period_date": {"$gte": week_start.isoformat(), "$lte": week_end.isoformat()}}},
        {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
    ]):
        bucket = "level" if r["_id"] == "matching" else r["_id"]
        week_pending[bucket] = week_pending.get(bucket, 0.0) + float(r.get("total", 0.0))

    total_invested = 0.0
    async for r in db.investments.aggregate([
        {"$match": {"status": "active"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]):
        total_invested = float(r.get("total", 0.0))
    total_cap = total_invested * TARGET_MULTIPLE

    total_used = 0.0
    async for r in db.earnings.aggregate([{"$group": {"_id": None, "total": {"$sum": "$amount"}}}]):
        total_used = float(r.get("total", 0.0))

    active_users = {}
    async for r in db.investments.aggregate([
        {"$match": {"status": "active"}},
        {"$group": {"_id": "$user_id", "invested": {"$sum": "$amount"}}},
    ]):
        active_users[r["_id"]] = float(r.get("invested", 0.0))

    earnings_by_user = {}
    async for r in db.earnings.aggregate([{"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}}]):
        earnings_by_user[r["_id"]] = float(r.get("total", 0.0))

    capped_count = 0
    near_cap_count = 0
    for uid, invested in active_users.items():
        used = earnings_by_user.get(uid, 0.0)
        cap = invested * TARGET_MULTIPLE
        if cap <= 0:
            continue
        pct = (used / cap) * 100
        if pct >= 100:
            capped_count += 1
        elif pct >= 80:
            near_cap_count += 1

    last_sweep = await db.weekly_payouts.find_one({}, {"_id": 0}, sort=[("created_at", -1)])

    return {
        "target_date": target.isoformat(),
        "today_obligation": {
            "roi": round(today_totals["roi"], 2),
            "level": round(today_totals["level"], 2),
            "matching": round(today_totals["level"], 2),  # legacy alias
            "referral": round(today_totals["referral"], 2),
            "total": round(sum(today_totals.values()), 2),
        },
        "this_week_pending": {
            "start": week_start.isoformat(),
            "working_end": week_working_end.isoformat(),
            "end": week_end.isoformat(),
            "next_payout_date": upcoming_saturday(today).isoformat(),
            "roi": round(week_pending["roi"], 2),
            "level": round(week_pending["level"], 2),
            "matching": round(week_pending["level"], 2),
            "referral": round(week_pending["referral"], 2),
            "total": round(sum(week_pending.values()), 2),
        },
        "system_cap": {
            "total_active_invested": round(total_invested, 2),
            "total_cap": round(total_cap, 2),
            "total_used": round(total_used, 2),
            "pct": round((total_used / total_cap * 100) if total_cap > 0 else 0, 2),
            "capped_users": capped_count,
            "near_cap_users": near_cap_count,
            "active_users": len(active_users),
        },
        "last_sweep": last_sweep,
    }
