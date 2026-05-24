# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
meshsec.py — authentication & integrity for the MANET (Workstream D, security).

A shared mesh secret (``ARES_MESH_SECRET`` env, or ``data/.mesh_secret`` — a random
one is written on first use only if the env isn't set *and* a peer or WS-with-secret
is ever needed) lets Ares nodes prove they belong to the same mesh and lets every
inter-node LoB / chat message carry an HMAC-SHA256 signature over its *content*, so:

  * a node that has a secret rejects peer LoBs / chat that aren't signed (or are
    signed wrong, or were replayed under a different origin) — a rogue peer (or a
    spoofed UDP CoT) can't inject bogus bearings that bias every node's fixes;
  * a node that has *no* secret signs nothing and accepts everything (single-node /
    open-lab back-compat — set the secret for any real multi-node deployment);
  * the ``/api/v1/sdr/stream`` WebSocket, when auth is enabled, accepts either a
    valid bearer token *or* ``?mesh_secret=<secret>`` (how peer nodes connect).

The signature covers a canonical subset of fields (sorted, ``|``-joined) so it's
stable across JSON round-trips and pins ``origin_node`` + ``id`` + ``t`` against
tampering/replay. Signatures are truncated to 32 hex chars (128-bit) — plenty.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR

log = logging.getLogger(__name__)

_SECRET_FILE = DATA_DIR / ".mesh_secret"
# NB: `hops` is deliberately *not* signed — it's mutable transport metadata that a
# relay increments in flight; the signature pins the content (who/where/what/when).
_LOB_FIELDS = ("origin_node", "origin_device", "id", "device_id", "lat", "lon",
               "azimuth_deg", "frequency_hz", "rssi_dbm", "t")
_CHAT_FIELDS = ("from_node", "id", "room", "text", "lat", "lon", "t")


def _env_secret() -> Optional[str]:
    v = os.getenv("ARES_MESH_SECRET", "").strip()
    return v or None


_SECRET: Optional[str] = _env_secret()


def secret() -> Optional[str]:
    """The active mesh secret, or None if mesh auth/signing is disabled."""
    return _SECRET


def ensure_secret() -> str:
    """Return a mesh secret, generating + persisting one to ``data/.mesh_secret`` if
    none is configured. Call this only when a secret is actually required (a peer is
    added, or WS-with-secret is requested) — so a pure single-node install never
    creates one and stays in 'no auth' mode unless the operator opts in."""
    global _SECRET
    if _SECRET:
        return _SECRET
    try:
        if _SECRET_FILE.exists():
            v = _SECRET_FILE.read_text().strip()
            if v:
                _SECRET = v
                return v
    except OSError:
        pass
    v = secrets.token_urlsafe(32)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(v)
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except OSError:
            pass
        log.warning("mesh: generated a new mesh secret in %s — copy it to the other nodes "
                    "(or set ARES_MESH_SECRET) so they can join + verify each other", _SECRET_FILE)
    except OSError:
        pass
    _SECRET = v
    return v


def _canon(d: dict, fields) -> str:
    parts = []
    for f in fields:
        v = d.get(f)
        if isinstance(v, float):
            v = f"{v:.6f}"
        parts.append(f"{f}={'' if v is None else v}")
    return "|".join(parts)


def _hmac(msg: str) -> str:
    s = _SECRET
    if not s:
        return ""
    return hmac.new(s.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def sign_lob(d: dict) -> Optional[str]:
    return _hmac(_canon(d, _LOB_FIELDS)) if _SECRET else None


def sign_chat(d: dict) -> Optional[str]:
    return _hmac(_canon(d, _CHAT_FIELDS)) if _SECRET else None


def _verify(d: dict, fields, kind: str) -> bool:
    """True if the dict's ``sig`` is valid (or if no mesh secret is configured here —
    then we don't enforce). False ⇒ reject the message."""
    if not _SECRET:
        return True                           # mesh auth disabled on this node
    want = _hmac(_canon(d, fields))
    got = str(d.get("sig") or "")
    ok = bool(got) and hmac.compare_digest(got, want)
    if not ok:
        log.debug("mesh: rejecting unsigned/bad-signed %s from %s", kind, d.get("origin_node") or d.get("from_node"))
    return ok


def verify_lob(d: dict) -> bool:
    return _verify(d, _LOB_FIELDS, "LoB")


def verify_chat(d: dict) -> bool:
    return _verify(d, _CHAT_FIELDS, "chat")


def ws_secret_ok(supplied: Optional[str]) -> bool:
    """Constant-time compare of a ``?mesh_secret=`` query value against the active
    secret. False if no secret is configured (then this auth path is just disabled)."""
    if not _SECRET or not supplied:
        return False
    return hmac.compare_digest(str(supplied), _SECRET)
