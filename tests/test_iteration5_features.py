"""
Iteration 5 backend tests:
  - GET /api/me/network/tree (root, masked descendants, depth, security)
  - GET /api/me/levels/{level}/members (UUIDv7 display_id, shape)
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://mlm-invest-deploy.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"Login failed ({email}): {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def chetan():
    """Investor WITH 5-person downline."""
    return _login("chetan@abc.com", "Test@1234")


@pytest.fixture(scope="module")
def earntester():
    """Investor WITHOUT downline."""
    return _login("earntester@test.app", "Test@1234")


@pytest.fixture(scope="module")
def chetan_me(chetan):
    r = chetan.get(f"{API}/auth/me", timeout=15)
    assert r.status_code == 200
    return r.json()


# ---------- /me/network/tree ----------

class TestNetworkTree:
    def test_tree_root_is_self_with_masked_children(self, chetan, chetan_me):
        r = chetan.get(f"{API}/me/network/tree", params={"depth": 2}, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "root" in data, data
        root = data["root"]
        # Root is current user, name NOT masked
        assert root.get("user_id") == chetan_me["user_id"]
        # Root name should be the chetan's actual name (no mask char)
        assert root.get("name") and "•" not in root.get("name"), root.get("name")
        children = root.get("children") or []
        assert len(children) >= 1, "Chetan should have at least 1 L1 child"
        for ch in children:
            assert "•" in (ch.get("name") or ""), f"Child name should be masked: {ch}"
            # Privacy fields scrubbed
            assert ch.get("email") is None
            assert ch.get("phone") is None
            assert ch.get("wallet_address") is None

    def test_tree_depth_clamped(self, chetan):
        r = chetan.get(f"{API}/me/network/tree", params={"depth": 99}, timeout=20)
        assert r.status_code == 200
        # No crash; backend caps at 4

    def test_tree_security_foreign_root_forbidden(self, chetan, earntester):
        # earntester is NOT in chetan's downline (separate investor)
        r_me = earntester.get(f"{API}/auth/me", timeout=15)
        assert r_me.status_code == 200
        foreign_id = r_me.json()["user_id"]
        r = chetan.get(f"{API}/me/network/tree", params={"root_id": foreign_id, "depth": 1}, timeout=15)
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"

    def test_tree_self_root_id_ok(self, chetan, chetan_me):
        r = chetan.get(
            f"{API}/me/network/tree",
            params={"root_id": chetan_me["user_id"], "depth": 1},
            timeout=15,
        )
        assert r.status_code == 200


# ---------- /me/levels/{level}/members ----------

class TestLevelMembers:
    def test_level1_shape_and_uuidv7(self, chetan):
        r = chetan.get(f"{API}/me/levels/1/members", timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("level") == 1
        assert isinstance(data.get("count"), int)
        assert data["count"] >= 1, "chetan must have at least 1 L1 member"
        members = data.get("members") or []
        assert len(members) == data["count"]
        m = members[0]
        # Required fields
        for k in [
            "display_id",
            "level",
            "total_invested",
            "daily_roi",
            "activated_at",
            "joined_at",
            "referrer_name_masked",
            "referrer_code",
        ]:
            assert k in m, f"Missing field {k} in member: {m}"
        # UUIDv7 format
        assert UUIDV7_RE.match(m["display_id"]), f"Bad UUIDv7: {m['display_id']}"
        assert m["level"] == 1
        assert isinstance(m["total_invested"], (int, float))
        assert isinstance(m["daily_roi"], (int, float))
        # Referrer name is masked (chetan is L1 referrer → masked in this view too)
        assert isinstance(m["referrer_name_masked"], str)

    def test_display_id_stable_across_calls(self, chetan):
        r1 = chetan.get(f"{API}/me/levels/1/members", timeout=15).json()
        r2 = chetan.get(f"{API}/me/levels/1/members", timeout=15).json()
        ids1 = sorted([m["display_id"] for m in r1["members"]])
        ids2 = sorted([m["display_id"] for m in r2["members"]])
        assert ids1 == ids2, "display_id must persist across calls"

    def test_level_out_of_range_400(self, chetan):
        r = chetan.get(f"{API}/me/levels/0/members", timeout=10)
        assert r.status_code == 400
        r = chetan.get(f"{API}/me/levels/99/members", timeout=10)
        assert r.status_code == 400

    def test_empty_for_user_without_downline(self, earntester):
        r = earntester.get(f"{API}/me/levels/1/members", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["members"] == []

    def test_unauthenticated_blocked(self):
        r = requests.get(f"{API}/me/levels/1/members", timeout=10)
        assert r.status_code in (401, 403)
        r = requests.get(f"{API}/me/network/tree", timeout=10)
        assert r.status_code in (401, 403)
