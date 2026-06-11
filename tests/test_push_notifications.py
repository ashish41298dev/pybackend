"""Backend tests for Web Push Notifications (RFC 8030 + VAPID)."""
import os
import time
from datetime import datetime, timezone, timedelta

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://mlm-invest-deploy.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@tradegain.app"
ADMIN_PASS = "Admin@1234"
INVESTOR_EMAIL = "earntester@test.app"
INVESTOR_PASS = "Test@1234"


# --- Fixtures --------------------------------------------------------------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def investor_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": INVESTOR_EMAIL, "password": INVESTOR_PASS}, timeout=20)
    assert r.status_code == 200, f"Investor login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def anon_session():
    return requests.Session()


# --- VAPID public key ------------------------------------------------------
class TestVapidKey:
    def test_vapid_public_key_returns_non_empty(self, anon_session):
        r = anon_session.get(f"{API}/push/vapid-public-key", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "public_key" in data
        assert isinstance(data["public_key"], str)
        assert len(data["public_key"]) > 20  # P-256 raw key b64url ~ 87 chars

    def test_vapid_public_key_stable(self, anon_session):
        r1 = anon_session.get(f"{API}/push/vapid-public-key", timeout=15).json()
        r2 = anon_session.get(f"{API}/push/vapid-public-key", timeout=15).json()
        assert r1["public_key"] == r2["public_key"], "VAPID key must be stable across requests"


# --- Admin RBAC ------------------------------------------------------------
class TestAdminRBAC:
    def test_investor_forbidden_campaigns_list(self, investor_session):
        r = investor_session.get(f"{API}/admin/push/campaigns", timeout=15)
        assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    def test_investor_forbidden_subscribers(self, investor_session):
        r = investor_session.get(f"{API}/admin/push/subscribers", timeout=15)
        assert r.status_code == 403

    def test_investor_forbidden_stats(self, investor_session):
        r = investor_session.get(f"{API}/admin/push/stats", timeout=15)
        assert r.status_code == 403

    def test_investor_forbidden_create_campaign(self, investor_session):
        r = investor_session.post(f"{API}/admin/push/campaigns", json={
            "title": "TEST_hack", "body": "x", "target_type": "all"
        }, timeout=15)
        assert r.status_code == 403


# --- Admin: stats endpoint --------------------------------------------------
class TestAdminStats:
    def test_stats_shape(self, admin_session):
        r = admin_session.get(f"{API}/admin/push/stats", timeout=15)
        assert r.status_code == 200
        data = r.json()
        for k in ("total_subscribers", "campaigns_total", "sent_total", "failed_total", "click_total"):
            assert k in data, f"Missing key {k}"
            assert isinstance(data[k], int)

    def test_subscribers_list_is_array(self, admin_session):
        r = admin_session.get(f"{API}/admin/push/subscribers", timeout=15)
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# --- Compose / send-now flow -----------------------------------------------
class TestComposeSendNow:
    created_id = None

    def test_create_send_now_campaign(self, admin_session):
        payload = {
            "title": "TEST_send_now",
            "body": "Send now flow",
            "cta_url": "/dashboard",
            "target_type": "all",
        }
        r = admin_session.post(f"{API}/admin/push/campaigns", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d["status"] == "draft"
        assert d["campaign_id"].startswith("camp_")
        TestComposeSendNow.created_id = d["campaign_id"]

    def test_send_campaign(self, admin_session):
        cid = TestComposeSendNow.created_id
        assert cid is not None
        r = admin_session.post(f"{API}/admin/push/campaigns/{cid}/send", timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        for k in ("sent", "failed", "total"):
            assert k in d
            assert isinstance(d[k], int)
        # With likely 0 subscribers — total=0, sent=0 is acceptable
        assert d["sent"] >= 0
        assert d["total"] >= 0

    def test_send_again_400(self, admin_session):
        cid = TestComposeSendNow.created_id
        r = admin_session.post(f"{API}/admin/push/campaigns/{cid}/send", timeout=15)
        assert r.status_code == 400, f"Expected 400 already-sent, got {r.status_code}"

    def test_status_now_sent(self, admin_session):
        cid = TestComposeSendNow.created_id
        r = admin_session.get(f"{API}/admin/push/campaigns/{cid}", timeout=15)
        assert r.status_code == 200
        c = r.json()
        assert c["status"] == "sent"
        assert "sent_count" in c and "failed_count" in c and "click_count" in c

    def test_list_sent_includes(self, admin_session):
        r = admin_session.get(f"{API}/admin/push/campaigns", params={"status_filter": "sent"}, timeout=15)
        assert r.status_code == 200
        items = r.json()
        ids = [c["campaign_id"] for c in items]
        assert TestComposeSendNow.created_id in ids


# --- Schedule for later flow ------------------------------------------------
class TestScheduled:
    created_id = None

    def test_create_scheduled_campaign(self, admin_session):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        payload = {
            "title": "TEST_scheduled",
            "body": "Scheduled flow",
            "cta_url": "/dashboard",
            "target_type": "all",
            "schedule_at": future,
        }
        r = admin_session.post(f"{API}/admin/push/campaigns", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "scheduled"
        TestScheduled.created_id = d["campaign_id"]

    def test_appears_in_scheduled_list(self, admin_session):
        r = admin_session.get(f"{API}/admin/push/campaigns", params={"status_filter": "scheduled"}, timeout=15)
        assert r.status_code == 200
        ids = [c["campaign_id"] for c in r.json()]
        assert TestScheduled.created_id in ids

    def test_send_scheduled_immediately(self, admin_session):
        cid = TestScheduled.created_id
        r = admin_session.post(f"{API}/admin/push/campaigns/{cid}/send", timeout=30)
        assert r.status_code == 200
        # Verify moved to sent
        d = admin_session.get(f"{API}/admin/push/campaigns/{cid}", timeout=15).json()
        assert d["status"] == "sent"


# --- Click tracking ---------------------------------------------------------
class TestClickTracking:
    def test_click_redirect_and_increment(self, admin_session, anon_session):
        # Create a campaign
        payload = {
            "title": "TEST_click",
            "body": "Click flow",
            "cta_url": "/dashboard",
            "target_type": "all",
        }
        r = admin_session.post(f"{API}/admin/push/campaigns", json=payload, timeout=15)
        assert r.status_code == 200
        cid = r.json()["campaign_id"]

        # Click endpoint - should 302 redirect (no auth required, no follow)
        r = anon_session.get(f"{API}/push/click/{cid}", allow_redirects=False, timeout=15)
        assert r.status_code in (302, 307), f"Expected redirect, got {r.status_code}"
        location = r.headers.get("location", "")
        assert "/dashboard" in location or location.endswith("/dashboard")

        # Verify click_count incremented
        c = admin_session.get(f"{API}/admin/push/campaigns/{cid}", timeout=15).json()
        assert c["click_count"] >= 1

        # cleanup
        admin_session.delete(f"{API}/admin/push/campaigns/{cid}", timeout=15)


# --- Delete -----------------------------------------------------------------
class TestDelete:
    def test_delete_campaign(self, admin_session):
        r = admin_session.post(f"{API}/admin/push/campaigns", json={
            "title": "TEST_delete", "body": "to delete", "target_type": "all"
        }, timeout=15)
        cid = r.json()["campaign_id"]
        r = admin_session.delete(f"{API}/admin/push/campaigns/{cid}", timeout=15)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Verify gone
        r = admin_session.get(f"{API}/admin/push/campaigns/{cid}", timeout=15)
        assert r.status_code == 404


# --- Cleanup of test data ---------------------------------------------------
def test_cleanup_test_campaigns(admin_session=None):
    """Best-effort cleanup of remaining TEST_ campaigns."""
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
    if r.status_code != 200:
        return
    items = s.get(f"{API}/admin/push/campaigns", timeout=15).json()
    for c in items:
        if isinstance(c, dict) and c.get("title", "").startswith("TEST_"):
            s.delete(f"{API}/admin/push/campaigns/{c['campaign_id']}", timeout=15)
