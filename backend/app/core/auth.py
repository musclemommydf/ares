"""
Ares — authentication layer.

Stateless HMAC-signed bearer tokens (no external deps). Users live in a JSON
file (``data/users.json``) with PBKDF2-HMAC-SHA256 password hashes. On first run,
if no users file exists, a single ``admin`` account is created with a random
password that is logged once — the operator is expected to change it.

Auth is **disabled by default** (``ARES_AUTH=false``) so existing single-user /
localhost workflows are untouched. Any networked or field deployment (and the
ATAK plugin) should set ``ARES_AUTH=true``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings, DATA_DIR

log = logging.getLogger(__name__)

USERS_FILE = DATA_DIR / "users.json"
_PBKDF2_ROUNDS = 200_000
_TOKEN_TTL_S = 12 * 3600  # 12 hours


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing
# ─────────────────────────────────────────────────────────────────────────────
def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """Return ``pbkdf2_sha256$<rounds>$<salt_b64>$<hash_b64>``."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt_b64), int(rounds_s)
        )
        return hmac.compare_digest(dk, _unb64(hash_b64))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# User store
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class User:
    username: str
    password_hash: str
    role: str = "operator"  # operator | admin


def _load_users() -> dict[str, User]:
    if not USERS_FILE.exists():
        return {}
    try:
        raw = json.loads(USERS_FILE.read_text())
        return {
            u["username"]: User(u["username"], u["password_hash"], u.get("role", "operator"))
            for u in raw.get("users", [])
        }
    except Exception:
        log.exception("Failed to read %s — treating as empty", USERS_FILE)
        return {}


def _save_users(users: dict[str, User]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(
        json.dumps(
            {"users": [{"username": u.username, "password_hash": u.password_hash, "role": u.role}
                       for u in users.values()]},
            indent=2,
        )
    )
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def ensure_default_user() -> None:
    """Ensure an ``admin`` account exists.

    Headless/appliance deployments set ``ARES_ADMIN_PASSWORD`` so the operator can
    log in over the network without scraping the boot log — that pins (creates or
    updates) the admin password every start. Otherwise, if the store is empty, a
    random-password admin is created and logged once."""
    users = _load_users()
    env_pw = os.environ.get("ARES_ADMIN_PASSWORD", "").strip()
    if env_pw:
        existing = users.get("admin")
        if existing is None or not verify_password(env_pw, existing.password_hash):
            users["admin"] = User("admin", hash_password(env_pw), role="admin")
            _save_users(users)
            log.info("admin password set from ARES_ADMIN_PASSWORD (stored hashed in %s)", USERS_FILE)
        return
    if users:
        return
    pw = secrets.token_urlsafe(12)
    users["admin"] = User("admin", hash_password(pw), role="admin")
    _save_users(users)
    log.warning(
        "No users found — created default account  username=admin  password=%s  "
        "(change it; stored hashed in %s)", pw, USERS_FILE,
    )


def add_or_update_user(username: str, password: str, role: str = "operator") -> None:
    users = _load_users()
    users[username] = User(username, hash_password(password), role)
    _save_users(users)


def authenticate(username: str, password: str) -> Optional[User]:
    backend = (settings.auth_backend or "local").lower()
    # local user store (default; also the first hop for "ldap+local")
    if backend in ("local", "ldap+local"):
        user = _load_users().get(username)
        if user and verify_password(password, user.password_hash):
            return user
        if backend == "local":
            return None
    # LDAP / Active Directory bind
    if backend in ("ldap", "ldap+local"):
        return _ldap_authenticate(username, password)
    return None


def _ldap_authenticate(username: str, password: str) -> Optional[User]:
    """Bind to LDAP/AD as the user; on success return a transient (non-persisted)
    User. Role is ``admin`` if the user is in ``ARES_LDAP_ADMIN_GROUP`` (when
    configured), else ``operator``. Requires the optional ``ldap3`` package."""
    if not password or not settings.ldap_server or not settings.ldap_user_dn_template:
        if not settings.ldap_server:
            log.warning("auth_backend=ldap but ARES_LDAP_SERVER / ARES_LDAP_USER_DN not set — denying")
        return None
    try:
        import ldap3  # type: ignore
    except ImportError:
        log.warning("auth_backend=ldap but the 'ldap3' package isn't installed (`pip install ldap3`) — denying")
        return None
    user_dn = settings.ldap_user_dn_template.format(username=username)
    try:
        server = ldap3.Server(settings.ldap_server, get_info=ldap3.NONE, connect_timeout=5)
        conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True)
    except Exception as e:
        log.info("LDAP bind failed for %s: %s", username, e)
        return None
    role = "operator"
    try:
        if settings.ldap_admin_group:
            # look up the admin group and see if user_dn is one of its members
            base = settings.ldap_admin_group  # the group's own DN is a fine search base
            conn.search(base, "(objectClass=*)", search_scope="BASE", attributes=["member", "uniqueMember"])
            members: list[str] = []
            for entry in conn.entries:
                for attr in ("member", "uniqueMember"):
                    if attr in entry:
                        members += [str(m) for m in entry[attr].values]
            if any(user_dn.lower() == m.lower() for m in members):
                role = "admin"
    except Exception:  # group check is best-effort
        pass
    finally:
        try:
            conn.unbind()
        except Exception:
            pass
    log.info("LDAP authenticated %s (role=%s)", username, role)
    return User(username, password_hash="ldap:external", role=role)


# ─────────────────────────────────────────────────────────────────────────────
# Tokens — stateless, HMAC-SHA256 signed:  base64url(payload).base64url(sig)
# ─────────────────────────────────────────────────────────────────────────────
def _secret() -> bytes:
    return settings.auth_secret.encode()


def issue_token(user: User, ttl_s: int = _TOKEN_TTL_S) -> tuple[str, int]:
    exp = int(time.time()) + ttl_s
    payload = {"sub": user.username, "role": user.role, "exp": exp}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", exp


def decode_token(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".")
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency
# ─────────────────────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Dependency that enforces a valid bearer token when ``auth_enabled``.

    When auth is disabled returns a synthetic anonymous principal so route code
    can treat the return value uniformly.
    """
    if not settings.auth_enabled:
        return {"sub": "anonymous", "role": "admin", "auth": "disabled"}
    if creds is None or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    payload = decode_token(creds.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
