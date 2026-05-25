# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
api/aprs_routes.py — APRS decode + station map feed.

  GET    /aprs/status                 — module status (station / fix counts)
  POST   /aprs/decode                 — decode TNC2 lines and/or AX.25 hex frames  [auth]
  GET    /aprs/stations               — decoded stations (table)
  GET    /aprs/stations.geojson       — decoded stations as a GeoJSON layer (the map feed)
  DELETE /aprs/stations               — clear the station table                     [auth]

Decode is local-only (no APRS-IS / cloud); frames come from an SDR AFSK1200 demod,
an external TNC, or pasted packets. The stations also surface on the map via the
OSINT layer "aprs" (osint.feeds) and to ATAK via app.core.cot.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.core.decoders import aprs
from app.core.security import audit

router = APIRouter(tags=["aprs"], prefix="/aprs")


class DecodeReq(BaseModel):
    lines: list[str] = Field(default_factory=list, description="TNC2 text frames (SRC>DEST,path:info)")
    hex: list[str] = Field(default_factory=list, description="AX.25 / KISS frames as hex strings")


def _station_dict(st: aprs.Station) -> dict:
    return {
        "callsign": st.callsign, "lat": st.lat, "lon": st.lon, "symbol": st.symbol,
        "course_deg": st.course_deg, "speed_kt": st.speed_kt, "altitude_ft": st.altitude_ft,
        "comment": st.comment, "dest": st.dest, "path": st.path, "object": st.is_object,
        "n_msgs": st.n_msgs, "last_update_t": st.last_update_t,
    }


@router.get("/status")
def status() -> dict:
    stns = aprs.decoder.stations
    return {"stations": len(stns),
            "with_fix": sum(1 for s in stns.values() if s.lat is not None)}


@router.post("/decode")
def decode(req: DecodeReq, _auth=Depends(require_auth)) -> dict:
    updated: list[dict] = []
    for ln in req.lines:
        st = aprs.decoder.step(ln)
        if st:
            updated.append(_station_dict(st))
    for h in req.hex:
        try:
            frame = bytes.fromhex(h.replace(" ", ""))
        except ValueError:
            continue
        st = aprs.decoder.step(frame)
        if st:
            updated.append(_station_dict(st))
    audit("aprs.decode", frames=len(req.lines) + len(req.hex), updated=len(updated))
    return {"updated": updated, "stations": len(aprs.decoder.stations)}


@router.get("/stations")
def stations() -> dict:
    return {"stations": [_station_dict(s) for s in aprs.decoder.stations.values()]}


@router.get("/stations.geojson")
def stations_geojson() -> dict:
    return aprs.stations_geojson()


@router.delete("/stations")
def clear(_auth=Depends(require_auth)) -> dict:
    n = len(aprs.decoder.stations)
    aprs.decoder.stations.clear()
    return {"cleared": n}
