"""
Ares — OSINT feed routes (Workstream A.4).

Import live OSINT mapping data (DeepState / GDELT / ADS-B / NASA FIRMS / ACLED /
AIS / LiveUAMap / Signal Cockpit / any GeoJSON-KML-GeoRSS-GPX URL), normalised to
GeoJSON and filtered (source query → bbox clip → hard cap) so the UI never floods.
Each feed renders as a normal toggleable map layer.

Endpoints
---------
GET    /api/v1/osint/feeds                list sources + status (configured, cached count/as-of)
POST   /api/v1/osint/feeds/{id}/fetch     fetch+filter a feed → GeoJSON + meta (total/truncated/source)
GET    /api/v1/osint/feeds/{id}           last cached GeoJSON
POST   /api/v1/osint/feeds                add a custom feed {name, url, format, color}
DELETE /api/v1/osint/feeds/{id}           remove a custom feed
PUT    /api/v1/osint/feeds/{id}/config    set api_key / email / url / region for a keyed source
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.core.security import audit
from app.core import osint

log = logging.getLogger(__name__)
router = APIRouter(tags=["osint"], prefix="/osint")


class FetchBody(BaseModel):
    bbox: Optional[list[float]] = None        # [west, south, east, north]
    params: dict[str, Any] = {}
    max_features: int = Field(2000, ge=1, le=50000)
    force: bool = False


class CustomFeedBody(BaseModel):
    name: str
    url: str
    format: str = Field("auto", pattern="^(auto|geojson|kml|georss|gpx)$")
    color: str = "#06d6a0"


class ConfigBody(BaseModel):
    api_key: Optional[str] = None
    email: Optional[str] = None
    url: Optional[str] = None
    region: Optional[str] = None


@router.get("/feeds")
async def osint_list(principal: dict = Depends(require_auth)):
    return {"feeds": osint.list_feeds()}


@router.post("/feeds/{feed_id}/fetch")
async def osint_fetch(feed_id: str, body: FetchBody, principal: dict = Depends(require_auth)):
    bbox = body.bbox
    if bbox is not None and len(bbox) != 4:
        raise HTTPException(400, "bbox must be [west, south, east, north]")
    res = await osint.fetch_feed(feed_id, bbox=bbox, params=body.params,
                                 max_features=body.max_features, force=body.force)
    audit("osint.fetch", id=feed_id, source=res.get("source"), count=res.get("count"),
          total=res.get("total"), by=principal.get("sub"))
    return res


@router.get("/feeds/{feed_id}")
async def osint_cached(feed_id: str, principal: dict = Depends(require_auth)):
    cached = osint.get_cached(feed_id)
    if cached is None:
        raise HTTPException(404, "no cached copy — fetch the feed first")
    return {**cached.get("meta", {}), "geojson": cached["geojson"], "source": "cache"}


@router.post("/feeds")
async def osint_add(body: CustomFeedBody, principal: dict = Depends(require_auth)):
    try:
        fd = osint.add_custom_feed(body.name, body.url, body.format, body.color)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("osint.feed.add", id=fd["id"], url=body.url, by=principal.get("sub"))
    return fd


@router.delete("/feeds/{feed_id}")
async def osint_remove(feed_id: str, principal: dict = Depends(require_auth)):
    if not osint.remove_feed(feed_id):
        raise HTTPException(404, "no such custom feed / config")
    audit("osint.feed.remove", id=feed_id, by=principal.get("sub"))
    return {"status": "removed", "id": feed_id}


@router.put("/feeds/{feed_id}/config")
async def osint_config(feed_id: str, body: ConfigBody, principal: dict = Depends(require_auth)):
    try:
        res = osint.set_config(feed_id, body.dict(exclude_none=False))
    except KeyError:
        raise HTTPException(404, "no such feed")
    audit("osint.feed.config", id=feed_id, configured=res.get("configured"), by=principal.get("sub"))
    return res
