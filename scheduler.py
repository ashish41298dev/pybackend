"""Daily payout scheduler — runs the Trade Gain Capital payout engine.

Cron schedule (IST):
  • 00:05 daily — accrue Direct ROI + Level Income for the day, queue referrals
  • 00:30 Saturday — sweep all locked earnings into weekly_payouts

Admin can also trigger both manually via the existing /api/admin/payouts/sweep
endpoint plus the new /api/admin/payouts/run-daily endpoint.
"""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import earnings as earnings_engine
import push as push_module

logger = logging.getLogger("scheduler")

IST = pytz.timezone("Asia/Kolkata")

_scheduler: AsyncIOScheduler | None = None


async def _daily_job(db):
    """Run the daily payout job — idempotent, safe to re-run."""
    today_ist = datetime.now(IST).date()
    try:
        logger.info("scheduler: starting daily accrual for %s (IST)", today_ist)
        result = await earnings_engine.accrue_daily_all(db, today_ist)
        logger.info("scheduler: daily accrual done %s", result)
        # If today (IST) is Saturday, also run the sweep + push notifications
        if earnings_engine.is_saturday(today_ist):
            logger.info("scheduler: Saturday detected — running weekly sweep")
            sweep_result = await earnings_engine.sweep_weekly_payouts(db, today_ist)
            logger.info("scheduler: sweep done %s", sweep_result)
            # Push every user who received a payout this week
            try:
                await _push_weekly_payouts(db, today_ist)
            except Exception as e:
                logger.exception("scheduler: weekly payout push failed: %s", e)
        # Cap-reached watch — push once when a user crosses 2X
        try:
            await _push_cap_reached(db)
        except Exception as e:
            logger.exception("scheduler: cap-reached push failed: %s", e)
        # Record in audit log
        await db.audit_log.insert_one({
            "audit_id": f"aud_sched_{today_ist.isoformat()}",
            "ts": earnings_engine.iso(earnings_engine.now_utc()),
            "actor": "scheduler",
            "action": "scheduler.daily_run",
            "target_user_id": None,
            "amount": result.get("total_roi", 0) + result.get("total_level", 0),
            "meta": {"date": today_ist.isoformat(), "result": result},
        })
    except Exception as e:
        logger.exception("scheduler: daily job failed: %s", e)


async def _push_weekly_payouts(db, today_ist):
    """Notify every investor who received a payout on `today_ist`."""
    iso_date = today_ist.isoformat()
    cursor = db.weekly_payouts.find({"week_end_date": iso_date}, {"_id": 0})
    async for p in cursor:
        try:
            await push_module.push_event(
                db,
                event="payout.settled",
                title="Weekly payout settled",
                body=f"{round(p.get('total', 0), 2)} USDT just landed in your wallet for this week.",
                user_id=p.get("user_id"),
                cta_url="/dashboard/payouts",
            )
        except Exception as e:
            logger.warning("payout push failed for %s: %s", p.get("user_id"), e)


async def _push_cap_reached(db):
    """Send a one-time cap-reached push to any user who just crossed 2X.

    We dedupe by writing `cap_push_sent: true` on the user record. Subsequent
    deposits reset this flag at admin-decide time.
    """
    cursor = db.users.find({"cap_push_sent": {"$ne": True}}, {"_id": 0, "user_id": 1})
    async for u in cursor:
        try:
            summary = await earnings_engine.user_earnings_summary(db, u["user_id"])
            cap = summary.get("cap") or {}
            if cap.get("reached") or (cap.get("pct", 0) >= 100):
                await push_module.push_event(
                    db,
                    event="cap.reached",
                    title="2X cap reached",
                    body="You've hit your 2X cap on current capital. Open a new deposit to keep earning.",
                    user_id=u["user_id"],
                    cta_url="/dashboard/invest",
                )
                await db.users.update_one(
                    {"user_id": u["user_id"]},
                    {"$set": {"cap_push_sent": True}},
                )
        except Exception as e:
            logger.warning("cap watch push failed for %s: %s", u.get("user_id"), e)


async def _dispatch_scheduled_pushes(db):
    """Send any push campaigns whose scheduled_at <= now (UTC)."""
    now_iso = earnings_engine.iso(earnings_engine.now_utc())
    cursor = db.push_campaigns.find({
        "status": "scheduled",
        "scheduled_at": {"$lte": now_iso},
    }, {"_id": 0})
    async for c in cursor:
        try:
            logger.info("dispatching scheduled push campaign %s", c.get("campaign_id"))
            await push_module.send_campaign(db, c)
        except Exception as e:
            logger.exception("scheduled push dispatch failed campaign=%s err=%s", c.get("campaign_id"), e)


def start_scheduler(db) -> AsyncIOScheduler:
    """Start the daily APScheduler. Idempotent — second call is a no-op."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler(timezone=IST)

    # Daily at 00:05 IST (covers Mon-Sat; Sun is naturally non-working)
    sched.add_job(
        _daily_job,
        CronTrigger(hour=0, minute=5, timezone=IST),
        args=[db],
        id="tg_daily_payout",
        name="Trade Gain — Daily payout accrual",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )

    sched.start()
    _scheduler = sched
    # Scheduled push campaigns: dispatch every minute
    sched.add_job(
        _dispatch_scheduled_pushes,
        CronTrigger(minute="*"),
        args=[db],
        id="tg_push_dispatch",
        name="Trade Gain — Scheduled push dispatch",
        misfire_grace_time=120,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("scheduler: APScheduler started — daily payout job registered (00:05 IST) + push dispatch (every minute)")
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def trigger_now(db) -> dict:
    """Run the daily job immediately (admin-triggered)."""
    today_ist = datetime.now(IST).date()
    return await earnings_engine.accrue_daily_all(db, today_ist)
