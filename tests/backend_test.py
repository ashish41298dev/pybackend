"""TradeGain Capital backend regression tests.

Covers: auth (register/login/me/logout), plans CRUD + RBAC, investments lifecycle,
leads, admin overview. Uses real HTTP against the public preview URL with cookies.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else "https://mlm-invest-deploy.preview.emergentagent.com"
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@tradegain.app"
ADMIN_PASS = "Admin@1234"


# ---------- shared fixtures ----------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    assert r.json()["user"]["role"] == "admin"
    return s


@pytest.fixture(scope="session")
def investor_creds():
    return {
        "name": "TEST Investor",
        "email": f"test_inv_{uuid.uuid4().hex[:8]}@tradegain.app",
        "password": "Test@1234",
    }


@pytest.fixture(scope="session")
def investor_session(investor_creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json=investor_creds, timeout=20)
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    body = r.json()
    assert body["user"]["email"] == investor_creds["email"].lower()
    assert body["user"]["role"] == "investor"
    assert "session_token" in s.cookies, "session_token cookie not set on register"
    return s


# ---------- AUTH ----------
class TestAuth:
    def test_register_sets_cookie(self, investor_session):
        # cookie was set in fixture; verify /auth/me works
        r = investor_session.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 200
        assert r.json()["role"] == "investor"

    def test_admin_login_role(self, admin_session):
        r = admin_session.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "admin"
        assert data["email"] == ADMIN_EMAIL

    def test_login_invalid(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"}, timeout=15)
        assert r.status_code == 401

    def test_me_requires_auth(self):
        r = requests.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 401

    def test_logout_clears_cookie(self, investor_creds):
        s = requests.Session()
        s.post(f"{API}/auth/login", json={"email": investor_creds["email"], "password": investor_creds["password"]}, timeout=15)
        assert "session_token" in s.cookies
        r = s.post(f"{API}/auth/logout", timeout=15)
        assert r.status_code == 200
        # After logout, /auth/me should fail when no cookie sent
        s2 = requests.Session()
        assert s2.get(f"{API}/auth/me", timeout=15).status_code == 401


# ---------- PLANS ----------
class TestPlans:
    def test_list_seeded_plans(self):
        r = requests.get(f"{API}/plans", timeout=15)
        assert r.status_code == 200
        plans = r.json()
        assert len(plans) >= 6, f"expected >=6 seeded plans, got {len(plans)}"
        sort_orders = [p["sort_order"] for p in plans]
        assert sort_orders == sorted(sort_orders), "plans not sorted by sort_order"

    def test_admin_crud_plan(self, admin_session):
        # create
        payload = {
            "name": "TEST_Plan",
            "min_amount": 100,
            "max_amount": 500,
            "daily_roi": 0.006,
            "working_days": 300,
            "badge": "TEST",
            "blurb": "test plan",
            "is_active": True,
            "sort_order": 99,
        }
        r = admin_session.post(f"{API}/plans", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        plan = r.json()
        pid = plan["plan_id"]
        assert plan["name"] == "TEST_Plan"

        # update
        payload["name"] = "TEST_Plan_Updated"
        r = admin_session.put(f"{API}/plans/{pid}", json=payload, timeout=15)
        assert r.status_code == 200
        assert r.json()["name"] == "TEST_Plan_Updated"

        # GET verify
        r = requests.get(f"{API}/plans", timeout=15)
        names = [p["name"] for p in r.json()]
        assert "TEST_Plan_Updated" in names

        # delete
        r = admin_session.delete(f"{API}/plans/{pid}", timeout=15)
        assert r.status_code == 200

    def test_non_admin_cannot_create_plan(self, investor_session):
        payload = {"name": "BadPlan", "min_amount": 100, "max_amount": 200, "daily_roi": 0.005, "working_days": 100, "is_active": True, "sort_order": 0}
        r = investor_session.post(f"{API}/plans", json=payload, timeout=15)
        assert r.status_code == 403

    def test_anonymous_cannot_create_plan(self):
        r = requests.post(f"{API}/plans", json={"name": "x", "min_amount": 100, "max_amount": 200, "daily_roi": 0.005, "working_days": 100, "is_active": True, "sort_order": 0}, timeout=15)
        assert r.status_code == 401


# ---------- INVESTMENTS ----------
class TestInvestments:
    def test_create_investment_and_my_list(self, investor_session):
        plans = requests.get(f"{API}/plans", timeout=15).json()
        p = plans[0]  # starter $100-$199
        amount = (p["min_amount"] + p["max_amount"]) / 2
        r = investor_session.post(
            f"{API}/investments",
            json={"plan_id": p["plan_id"], "amount": amount, "tx_hash": "TESTTXHASH123456", "network": "TRC20"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["status"] == "pending"
        assert inv["amount"] == amount

        # my investments
        r = investor_session.get(f"{API}/investments/me", timeout=15)
        assert r.status_code == 200
        ids = [i["investment_id"] for i in r.json()]
        assert inv["investment_id"] in ids

    def test_invest_amount_out_of_range(self, investor_session):
        plans = requests.get(f"{API}/plans", timeout=15).json()
        p = plans[0]
        r = investor_session.post(
            f"{API}/investments",
            json={"plan_id": p["plan_id"], "amount": p["max_amount"] + 100000, "tx_hash": "BADTXHASH123", "network": "TRC20"},
            timeout=15,
        )
        assert r.status_code == 400

    def test_admin_list_all_and_filter(self, admin_session):
        r = admin_session.get(f"{API}/investments?status_filter=pending", timeout=15)
        assert r.status_code == 200
        rows = r.json()
        assert all(i["status"] == "pending" for i in rows)

    def test_non_admin_cannot_list_all(self, investor_session):
        r = investor_session.get(f"{API}/investments", timeout=15)
        assert r.status_code == 403

    def test_admin_decision_activates(self, admin_session):
        rows = admin_session.get(f"{API}/investments?status_filter=pending", timeout=15).json()
        assert rows, "expected at least one pending investment"
        inv_id = rows[0]["investment_id"]
        r = admin_session.post(
            f"{API}/investments/{inv_id}/decision",
            json={"status": "active", "admin_note": "ok"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "active"
        assert body["activated_at"]


# ---------- LEADS ----------
class TestLeads:
    def test_public_create_lead(self):
        r = requests.post(
            f"{API}/leads",
            json={"email": f"test_lead_{uuid.uuid4().hex[:6]}@tradegain.app", "ticket_size": "$10k+", "note": "TEST"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ticket_size"] == "$10k+"

    def test_anonymous_cannot_list_leads(self):
        r = requests.get(f"{API}/leads", timeout=15)
        assert r.status_code == 401

    def test_admin_lists_leads(self, admin_session):
        r = admin_session.get(f"{API}/leads", timeout=15)
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------- ADMIN OVERVIEW ----------
class TestAdminOverview:
    def test_overview_counts(self, admin_session):
        r = admin_session.get(f"{API}/admin/overview", timeout=15)
        assert r.status_code == 200
        body = r.json()
        for k in ("total_users", "total_investments", "pending_investments", "active_investments", "active_capital", "total_leads"):
            assert k in body

    def test_non_admin_overview_forbidden(self, investor_session):
        r = investor_session.get(f"{API}/admin/overview", timeout=15)
        assert r.status_code == 403
