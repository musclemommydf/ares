"""
Ares — ATAK-plugin support routes (Workstream C).

Templates:
  GET    /api/v1/atak/templates              list
  GET    /api/v1/atak/templates/{id}         one
  PUT    /api/v1/atak/templates/{id}         create / replace
  DELETE /api/v1/atak/templates/{id}         remove
  POST   /api/v1/atak/templates/{id}/coverage_request?lat=&lon=&azimuth_deg=
                                             flatten template+location → /simulate/coverage body

Export:
  POST   /api/v1/atak/export/kmz             coverage GeoJSON → KMZ GroundOverlay (file download)
"""
from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.auth import require_auth
from app.core import templates as tmpl
from app.core.kmz import coverage_geojson_to_kmz

router = APIRouter(prefix="/atak", tags=["atak"])


# ── templates ────────────────────────────────────────────────────────────────
@router.get("/templates")
async def list_templates(principal: dict = Depends(require_auth)):
    return {"templates": tmpl.list_templates()}


@router.get("/templates/{template_id}")
async def get_template(template_id: str, principal: dict = Depends(require_auth)):
    t = tmpl.get_template(template_id)
    if t is None:
        raise HTTPException(404, "no such template")
    return t


@router.put("/templates/{template_id}")
async def put_template(template_id: str, body: dict, principal: dict = Depends(require_auth)):
    body = dict(body or {})
    body["id"] = template_id
    return tmpl.save_template(body)


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str, principal: dict = Depends(require_auth)):
    if not tmpl.delete_template(template_id):
        raise HTTPException(404, "no such template")
    return {"status": "deleted", "id": template_id}


@router.post("/templates/{template_id}/coverage_request")
async def template_coverage_request(
    template_id: str,
    lat: float = Query(...), lon: float = Query(...),
    azimuth_deg: Optional[float] = Query(None),
    principal: dict = Depends(require_auth),
):
    t = tmpl.get_template(template_id)
    if t is None:
        raise HTTPException(404, "no such template")
    return tmpl.to_coverage_request(t, lat, lon, azimuth_deg)


# ── KMZ export ───────────────────────────────────────────────────────────────
class KmzExportRequest(BaseModel):
    geojson: dict
    name: str = "Ares coverage"
    min_signal_dbm: float = -120.0


@router.post("/export/kmz")
async def export_kmz(req: KmzExportRequest, principal: dict = Depends(require_auth)):
    kmz = coverage_geojson_to_kmz(req.geojson, name=req.name, min_signal_dbm=req.min_signal_dbm)
    if kmz is None:
        raise HTTPException(422, "no coverage points to rasterise (or Pillow not installed)")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in req.name)[:64] or "coverage"
    return StreamingResponse(
        io.BytesIO(kmz),
        media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": f'attachment; filename="{safe}.kmz"'},
    )
