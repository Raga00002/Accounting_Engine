"""Master (administrator) database: accounts, sessions, plans, subscriptions, payments, usage,
settings / customizations and the audit log. Lives in ``data/master.db`` – separate from every
tenant database, which holds only that user's accounting data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config, secrets

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name           TEXT NOT NULL DEFAULT '',
    password_hash  TEXT,
    role           TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending','active','disabled')),
    google_sub     TEXT UNIQUE,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    own_api_key    TEXT,                       -- encrypted; optional user-supplied Anthropic key
    allow_own_key  INTEGER NOT NULL DEFAULT 1,
    business_name  TEXT,
    phone          TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_login     TEXT,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   TEXT,
    signup_source  TEXT NOT NULL DEFAULT 'admin'  -- admin | self | google
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    user_agent  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    days        INTEGER NOT NULL DEFAULT 30,
    scan_quota  INTEGER,                        -- NULL = unlimited scans within the period
    price_inr   REAL NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    description TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_name   TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    scan_quota  INTEGER,
    scans_used  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | razorpay | trial
    reference   TEXT,
    note        TEXT,
    created_by  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_name      TEXT NOT NULL,
    amount_inr     REAL NOT NULL,
    provider       TEXT NOT NULL DEFAULT 'razorpay',
    link_id        TEXT,                          -- razorpay payment link id
    short_url      TEXT,
    payment_id     TEXT,
    status         TEXT NOT NULL DEFAULT 'created', -- created | paid | expired | cancelled | failed
    raw            TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    paid_at        TEXT,
    subscription_id INTEGER,
    plan_days      INTEGER,                       -- snapshot of the plan when the link was created
    plan_quota     INTEGER,
    claimed_at     TEXT                           -- set while a 'confirming' activation is in progress
);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts            TEXT NOT NULL DEFAULT (datetime('now')),
    kind          TEXT NOT NULL,                  -- bill_image | bill_text | slip_image
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    own_key       INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    scope   TEXT NOT NULL,                        -- 'global' or 'user:<id>'
    key     TEXT NOT NULL,
    value   TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    actor_id  INTEGER,
    actor     TEXT,
    action    TEXT NOT NULL,
    target    TEXT,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id, end_date);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""

SESSION_DAYS = 14
LOCK_AFTER_FAILURES = 6
LOCK_MINUTES = 15
_DUMMY_HASH = secrets.hash_password("dummy-password-for-timing")

# Per-client throttling of login failures and sign-ups (in memory, per process). Protects against
# an attacker locking other people's accounts on purpose and against sign-up spam.
IP_MAX_FAILURES = 20       # failed logins per IP within IP_WINDOW_MINUTES -> refused for IP_WINDOW_MINUTES
IP_MAX_SIGNUPS = 5
IP_WINDOW_MINUTES = 15   # also the window of the per-(account, client) lock (LOCK_MINUTES)
_ip_events: dict[tuple[str, str], list[float]] = {}
_ip_lock = threading.Lock()


def _throttle(kind: str, client: str, limit: int) -> tuple[bool, int]:
    """(allowed, attempts so far). Records nothing – call throttle_hit() after a failure."""
    import time as _t

    now = _t.time()
    window = (LOCK_MINUTES if kind == "account" else IP_WINDOW_MINUTES) * 60
    with _ip_lock:
        ev = [t for t in _ip_events.get((kind, client), []) if now - t < window]
        _ip_events[(kind, client)] = ev
        if len(_ip_events) > 5000:   # never grow without bound
            for k in [k for k, v in _ip_events.items() if not v or now - v[-1] > IP_WINDOW_MINUTES * 60]:
                _ip_events.pop(k, None)
        return len(ev) < limit, len(ev)


def throttle_check(kind: str, client: str) -> bool:
    limit = IP_MAX_SIGNUPS if kind == "signup" else IP_MAX_FAILURES
    return _throttle(kind, client or "?", limit)[0]


def throttle_hit(kind: str, client: str) -> None:
    import time as _t

    with _ip_lock:
        _ip_events.setdefault((kind, client or "?"), []).append(_t.time())

DEFAULT_PLANS = [
    ("Trial 7 days", 7, 25, 0, "Free trial – 25 scans"),
    ("Monthly", 30, 500, 999, "30 days · 500 scans"),
    ("Quarterly", 90, 1800, 2499, "90 days · 1,800 scans"),
    ("Yearly", 365, 8000, 8999, "365 days · 8,000 scans"),
]

# Keys the administrator may set globally and override per user. Anything else is ignored.
CUSTOMIZABLE_KEYS = {
    "app_name": "App name shown in the sidebar and browser tab",
    "logo_url": "Logo image URL (https://…) shown in the sidebar",
    "primary_color": "Accent colour, e.g. #1f77b4",
    "custom_css": "Extra CSS injected into every page",
    "welcome_text": "Text shown at the top of the New Bill page",
    "announcement": "Banner shown to users on every page (blank = none)",
    "labels_json": "JSON of UI label overrides, e.g. {\"New Bill\": \"Naya Bill\"}",
    "features_json": "JSON of feature flags: voice, bank_slips, tally_import, own_api_key, google_login, signup",
    "prompt_notes": "Extra instructions appended to the bill/slip reading prompt (trade conventions, item names)",
    "default_tally_json": "JSON of default Tally voucher settings for new users",
    "support_contact": "Shown on the subscription page (phone / UPI / email for recharge)",
    "upi_id": "UPI ID shown for manual recharge",
}

DEFAULT_FEATURES = {"voice": True, "bank_slips": True, "tally_import": True, "own_api_key": True,
                    "google_login": True, "signup": True}


def master_path() -> Path:
    return config.DATA_DIR / "master.db"


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    p = path or master_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(c: sqlite3.Connection, table: str, column: str, decl: str) -> bool:
    """Add a column to an existing database; True when it was just added."""
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def init(path: Optional[Path] = None) -> None:
    with connect(path) as c:
        c.executescript(SCHEMA)
        _ensure_column(c, "payments", "plan_days", "INTEGER")
        _ensure_column(c, "payments", "plan_quota", "INTEGER")
        _ensure_column(c, "payments", "claimed_at", "TEXT")
        if _ensure_column(c, "users", "signup_source", "TEXT NOT NULL DEFAULT 'admin'"):
            # accounts that registered themselves before this column existed must not become Google-linkable
            c.execute("""UPDATE users SET signup_source='self' WHERE role='user' AND google_sub IS NULL
                         AND email IN (SELECT target FROM audit WHERE action='signup')""")
        n = c.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        if n == 0:
            c.executemany(
                "INSERT INTO plans (name, days, scan_quota, price_inr, description) VALUES (?,?,?,?,?)",
                DEFAULT_PLANS,
            )


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    status: str
    must_change_pw: bool
    allow_own_key: bool
    has_own_key: bool
    business_name: Optional[str]
    phone: Optional[str]
    google_sub: Optional[str]
    created_at: str
    last_login: Optional[str]
    notes: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _user(r: sqlite3.Row) -> User:
    return User(
        id=r["id"], email=r["email"], name=r["name"] or "", role=r["role"], status=r["status"],
        must_change_pw=bool(r["must_change_pw"]), allow_own_key=bool(r["allow_own_key"]),
        has_own_key=bool(r["own_api_key"]), business_name=r["business_name"], phone=r["phone"],
        google_sub=r["google_sub"], created_at=r["created_at"], last_login=r["last_login"], notes=r["notes"],
    )


@dataclass
class Access:
    allowed: bool
    reason: str = ""
    plan: Optional[str] = None
    end_date: Optional[date] = None
    scans_left: Optional[int] = None  # None = unlimited
    days_left: Optional[int] = None
    subscription_id: Optional[int] = None


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def user_count(path=None) -> int:
    with connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def admin_exists(path=None) -> bool:
    with connect(path) as c:
        return c.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None


def get_user(user_id: int, path=None) -> Optional[User]:
    with connect(path) as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user(r) if r else None


def get_user_by_email(email: str, path=None) -> Optional[User]:
    with connect(path) as c:
        r = c.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", ((email or "").strip(),)).fetchone()
    return _user(r) if r else None


def list_users(path=None, search: Optional[str] = None) -> list[User]:
    with connect(path) as c:
        if search:
            like = f"%{search.strip()}%"
            rows = c.execute("SELECT * FROM users WHERE email LIKE ? OR name LIKE ? OR business_name LIKE ? ORDER BY id",
                             (like, like, like)).fetchall()
        else:
            rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_user(r) for r in rows]


def create_user(email: str, password: Optional[str], name: str = "", role: str = "user", status: str = "active",
                must_change_pw: bool = False, business_name: Optional[str] = None, phone: Optional[str] = None,
                google_sub: Optional[str] = None, signup_source: str = "admin", path=None) -> User:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("Enter a valid e-mail address.")
    if role not in ("admin", "user") or status not in ("pending", "active", "disabled"):
        raise ValueError("bad role/status")
    pw_hash = secrets.hash_password(password) if password else None
    with connect(path) as c:
        if c.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
            raise ValueError("An account with this e-mail already exists.")
        cur = c.execute(
            """INSERT INTO users (email, name, password_hash, role, status, must_change_pw, business_name, phone, google_sub, signup_source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (email, (name or "").strip(), pw_hash, role, status, int(must_change_pw), business_name, phone, google_sub,
             signup_source if signup_source in ("admin", "self", "google") else "admin"),
        )
        r = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return _user(r)


def update_user(user_id: int, path=None, **fields) -> User:
    allowed = {"name", "role", "status", "must_change_pw", "allow_own_key", "business_name", "phone", "notes", "google_sub"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {bad}")
    if "role" in fields and fields["role"] not in ("admin", "user"):
        raise ValueError("bad role")
    if "status" in fields and fields["status"] not in ("pending", "active", "disabled"):
        raise ValueError("bad status")
    with connect(path) as c:
        if fields.get("role") == "user":
            # never demote the last administrator
            n_admin = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND id<>?", (user_id,)).fetchone()[0]
            cur = c.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if cur and cur["role"] == "admin" and n_admin == 0:
                raise ValueError("Cannot demote the only administrator.")
        if fields.get("status") in ("disabled", "pending"):
            n_admin = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND id<>?", (user_id,)).fetchone()[0]
            cur = c.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if cur and cur["role"] == "admin" and n_admin == 0:
                raise ValueError("Cannot disable the only administrator.")
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        c.execute(f"UPDATE users SET {sets} WHERE id=?", (*vals, user_id))
        if fields.get("status") == "disabled":
            c.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))
        r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not r:
        raise ValueError("user not found")
    return _user(r)


def set_password(user_id: int, password: str, must_change: bool = False, revoke_sessions: bool = True, path=None) -> None:
    h = secrets.hash_password(password)
    with connect(path) as c:
        c.execute("UPDATE users SET password_hash=?, must_change_pw=?, failed_logins=0, locked_until=NULL WHERE id=?",
                  (h, int(must_change), user_id))
        if revoke_sessions:
            c.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))


def set_own_api_key(user_id: int, api_key: Optional[str], path=None) -> None:
    enc = secrets.encrypt(api_key.strip()) if api_key and api_key.strip() else None
    with connect(path) as c:
        c.execute("UPDATE users SET own_api_key=? WHERE id=?", (enc, user_id))


def get_own_api_key(user_id: int, path=None) -> Optional[str]:
    with connect(path) as c:
        r = c.execute("SELECT own_api_key, allow_own_key FROM users WHERE id=?", (user_id,)).fetchone()
    if not r or not r["allow_own_key"]:
        return None
    return secrets.decrypt(r["own_api_key"])


def delete_user(user_id: int, path=None) -> None:
    with connect(path) as c:
        r = c.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if r and r["role"] == "admin":
            n = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND id<>?", (user_id,)).fetchone()[0]
            if n == 0:
                raise ValueError("Cannot delete the only administrator.")
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


# --------------------------------------------------------------------------- #
# authentication & sessions
# --------------------------------------------------------------------------- #
SLOWDOWN_SECONDS = 2.0   # every attempt on an account with >= LOCK_AFTER_FAILURES recent failures waits this long


def authenticate(email: str, password: str, client: str = "", path=None) -> tuple[Optional[User], str]:
    """Returns (user, message). user is None when login is refused; message explains why.

    Brute-force protection without a way to lock somebody else out:
    * ``LOCK_AFTER_FAILURES`` wrong passwords for one account *from one client* block that client
      for ``LOCK_MINUTES`` (the owner, on another connection, can still sign in);
    * after that many failures from anywhere the account answers slowly (``SLOWDOWN_SECONDS``);
    * the per-client throttle in the login page caps failures per address."""
    import time as _t

    email = (email or "").strip().lower()
    key = f"{email}|{client or '?'}"
    with connect(path) as c:
        r = c.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        if not r:
            secrets.verify_password(password or "x", _DUMMY_HASH)   # same cost as a real check (no e-mail enumeration by timing)
            return None, "Unknown e-mail or wrong password."
        allowed, n = _throttle("account", key, LOCK_AFTER_FAILURES)
        if not allowed:
            return None, f"Too many failed attempts – try again in {LOCK_MINUTES} minutes."
        if (r["failed_logins"] or 0) >= LOCK_AFTER_FAILURES:
            _t.sleep(SLOWDOWN_SECONDS)
        if not secrets.verify_password(password, r["password_hash"] or _DUMMY_HASH):
            throttle_hit("account", key)
            c.execute("UPDATE users SET failed_logins=failed_logins+1 WHERE id=?", (r["id"],))
            return None, "Unknown e-mail or wrong password."
        if r["status"] == "pending":
            return None, "Your account is waiting for the administrator's approval."
        if r["status"] == "disabled":
            return None, "This account is disabled. Contact the administrator."
        c.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login=datetime('now') WHERE id=?", (r["id"],))
        return _user(r), "ok"


def _token_hash(token: str) -> str:
    """Only a hash of the session token is stored – a copy of the database cannot be used to log in."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, user_agent: str = "", days: int = SESSION_DAYS, path=None) -> str:
    token = secrets.new_token(32)
    with connect(path) as c:
        c.execute("INSERT INTO sessions (token, user_id, expires_at, user_agent) VALUES (?,?,datetime('now', ?),?)",
                  (_token_hash(token), user_id, f"{int(days):+d} days", (user_agent or "")[:200]))
        # housekeeping
        c.execute("DELETE FROM sessions WHERE expires_at < datetime('now','-30 days')")
    return token


def user_for_session(token: Optional[str], path=None) -> Optional[User]:
    if not token or len(token) < 20:
        return None
    with connect(path) as c:
        r = c.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token=? AND s.revoked=0 AND s.expires_at > datetime('now') AND u.status='active'""",
            (_token_hash(token),),
        ).fetchone()
    return _user(r) if r else None


def revoke_session(token: Optional[str], path=None) -> None:
    if not token:
        return
    with connect(path) as c:
        c.execute("UPDATE sessions SET revoked=1 WHERE token=?", (_token_hash(token),))


def revoke_all_sessions(user_id: int, except_token: Optional[str] = None, path=None) -> None:
    with connect(path) as c:
        if except_token:
            c.execute("UPDATE sessions SET revoked=1 WHERE user_id=? AND token<>?", (user_id, _token_hash(except_token)))
        else:
            c.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))


def login_google(email: str, sub: str, name: str = "", auto_signup: bool = True, email_verified: bool = False,
                 path=None) -> tuple[Optional[User], str]:
    """Map a Google identity to an account.

    * an account already linked to this Google id signs in;
    * otherwise an account with the same e-mail is linked only when Google says the e-mail is verified
      and the account was created by the administrator (or by Google) – an e-mail that somebody
      registered themselves with a password is never taken over by a Google login;
    * unknown e-mails become *pending* accounts when sign-up is on."""
    email = (email or "").strip().lower()
    if not email or not sub:
        return None, "Google did not return an e-mail address."
    with connect(path) as c:
        r = c.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        if r is None:
            if not email_verified:
                return None, "Google reports this e-mail as not verified – verify it with Google or sign in with a password."
            r = c.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
            if r is not None and r["google_sub"] and r["google_sub"] != sub:
                return None, "This e-mail is linked to a different Google account."
            if r is not None and not r["google_sub"] and (r["signup_source"] or "admin") == "self":
                return None, "This e-mail was registered with a password – sign in with it, or ask the administrator to link Google."
        if r is None:
            if not auto_signup:
                return None, "No account for this Google e-mail. Ask the administrator to create one."
            c.execute("INSERT INTO users (email, name, role, status, google_sub, signup_source) VALUES (?,?,?,?,?,?)",
                      (email, name or "", "user", "pending", sub, "google"))
            return None, "Account created – it is waiting for the administrator's approval."
        if not r["google_sub"]:
            c.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, r["id"]))
        if r["status"] == "pending":
            return None, "Your account is waiting for the administrator's approval."
        if r["status"] == "disabled":
            return None, "This account is disabled. Contact the administrator."
        c.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (r["id"],))
        return _user(r), "ok"


# --------------------------------------------------------------------------- #
# plans & subscriptions
# --------------------------------------------------------------------------- #
def list_plans(path=None, only_active: bool = False) -> list[dict]:
    with connect(path) as c:
        rows = c.execute("SELECT * FROM plans" + (" WHERE active=1" if only_active else "") + " ORDER BY days, price_inr").fetchall()
    return [dict(r) for r in rows]


def upsert_plan(name: str, days: int, scan_quota: Optional[int], price_inr: float, active: bool = True,
                description: str = "", plan_id: Optional[int] = None, path=None) -> None:
    name = (name or "").strip()
    if not name or days <= 0:
        raise ValueError("Plan needs a name and a positive number of days.")
    with connect(path) as c:
        if plan_id:
            c.execute("UPDATE plans SET name=?, days=?, scan_quota=?, price_inr=?, active=?, description=? WHERE id=?",
                      (name, int(days), scan_quota, float(price_inr), int(active), description, plan_id))
        else:
            c.execute("""INSERT INTO plans (name, days, scan_quota, price_inr, active, description) VALUES (?,?,?,?,?,?)
                         ON CONFLICT(name) DO UPDATE SET days=excluded.days, scan_quota=excluded.scan_quota,
                         price_inr=excluded.price_inr, active=excluded.active, description=excluded.description""",
                      (name, int(days), scan_quota, float(price_inr), int(active), description))


def get_plan(name: str, path=None) -> Optional[dict]:
    with connect(path) as c:
        r = c.execute("SELECT * FROM plans WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def add_subscription(user_id: int, plan_name: str, source: str = "manual", reference: str = "", note: str = "",
                     created_by: Optional[int] = None, start: Optional[date] = None, days: Optional[int] = None,
                     scan_quota: Optional[int] = -1, path=None) -> int:
    """Add a subscription period.

    Stacking rule: if the user still has a usable period (days left AND quota left) the new period
    starts the day after the latest such period ends; if the current period is exhausted or there is
    none, it starts today so a recharge takes effect immediately. ``scan_quota=-1`` = plan's quota."""
    plan = get_plan(plan_name, path)
    if plan is None:
        if days is not None and scan_quota != -1:   # plan renamed/deleted meanwhile – use the snapshot given
            plan = {"name": plan_name, "days": int(days), "scan_quota": scan_quota}
        else:
            raise ValueError(f"Unknown plan '{plan_name}'.")
    with connect(path) as c:
        today = date.today().isoformat()
        rows = c.execute(
            "SELECT end_date, scan_quota, scans_used FROM subscriptions WHERE user_id=? AND cancelled=0 AND end_date >= ?",
            (user_id, today),
        ).fetchall()
        usable_ends = [r["end_date"] for r in rows if r["scan_quota"] is None or r["scans_used"] < r["scan_quota"]]
        s = start or (date.fromisoformat(max(usable_ends)) + timedelta(days=1) if usable_ends else date.today())
        d = int(days if days is not None else plan["days"])
        e = s + timedelta(days=d - 1)
        q = plan["scan_quota"] if scan_quota == -1 else scan_quota
        cur2 = c.execute(
            """INSERT INTO subscriptions (user_id, plan_name, start_date, end_date, scan_quota, source, reference, note, created_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, plan["name"], s.isoformat(), e.isoformat(), q, source, reference, note, created_by),
        )
        return cur2.lastrowid


def find_subscription(user_id: int, source: str, reference: str, path=None) -> Optional[int]:
    """Id of a period already added for this payment reference (idempotency key), or None."""
    if not reference:
        return None
    with connect(path) as c:
        r = c.execute("SELECT id FROM subscriptions WHERE user_id=? AND source=? AND reference=? ORDER BY id LIMIT 1",
                      (user_id, source, reference)).fetchone()
    return r["id"] if r else None


def cancel_subscription(sub_id: int, path=None) -> None:
    with connect(path) as c:
        c.execute("UPDATE subscriptions SET cancelled=1 WHERE id=?", (sub_id,))


def list_subscriptions(user_id: Optional[int] = None, path=None, limit: int = 200) -> list[dict]:
    with connect(path) as c:
        if user_id:
            rows = c.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY start_date DESC, id DESC LIMIT ?",
                             (user_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM subscriptions ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def access_for(user: User, path=None) -> Access:
    """Can this user use the paid features right now? Admins always can."""
    if user.is_admin:
        return Access(True, "administrator", plan="admin")
    if user.status != "active":
        return Access(False, "Account not active.")
    today = date.today().isoformat()
    with connect(path) as c:
        r = c.execute(
            """SELECT * FROM subscriptions WHERE user_id=? AND cancelled=0 AND start_date <= ? AND end_date >= ?
               ORDER BY end_date ASC, id ASC""",
            (user.id, today, today),
        ).fetchall()
    if not r:
        return Access(False, "No active subscription – please recharge to continue.")
    # first period with quota left (or unlimited) is the one in use
    for row in r:
        q = row["scan_quota"]
        left = None if q is None else max(0, q - row["scans_used"])
        if left is None or left > 0:
            end = date.fromisoformat(row["end_date"])
            return Access(True, "ok", plan=row["plan_name"], end_date=end, scans_left=left,
                          days_left=(end - date.today()).days + 1, subscription_id=row["id"])
    end = date.fromisoformat(r[-1]["end_date"])
    return Access(False, "Scan quota of your plan is used up – please recharge.", plan=r[-1]["plan_name"], end_date=end,
                  scans_left=0, days_left=(end - date.today()).days + 1, subscription_id=r[-1]["id"])


@dataclass
class Reservation:
    """A scan slot taken atomically before the reader runs (released again if the scan fails)."""
    subscription_id: Optional[int] = None
    counted: bool = False


def reserve_scan(user: User, own_key: bool = False, path=None) -> tuple[Optional[str], Reservation]:
    """Atomically take one scan from the user's plan. Returns (error message or None, reservation).

    Two concurrent scans can never both pass on the last unit: the counter is incremented with a
    conditional UPDATE, and only the caller whose UPDATE changed a row gets the slot. Scans made with
    the user's own key need an active period but are not counted."""
    if user.is_admin:
        return None, Reservation()
    if user.status != "active":
        return "Account not active.", Reservation()
    today = date.today().isoformat()
    with connect(path) as c:
        rows = c.execute(
            """SELECT id, scan_quota, end_date, plan_name FROM subscriptions
               WHERE user_id=? AND cancelled=0 AND start_date <= ? AND end_date >= ? ORDER BY end_date ASC, id ASC""",
            (user.id, today, today),
        ).fetchall()
        if not rows:
            return "No active subscription – please recharge to continue.", Reservation()
        if own_key:
            return None, Reservation()
        for r in rows:
            if r["scan_quota"] is None:
                return None, Reservation(r["id"], False)
            cur = c.execute(
                "UPDATE subscriptions SET scans_used = scans_used + 1 WHERE id=? AND scans_used < scan_quota",
                (r["id"],),
            )
            if cur.rowcount == 1:
                return None, Reservation(r["id"], True)
    return "Scan quota of your plan is used up – please recharge.", Reservation()


def release_scan(res: Optional[Reservation], path=None) -> None:
    """Give a reserved slot back (the scan failed before/at the reader)."""
    if res and res.counted and res.subscription_id:
        with connect(path) as c:
            c.execute("UPDATE subscriptions SET scans_used = MAX(scans_used - 1, 0) WHERE id=?", (res.subscription_id,))


def consume_scan(user: User, kind: str, model: str = "", input_tokens: int = 0, output_tokens: int = 0,
                 cost_usd: float = 0.0, own_key: bool = False, ok: bool = True, reservation: Optional[Reservation] = None,
                 path=None) -> None:
    """Record one scan. With a reservation the quota was already taken by reserve_scan (and is released
    again when ok=False); without one (tests / legacy callers) the counter is incremented here."""
    with connect(path) as c:
        c.execute(
            "INSERT INTO usage (user_id, kind, model, input_tokens, output_tokens, cost_usd, own_key, ok) VALUES (?,?,?,?,?,?,?,?)",
            (user.id, kind, model, int(input_tokens), int(output_tokens), float(cost_usd), int(own_key), int(ok)),
        )
        if reservation is not None:
            if not ok and reservation.counted and reservation.subscription_id:
                c.execute("UPDATE subscriptions SET scans_used = MAX(scans_used - 1, 0) WHERE id=?", (reservation.subscription_id,))
            return
        if ok and not own_key and not user.is_admin:
            acc = access_for(user, path)
            if acc.subscription_id and acc.scans_left is not None:
                c.execute("UPDATE subscriptions SET scans_used = scans_used + 1 WHERE id=?", (acc.subscription_id,))


def usage_summary(user_id: Optional[int] = None, days: int = 30, path=None) -> list[dict]:
    with connect(path) as c:
        if user_id:
            rows = c.execute(
                """SELECT kind, COUNT(*) AS n, SUM(input_tokens) AS inp, SUM(output_tokens) AS outp, SUM(cost_usd) AS cost
                   FROM usage WHERE user_id=? AND ts >= datetime('now', ?) GROUP BY kind""",
                (user_id, f"-{int(days)} days"),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT u.user_id, us.email, COUNT(*) AS n, SUM(u.input_tokens) AS inp, SUM(u.output_tokens) AS outp,
                          SUM(u.cost_usd) AS cost, SUM(u.own_key) AS own
                   FROM usage u JOIN users us ON us.id = u.user_id
                   WHERE u.ts >= datetime('now', ?) GROUP BY u.user_id ORDER BY n DESC""",
                (f"-{int(days)} days",),
            ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# payments (Razorpay payment links – stored here, API calls live in engine/payments.py)
# --------------------------------------------------------------------------- #
def record_payment(user_id: int, plan_name: str, amount_inr: float, link_id: str, short_url: str, raw: str = "",
                   plan_days: Optional[int] = None, plan_quota: Optional[int] = None, path=None) -> int:
    with connect(path) as c:
        cur = c.execute(
            "INSERT INTO payments (user_id, plan_name, amount_inr, link_id, short_url, raw, plan_days, plan_quota) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, plan_name, float(amount_inr), link_id, short_url, raw, plan_days, plan_quota),
        )
        return cur.lastrowid


def claim_payment(payment_row_id: int, path=None) -> bool:
    """Atomically move a pending payment to 'confirming' so only one caller activates the plan."""
    with connect(path) as c:
        cur = c.execute(
            """UPDATE payments SET status='confirming', claimed_at=datetime('now')
               WHERE id=? AND status <> 'paid' AND subscription_id IS NULL
                 AND (status <> 'confirming' OR claimed_at IS NULL OR claimed_at < datetime('now', '-5 minutes'))""",
            (payment_row_id,))
        return cur.rowcount == 1


def list_payments(user_id: Optional[int] = None, path=None, limit: int = 200) -> list[dict]:
    with connect(path) as c:
        if user_id:
            rows = c.execute("SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_payment(payment_row_id: int, status: str, payment_id: Optional[str] = None, raw: str = "",
                 subscription_id: Optional[int] = None, path=None) -> None:
    with connect(path) as c:
        c.execute(
            """UPDATE payments SET status=?, payment_id=COALESCE(?, payment_id), raw=?, subscription_id=COALESCE(?, subscription_id),
               paid_at=CASE WHEN ?='paid' THEN datetime('now') ELSE paid_at END WHERE id=?""",
            (status, payment_id, raw, subscription_id, status, payment_row_id),
        )


# --------------------------------------------------------------------------- #
# settings / customizations
# --------------------------------------------------------------------------- #
def get_setting(key: str, user_id: Optional[int] = None, default: Optional[str] = None, path=None) -> Optional[str]:
    """Per-user override first, then the global value, then ``default``."""
    with connect(path) as c:
        if user_id:
            r = c.execute("SELECT value FROM settings WHERE scope=? AND key=?", (f"user:{user_id}", key)).fetchone()
            if r and r["value"] not in (None, ""):
                return r["value"]
        r = c.execute("SELECT value FROM settings WHERE scope='global' AND key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] not in (None, "") else default


def set_setting(key: str, value: Optional[str], user_id: Optional[int] = None, path=None) -> None:
    scope = f"user:{user_id}" if user_id else "global"
    with connect(path) as c:
        if value is None or value == "":
            c.execute("DELETE FROM settings WHERE scope=? AND key=?", (scope, key))
        else:
            c.execute(
                """INSERT INTO settings (scope, key, value) VALUES (?,?,?)
                   ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')""",
                (scope, key, value),
            )


def all_settings(user_id: Optional[int] = None, path=None) -> dict[str, str]:
    scope = f"user:{user_id}" if user_id else "global"
    with connect(path) as c:
        rows = c.execute("SELECT key, value FROM settings WHERE scope=?", (scope,)).fetchall()
    return {r["key"]: r["value"] for r in rows}


def effective_customization(user_id: Optional[int], path=None) -> dict[str, Any]:
    """Merged view (global + per-user) with parsed JSON fields and feature defaults."""
    out: dict[str, Any] = {}
    for k in CUSTOMIZABLE_KEYS:
        out[k] = get_setting(k, user_id, None, path)
    feats = dict(DEFAULT_FEATURES)
    for scope_val in (get_setting("features_json", None, None, path), get_setting("features_json", user_id, None, path) if user_id else None):
        if scope_val:
            try:
                feats.update({k: bool(v) for k, v in json.loads(scope_val).items() if k in DEFAULT_FEATURES})
            except (ValueError, TypeError, AttributeError):
                pass
    out["features"] = feats
    try:
        out["labels"] = json.loads(out.get("labels_json") or "{}") or {}
        if not isinstance(out["labels"], dict):
            out["labels"] = {}
    except ValueError:
        out["labels"] = {}
    try:
        out["default_tally"] = json.loads(out.get("default_tally_json") or "{}") or {}
        if not isinstance(out["default_tally"], dict):
            out["default_tally"] = {}
    except ValueError:
        out["default_tally"] = {}
    return out


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
AUDIT_KEEP_ROWS = 50_000


def audit(action: str, actor: Optional[User] = None, target: str = "", detail: str = "", path=None) -> None:
    with connect(path) as c:
        cur = c.execute("INSERT INTO audit (actor_id, actor, action, target, detail) VALUES (?,?,?,?,?)",
                        (actor.id if actor else None, actor.email if actor else "system", action, target, detail[:2000]))
        if cur.lastrowid and cur.lastrowid % 500 == 0:   # bounded growth (login-failure / sign-up spam)
            c.execute("DELETE FROM audit WHERE id <= ?", (cur.lastrowid - AUDIT_KEEP_ROWS,))


def list_audit(limit: int = 300, path=None) -> list[dict]:
    with connect(path) as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats(path=None) -> dict:
    with connect(path) as c:
        today = date.today().isoformat()
        return {
            "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "pending": c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0],
            "active_subs": c.execute(
                "SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE cancelled=0 AND start_date<=? AND end_date>=?",
                (today, today)).fetchone()[0],
            "scans_30d": c.execute("SELECT COUNT(*) FROM usage WHERE ts >= datetime('now','-30 days')").fetchone()[0],
            "cost_30d": c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE ts >= datetime('now','-30 days')").fetchone()[0],
        }
