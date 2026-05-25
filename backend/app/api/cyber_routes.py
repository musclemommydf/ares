# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Cyber tab API (roadmap item 11 / C6). Exposes pentest-class capabilities by what
they do (sub-GHz, RFID LF, NFC HF, IR, iButton, GPIO, HID) — never by device brand.

Active/transmitting actions are refused (403) unless the authorized-active gate is
on, and every active attempt is audit-logged. Passive actions need only the
hardware; with nothing suitable connected they return 409 (honest, not faked).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import cyber
from app.core.auth import require_auth
from app.core.cyber import subghz
from app.core.cyber.tools import ToolUnavailable

router = APIRouter(prefix="/cyber", tags=["cyber"])


class GateRequest(BaseModel):
    enabled: bool


class RunRequest(BaseModel):
    category: str
    action: str
    params: Optional[dict[str, Any]] = None


class CliRequest(BaseModel):
    tool_id: str
    command: str


@router.get("/capabilities")
async def capabilities(principal: dict = Depends(require_auth)):
    """Static catalog of capability categories + actions (passive vs active)."""
    return {"catalog": cyber.CATALOG}


@router.get("/detect")
async def detect(principal: dict = Depends(require_auth)):
    """Hardware connected right now, mapped to available capabilities."""
    return cyber.detect()


@router.get("/authorized")
async def get_authorized(principal: dict = Depends(require_auth)):
    return {"authorized_active": cyber.authorized_active()}


@router.post("/authorized")
async def set_authorized(body: GateRequest, principal: dict = Depends(require_auth)):
    """Toggle the authorized-active master gate (persisted + audit-logged)."""
    state = cyber.set_authorized_active(body.enabled, by=principal.get("sub", ""))
    return {"authorized_active": state}


@router.get("/subghz/captures")
async def subghz_captures(principal: dict = Depends(require_auth)):
    return {"captures": subghz.list_captures()}


@router.post("/cli")
async def raw_cli(body: CliRequest, principal: dict = Depends(require_auth)):
    """Raw command passthrough to a connected field tool (active — gated + audited)."""
    try:
        return cyber.raw_cli(body.tool_id, body.command, by=principal.get("sub", ""))
    except cyber.NotAuthorized as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ToolUnavailable as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@router.post("/run")
async def run(body: RunRequest, principal: dict = Depends(require_auth)):
    """Execute one capability action. Maps domain errors to honest HTTP codes."""
    try:
        return cyber.run(body.category, body.action, body.params or {},
                         by=principal.get("sub", ""))
    except cyber.NotAuthorized as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (ToolUnavailable, subghz.NoRadio) as e:
        raise HTTPException(409, str(e))
    except subghz.RadioBusy as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001 — surface the real failure, don't fake success
        raise HTTPException(500, f"{type(e).__name__}: {e}")
