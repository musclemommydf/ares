# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Ares — group-chat routes (Workstream D).

GET  /api/v1/chat/messages?room=&limit=    recent messages (a room, or all rooms)
GET  /api/v1/chat/rooms                     known rooms / channels
POST /api/v1/chat/send                      send a message — propagates over the MANET mesh and out as a CoT GeoChat to ATAK

Live delivery rides the existing ``WS /api/v1/sdr/stream`` as ``{"type":"chat","msg":{...}}``
(so the peer mesh re-ingests it on every node); inbound GeoChat CoT from ATAK is
routed back in by the CoT listener — one conversation across Ares nodes and ATAK.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.core.chat import chat_hub

router = APIRouter(tags=["chat"], prefix="/chat")


@router.get("/messages")
async def messages(room: Optional[str] = None, limit: int = 100, principal: dict = Depends(require_auth)):
    return {"room": room, "messages": chat_hub.recent(room, min(500, max(1, int(limit)))),
            "rooms": chat_hub.rooms()}


@router.get("/rooms")
async def rooms(principal: dict = Depends(require_auth)):
    return {"rooms": chat_hub.rooms()}


class ChatSend(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    room: str = "All"
    callsign: str = ""
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)


@router.post("/send")
async def send(body: ChatSend, principal: dict = Depends(require_auth)):
    try:
        msg = chat_hub.send(body.text, room=body.room, callsign=body.callsign, lat=body.lat, lon=body.lon)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "msg": msg}
