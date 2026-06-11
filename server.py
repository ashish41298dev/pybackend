from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import time
import uuid
import logging
import bcrypt
import jwt
import httpx
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Literal, Dict, Any


def _uuid7() -> str:
    """Generate a UUIDv7 (time-ordered, 48-bit unix-ms timestamp prefix + 74 random bits).
    Used as a public, stable, non-guessable display identifier for investors.
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = os.urandom(10)
    b = bytearray(ms.to_bytes(6, "big") + rand)
    # version 7 → high nibble of byte 6
    b[6] = (b[6] & 0x0F) | 0x70
    # RFC 4122 variant (10xxxxxx) → high bits of byte 8
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr, ConfigDict

import earnings as earnings_engine
import push as push_module
import reports as reports_module
from scheduler import start_scheduler, stop_scheduler, trigger_now as scheduler_trigger_now

# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tradegain")

# --- Critical env vars (must exist in production secrets) -------------------
# We DO NOT silently fall back for these — a missing value is a configuration
# bug that should be surfaced loudly, not hidden behind a random default.
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME")
JWT_SECRET = os.environ.get("JWT_SECRET")



_missing_env = [k for k, v in {
    "MONGO_URL": MONGO_URL,
    "DB_NAME": DB_NAME,
    "JWT_SECRET": JWT_SECRET,
}.items() if not v]
if _missing_env:
    # Log a clear, actionable error before raising so the deployment platform
    # surfaces something more useful than a bare KeyError stack trace.
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("tradegain").error(
        "STARTUP ABORTED — missing required environment variables: %s. "
        "Please ensure these are set in the deployment secrets manager.",
        ", ".join(_missing_env),
    )
    raise RuntimeError(f"Missing required env vars: {', '.join(_missing_env)}")

JWT_ALGO = "HS256"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@tradegain.com").lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@1234")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")
# Comma-separated list of additional origins (Atlas-deployed prod, custom
# domains, etc.). Wildcard "*" disables credentialed CORS automatically.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "")

client = AsyncIOMotorClient(MONGO_URL, srv_service_name='mongodb')
db = client[DB_NAME]

app = FastAPI(title="TradeGain Capital API")

@app.on_event("startup")
async def check_mongo():
    try:
        await client.admin.command("ping")
        print("✅ MongoDB Connected Successfully")
        print(f"📂 Database: {DB_NAME}")
    except Exception as e:
        print("❌ MongoDB Connection Failed:", e)
api = APIRouter(prefix="/api")

# ---- CORS ------------------------------------------------------------------
# In production the React bundle resolves the API base URL to
# `window.location.origin` (see frontend/src/lib/api.js), so requests are
# typically same-origin and CORS is not even invoked. Still, we accept
# multiple origins via CORS_ORIGINS / FRONTEND_URL for any non-browser
# clients, the iframe preview, or edge cases.
#
# Key constraint: browsers reject `Access-Control-Allow-Origin: *` when
# `allow_credentials=True`. So we wildcard ONLY when no specific origins are
# configured, and in that case turn credentialed CORS off (the cookie auth
# still works because requests are same-origin).
_raw_origins = [o.strip() for o in (CORS_ORIGINS or "").split(",") if o.strip()]
if FRONTEND_URL and FRONTEND_URL != "*":
    _raw_origins.append(FRONTEND_URL)
_explicit_origins = [o for o in _raw_origins if o != "*"]
_wildcard_requested = (not _explicit_origins) or ("*" in _raw_origins)

if _explicit_origins and not _wildcard_requested:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(dict.fromkeys(_explicit_origins)),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Wildcard fallback — same-origin auth still works because the frontend
    # resolves the API base URL at runtime to its own origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    email: EmailStr
    name: str
    role: Literal["investor", "admin"] = "investor"
    auth_provider: Literal["password", "google", "both"] = "password"
    wallet_address: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    referral_code: Optional[str] = None
    referred_by: Optional[str] = None
    picture: Optional[str] = None
    is_active: bool = True
    force_password_change: bool = False
    created_at: str


class RegisterPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    wallet_address: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    referral_code: Optional[str] = None


class LoginPayload(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordPayload(BaseModel):
    current_password: Optional[str] = None  # not required when force_password_change=True
    new_password: str = Field(..., min_length=8, max_length=128)


class AdminCreateUserPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    role: Literal["investor", "admin"] = "investor"
    wallet_address: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    force_password_change: bool = True


class AdminUpdateUserPayload(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    role: Optional[Literal["investor", "admin"]] = None
    wallet_address: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    is_active: Optional[bool] = None
    force_password_change: Optional[bool] = None


class AdminResetUserPasswordPayload(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)
    force_change_on_next_login: bool = True



class PlanIn(BaseModel):
    name: str
    min_amount: float = Field(100, ge=100, le=100000)
    max_amount: float = Field(100000, ge=100, le=100000)
    daily_roi: float = 0.005
    working_days: int = 400
    badge: Optional[str] = None
    blurb: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


class Plan(PlanIn):
    plan_id: str
    created_at: str


class InvestmentIn(BaseModel):
    plan_id: str
    amount: float
    network: Literal["TGcoin", "Cash"] = "TGcoin"
    tx_hash: Optional[str] = None       # required when network = "TGcoin"
    mobile_number: Optional[str] = None  # required when network = "Cash"
    screenshot: Optional[str] = None  # data URL (base64), e.g. "data:image/png;base64,..."


class Investment(BaseModel):
    investment_id: str
    user_id: str
    plan_id: str
    plan_name: str
    amount: float
    daily_roi: float
    working_days: int
    network: str
    tx_hash: Optional[str] = None
    mobile_number: Optional[str] = None
    screenshot: Optional[str] = None
    status: Literal["pending", "active", "rejected", "completed"] = "pending"
    admin_note: Optional[str] = None
    created_at: str
    activated_at: Optional[str] = None


class LeadIn(BaseModel):
    email: EmailStr
    ticket_size: Literal["1k+ USDT", "10k+ USDT", "50k+ USDT", "Institutional"]
    note: Optional[str] = None


class Lead(LeadIn):
    lead_id: str
    created_at: str


class AdminInvestmentDecision(BaseModel):
    status: Literal["active", "rejected"]
    admin_note: Optional[str] = None


# -----------------------------------------------------------------------------
# Auth helpers
# -----------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": "access",
        "exp": now_utc() + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 3600,
        path="/",
    )


def _clear_session_cookie(response: Response):
    response.delete_cookie("session_token", path="/")


async def _resolve_user_from_token(token: str) -> Optional[dict]:
    # 1) Try as JWT (email/password flow)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        if payload.get("type") == "access":
            user = await db.users.find_one({"user_id": payload["sub"]}, {"_id": 0})
            return user
    except jwt.PyJWTError:
        pass

    # 2) Try as Emergent session_token (Google flow)
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if sess:
        expires_at = sess.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at > now_utc():
            user = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
            return user
    return None


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("session_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await _resolve_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session invalid or expired")
    user.pop("password_hash", None)
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def public_user(u: dict) -> dict:
    return {
        "user_id": u["user_id"],
        "email": u["email"],
        "name": u.get("name", ""),
        "role": u.get("role", "investor"),
        "wallet_address": u.get("wallet_address"),
        "phone": u.get("phone"),
        "country": u.get("country"),
        "referral_code": u.get("referral_code"),
        "referred_by": u.get("referred_by"),
        "picture": u.get("picture"),
        "auth_provider": u.get("auth_provider", "password"),
        "is_active": u.get("is_active", True),
        "force_password_change": bool(u.get("force_password_change", False)),
        "lockout_until": u.get("lockout_until"),
        "failed_login_attempts": int(u.get("failed_login_attempts", 0) or 0),
        "created_at": u.get("created_at"),
    }


def _gen_referral_code() -> str:
    import secrets
    import string
    alphabet = string.ascii_uppercase + string.digits
    return "TG" + "".join(secrets.choice(alphabet) for _ in range(6))


async def _unique_referral_code() -> str:
    for _ in range(8):
        code = _gen_referral_code()
        if not await db.users.find_one({"referral_code": code}):
            return code
    return _gen_referral_code()


async def _audit(
    action: str,
    actor: Optional[str],
    target_user_id: Optional[str] = None,
    amount: float = 0,
    meta: Optional[dict] = None,
):
    """Append a single row to db.audit_log. Best-effort — never blocks the request."""
    try:
        await db.audit_log.insert_one({
            "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
            "ts": iso(now_utc()),
            "actor": actor,
            "action": action,
            "target_user_id": target_user_id,
            "amount": float(amount or 0),
            "meta": meta or {},
        })
    except Exception as e:
        logger.warning("audit_log insert failed action=%s err=%s", action, e)


# -----------------------------------------------------------------------------
# Push helpers — referrer/name lookups for system-fired notifications
# -----------------------------------------------------------------------------
async def _get_user_name(user_id: Optional[str]) -> str:
    if not user_id:
        return "Someone"
    u = await db.users.find_one({"user_id": user_id}, {"_id": 0, "name": 1})
    return (u or {}).get("name") or "Someone"


async def _get_user_referrer_id(user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    u = await db.users.find_one({"user_id": user_id}, {"_id": 0, "referred_by": 1})
    return (u or {}).get("referred_by")


# -----------------------------------------------------------------------------
# Auth routes
# -----------------------------------------------------------------------------
@api.post("/auth/register")
async def register(payload: RegisterPayload, response: Response):
    email = payload.email.lower()
    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing and existing.get("password_hash"):
        raise HTTPException(status_code=400, detail="Email already registered")

    # Resolve referrer (if a code was supplied)
    referred_by = None
    if payload.referral_code:
        code = payload.referral_code.strip().upper()
        if code:
            ref = await db.users.find_one({"referral_code": code}, {"_id": 0})
            if not ref:
                raise HTTPException(status_code=400, detail="Invalid referral code")
            referred_by = ref["user_id"]

    user_id = existing["user_id"] if existing else f"user_{uuid.uuid4().hex[:12]}"
    referral_code = existing.get("referral_code") if existing else await _unique_referral_code()
    doc = {
        "user_id": user_id,
        "email": email,
        "name": payload.name,
        "password_hash": hash_password(payload.password),
        "role": "investor",
        "auth_provider": "both" if existing else "password",
        "wallet_address": payload.wallet_address,
        "phone": payload.phone,
        "country": payload.country,
        "referral_code": referral_code,
        "referred_by": referred_by or (existing.get("referred_by") if existing else None),
        "picture": existing.get("picture") if existing else None,
        "created_at": existing.get("created_at") if existing else iso(now_utc()),
    }
    if existing:
        await db.users.update_one({"user_id": user_id}, {"$set": doc})
    else:
        await db.users.insert_one(doc)

    token = create_access_token(user_id, email, doc["role"])
    _set_session_cookie(response, token)
    await _audit(
        "auth.register",
        actor=user_id,
        target_user_id=user_id,
        meta={"email": email, "referred_by": referred_by, "had_existing_oauth_row": bool(existing)},
    )
    return {"user": public_user(doc), "token": token}


@api.post("/auth/login")
async def login(payload: LoginPayload, response: Response):
    email = payload.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # ── Brute-force lockout ───────────────────────────────────────────────
    # After 3 wrong passwords in a row, the account is locked for 1 hour.
    MAX_ATTEMPTS = 3
    LOCKOUT_MINUTES = 60
    now = now_utc()
    locked_until_raw = user.get("lockout_until")
    if locked_until_raw:
        try:
            locked_until_dt = datetime.fromisoformat(locked_until_raw)
            if locked_until_dt.tzinfo is None:
                locked_until_dt = locked_until_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            locked_until_dt = None
        if locked_until_dt and locked_until_dt > now:
            mins_left = max(1, int((locked_until_dt - now).total_seconds() // 60) + 1)
            raise HTTPException(
                status_code=423,
                detail=(
                    f"Account locked due to too many failed login attempts. "
                    f"Try again in {mins_left} minute{'s' if mins_left != 1 else ''}."
                ),
            )

    if not verify_password(payload.password, user["password_hash"]):
        # Track the failure and possibly lock the account
        current_fails = int(user.get("failed_login_attempts", 0) or 0) + 1
        update: dict = {"failed_login_attempts": current_fails}
        lock_triggered = False
        if current_fails >= MAX_ATTEMPTS:
            update["lockout_until"] = iso(now + timedelta(minutes=LOCKOUT_MINUTES))
            update["failed_login_attempts"] = 0  # reset counter once locked
            lock_triggered = True
        await db.users.update_one({"user_id": user["user_id"]}, {"$set": update})
        await _audit(
            "auth.login_failed",
            actor=user["user_id"],
            target_user_id=user["user_id"],
            meta={
                "email": email,
                "attempts": current_fails,
                "locked": lock_triggered,
                "lockout_until": update.get("lockout_until"),
            },
        )
        if lock_triggered:
            raise HTTPException(
                status_code=423,
                detail=(
                    f"Account locked for {LOCKOUT_MINUTES} minutes after "
                    f"{MAX_ATTEMPTS} failed attempts. Please try again later."
                ),
            )
        attempts_left = MAX_ATTEMPTS - current_fails
        raise HTTPException(
            status_code=401,
            detail=(
                f"Invalid email or password. "
                f"{attempts_left} attempt{'s' if attempts_left != 1 else ''} left "
                f"before the account is locked."
            ),
        )
    if user.get("is_active") is False:
        raise HTTPException(status_code=403, detail="Account is disabled. Contact support.")

    # Success — clear any failure tracking
    if user.get("failed_login_attempts") or user.get("lockout_until"):
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"failed_login_attempts": 0, "lockout_until": None}},
        )

    user["_id"] = str(user.get("_id"))
    token = create_access_token(user["user_id"], user["email"], user.get("role", "investor"))
    _set_session_cookie(response, token)
    await _audit(
        "auth.login",
        actor=user["user_id"],
        target_user_id=user["user_id"],
        meta={"email": email, "role": user.get("role", "investor")},
    )
    return {"user": public_user(user), "token": token}


@api.post("/auth/change-password")
async def change_password(
    payload: ChangePasswordPayload,
    response: Response,
    user: dict = Depends(get_current_user),
):
    # If the user is NOT in a force-change state, current_password is required
    if not user.get("force_password_change"):
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="Current password is required")
        if not verify_password(payload.current_password, user.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="Current password is incorrect")
    # Basic strength check beyond min_length=8: require letters + digits
    if not (any(c.isalpha() for c in payload.new_password) and any(c.isdigit() for c in payload.new_password)):
        raise HTTPException(status_code=400, detail="Password must contain at least one letter and one digit")
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "password_hash": hash_password(payload.new_password),
            "force_password_change": False,
        }},
    )
    # Issue fresh token (defensive — keeps session alive after rotation)
    token = create_access_token(user["user_id"], user["email"], user.get("role", "investor"))
    _set_session_cookie(response, token)
    await _audit(
        "auth.password_changed",
        actor=user["user_id"],
        target_user_id=user["user_id"],
        meta={"was_forced": bool(user.get("force_password_change"))},
    )
    return {"ok": True, "token": token}


@api.post("/auth/logout")
async def logout(response: Response, request: Request):
    token = request.cookies.get("session_token")
    if token:
        await db.user_sessions.delete_one({"session_token": token})
    _clear_session_cookie(response)
    return {"ok": True}


@api.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return public_user(user)


# REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
@api.post("/auth/google/session")
async def google_session(request: Request, response: Response):
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    async with httpx.AsyncClient(timeout=15.0) as http:
        r = await http.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": session_id},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Failed to verify Google session")
    data = r.json()

    email = (data.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email missing from Google response")

    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        user_id = existing["user_id"]
        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {
                "name": data.get("name") or existing.get("name") or email.split("@")[0],
                "picture": data.get("picture") or existing.get("picture"),
                "auth_provider": "both" if existing.get("password_hash") else "google",
            }},
        )
        user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user = {
            "user_id": user_id,
            "email": email,
            "name": data.get("name") or email.split("@")[0],
            "role": "investor",
            "auth_provider": "google",
            "picture": data.get("picture"),
            "wallet_address": None,
            "created_at": iso(now_utc()),
        }
        await db.users.insert_one(user)

    session_token = data["session_token"]
    expires_at = now_utc() + timedelta(days=7)
    await db.user_sessions.insert_one({
        "user_id": user_id,
        "session_token": session_token,
        "expires_at": expires_at,
        "created_at": now_utc(),
    })
    _set_session_cookie(response, session_token)
    return {"user": public_user(user)}


# -----------------------------------------------------------------------------
# Plans
# -----------------------------------------------------------------------------
@api.get("/plans", response_model=List[Plan])
async def list_plans(include_inactive: bool = False):
    query = {} if include_inactive else {"is_active": True}
    cursor = db.plans.find(query, {"_id": 0}).sort([("sort_order", 1), ("min_amount", 1)])
    return [Plan(**p) async for p in cursor]


@api.post("/plans", response_model=Plan)
async def create_plan(payload: PlanIn, _admin: dict = Depends(require_admin)):
    plan_id = f"plan_{uuid.uuid4().hex[:10]}"
    doc = payload.model_dump()
    doc.update({"plan_id": plan_id, "created_at": iso(now_utc())})
    await db.plans.insert_one(doc)
    doc.pop("_id", None)
    await _audit(
        "plan.created",
        actor=_admin["user_id"],
        meta={"plan_id": plan_id, "name": doc.get("name"), "min": doc.get("min_amount"), "max": doc.get("max_amount")},
    )
    return Plan(**doc)


@api.put("/plans/{plan_id}", response_model=Plan)
async def update_plan(plan_id: str, payload: PlanIn, _admin: dict = Depends(require_admin)):
    result = await db.plans.update_one({"plan_id": plan_id}, {"$set": payload.model_dump()})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Plan not found")
    doc = await db.plans.find_one({"plan_id": plan_id}, {"_id": 0})
    await _audit(
        "plan.updated",
        actor=_admin["user_id"],
        meta={"plan_id": plan_id, "name": doc.get("name"), "min": doc.get("min_amount"), "max": doc.get("max_amount")},
    )
    return Plan(**doc)


@api.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str, _admin: dict = Depends(require_admin)):
    result = await db.plans.delete_one({"plan_id": plan_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Plan not found")
    await _audit("plan.deleted", actor=_admin["user_id"], meta={"plan_id": plan_id})
    return {"ok": True}


# -----------------------------------------------------------------------------
# Investments
# -----------------------------------------------------------------------------
@api.post("/investments", response_model=Investment)
async def create_investment(payload: InvestmentIn, user: dict = Depends(get_current_user)):
    # Hard global cap — no single investment may exceed 100,000 USDT
    if payload.amount > 100000:
        raise HTTPException(status_code=400, detail="Maximum investment is 100,000 USDT")
    if payload.amount < 100:
        raise HTTPException(status_code=400, detail="Minimum investment is 100 USDT")
    plan = await db.plans.find_one({"plan_id": payload.plan_id, "is_active": True}, {"_id": 0})
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found or inactive")
    if payload.amount < plan["min_amount"] or payload.amount > plan["max_amount"]:
        raise HTTPException(
            status_code=400,
            detail=f"Amount must be between {plan['min_amount']:.0f} USDT and {plan['max_amount']:.0f} USDT",
        )

    # Validate payment-method specific fields
    network = payload.network
    tx_hash = (payload.tx_hash or "").strip()
    mobile_number = (payload.mobile_number or "").strip()
    if network == "TGcoin":
        if len(tx_hash) < 6:
            raise HTTPException(status_code=400, detail="Transaction hash is required for TGcoin deposits (min 6 chars)")
    elif network == "Cash":
        # Mobile number: digits, spaces, +, - allowed; require at least 5 digits
        # (final per-country length validation happens on the frontend with
        # the country dial-code + max-digits map).
        digits = "".join(ch for ch in mobile_number if ch.isdigit())
        if len(digits) < 5:
            raise HTTPException(status_code=400, detail="A valid mobile number is required for Cash deposits")

    # Validate screenshot (if provided) — must be a data URL of an image, max ~6MB encoded
    screenshot = payload.screenshot
    if screenshot:
        if not screenshot.startswith("data:image/"):
            raise HTTPException(status_code=400, detail="Screenshot must be an image (JPG/PNG/WEBP)")
        # base64 grows the size by ~33% — 8MB string ≈ 6MB binary
        if len(screenshot) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Screenshot too large (max 6MB)")

    investment_id = f"inv_{uuid.uuid4().hex[:10]}"
    doc = {
        "investment_id": investment_id,
        "user_id": user["user_id"],
        "plan_id": plan["plan_id"],
        "plan_name": plan["name"],
        "amount": payload.amount,
        "daily_roi": plan["daily_roi"],
        "working_days": plan["working_days"],
        "network": network,
        "tx_hash": tx_hash if network == "TGcoin" else None,
        "mobile_number": mobile_number if network == "Cash" else None,
        "screenshot": screenshot,
        "status": "pending",
        "admin_note": None,
        "created_at": iso(now_utc()),
        "activated_at": None,
    }
    await db.investments.insert_one(doc)
    doc.pop("_id", None)
    await _audit(
        "investment.created",
        actor=user["user_id"],
        target_user_id=user["user_id"],
        amount=payload.amount,
        meta={
            "investment_id": investment_id,
            "plan_id": plan["plan_id"],
            "plan_name": plan["name"],
            "network": network,
            "tx_hash": tx_hash if network == "TGcoin" else None,
            "mobile_number": mobile_number if network == "Cash" else None,
        },
    )
    return Investment(**doc)


@api.get("/investments/me", response_model=List[Investment])
async def my_investments(user: dict = Depends(get_current_user)):
    cursor = db.investments.find({"user_id": user["user_id"]}, {"_id": 0}).sort("created_at", -1)
    return [Investment(**i) async for i in cursor]


@api.get("/investments", response_model=List[Investment])
async def all_investments(
    status_filter: Optional[str] = None,
    _admin: dict = Depends(require_admin),
):
    query = {}
    if status_filter:
        query["status"] = status_filter
    cursor = db.investments.find(query, {"_id": 0}).sort("created_at", -1)
    return [Investment(**i) async for i in cursor]


@api.post("/investments/{investment_id}/decision", response_model=Investment)
async def decide_investment(
    investment_id: str,
    payload: AdminInvestmentDecision,
    _admin: dict = Depends(require_admin),
):
    # Load current state first to detect activation transition
    prior = await db.investments.find_one({"investment_id": investment_id}, {"_id": 0})
    if not prior:
        raise HTTPException(status_code=404, detail="Investment not found")
    update = {"status": payload.status, "admin_note": payload.admin_note}
    is_first_activation = payload.status == "active" and prior.get("status") != "active" and not prior.get("activated_at")
    if payload.status == "active":
        update.setdefault("activated_at", iso(now_utc())) if not prior.get("activated_at") else None
        if not prior.get("activated_at"):
            update["activated_at"] = iso(now_utc())
    await db.investments.update_one({"investment_id": investment_id}, {"$set": update})
    inv = await db.investments.find_one({"investment_id": investment_id}, {"_id": 0})

    # On first activation, credit upline referral 5% one-time per level (locked until Saturday)
    if is_first_activation:
        try:
            await earnings_engine.credit_referral_for_investment(db, inv)
        except Exception as e:
            logger.exception("referral credit failed: %s", e)

    # ---- Push notifications (best-effort, non-blocking semantics) ---------
    try:
        amt = inv.get("amount", 0)
        if payload.status == "active":
            await push_module.push_event(
                db,
                event="deposit.approved",
                title="Deposit approved",
                body=f"Your {amt} USDT deposit has been activated. Your daily ROI starts now.",
                user_id=inv.get("user_id"),
                cta_url="/dashboard/earnings",
            )
            # New capital → reset cap-reached push flag so the user gets a new alert
            # if they hit 2X again on the increased base.
            await db.users.update_one(
                {"user_id": inv.get("user_id")},
                {"$set": {"cap_push_sent": False}},
            )
            # Notify the upline that they have a new referral / downline activation
            referrer_id = await _get_user_referrer_id(inv.get("user_id"))
            if referrer_id and is_first_activation:
                downline_name = await _get_user_name(inv.get("user_id"))
                await push_module.push_event(
                    db,
                    event="referral.activated",
                    title="New referral activated",
                    body=f"{downline_name} just activated a {amt} USDT deposit — you'll see level + referral income tomorrow.",
                    user_id=referrer_id,
                    cta_url="/dashboard/network",
                )
        elif payload.status == "rejected":
            await push_module.push_event(
                db,
                event="deposit.rejected",
                title="Deposit needs attention",
                body=(payload.admin_note or "Your deposit could not be verified. Please contact support."),
                user_id=inv.get("user_id"),
                cta_url="/dashboard/deposits?tab=history",
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("push_event on investment decision failed: %s", e)

    await _audit(
        f"investment.{payload.status}",
        actor=_admin["user_id"],
        target_user_id=inv.get("user_id"),
        amount=inv.get("amount", 0),
        meta={
            "investment_id": investment_id,
            "prior_status": prior.get("status"),
            "new_status": payload.status,
            "is_first_activation": is_first_activation,
            "admin_note": payload.admin_note,
        },
    )
    return Investment(**inv)


# -----------------------------------------------------------------------------
# Admin — Network Tree (genealogy)
# -----------------------------------------------------------------------------
async def _active_capital_by_user() -> dict:
    """user_id -> sum of active investment amount"""
    out: dict = {}
    pipeline = [
        {"$match": {"status": "active"}},
        {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]
    async for r in db.investments.aggregate(pipeline):
        out[r["_id"]] = {"capital": float(r.get("total", 0.0)), "active_count": int(r.get("count", 0))}
    return out


async def _children_map() -> dict:
    out: dict = {}
    async for u in db.users.find({"role": "investor"}, {"_id": 0, "user_id": 1, "referred_by": 1}):
        parent = u.get("referred_by")
        if parent:
            out.setdefault(parent, []).append(u["user_id"])
    return out


async def _users_by_id() -> dict:
    out: dict = {}
    async for u in db.users.find({}, {"_id": 0, "password_hash": 0}):
        out[u["user_id"]] = u
    return out


def _subtree_stats(node_id: str, children_map: dict, cap_by_user: dict) -> dict:
    """Aggregate descendants of node_id (excluding node itself). Iterative BFS."""
    total_size = 0
    total_capital = 0.0
    active_members = 0
    max_depth = 0
    level_breakdown: dict = {}

    frontier = [(c, 1) for c in children_map.get(node_id, [])]
    while frontier:
        next_frontier = []
        for uid, depth in frontier:
            total_size += 1
            max_depth = max(max_depth, depth)
            cap = cap_by_user.get(uid, {}).get("capital", 0.0)
            total_capital += cap
            if cap > 0:
                active_members += 1
            bucket = level_breakdown.setdefault(depth, {"level": depth, "count": 0, "active": 0, "volume": 0.0})
            bucket["count"] += 1
            if cap > 0:
                bucket["active"] += 1
            bucket["volume"] += cap
            for ch in children_map.get(uid, []):
                next_frontier.append((ch, depth + 1))
        frontier = next_frontier

    return {
        "team_size": total_size,
        "team_capital": round(total_capital, 2),
        "active_team_members": active_members,
        "max_depth": max_depth,
        "level_breakdown": [
            {**v, "volume": round(v["volume"], 2)} for v in sorted(level_breakdown.values(), key=lambda x: x["level"])
        ],
    }


def _node_payload(uid: str, users_by_id: dict, cap_by_user: dict, children_map: dict, level: int, *, with_subtree_stats: bool = True) -> dict:
    u = users_by_id.get(uid) or {}
    cap = cap_by_user.get(uid, {})
    direct = children_map.get(uid, [])
    active_capital = round(float(cap.get("capital", 0.0)), 2)
    node = {
        "user_id": uid,
        "name": u.get("name"),
        "email": u.get("email"),
        "phone": u.get("phone"),
        "country": u.get("country"),
        "referral_code": u.get("referral_code"),
        "referred_by": u.get("referred_by"),
        "wallet_address": u.get("wallet_address"),
        "created_at": u.get("created_at"),
        "role": u.get("role", "investor"),
        "level": level,
        "direct_referrals": len(direct),
        "active_capital": active_capital,
        "active_investments": int(cap.get("active_count", 0)),
        "daily_roi": round(active_capital * earnings_engine.DAILY_RATE, 2),
        "status": "active" if cap.get("capital", 0) > 0 else "inactive",
    }
    if with_subtree_stats:
        stats = _subtree_stats(uid, children_map, cap_by_user)
        node.update({
            "team_size": stats["team_size"],
            "team_capital": stats["team_capital"],
            "active_team_members": stats["active_team_members"],
            "max_depth_below": stats["max_depth"],
        })
    return node


def _build_subtree(uid: str, depth: int, level: int, users_by_id: dict, cap_by_user: dict, children_map: dict) -> dict:
    node = _node_payload(uid, users_by_id, cap_by_user, children_map, level)
    if depth > 0:
        node["children"] = [
            _build_subtree(c, depth - 1, level + 1, users_by_id, cap_by_user, children_map)
            for c in children_map.get(uid, [])
        ]
        node["has_more_below"] = False
    else:
        # placeholder — admin must expand to fetch
        kids = children_map.get(uid, [])
        node["children"] = []
        node["has_more_below"] = len(kids) > 0
    return node


@api.get("/admin/network-tree")
async def admin_network_tree(
    root_id: Optional[str] = None,
    depth: int = 2,
    _admin: dict = Depends(require_admin),
):
    """Return the genealogy subtree under `root_id` to a given depth.
    If `root_id` is omitted, returns a synthetic root with all top-level investors
    (users who have no referrer)."""
    depth = max(0, min(depth, 6))
    users_by_id = await _users_by_id()
    children_map = await _children_map()
    cap_by_user = await _active_capital_by_user()

    if root_id:
        if root_id not in users_by_id:
            raise HTTPException(status_code=404, detail="User not found")
        tree = _build_subtree(root_id, depth, 0, users_by_id, cap_by_user, children_map)
        return {"root": tree, "synthetic_root": False}

    # Synthetic root — investors with no referred_by
    top_level = [u["user_id"] for u in users_by_id.values()
                 if u.get("role") == "investor" and not u.get("referred_by")]
    children = [_build_subtree(uid, max(depth - 1, 0), 1, users_by_id, cap_by_user, children_map)
                for uid in top_level]
    total_capital = sum(c.get("active_capital", 0) + c.get("team_capital", 0) for c in children)
    synth = {
        "user_id": "__root__",
        "name": "Trade Gain Capital",
        "email": "Genealogy root",
        "referral_code": None,
        "level": 0,
        "direct_referrals": len(top_level),
        "active_capital": 0,
        "team_size": sum(c.get("team_size", 0) for c in children) + len(children),
        "team_capital": round(total_capital, 2),
        "active_team_members": sum((1 if c.get("active_capital", 0) > 0 else 0) for c in children)
                              + sum(c.get("active_team_members", 0) for c in children),
        "status": "root",
        "children": children,
        "has_more_below": False,
        "is_synthetic": True,
    }
    return {"root": synth, "synthetic_root": True}


@api.get("/admin/network-levels")
async def admin_network_levels(user_id: str, _admin: dict = Depends(require_admin)):
    users_by_id = await _users_by_id()
    if user_id not in users_by_id:
        raise HTTPException(status_code=404, detail="User not found")
    children_map = await _children_map()
    cap_by_user = await _active_capital_by_user()
    return _subtree_stats(user_id, children_map, cap_by_user)


@api.get("/admin/search-user")
async def admin_search_user(q: str, _admin: dict = Depends(require_admin)):
    q = (q or "").strip()
    if len(q) < 1:
        return []
    import re
    pattern = re.compile(re.escape(q), re.IGNORECASE)
    cursor = db.users.find(
        {
            "role": "investor",
            "$or": [
                {"name": {"$regex": pattern}},
                {"email": {"$regex": pattern}},
                {"user_id": {"$regex": pattern}},
                {"referral_code": {"$regex": pattern}},
                {"wallet_address": {"$regex": pattern}},
            ],
        },
        {"_id": 0, "password_hash": 0},
    ).limit(20)
    out = []
    cap_by_user = await _active_capital_by_user()
    async for u in cursor:
        cap = cap_by_user.get(u["user_id"], {}).get("capital", 0.0)
        out.append({
            "user_id": u["user_id"],
            "name": u.get("name"),
            "email": u["email"],
            "referral_code": u.get("referral_code"),
            "active_capital": round(float(cap), 2),
        })
    return out


@api.get("/admin/user-detail")
async def admin_user_detail(user_id: str, _admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    children_map = await _children_map()
    cap_by_user = await _active_capital_by_user()
    stats = _subtree_stats(user_id, children_map, cap_by_user)
    # Aggregate this user's own investments
    own = {"total_invested": 0.0, "total_active": 0.0, "count": 0, "active_count": 0}
    async for inv in db.investments.find({"user_id": user_id}, {"_id": 0}):
        own["total_invested"] += float(inv["amount"])
        own["count"] += 1
        if inv.get("status") == "active":
            own["total_active"] += float(inv["amount"])
            own["active_count"] += 1
    sponsor = None
    if u.get("referred_by"):
        s = await db.users.find_one({"user_id": u["referred_by"]}, {"_id": 0, "user_id": 1, "name": 1, "email": 1, "referral_code": 1})
        if s:
            sponsor = s
    return {
        "user": public_user(u),
        "sponsor": sponsor,
        "investments": {
            "total_invested": round(own["total_invested"], 2),
            "total_active": round(own["total_active"], 2),
            "count": own["count"],
            "active_count": own["active_count"],
        },
        "team": stats,
    }


# -----------------------------------------------------------------------------
# Investor stats, payouts, network
# -----------------------------------------------------------------------------
def _date(dt_or_str) -> Optional[date]:
    if not dt_or_str:
        return None
    if isinstance(dt_or_str, str):
        dt = datetime.fromisoformat(dt_or_str)
    else:
        dt = dt_or_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date()


def _weekdays_elapsed(start: date, end: date, cap: int) -> int:
    if not start or not end or start > end:
        return 0
    days = (end - start).days
    weeks, extra = divmod(days, 7)
    count = weeks * 5
    weekday = start.weekday()  # Mon=0
    for i in range(1, extra + 1):
        if (weekday + i) % 7 < 5:
            count += 1
    return min(count, cap)


def _saturdays_elapsed(start: date, end: date, cap_weeks: int) -> int:
    """Number of Saturdays strictly after start, up to and including end."""
    if not start or not end or start > end:
        return 0
    count = 0
    days = (end - start).days
    weeks, extra = divmod(days, 7)
    count = weeks
    weekday = start.weekday()  # Mon=0; Sat=5
    for i in range(1, extra + 1):
        if (weekday + i) % 7 == 5:
            count += 1
    return min(count, cap_weeks)


async def _investor_stats(user_id: str) -> dict:
    today = now_utc().date()
    cursor = db.investments.find({"user_id": user_id}, {"_id": 0})
    total_invested = 0.0
    total_accrued = 0.0
    total_paid_out = 0.0
    async for inv in cursor:
        if inv.get("status") != "active":
            continue
        amount = float(inv["amount"])
        daily = amount * float(inv["daily_roi"])
        cap_days = int(inv["working_days"])
        cap_weeks = cap_days // 5
        activated = _date(inv.get("activated_at"))
        days_elapsed = _weekdays_elapsed(activated, today, cap_days) if activated else 0
        sats = _saturdays_elapsed(activated, today, cap_weeks) if activated else 0
        total_invested += amount
        total_accrued += days_elapsed * daily
        total_paid_out += sats * daily * 5
    pending_payout = max(total_accrued - total_paid_out, 0.0)
    return {
        "total_invested": round(total_invested, 2),
        "total_accrued": round(total_accrued, 2),
        "pending_payout": round(pending_payout, 2),
        "total_paid_out": round(total_paid_out, 2),
    }


@api.get("/me/stats")
async def my_stats(user: dict = Depends(get_current_user)):
    return await _investor_stats(user["user_id"])


@api.get("/me/payouts")
async def my_payouts(user: dict = Depends(get_current_user)):
    """Synthesize the weekly Saturday payouts for each of the investor's active investments."""
    today = now_utc().date()
    cursor = db.investments.find({"user_id": user["user_id"], "status": "active"}, {"_id": 0})
    rows = []
    async for inv in cursor:
        amount = float(inv["amount"])
        weekly = amount * float(inv["daily_roi"]) * 5
        cap_weeks = int(inv["working_days"]) // 5
        activated = _date(inv.get("activated_at"))
        if not activated:
            continue
        # Find the first Saturday strictly after activation
        days_to_sat = (5 - activated.weekday()) % 7
        if days_to_sat == 0:
            days_to_sat = 7
        first_sat = activated + timedelta(days=days_to_sat)
        sat = first_sat
        week = 1
        while sat <= today and week <= cap_weeks:
            rows.append({
                "investment_id": inv["investment_id"],
                "plan_name": inv["plan_name"],
                "week": week,
                "amount": round(weekly, 2),
                "paid_at": sat.isoformat(),
            })
            sat += timedelta(days=7)
            week += 1
    rows.sort(key=lambda r: r["paid_at"], reverse=True)
    return rows


@api.get("/me/network")
async def my_network(user: dict = Depends(get_current_user)):
    # If this account was created before referral codes existed, mint one now
    # (single targeted write per user — never overwrites existing data).
    user_id = user["user_id"]
    referral_code = user.get("referral_code")
    if not referral_code:
        referral_code = await _unique_referral_code()
        await db.users.update_one(
            {"user_id": user_id, "$or": [{"referral_code": None}, {"referral_code": {"$exists": False}}]},
            {"$set": {"referral_code": referral_code}},
        )

    # Batch-load referred users + their active capital in 2 queries (no N+1)
    referred_users = await db.users.find(
        {"referred_by": user_id}, {"_id": 0, "password_hash": 0}
    ).sort("created_at", -1).to_list(None)
    referred_ids = [u["user_id"] for u in referred_users]
    capital_map: dict = {}
    if referred_ids:
        async for r in db.investments.aggregate([
            {"$match": {"user_id": {"$in": referred_ids}, "status": "active"}},
            {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}},
        ]):
            capital_map[r["_id"]] = float(r.get("total", 0.0))
    referred = [
        {
            "user_id": u["user_id"],
            "name": u.get("name"),
            "email": u["email"],
            "country": u.get("country"),
            "joined": u.get("created_at"),
            "active_capital": round(capital_map.get(u["user_id"], 0.0), 2),
        }
        for u in referred_users
    ]
    return {
        "referral_code": referral_code,
        "total_referred": len(referred),
        "total_capital": round(sum(r["active_capital"] for r in referred), 2),
        "members": referred,
    }


# -----------------------------------------------------------------------------
# Investor: own downline tree + per-level member rosters
# -----------------------------------------------------------------------------
def _mask_name(name: Optional[str]) -> str:
    """Privacy-mask: 'John Doe' -> 'Jo•• D•'.
    Keeps first 2 chars of first token, first char of remaining tokens."""
    if not name:
        return "—"
    parts = str(name).strip().split()
    if not parts:
        return "—"
    head = parts[0][:2] + ("•" * max(1, len(parts[0]) - 2))
    rest = [p[:1] + "•" for p in parts[1:]]
    return " ".join([head, *rest])


async def _ensure_display_id(user_id: str, users_by_id: dict) -> str:
    """Return the user's stable public display_id (UUIDv7). Mints + persists on first read."""
    u = users_by_id.get(user_id) or {}
    did = u.get("display_id")
    if did:
        return did
    did = _uuid7()
    await db.users.update_one(
        {"user_id": user_id, "$or": [{"display_id": None}, {"display_id": {"$exists": False}}]},
        {"$set": {"display_id": did}},
    )
    u["display_id"] = did
    return did


async def _is_in_my_downline(root_user_id: str, target_user_id: str) -> bool:
    """Walk up `referred_by` chain from target. True if root is anywhere above."""
    if target_user_id == root_user_id:
        return True
    seen = set()
    cur = target_user_id
    while cur and cur not in seen:
        seen.add(cur)
        u = await db.users.find_one({"user_id": cur}, {"_id": 0, "referred_by": 1})
        if not u:
            return False
        parent = u.get("referred_by")
        if parent == root_user_id:
            return True
        cur = parent
    return False


@api.get("/me/network/tree")
async def my_network_tree(
    root_id: Optional[str] = None,
    depth: int = 2,
    user: dict = Depends(get_current_user),
):
    """Investor-facing genealogy tree.

    Default view: rooted at the current user's REFERRER (1-step up) — the user
    sees who brought them in, with themselves as the visible child (siblings
    hidden for privacy). When the user has no referrer, they themselves are
    the root.

    When `root_id` is provided, it must be the current user or any descendant
    in their downline, so investors can only browse their own subtree."""
    me_id = user["user_id"]
    depth = max(0, min(depth, 4))

    users_by_id = await _users_by_id()
    children_map = await _children_map()
    cap_by_user = await _active_capital_by_user()

    if root_id and root_id != me_id:
        # Must be inside my downline
        if not await _is_in_my_downline(me_id, root_id):
            raise HTTPException(status_code=403, detail="Out of your downline")
        effective_root = root_id
        tree = _build_subtree(effective_root, depth, 0, users_by_id, cap_by_user, children_map)
        upline_view = False
    elif root_id == me_id:
        # Explicitly asked for self (used by Load-deeper of own subtree)
        tree = _build_subtree(me_id, depth, 0, users_by_id, cap_by_user, children_map)
        upline_view = False
    else:
        # Default: 1-step-up upline view
        me_user = users_by_id.get(me_id) or {}
        upline_id = me_user.get("referred_by")
        if upline_id and upline_id in users_by_id:
            # Build the user's own subtree first
            my_subtree = _build_subtree(me_id, max(0, depth - 1), 1, users_by_id, cap_by_user, children_map)
            # Wrap it in the referrer node, hiding the referrer's other downlines
            tree = _node_payload(upline_id, users_by_id, cap_by_user, children_map, 0)
            tree["children"] = [my_subtree]
            tree["has_more_below"] = False
            # Note: direct_referrals stays accurate at backend; on the client
            # we'll display it but the user only sees themselves as the visible child.
            upline_view = True
        else:
            tree = _build_subtree(me_id, depth, 0, users_by_id, cap_by_user, children_map)
            upline_view = False

    # Privacy-mask any node that is NOT the current user. The 1-step-up
    # referrer's name is also masked.
    def _scrub(node):
        if node.get("user_id") != me_id:
            node["name"] = _mask_name(node.get("name"))
            node["email"] = None
            node["phone"] = None
            node["wallet_address"] = None
        for c in node.get("children") or []:
            _scrub(c)
    _scrub(tree)
    return {"root": tree, "me_id": me_id, "upline_view": upline_view}


@api.get("/me/levels/{level}/members")
async def my_level_members(level: int, user: dict = Depends(get_current_user)):
    """List members under the current user at the given depth (1..10).
    Each row carries: display_id (UUIDv7), level, total_invested, daily_roi,
    activated_at, referrer_name (masked), referrer_code."""
    if level < 1 or level > earnings_engine.MAX_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid level")
    me_id = user["user_id"]
    children_map = await _children_map()
    users_by_id = await _users_by_id()
    levels = earnings_engine.downline_levels(me_id, children_map, max_depth=level)
    member_ids = levels.get(level, [])

    rows: List[dict] = []
    for uid in member_ids:
        u = users_by_id.get(uid) or {}
        # Stable public ID
        display_id = await _ensure_display_id(uid, users_by_id)
        # Total active investment + earliest activation
        agg = db.investments.aggregate([
            {"$match": {"user_id": uid, "status": "active"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}, "first_active": {"$min": "$activated_at"}}},
        ])
        total = 0.0
        activated_at = None
        async for r in agg:
            total = float(r.get("total", 0.0))
            activated_at = r.get("first_active")
        daily_roi = round(total * earnings_engine.DAILY_RATE, 2)

        # Referrer (this member's upline)
        ref_uid = u.get("referred_by")
        ref_u = users_by_id.get(ref_uid) if ref_uid else None
        rows.append({
            "display_id": display_id,
            "level": level,
            "total_invested": round(total, 2),
            "daily_roi": daily_roi,
            "activated_at": activated_at,
            "joined_at": u.get("created_at"),
            "country": u.get("country"),
            "referrer_name_masked": _mask_name(ref_u.get("name") if ref_u else None),
            "referrer_code": (ref_u or {}).get("referral_code") if ref_u else None,
        })
    # Sort: most invested first, then newest joined
    def _sort_key(r):
        ts = r.get("joined_at") or ""
        return (-(r.get("total_invested") or 0), ts and (-1 * int("".join(ch for ch in ts if ch.isdigit())[:14] or 0)) or 0)
    rows.sort(key=_sort_key)
    return {
        "level": level,
        "count": len(rows),
        "members": rows,
    }


# -----------------------------------------------------------------------------
# Leads (Allocator inquiries)
# -----------------------------------------------------------------------------
@api.post("/leads", response_model=Lead)
async def create_lead(payload: LeadIn):
    lead_id = f"lead_{uuid.uuid4().hex[:10]}"
    doc = payload.model_dump()
    doc.update({"lead_id": lead_id, "created_at": iso(now_utc())})
    await db.leads.insert_one(doc)
    doc.pop("_id", None)
    return Lead(**doc)


@api.get("/leads", response_model=List[Lead])
async def list_leads(_admin: dict = Depends(require_admin)):
    cursor = db.leads.find({}, {"_id": 0}).sort("created_at", -1)
    return [Lead(**lead) async for lead in cursor]


# -----------------------------------------------------------------------------
# Earnings & weekly payouts (real engine — see earnings.py)
# -----------------------------------------------------------------------------
@api.get("/me/earnings/summary")
async def me_earnings_summary(user: dict = Depends(get_current_user)):
    return await earnings_engine.earnings_summary(db, user["user_id"])


@api.get("/me/earnings/levels")
async def me_earnings_levels(user: dict = Depends(get_current_user)):
    return await earnings_engine.levels_breakdown(db, user["user_id"])


@api.get("/me/earnings/ledger")
async def me_earnings_ledger(
    days: int = 30,
    user: dict = Depends(get_current_user),
):
    days = max(1, min(days, 365))
    cutoff = (now_utc().date() - timedelta(days=days)).isoformat()
    cursor = db.earnings.find(
        {"user_id": user["user_id"], "period_date": {"$gte": cutoff}},
        {"_id": 0},
    ).sort([("period_date", -1), ("created_at", -1)])
    return [doc async for doc in cursor]


@api.get("/me/payouts/weekly")
async def me_weekly_payouts(user: dict = Depends(get_current_user)):
    cursor = db.weekly_payouts.find({"user_id": user["user_id"]}, {"_id": 0}).sort("week_end_date", -1)
    return [doc async for doc in cursor]


@api.get("/admin/earnings/overview")
async def admin_earnings_overview_endpoint(_admin: dict = Depends(require_admin)):
    return await earnings_engine.admin_overview_earnings(db)


@api.get("/admin/earnings/user/{user_id}")
async def admin_earnings_user(user_id: str, _admin: dict = Depends(require_admin)):
    summary = await earnings_engine.earnings_summary(db, user_id)
    levels = await earnings_engine.levels_breakdown(db, user_id)
    # Recent ledger
    cutoff = (now_utc().date() - timedelta(days=60)).isoformat()
    cursor = db.earnings.find(
        {"user_id": user_id, "period_date": {"$gte": cutoff}}, {"_id": 0}
    ).sort([("period_date", -1), ("created_at", -1)])
    ledger = [doc async for doc in cursor]
    payouts_cursor = db.weekly_payouts.find({"user_id": user_id}, {"_id": 0}).sort("week_end_date", -1)
    payouts = [doc async for doc in payouts_cursor]
    return {"summary": summary, "levels": levels, "ledger": ledger, "weekly_payouts": payouts}


@api.post("/admin/earnings/accrue-today")
async def admin_accrue_today(_admin: dict = Depends(require_admin)):
    """Idempotent — run today's ROI + Matching accrual for every active investor.
    Safe to call multiple times per day (duplicate entries are skipped)."""
    return await earnings_engine.accrue_daily_all(db)


class AccrueRangePayload(BaseModel):
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD


@api.post("/admin/earnings/accrue-range")
async def admin_accrue_range(
    payload: AccrueRangePayload, _admin: dict = Depends(require_admin)
):
    """Backfill ROI + Matching for every working day in [start_date, end_date]."""
    from datetime import date as _date
    start = _date.fromisoformat(payload.start_date)
    end = _date.fromisoformat(payload.end_date)
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    out = []
    d = start
    while d <= end:
        out.append(await earnings_engine.accrue_daily_all(db, d))
        d += timedelta(days=1)
    return {"days_processed": len(out), "results": out}


@api.post("/admin/payouts/sweep")
async def admin_sweep_payouts(_admin: dict = Depends(require_admin)):
    """Idempotent — sweep all locked earnings into a weekly payout (one row per user)
    for the upcoming Saturday. Marks earnings as paid."""
    return await earnings_engine.sweep_weekly_payouts(db)


@api.post("/admin/payouts/run-daily")
async def admin_run_daily(_admin: dict = Depends(require_admin)):
    """Manually trigger the same daily accrual the scheduler runs at 00:05 IST.
    Idempotent — users who already accrued today are skipped."""
    result = await scheduler_trigger_now(db)
    await _audit(
        "payouts.daily_triggered_manual",
        actor=_admin["user_id"],
        meta={"result": result},
    )
    return result


@api.post("/admin/payouts/recompute")
async def admin_recompute(
    confirm: str = "",
    _admin: dict = Depends(require_admin),
):
    """DESTRUCTIVE: wipe all earnings + weekly_payouts and replay every working
    day under the new payout-engine rules.

    Required: ?confirm=YES to actually run.
    """
    if confirm != "YES":
        raise HTTPException(
            status_code=400,
            detail="Recompute is destructive. Pass ?confirm=YES to proceed.",
        )
    result = await earnings_engine.recompute_all_from_scratch(db)
    await _audit(
        "payouts.recomputed",
        actor=_admin["user_id"],
        meta={"result": result},
    )
    return result


@api.get("/admin/payouts/history")
async def admin_payouts_history(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 500,
    _admin: dict = Depends(require_admin),
):
    """All settled weekly payouts across every user, newest first. Optional
    date_from / date_to (YYYY-MM-DD) filter by week_end_date."""
    limit = max(1, min(int(limit or 500), 2000))
    query: dict = {}
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        query["week_end_date"] = rng
    cursor = db.weekly_payouts.find(query, {"_id": 0}).sort("week_end_date", -1).limit(limit)
    rows = []
    # Join user name/email for display (cheap — small set per page)
    user_cache: dict = {}
    async for p in cursor:
        uid = p.get("user_id")
        if uid and uid not in user_cache:
            u = await db.users.find_one({"user_id": uid}, {"_id": 0, "name": 1, "email": 1})
            user_cache[uid] = u or {}
        u = user_cache.get(uid, {})
        rows.append({
            **p,
            "user_name": u.get("name") or "",
            "user_email": u.get("email") or "",
        })
    return rows


@api.get("/admin/audit-log")
async def admin_audit_log(
    user_id: Optional[str] = None,
    limit: int = 200,
    _admin: dict = Depends(require_admin),
):
    limit = max(1, min(limit, 1000))
    query = {}
    if user_id:
        query["target_user_id"] = user_id
    cursor = db.audit_log.find(query, {"_id": 0}).sort("ts", -1).limit(limit)
    return [doc async for doc in cursor]


@api.post("/admin/users/{user_id}/unlock")
async def admin_unlock_user(user_id: str, _admin: dict = Depends(require_admin)):
    """Manually clear a brute-force lockout for a user."""
    u = await db.users.find_one({"user_id": user_id}, {"_id": 0, "lockout_until": 1, "failed_login_attempts": 1})
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"lockout_until": None, "failed_login_attempts": 0}},
    )
    await _audit(
        "admin.user.unlocked",
        actor=_admin["user_id"],
        target_user_id=user_id,
        meta={"prev_lockout_until": u.get("lockout_until")},
    )
    return {"ok": True}


# -----------------------------------------------------------------------------
# Admin overview
# -----------------------------------------------------------------------------
@api.get("/admin/users")
async def list_users(_admin: dict = Depends(require_admin)):
    cursor = db.users.find({}, {"_id": 0, "password_hash": 0}).sort("created_at", -1)
    return [public_user(u) async for u in cursor]


@api.post("/admin/users")
async def admin_create_user(payload: AdminCreateUserPayload, _admin: dict = Depends(require_admin)):
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    doc = {
        "user_id": user_id,
        "email": email,
        "name": payload.name,
        "password_hash": hash_password(payload.password),
        "role": payload.role,
        "auth_provider": "password",
        "wallet_address": payload.wallet_address,
        "phone": payload.phone,
        "country": payload.country,
        "referral_code": await _unique_referral_code(),
        "referred_by": None,
        "picture": None,
        "is_active": True,
        "force_password_change": payload.force_password_change,
        "created_at": iso(now_utc()),
    }
    await db.users.insert_one(doc)
    await db.audit_log.insert_one({
        "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
        "ts": iso(now_utc()),
        "actor": _admin["user_id"],
        "action": "admin.user.created",
        "target_user_id": user_id,
        "amount": 0,
        "meta": {"role": payload.role, "force_password_change": payload.force_password_change},
    })
    return public_user(doc)


@api.patch("/admin/users/{user_id}")
async def admin_update_user(
    user_id: str,
    payload: AdminUpdateUserPayload,
    _admin: dict = Depends(require_admin),
):
    existing = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    update = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if not update:
        return public_user(existing)
    # Safety: never let admin demote themselves
    if user_id == _admin["user_id"] and "role" in update and update["role"] != "admin":
        raise HTTPException(status_code=400, detail="You cannot change your own role")
    await db.users.update_one({"user_id": user_id}, {"$set": update})
    await db.audit_log.insert_one({
        "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
        "ts": iso(now_utc()),
        "actor": _admin["user_id"],
        "action": "admin.user.updated",
        "target_user_id": user_id,
        "amount": 0,
        "meta": update,
    })
    refreshed = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    return public_user(refreshed)


@api.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: str,
    payload: AdminResetUserPasswordPayload,
    _admin: dict = Depends(require_admin),
):
    existing = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    if not (any(c.isalpha() for c in payload.new_password) and any(c.isdigit() for c in payload.new_password)):
        raise HTTPException(status_code=400, detail="Password must contain at least one letter and one digit")
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {
            "password_hash": hash_password(payload.new_password),
            "force_password_change": payload.force_change_on_next_login,
        }},
    )
    # Invalidate any active sessions
    await db.user_sessions.delete_many({"user_id": user_id})
    await db.audit_log.insert_one({
        "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
        "ts": iso(now_utc()),
        "actor": _admin["user_id"],
        "action": "admin.user.password_reset",
        "target_user_id": user_id,
        "amount": 0,
        "meta": {"force_change_on_next_login": payload.force_change_on_next_login},
    })
    return {"ok": True}


class AdminBonusPayload(BaseModel):
    amount: float = Field(..., gt=0)
    note: Optional[str] = None


@api.post("/admin/users/{user_id}/bonus")
async def admin_add_bonus(
    user_id: str,
    payload: AdminBonusPayload,
    _admin: dict = Depends(require_admin),
):
    """Manually credit a bonus to the user. Counts toward the user's 2X cap."""
    existing = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    cap_st = await earnings_engine.cap_status(db, user_id)
    if cap_st["reached"]:
        raise HTTPException(status_code=400, detail="User has already reached the 2X cap")
    clipped = min(payload.amount, cap_st["remaining"])
    today = now_utc().date()
    earning_id = f"earn_{uuid.uuid4().hex[:12]}"
    await db.earnings.insert_one({
        "earning_id": earning_id,
        "user_id": user_id,
        "type": "referral",  # bonus modeled as a referral-style one-time credit
        "amount": round(float(clipped), 6),
        "period_date": today.isoformat(),
        "source_user_id": None,
        "source_investment_id": None,
        "source_level": 0,
        "capped": clipped < payload.amount,
        "status": "locked",
        "payout_id": None,
        "meta": {"is_admin_bonus": True, "note": payload.note, "actor": _admin["user_id"]},
        "created_at": iso(now_utc()),
        "paid_at": None,
    })
    await db.audit_log.insert_one({
        "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
        "ts": iso(now_utc()),
        "actor": _admin["user_id"],
        "action": "admin.user.bonus_added",
        "target_user_id": user_id,
        "amount": round(float(clipped), 6),
        "meta": {"requested": payload.amount, "clipped": clipped < payload.amount, "note": payload.note},
    })
    return {"ok": True, "credited": round(float(clipped), 6), "earning_id": earning_id}


@api.get("/admin/users/{user_id}/transactions")
async def admin_user_transactions(user_id: str, _admin: dict = Depends(require_admin)):
    """Return all deposits / investments for a user."""
    if not await db.users.find_one({"user_id": user_id}, {"_id": 0}):
        raise HTTPException(status_code=404, detail="User not found")
    cursor = db.investments.find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1)
    return [doc async for doc in cursor]


@api.get("/admin/users/{user_id}/withdrawals")
async def admin_user_withdrawals(user_id: str, _admin: dict = Depends(require_admin)):
    """Return weekly payouts (settled withdrawals) for a user."""
    if not await db.users.find_one({"user_id": user_id}, {"_id": 0}):
        raise HTTPException(status_code=404, detail="User not found")
    cursor = db.weekly_payouts.find({"user_id": user_id}, {"_id": 0}).sort("week_end_date", -1)
    return [doc async for doc in cursor]


# -----------------------------------------------------------------------------
# Deposit network toggles — admin can enable/disable TGcoin
# -----------------------------------------------------------------------------
DEFAULT_NETWORKS = [
    {"network_id": "tgcoin", "label": "USDT · TGcoin", "address": "TGxxxx...your-tgcoin-address",
     "is_active": True, "sort_order": 0, "kind": "crypto"},
    {"network_id": "cash",   "label": "Cash payment",  "address": "Visit our office / call to coordinate cash pickup",
     "is_active": True, "sort_order": 1, "kind": "cash"},
]


async def _ensure_networks_seeded():
    # Insert any of the default rows that are missing — idempotent, so old DBs
    # automatically gain the new "cash" row the first time this runs.
    for n in DEFAULT_NETWORKS:
        await db.deposit_networks.update_one(
            {"network_id": n["network_id"]},
            {"$setOnInsert": {**n, "created_at": iso(now_utc())}},
            upsert=True,
        )


@api.get("/deposit-networks")
async def list_active_networks():
    """Public — used by the Invest page to render active deposit options."""
    await _ensure_networks_seeded()
    cursor = db.deposit_networks.find({"is_active": True}, {"_id": 0}).sort("sort_order", 1)
    return [doc async for doc in cursor]


@api.get("/admin/deposit-networks")
async def admin_list_networks(_admin: dict = Depends(require_admin)):
    await _ensure_networks_seeded()
    cursor = db.deposit_networks.find({}, {"_id": 0}).sort("sort_order", 1)
    return [doc async for doc in cursor]


class NetworkUpdatePayload(BaseModel):
    label: Optional[str] = None
    address: Optional[str] = None
    is_active: Optional[bool] = None


@api.patch("/admin/deposit-networks/{network_id}")
async def admin_update_network(
    network_id: str,
    payload: NetworkUpdatePayload,
    _admin: dict = Depends(require_admin),
):
    update = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if not update:
        n = await db.deposit_networks.find_one({"network_id": network_id}, {"_id": 0})
        if not n:
            raise HTTPException(status_code=404, detail="Network not found")
        return n
    result = await db.deposit_networks.update_one({"network_id": network_id}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Network not found")
    # Safety — keep at least one network active
    active_count = await db.deposit_networks.count_documents({"is_active": True})
    if active_count == 0:
        await db.deposit_networks.update_one({"network_id": network_id}, {"$set": {"is_active": True}})
        raise HTTPException(status_code=400, detail="At least one deposit network must remain active")
    n = await db.deposit_networks.find_one({"network_id": network_id}, {"_id": 0})
    return n


@api.get("/admin/overview")
async def admin_overview(_admin: dict = Depends(require_admin)):
    total_users = await db.users.count_documents({"role": "investor"})
    total_invs = await db.investments.count_documents({})
    pending = await db.investments.count_documents({"status": "pending"})
    active = await db.investments.count_documents({"status": "active"})
    leads = await db.leads.count_documents({})
    # Sum of active capital
    pipeline = [
        {"$match": {"status": "active"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    total_capital = 0.0
    async for r in db.investments.aggregate(pipeline):
        total_capital = r.get("total", 0.0)
    return {
        "total_users": total_users,
        "total_investments": total_invs,
        "pending_investments": pending,
        "active_investments": active,
        "active_capital": total_capital,
        "total_leads": leads,
    }


# -----------------------------------------------------------------------------
# Startup — indexes + admin seed + default plans
# -----------------------------------------------------------------------------
DEFAULT_PLANS = [
    {"name": "Starter",     "min_amount": 100,    "max_amount": 499,    "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "Test the waters with a disciplined first position.",     "sort_order": 1},
    {"name": "Bronze",      "min_amount": 500,    "max_amount": 999,    "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "A measured step up — same formula, larger output.",      "sort_order": 2},
    {"name": "Silver",      "min_amount": 1000,   "max_amount": 4999,   "daily_roi": 0.005, "working_days": 400, "badge": "Most Popular",  "blurb": "The sweet spot most investors land on.",                 "sort_order": 3},
    {"name": "Gold",        "min_amount": 5000,   "max_amount": 9999,   "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "Serious capital, serious weekly payouts.",               "sort_order": 4},
    {"name": "Platinum",    "min_amount": 10000,  "max_amount": 99999,  "daily_roi": 0.005, "working_days": 400, "badge": None,            "blurb": "Built for committed allocators.",                        "sort_order": 5},
    {"name": "Sovereign",   "min_amount": 100000, "max_amount": 100000, "daily_roi": 0.005, "working_days": 400, "badge": "Institutional", "blurb": "Institutional tier. White-glove onboarding.",            "sort_order": 6},
]


@app.on_event("startup")
async def on_startup():
    """All startup side-effects are wrapped so that a single failure
    (e.g. a stale index, a Mongo timeout during seeding) doesn't take the
    whole pod down. The app should always start serving health-check
    traffic — even if downstream seeding fails."""

    async def _safe(label, coro):
        try:
            return await coro
        except Exception as e:
            logger.exception("startup step '%s' failed (continuing): %s", label, e)
            return None

    # ---- Indexes -----------------------------------------------------------
    async def _safe_index(coll, keys, **opts):
        try:
            await coll.create_index(keys, **opts)
        except Exception as e:
            logger.warning("create_index skip on %s %s: %s", coll.name, keys, e)

    await _safe_index(db.users, "email", unique=True)
    await _safe_index(db.users, "user_id", unique=True)
    # Use partialFilterExpression so docs with null/missing referral_code don't
    # collide on the unique index. (Plain `sparse=True` still indexes null values
    # in some Mongo versions and breaks MongoDB Atlas migrations.)
    await _safe_index(
        db.users,
        "referral_code",
        unique=True,
        partialFilterExpression={"referral_code": {"$type": "string"}},
        name="referral_code_unique_str",
    )
    await _safe_index(db.users, "referred_by")
    await _safe_index(db.investments, [("user_id", 1), ("created_at", -1)])
    await _safe_index(db.investments, "investment_id", unique=True)
    await _safe_index(db.plans, "plan_id", unique=True)
    await _safe_index(db.leads, "lead_id", unique=True)
    await _safe_index(db.user_sessions, "session_token", unique=True)
    await _safe_index(db.earnings, [("user_id", 1), ("period_date", -1)])
    await _safe_index(db.earnings, [("user_id", 1), ("type", 1), ("period_date", 1)])
    await _safe_index(db.earnings, "earning_id", unique=True)
    await _safe_index(db.earnings, [("status", 1), ("period_date", 1)])
    await _safe_index(db.weekly_payouts, "payout_id", unique=True)
    await _safe_index(db.weekly_payouts, [("user_id", 1), ("week_end_date", -1)])
    await _safe_index(db.audit_log, [("ts", -1)])
    await _safe_index(db.audit_log, [("target_user_id", 1), ("ts", -1)])

    # ---- Admin seed (idempotent) ------------------------------------------
    try:
        existing = await db.users.find_one({"email": ADMIN_EMAIL})
        if not existing:
            await db.users.insert_one({
                "user_id": f"user_{uuid.uuid4().hex[:12]}",
                "email": ADMIN_EMAIL,
                "name": "Trade Gain Admin",
                "password_hash": hash_password(ADMIN_PASSWORD),
                "role": "admin",
                "auth_provider": "password",
                "wallet_address": None,
                "phone": None,
                "country": None,
                "referral_code": await _unique_referral_code(),
                "referred_by": None,
                "picture": None,
                "created_at": iso(now_utc()),
            })
            logger.info("Seeded admin user: %s", ADMIN_EMAIL)
        else:
            # Keep password in sync with env (idempotent reset)
            if not verify_password(ADMIN_PASSWORD, existing.get("password_hash", "")):
                await db.users.update_one(
                    {"email": ADMIN_EMAIL},
                    {"$set": {"password_hash": hash_password(ADMIN_PASSWORD), "role": "admin"}},
                )
                logger.info("Re-synced admin password from env")
            # Backfill referral_code if missing
            if not existing.get("referral_code"):
                await db.users.update_one(
                    {"email": ADMIN_EMAIL},
                    {"$set": {"referral_code": await _unique_referral_code()}},
                )
    except Exception as e:
        logger.exception("admin seed failed (continuing): %s", e)

    # ---- Default plans (idempotent) ---------------------------------------
    try:
        count = await db.plans.count_documents({})
        if count == 0:
            docs = []
            for p in DEFAULT_PLANS:
                d = dict(p)
                d["plan_id"] = f"plan_{uuid.uuid4().hex[:10]}"
                d["is_active"] = True
                d["created_at"] = iso(now_utc())
                docs.append(d)
            await db.plans.insert_many(docs)
            logger.info("Seeded %d default plans", len(docs))
    except Exception as e:
        logger.exception("plan seed failed (continuing): %s", e)

    # ---- Daily payout scheduler (best-effort) -----------------------------
    try:
        start_scheduler(db)
    except Exception as e:
        logger.exception("Failed to start payout scheduler (continuing): %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    try:
        stop_scheduler()
    except Exception:
        pass
    client.close()


@api.get("/")
async def root():
    return {"service": "tradegain-capital", "status": "ok"}


@api.get("/health")
async def healthcheck():
    """Lightweight health probe that does NOT touch MongoDB.

    Used by Kubernetes readiness/liveness probes. A dedicated Mongo ping
    endpoint can be added later if needed; this one stays cheap so a
    transient Atlas hiccup never marks the pod unhealthy.
    """
    return {"status": "ok"}


# =============================================================================
# Web Push (RFC 8030 + VAPID)
# =============================================================================
class PushSubscriptionPayload(BaseModel):
    endpoint: str
    keys: Dict[str, str]
    user_agent: Optional[str] = None


class PushCampaignPayload(BaseModel):
    title: str = Field(..., max_length=80)
    body: str = Field(..., max_length=240)
    icon: Optional[str] = None
    image: Optional[str] = None  # large hero image URL (Chrome/Edge/Android)
    cta_url: Optional[str] = None
    target_type: Literal["all", "user"] = "all"
    target_user_id: Optional[str] = None
    # If schedule_at is None → send immediately when triggered.
    schedule_at: Optional[str] = None  # ISO 8601 (UTC). None → send now.


@api.get("/push/vapid-public-key")
async def push_public_key():
    keys = await push_module.get_vapid_keys(db)
    return {"public_key": keys["public"]}


@api.post("/push/subscribe")
async def push_subscribe(payload: PushSubscriptionPayload, user: dict = Depends(get_current_user)):
    if not payload.keys.get("p256dh") or not payload.keys.get("auth"):
        raise HTTPException(status_code=400, detail="Missing p256dh / auth keys")
    sub_id = f"sub_{uuid.uuid4().hex[:14]}"
    doc = {
        "sub_id": sub_id,
        "user_id": user["user_id"],
        "endpoint": payload.endpoint,
        "p256dh": payload.keys["p256dh"],
        "auth": payload.keys["auth"],
        "user_agent": payload.user_agent,
        "created_at": iso(now_utc()),
    }
    # Upsert on endpoint — one subscription per browser/device
    await db.push_subscriptions.update_one(
        {"endpoint": payload.endpoint},
        {"$set": doc},
        upsert=True,
    )
    return {"ok": True, "sub_id": sub_id}


@api.post("/push/unsubscribe")
async def push_unsubscribe(payload: Dict[str, str], user: dict = Depends(get_current_user)):
    endpoint = payload.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint required")
    await db.push_subscriptions.delete_one({"endpoint": endpoint, "user_id": user["user_id"]})
    return {"ok": True}


@api.get("/push/click/{campaign_id}")
async def push_click(campaign_id: str):
    """Click-tracking redirect. Increments campaign.click_count then 302s to cta_url."""
    camp = await db.push_campaigns.find_one({"campaign_id": campaign_id}, {"_id": 0})
    target = (camp or {}).get("cta_url_real") or (camp or {}).get("cta_url") or "/"
    # The campaign's stored cta_url is the user-supplied destination; on send we
    # rewrite it to the tracking URL. We capture the original under cta_url_real.
    await db.push_campaigns.update_one(
        {"campaign_id": campaign_id},
        {"$inc": {"click_count": 1}},
    )
    return RedirectResponse(url=target, status_code=302)


# --- Admin endpoints --------------------------------------------------------
@api.post("/admin/push/campaigns")
async def admin_create_campaign(payload: PushCampaignPayload, user: dict = Depends(require_admin)):
    cid = f"camp_{uuid.uuid4().hex[:12]}"
    is_scheduled = bool(payload.schedule_at)
    doc = {
        "campaign_id": cid,
        "title": payload.title,
        "body": payload.body,
        "icon": payload.icon,
        "image": payload.image,
        "cta_url_real": payload.cta_url or "/",
        "cta_url": payload.cta_url or "/",
        "target_type": payload.target_type,
        "target_user_id": payload.target_user_id,
        "status": "scheduled" if is_scheduled else "draft",
        "scheduled_at": payload.schedule_at,
        "created_by": user["user_id"],
        "created_at": iso(now_utc()),
        "sent_count": 0,
        "failed_count": 0,
        "click_count": 0,
        "total_targets": 0,
    }
    await db.push_campaigns.insert_one(doc)
    return {"ok": True, "campaign_id": cid, "status": doc["status"]}


@api.get("/admin/push/campaigns")
async def admin_list_campaigns(status_filter: Optional[str] = None, user: dict = Depends(require_admin)):
    q: Dict[str, Any] = {}
    if status_filter:
        q["status"] = status_filter
    cursor = db.push_campaigns.find(q, {"_id": 0}).sort("created_at", -1).limit(200)
    return await cursor.to_list(None)


@api.get("/admin/push/campaigns/{campaign_id}")
async def admin_campaign_detail(campaign_id: str, user: dict = Depends(require_admin)):
    c = await db.push_campaigns.find_one({"campaign_id": campaign_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c


@api.post("/admin/push/campaigns/{campaign_id}/send")
async def admin_send_campaign(campaign_id: str, user: dict = Depends(require_admin)):
    c = await db.push_campaigns.find_one({"campaign_id": campaign_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c["status"] == "sent":
        raise HTTPException(status_code=400, detail="Already sent")
    res = await push_module.send_campaign(db, c)
    return {"ok": True, **res}


@api.delete("/admin/push/campaigns/{campaign_id}")
async def admin_delete_campaign(campaign_id: str, user: dict = Depends(require_admin)):
    await db.push_campaigns.delete_one({"campaign_id": campaign_id})
    return {"ok": True}


@api.get("/admin/push/subscribers")
async def admin_list_subscribers(user: dict = Depends(require_admin)):
    cursor = db.push_subscriptions.find({}, {"_id": 0, "p256dh": 0, "auth": 0}).sort("created_at", -1).limit(500)
    return await cursor.to_list(None)


@api.get("/admin/push/stats")
async def admin_push_stats(user: dict = Depends(require_admin)):
    total_subs = await db.push_subscriptions.count_documents({})
    pipe = [
        {"$group": {
            "_id": None,
            "sent": {"$sum": "$sent_count"},
            "failed": {"$sum": "$failed_count"},
            "clicks": {"$sum": "$click_count"},
            "campaigns": {"$sum": 1},
        }}
    ]
    agg = {}
    async for r in db.push_campaigns.aggregate(pipe):
        agg = r
    return {
        "total_subscribers": total_subs,
        "campaigns_total": int(agg.get("campaigns", 0)),
        "sent_total": int(agg.get("sent", 0)),
        "failed_total": int(agg.get("failed", 0)),
        "click_total": int(agg.get("clicks", 0)),
    }

# ============================================================
# Admin: production data cleanup (DESTRUCTIVE)
# ============================================================
class WipeTestDataPayload(BaseModel):
    confirm: str  # must equal "WIPE-ALL-TEST-DATA"


@api.post("/admin/system/wipe-test-data")
async def admin_wipe_test_data(
    payload: WipeTestDataPayload,
    admin: dict = Depends(require_admin),
):
    """One-shot endpoint to wipe ALL investor/transaction data before launch.

    Preserves: admin users, plans, app_config, deposit_networks.
    Wipes: investments, earnings, weekly_payouts, push_subscriptions,
           push_campaigns, audit_log, leads, user_sessions, all non-admin users.

    Requires admin auth + a literal `confirm` field set to `WIPE-ALL-TEST-DATA`
    so it cannot be called accidentally.
    """
    if payload.confirm != "WIPE-ALL-TEST-DATA":
        raise HTTPException(
            status_code=400,
            detail="Refusing to wipe: 'confirm' must equal 'WIPE-ALL-TEST-DATA'.",
        )

    truncate_collections = [
        "investments",
        "earnings",
        "weekly_payouts",
        "push_subscriptions",
        "push_campaigns",
        "audit_log",
        "leads",
        "user_sessions",
    ]

    report = {"truncated": {}, "users_kept": 0, "users_deleted": 0}
    for col in truncate_collections:
        result = await db[col].delete_many({})
        report["truncated"][col] = result.deleted_count

    report["users_kept"] = await db.users.count_documents({"role": "admin"})
    user_del = await db.users.delete_many({"role": {"$ne": "admin"}})
    report["users_deleted"] = user_del.deleted_count

    return {
        "ok": True,
        "ran_by": admin.get("email"),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        **report,
    }




# ============================================================
# Admin: full DB export (schema + data) — for moving the app
# to another host / making a backup.
#
# Self-updating: collections + indexes are discovered live from MongoDB
# at export time, so new collections or indexes added by future features
# are picked up automatically. No code change needed when the schema grows.
# ============================================================
import io
import json as _json
import zipfile
from fastapi.responses import StreamingResponse

# System collections / indexes we never want to ship in a backup.
_EXPORT_SKIP_COLLECTIONS = {"system.views", "system.indexes", "system.profile"}
_EXPORT_SKIP_INDEX_NAMES = {"_id_"}


async def _discover_collections() -> list:
    """Return every user collection currently in MongoDB, sorted for stable diffs."""
    names = await db.list_collection_names()
    return sorted(n for n in names if n not in _EXPORT_SKIP_COLLECTIONS and not n.startswith("system."))


async def _discover_indexes(coll_name: str):
    """Return list of (key_spec_dict, opts_dict) tuples for every index on the collection,
    EXCEPT the default _id index (Mongo creates that automatically on insert).
    Pulled live from MongoDB so future indexes are auto-included."""
    info = await db[coll_name].index_information()  # {name: {key: [...], unique?, partialFilterExpression?, ...}}
    out = []
    for name, spec in info.items():
        if name in _EXPORT_SKIP_INDEX_NAMES:
            continue
        # spec["key"] is a list of (field, direction) tuples
        key_dict = {k: v for k, v in spec.get("key", [])}
        opts = {}
        for opt_key in ("unique", "sparse", "partialFilterExpression", "expireAfterSeconds"):
            if opt_key in spec:
                opts[opt_key] = spec[opt_key]
        # Preserve original index name so the restored DB looks identical
        opts["name"] = name
        out.append((key_dict, opts))
    return out


def _strip_id(doc: dict) -> dict:
    """Strip Mongo _id so dumps replay cleanly on any host. ISO dates kept as strings."""
    return {k: v for k, v in doc.items() if k != "_id"}


def _build_mongosh_script(snapshot: dict, indexes: dict, collections: list) -> str:
    """Build a single self-contained mongosh script that drops + recreates everything."""
    lines = [
        "// =============================================================================",
        "// TradeGain Capital — Full MongoDB Database Dump",
        f"// Generated: {iso(now_utc())}",
        f"// Collections: {len(collections)}  ·  total documents: {sum(len(v) for v in snapshot.values())}",
        "//",
        "// Run on the target Mongo host:",
        '//   mongosh "$MONGO_URL/$DB_NAME" database_full.js',
        "// =============================================================================",
        "",
        "function _reviveDates(o) {",
        "  if (o === null || typeof o !== 'object') return o;",
        "  if (Array.isArray(o)) return o.map(_reviveDates);",
        "  if (Object.keys(o).length === 1 && '$date' in o) return new Date(o['$date']);",
        "  const out = {};",
        "  for (const k of Object.keys(o)) out[k] = _reviveDates(o[k]);",
        "  return out;",
        "}",
        "",
        "// Drop existing collections (clean slate)",
    ]
    for c in collections:
        lines.append(f"try {{ db['{c}'].drop(); }} catch (e) {{}}")
    lines.append("")
    lines.append("// Insert all documents")
    for c in collections:
        docs = snapshot.get(c, [])
        lines.append(f"// {c} — {len(docs)} document(s)")
        if not docs:
            lines.append(f"db.createCollection('{c}');")
        else:
            payload = _json.dumps(docs, indent=2, ensure_ascii=False, default=str)
            lines.append(f"db['{c}'].insertMany(_reviveDates({payload}));")
        lines.append("")
    lines.append("// Indexes")
    for c in collections:
        for keys, opts in indexes.get(c, []):
            lines.append(f"db['{c}'].createIndex({_json.dumps(keys)}, {_json.dumps(opts) if opts else '{}'});")
    lines.append("")
    lines.append("print('Database restored — collections, indexes, and all data are in place.');")
    return "\n".join(lines)


def _build_schema_md(collections: list, indexes: dict, snapshot: dict) -> str:
    lines = [
        "# TradeGain Capital — MongoDB Schema (export snapshot)\n",
        f"Generated: {iso(now_utc())}",
        f"Source DB: `{DB_NAME}`",
        f"Collections: **{len(collections)}**  ·  documents: **{sum(len(v) for v in snapshot.values())}**\n",
        "## Collections\n",
    ]
    for c in collections:
        doc_count = len(snapshot.get(c, []))
        idx_count = len(indexes.get(c, []))
        lines.append(f"- `{c}` — {doc_count} doc(s), {idx_count} index(es)")
    lines.append(
        "\nSee `init_mongo.js` for the full index list and `database_full.js`"
        " for a one-shot restore including all documents.\n"
    )
    return "\n".join(lines)


def _build_init_mongo_js(collections: list, indexes: dict) -> str:
    """Schema-only initialiser: collections + indexes, no data."""
    lines = [
        "// TradeGain Capital — schema-only init (collections + indexes, no data).",
        f"// Generated: {iso(now_utc())}  ·  {len(collections)} collections",
        "// Run on the target Mongo host:",
        "//   mongosh \"$MONGO_URL/$DB_NAME\" init_mongo.js",
        "",
    ]
    for c in collections:
        lines.append(f"try {{ db.createCollection('{c}'); }} catch (e) {{}}")
    lines.append("")
    for c in collections:
        for keys, opts in indexes.get(c, []):
            lines.append(f"db['{c}'].createIndex({_json.dumps(keys)}, {_json.dumps(opts) if opts else '{}'});")
    lines.append("")
    lines.append("print('Schema created — collections + indexes are in place.');")
    return "\n".join(lines)


_BUNDLE_README = """# TradeGain Capital — Database Bundle

This ZIP contains the FULL schema + all current data of the TradeGain Capital
MongoDB database. Use it to move the app to another host (Atlas, self-hosted,
local dev, etc.).

## What's inside

- `database_full.js`   ← single mongosh script (schema + indexes + ALL data)
- `init_mongo.js`      ← schema-only init (collections + indexes, no data)
- `schema.md`          ← human-readable schema reference
- `json/*.json`        ← per-collection data exports

## Restore — Option A (recommended, no extra tools)

    mongosh "<YOUR_MONGO_URI>/tradegain" database_full.js

This drops every collection, recreates them with indexes, and inserts every
document. Safe to re-run.

## Restore — Option B (mongoimport)

    for f in json/*.json; do
      mongoimport --uri="<YOUR_MONGO_URI>" --collection=$(basename $f .json) \\
                  --jsonArray --file="$f"
    done
    mongosh "<YOUR_MONGO_URI>/tradegain" init_mongo.js   # creates indexes

## Then point the backend at the new DB

Edit `backend/.env`:

    MONGO_URL=<your new mongo uri>
    DB_NAME=tradegain

…and restart the backend. The React frontend needs no changes.
"""


@api.get("/admin/system/export-db")
async def admin_export_db(_admin: dict = Depends(require_admin)):
    """Export the full database (schema + indexes + data) as a downloadable ZIP.

    Self-updating: collections and indexes are discovered live from MongoDB,
    so any new collection / index added by future features is automatically
    included — no code change needed.

    The ZIP contains:
      • database_full.js — single mongosh script, drops + recreates everything
      • init_mongo.js    — schema-only (collections + indexes)
      • schema.md        — human-readable schema reference with live doc counts
      • json/*.json      — per-collection data exports
      • README.md        — restore instructions

    Suitable for moving the application to any MongoDB host (Atlas included).
    """
    # 1) Discover collections + indexes LIVE — picks up anything new automatically.
    collections = await _discover_collections()
    indexes: dict = {}
    for coll_name in collections:
        indexes[coll_name] = await _discover_indexes(coll_name)

    # 2) Snapshot every collection (strip _id so dumps replay cleanly anywhere)
    snapshot: dict = {}
    for coll_name in collections:
        docs = []
        async for d in db[coll_name].find({}):
            docs.append(_strip_id(d))
        snapshot[coll_name] = docs

    # 3) Build the ZIP entirely in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", _BUNDLE_README)
        z.writestr("schema.md", _build_schema_md(collections, indexes, snapshot))
        z.writestr("init_mongo.js", _build_init_mongo_js(collections, indexes))
        z.writestr("database_full.js", _build_mongosh_script(snapshot, indexes, collections))
        for coll_name, docs in snapshot.items():
            z.writestr(f"json/{coll_name}.json", _json.dumps(docs, indent=2, ensure_ascii=False, default=str))

    buf.seek(0)
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    filename = f"tradegain-db-{stamp}.zip"
    await _audit(
        "admin.system.db_exported",
        actor=_admin["user_id"],
        meta={
            "collections": len(snapshot),
            "doc_counts": {k: len(v) for k, v in snapshot.items()},
            "index_counts": {k: len(v) for k, v in indexes.items()},
        },
    )
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================
# Admin: reports (investments / earnings / payouts / investors / leads)
# Each supports format=json|pdf|xlsx and date-range + free-text filters.
# ============================================================
@api.get("/admin/reports/{name}")
async def admin_get_report(
    name: str,
    format: str = "json",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    _admin: dict = Depends(require_admin),
):
    return await reports_module.run_report(
        db, name=name, fmt=format.lower(),
        date_from=date_from, date_to=date_to, q=q,
    )




app.include_router(api)

