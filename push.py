"""Web Push (RFC 8030) + VAPID — Trade Gain Capital.

Self-contained module wrapping pywebpush. Owns:
  * VAPID keypair lifecycle (auto-generated and cached in db.app_config)
  * Subscription persistence (db.push_subscriptions)
  * Campaign + send loop (db.push_campaigns) with delivery stats
  * Auto-cleanup of expired subscriptions on 404 / 410
"""
from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

logger = logging.getLogger("tradegain.push")

# RFC 8030: VAPID 'sub' is mailto: or https://. Used as contact for push gateways.
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@tradegain.app")
TTL_SECONDS = 3600 * 24  # default push retention at the gateway

# ---------------------------------------------------------------------------
# VAPID key management
# ---------------------------------------------------------------------------

def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _gen_vapid_keys() -> tuple[str, str]:
    """Generate a fresh VAPID P-256 keypair.

    Returns (public_url_b64, private_url_b64). Public key is the
    raw uncompressed 65-byte EC point base64url-encoded — that's the
    format the browser expects in `applicationServerKey`. Private key is
    the raw 32-byte scalar base64url-encoded — the format
    `py_vapid.Vapid.from_string` accepts, which is what `pywebpush` calls
    internally when given a string `vapid_private_key`.
    """
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_numbers = private_key.private_numbers()
    public_numbers = private_key.public_key().public_numbers()
    raw_public = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    raw_private = private_numbers.private_value.to_bytes(32, "big")
    return _b64url_encode(raw_public), _b64url_encode(raw_private)


def _maybe_migrate_pem_to_raw(private_val: str) -> Optional[str]:
    """If a previously persisted private key is a PKCS8 PEM (old format),
    extract the raw 32-byte scalar and return it as base64url. Returns
    None if the value is already raw (or unparseable)."""
    if not private_val or "BEGIN" not in private_val:
        return None
    try:
        pk = serialization.load_pem_private_key(private_val.encode("ascii"), password=None, backend=default_backend())
        d = pk.private_numbers().private_value.to_bytes(32, "big")
        return _b64url_encode(d)
    except Exception:  # noqa: BLE001
        return None


async def get_vapid_keys(db) -> Dict[str, str]:
    """Read VAPID keys from db.app_config or mint + persist on first call.

    Auto-migrates legacy PKCS8 PEM private keys (which `pywebpush` cannot
    consume directly) to the raw 32-byte base64url form expected by
    `py_vapid.Vapid.from_string`. Public key is preserved across the
    migration so existing browser subscriptions remain valid.
    """
    doc = await db.app_config.find_one({"_id": "vapid"})
    if doc and doc.get("public") and doc.get("private"):
        private_val = doc["private"]
        # One-time migration: previous versions stored a PKCS8 PEM here.
        migrated = _maybe_migrate_pem_to_raw(private_val)
        if migrated:
            await db.app_config.update_one(
                {"_id": "vapid"},
                {"$set": {"private": migrated, "migrated_at": datetime.now(timezone.utc).isoformat()}},
            )
            logger.info("Migrated VAPID private key from PKCS8 PEM to raw b64url")
            private_val = migrated
        return {"public": doc["public"], "private": private_val}
    public, private = _gen_vapid_keys()
    await db.app_config.update_one(
        {"_id": "vapid"},
        {"$set": {
            "public": public,
            "private": private,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    logger.info("Generated new VAPID keypair (one-time)")
    return {"public": public, "private": private}


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

async def _send_one(db, sub: Dict[str, Any], payload: Dict[str, Any], vapid_private: str) -> Dict[str, Any]:
    """Send a single notification.

    Returns {ok: bool, status: int|None, reason: str|None}.
    Auto-deletes the subscription on 404/410 (subscription gone).
    """
    subscription_info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
        resp = webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=vapid_private,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=TTL_SECONDS,
        )
        return {"ok": True, "status": getattr(resp, "status_code", 201), "reason": None}
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            # Subscription expired — purge it.
            await db.push_subscriptions.delete_one({"sub_id": sub["sub_id"]})
            return {"ok": False, "status": status, "reason": "expired_deleted"}
        logger.warning("webpush failed sub=%s status=%s err=%s", sub.get("sub_id"), status, str(e)[:200])
        return {"ok": False, "status": status, "reason": str(e)[:160]}
    except Exception as e:  # noqa: BLE001
        logger.exception("webpush unexpected error sub=%s: %s", sub.get("sub_id"), e)
        return {"ok": False, "status": None, "reason": f"{type(e).__name__}: {str(e)[:140]}"}


def build_payload(
    *,
    title: str,
    body: str,
    icon: Optional[str] = None,
    image: Optional[str] = None,
    cta_url: Optional[str] = None,
    campaign_id: Optional[str] = None,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Standardized payload contract — the service worker reads these keys."""
    payload = {
        "title": title[:80],
        "body": body[:240],
        "icon": icon or "/tg-logo.png",
        "badge": "/tg-logo.png",
        "url": cta_url or "/",
        "campaign_id": campaign_id,
        "tag": tag or campaign_id or "tradegain",
    }
    if image:
        payload["image"] = image
    return payload


# ---------------------------------------------------------------------------
# Targeting + send-all
# ---------------------------------------------------------------------------

async def _subscriptions_for_target(db, target_type: str, target_user_id: Optional[str]) -> List[Dict[str, Any]]:
    if target_type == "user" and target_user_id:
        cursor = db.push_subscriptions.find({"user_id": target_user_id}, {"_id": 0})
    else:
        cursor = db.push_subscriptions.find({}, {"_id": 0})
    return await cursor.to_list(None)


async def send_campaign(db, campaign: Dict[str, Any]) -> Dict[str, int]:
    """Run a campaign's send loop. Updates the campaign doc with results.

    Returns {sent, failed, total}.
    """
    keys = await get_vapid_keys(db)
    subs = await _subscriptions_for_target(db, campaign.get("target_type", "all"), campaign.get("target_user_id"))
    sent = 0
    failed = 0
    payload = build_payload(
        title=campaign["title"],
        body=campaign["body"],
        icon=campaign.get("icon"),
        image=campaign.get("image"),
        cta_url=f"/api/push/click/{campaign['campaign_id']}",
        campaign_id=campaign["campaign_id"],
    )
    for sub in subs:
        res = await _send_one(db, sub, payload, keys["private"])
        if res["ok"]:
            sent += 1
        else:
            failed += 1
    await db.push_campaigns.update_one(
        {"campaign_id": campaign["campaign_id"]},
        {"$set": {
            "status": "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "sent_count": sent,
            "failed_count": failed,
            "total_targets": len(subs),
        }},
    )
    logger.info("Campaign %s sent: %d/%d delivered", campaign["campaign_id"], sent, len(subs))
    return {"sent": sent, "failed": failed, "total": len(subs)}


# ---------------------------------------------------------------------------
# Auto-trigger (lightweight: writes a campaign, then sends immediately)
# ---------------------------------------------------------------------------

async def push_event(
    db,
    *,
    event: str,
    title: str,
    body: str,
    user_id: Optional[str] = None,
    cta_url: Optional[str] = None,
) -> None:
    """Fire-and-forget helper for system-generated notifications.
    Creates a campaign doc (for analytics) and sends immediately."""
    cid = f"camp_{uuid.uuid4().hex[:12]}"
    campaign = {
        "campaign_id": cid,
        "title": title,
        "body": body,
        "icon": None,
        "cta_url": cta_url,
        "target_type": "user" if user_id else "all",
        "target_user_id": user_id,
        "status": "scheduled",
        "scheduled_at": datetime.now(timezone.utc).isoformat(),
        "created_by": "system",
        "system_event": event,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sent_count": 0,
        "failed_count": 0,
        "click_count": 0,
        "total_targets": 0,
    }
    await db.push_campaigns.insert_one(campaign)
    try:
        await send_campaign(db, campaign)
    except Exception as e:  # noqa: BLE001
        logger.exception("push_event %s failed: %s", event, e)
