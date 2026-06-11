"""Regression tests for the 9 UX/feature tweaks batch.

Covers:
  1. PWA assets (/manifest.json, /sw.js) reachable + valid
  2. POST /api/investments with amount > 100000 → 400 (single deposit cap)
  3. POST /api/plans with max_amount > 100000 → validation error (422)
  4. GET /api/admin/audit-log returns rows, including auth.login + investment.created
  5. login produces a fresh audit.login row with target_user_id and meta.email
"""
import os
import uuid
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else "https://mlm-invest-deploy.preview.emergentagent.com"
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@tradegain.app"
ADMIN_PASS = "Admin@1234"


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def investor_creds():
    return {
        "name": "TEST Batch9 Investor",
        "email": f"test_batch9_{uuid.uuid4().hex[:8]}@tradegain.app",
        "password": "Test@1234",
    }


@pytest.fixture(scope="module")
def investor_session(investor_creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json=investor_creds, timeout=20)
    assert r.status_code == 200, r.text
    return s


# ---------- PWA ----------
class TestPWA:
    def test_manifest_reachable(self):
        r = requests.get(f"{BASE_URL}/manifest.json", timeout=15)
        assert r.status_code == 200
        m = r.json()
        assert m["name"]
        assert m["start_url"]
        assert m["theme_color"]
        assert isinstance(m["icons"], list) and len(m["icons"]) >= 1
        for ic in m["icons"]:
            assert "src" in ic and ic["src"].startswith("http")
            assert "sizes" in ic

    def test_service_worker_reachable(self):
        r = requests.get(f"{BASE_URL}/sw.js", timeout=15)
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "").lower()
        assert len(r.text) > 50

    def test_index_html_has_manifest_link(self):
        r = requests.get(f"{BASE_URL}/", timeout=15)
        assert r.status_code == 200
        assert 'rel="manifest"' in r.text


# ---------- Investment 100k cap ----------
class TestInvestmentCap:
    def test_investment_above_100k_rejected(self, investor_session):
        plans = requests.get(f"{API}/plans", timeout=15).json()
        # pick the one with the highest max so amount=200000 only fails due to global cap
        p = max(plans, key=lambda x: x["max_amount"])
        r = investor_session.post(
            f"{API}/investments",
            json={"plan_id": p["plan_id"], "amount": 200000, "tx_hash": "TESTCAP200K", "network": "TRC20"},
            timeout=15,
        )
        assert r.status_code == 400, f"expected 400 got {r.status_code} body={r.text}"
        body = r.json()
        detail = (body.get("detail") or "").lower()
        assert "100,000" in detail or "100000" in detail or "maximum" in detail, body

    def test_investment_exactly_100k_within_plan_ok(self, investor_session):
        # boundary check — we don't actually need this to succeed (plan max may be lower)
        # but the rejection above must not be off-by-one. Skip if no plan supports >=100000.
        plans = requests.get(f"{API}/plans", timeout=15).json()
        suitable = [p for p in plans if p["max_amount"] >= 100000]
        if not suitable:
            pytest.skip("no plan accepts 100k")
        p = suitable[0]
        r = investor_session.post(
            f"{API}/investments",
            json={"plan_id": p["plan_id"], "amount": 100000, "tx_hash": f"TEST100K_{uuid.uuid4().hex[:6]}", "network": "TRC20"},
            timeout=15,
        )
        assert r.status_code == 200, r.text


# ---------- Plan 100k validation ----------
class TestPlanCap:
    def test_plan_max_above_100k_rejected(self, admin_session):
        payload = {
            "name": "TEST_BadCapPlan",
            "min_amount": 100,
            "max_amount": 500000,
            "daily_roi": 0.005,
            "working_days": 300,
            "is_active": True,
            "sort_order": 999,
        }
        r = admin_session.post(f"{API}/plans", json=payload, timeout=15)
        # Pydantic Field(le=100000) → 422; if backend coerced to 400 also acceptable
        assert r.status_code in (400, 422), f"expected 400/422 got {r.status_code} body={r.text}"

    def test_plan_min_below_100_rejected(self, admin_session):
        payload = {
            "name": "TEST_BadMinPlan",
            "min_amount": 50,
            "max_amount": 200,
            "daily_roi": 0.005,
            "working_days": 300,
            "is_active": True,
            "sort_order": 999,
        }
        r = admin_session.post(f"{API}/plans", json=payload, timeout=15)
        assert r.status_code in (400, 422), r.text


# ---------- Audit log ----------
class TestAuditLog:
    def test_audit_requires_admin(self, investor_session):
        r = investor_session.get(f"{API}/admin/audit-log", timeout=15)
        assert r.status_code == 403

    def test_audit_anonymous_forbidden(self):
        r = requests.get(f"{API}/admin/audit-log", timeout=15)
        assert r.status_code in (401, 403)

    def test_login_creates_audit_row(self, admin_session, investor_creds):
        # trigger a fresh login
        s = requests.Session()
        before = time.time()
        r = s.post(f"{API}/auth/login", json={"email": investor_creds["email"], "password": investor_creds["password"]}, timeout=15)
        assert r.status_code == 200
        user_id = r.json()["user"]["user_id"]
        # give backend a moment to persist
        time.sleep(0.5)
        rows = admin_session.get(f"{API}/admin/audit-log?limit=200", timeout=15).json()
        assert isinstance(rows, list) and rows, "audit log empty"
        # find a login row for this user with email meta
        matches = [
            x for x in rows
            if x.get("action") == "auth.login"
            and x.get("target_user_id") == user_id
            and (x.get("meta") or {}).get("email", "").lower() == investor_creds["email"].lower()
        ]
        assert matches, f"no auth.login row for {investor_creds['email']} (user_id={user_id}) in latest {len(rows)} rows"

    def test_audit_has_diverse_actions(self, admin_session):
        rows = admin_session.get(f"{API}/admin/audit-log?limit=500", timeout=15).json()
        actions = {r.get("action") for r in rows}
        # at minimum, login + register + something else from this session
        assert "auth.login" in actions, actions
        assert "auth.register" in actions, actions
        # investment.created or plan.created — at least one state-change action
        assert actions & {"investment.created", "plan.created", "investment.active", "plan.updated", "auth.password_changed"}, actions
