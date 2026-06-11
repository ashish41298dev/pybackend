"""
Iteration 6 backend tests:
  - GET /api/me/network/tree default view for mid-tree user (upline_view=True)
  - GET /api/me/network/tree default view for top-level user (upline_view=False)
  - Security: foreign root_id → 403; self root_id allowed
  - daily_roi present on every node and equals round(active_capital * 0.005, 2)
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL", "https://mlm-invest-deploy.preview.emergentagent.com"
).rstrip("/")
API = f"{BASE_URL}/api"
DAILY_RATE = 0.005


def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"Login failed ({email}): {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def chetan():
    return _login("chetan@abc.com", "Test@1234")


@pytest.fixture(scope="module")
def dilip():
    return _login("dilip@abc.com", "Test@1234")


@pytest.fixture(scope="module")
def earntester():
    return _login("earntester@test.app", "Test@1234")


def _me(s):
    r = s.get(f"{API}/auth/me", timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _assert_daily_roi(node):
    """daily_roi must equal round(active_capital * 0.005, 2) on every node."""
    assert "daily_roi" in node, f"Missing daily_roi: {node}"
    expected = round(float(node.get("active_capital", 0)) * DAILY_RATE, 2)
    assert float(node["daily_roi"]) == expected, (
        f"daily_roi mismatch on {node.get('user_id')}: "
        f"{node['daily_roi']} != {expected} (cap={node.get('active_capital')})"
    )
    for c in node.get("children") or []:
        _assert_daily_roi(c)


# ---------- Iteration 6: upline-view default ----------

class TestUplineDefaultView:
    def test_mid_tree_user_upline_view(self, dilip):
        me = _me(dilip)
        dilip_id = me["user_id"]
        r = dilip.get(f"{API}/me/network/tree", timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        # Top-level response shape
        assert data.get("upline_view") is True, data
        assert data.get("me_id") == dilip_id
        root = data["root"]
        # Root is the user's referrer (chetan), not self
        assert root.get("user_id") and root["user_id"] != dilip_id
        # Root level 0, name masked (contains '•')
        assert root.get("level") == 0
        assert "•" in (root.get("name") or ""), f"Root name should be masked: {root.get('name')}"
        # Exactly one visible child = me
        children = root.get("children") or []
        assert len(children) == 1, f"Expected exactly 1 child (me), got {len(children)}: {children}"
        me_node = children[0]
        assert me_node.get("user_id") == dilip_id
        assert me_node.get("level") == 1
        # My own name is unmasked
        assert "•" not in (me_node.get("name") or ""), me_node.get("name")
        # has_more_below on wrapper is False (per spec)
        assert root.get("has_more_below") is False
        # daily_roi present everywhere with correct formula
        _assert_daily_roi(root)

    def test_top_level_user_no_upline(self, chetan):
        me = _me(chetan)
        chetan_id = me["user_id"]
        r = chetan.get(f"{API}/me/network/tree", timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("upline_view") is False, data
        assert data.get("me_id") == chetan_id
        root = data["root"]
        assert root.get("user_id") == chetan_id
        # Self name unmasked
        assert root.get("name") and "•" not in root.get("name")
        # daily_roi correct on every node
        _assert_daily_roi(root)

    def test_solo_user_upline_view_handles_no_upline(self, earntester):
        me = _me(earntester)
        my_id = me["user_id"]
        r = earntester.get(f"{API}/me/network/tree", timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        # earntester has no referred_by → upline_view False, self root
        assert data.get("upline_view") is False
        assert data.get("me_id") == my_id
        assert data["root"]["user_id"] == my_id
        _assert_daily_roi(data["root"])


# ---------- Iteration 6: security on root_id ----------

class TestTreeSecurity:
    def test_foreign_root_forbidden(self, dilip, earntester):
        foreign = _me(earntester)["user_id"]
        r = dilip.get(f"{API}/me/network/tree", params={"root_id": foreign, "depth": 1}, timeout=15)
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"

    def test_self_root_id_returns_self_subtree(self, dilip):
        me_id = _me(dilip)["user_id"]
        r = dilip.get(
            f"{API}/me/network/tree",
            params={"root_id": me_id, "depth": 2},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("upline_view") is False
        assert data["root"]["user_id"] == me_id
        # self name unmasked
        assert "•" not in (data["root"].get("name") or "")
        _assert_daily_roi(data["root"])


# ---------- Iteration 6: cap pill numeric correctness ----------

class TestEarningsCapSummary:
    def test_cap_summary_shape_for_dilip(self, dilip):
        r = dilip.get(f"{API}/me/earnings/summary", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        cap = data.get("cap") or {}
        # Required keys for pill rendering
        for k in ["pct", "used", "invested"]:
            assert k in cap, f"Missing cap.{k}: {cap}"
        assert isinstance(cap["pct"], (int, float))
        assert isinstance(cap["used"], (int, float))
        # pct should be non-negative
        assert cap["pct"] >= 0

    def test_cap_summary_no_earnings_user_renders_pill_safely(self, earntester):
        r = earntester.get(f"{API}/me/earnings/summary", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        cap = data.get("cap") or {}
        # Must be numeric (not None/missing) so the pill formatting "X.XX% · Y.YY USDT"
        # never crashes regardless of whether the user has earned yet.
        assert isinstance(cap.get("pct"), (int, float))
        assert isinstance(cap.get("used"), (int, float))
        assert cap["pct"] >= 0
        assert cap["used"] >= 0
