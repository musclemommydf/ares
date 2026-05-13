"""
Ares — authentication routes.

POST /api/v1/auth/login   -> { token, expires_at, user }
GET  /api/v1/auth/me      -> current principal (or anonymous when auth disabled)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.core.auth import authenticate, issue_token, require_auth

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: int
    user: dict


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    if not settings.auth_enabled:
        # Auth disabled: hand back a long-lived synthetic token so clients that
        # always log in (e.g. the ATAK plugin) keep working against dev servers.
        from app.core.auth import User
        token, exp = issue_token(User("anonymous", "", role="admin"))
        return LoginResponse(token=token, expires_at=exp,
                             user={"username": "anonymous", "role": "admin", "auth": "disabled"})
    user = authenticate(req.username, req.password)
    try:
        from app.core.security import audit
        audit("auth.login", user=req.username, ok=user is not None)
    except Exception:
        pass
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token, exp = issue_token(user)
    log.info("login ok: %s", user.username)
    return LoginResponse(token=token, expires_at=exp,
                         user={"username": user.username, "role": user.role})


@router.get("/me")
async def me(principal: dict = Depends(require_auth)):
    return {"user": principal, "auth_enabled": settings.auth_enabled}
