"""
Ares — SDR console routes (Workstream D).

Single-channel SDRs monitor a spectrum / decode audio; multi-channel SDRs (declare
the channel count) also produce lines of bearing — more channels ⇒ tighter LoBs.

Endpoints
---------
GET/POST/PUT/DELETE /api/v1/sdr/devices[/{id}]   register / list / patch / unregister SDR sources
POST   /api/v1/sdr/devices/{id}/test             probe connectivity (TCP / HTTP)
GET    /api/v1/sdr/devices/{id}/spectrum         a PSD frame for a channel (synthetic until hardware is wired)
GET    /api/v1/sdr/accuracy_estimate             expected LoB σ for a channel count + array geometry (device-setup hint)
GET    /api/v1/sdr/state                         snapshot — devices, recent LoBs/fixes, CoT targets, GPS fix
POST   /api/v1/sdr/lob                            push one LoB manually (testing without a radio)
POST   /api/v1/gps                                set the live operator GPS fix (shown on the map; observer for LoBs)
GET    /api/v1/gps                                the current GPS fix
GET    /api/v1/sdr/audio/modes                    decodable transmission modes (DMR/P25/TETRA/NXDN/…) + decoder status
POST   /api/v1/sdr/devices/{id}/audio             start decoding a transmission (needs a baseband + an installed decoder)
GET/PUT /api/v1/sdr/cot/targets                   list / replace CoT push targets  (the web UI surfaces this on the ATAK/Server console)
WS     /api/v1/sdr/stream                          live events: snapshot | lob | fix | device_status | gps | lob_rejected
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core import cot
from app.core.auth import require_auth
from app.core.security import audit
from app.core.sdr import sdr_manager
from app.core.sdr.manager import LobEvent

log = logging.getLogger(__name__)
router = APIRouter(tags=["sdr"], prefix="/sdr")


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
class DeviceCreate(BaseModel):
    name: str
    type: str = Field("generic", pattern="^(krakensdr|matchstiq_x40|generic)$")
    host: str
    port: int = 0
    source_class: str = Field("multi_channel", pattern="^(single_channel|multi_channel)$")
    channels: int = Field(5, ge=1, le=64)
    array_type: str = Field("uca", pattern="^(ula|uca|custom)$")
    array_spacing_wavelengths: float = 0.4
    azimuth_reference: str = Field("absolute", pattern="^(absolute|relative|clock)$")
    antenna_heading_deg: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    altitude_m: float = 0.0
    observer_height_m: float = 1.5
    frequency_hz: float = 0.0
    df_threshold_dbm: float = -90.0
    antenna_hpbw_deg: Optional[float] = None
    environment: str = "suburban"
    enabled: bool = True
    use_gps: bool = True
    auto_coverage: bool = False
    metadata: dict[str, Any] = {}


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    source_class: Optional[str] = None
    channels: Optional[int] = None
    array_type: Optional[str] = None
    array_spacing_wavelengths: Optional[float] = None
    azimuth_reference: Optional[str] = None
    antenna_heading_deg: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude_m: Optional[float] = None
    observer_height_m: Optional[float] = None
    frequency_hz: Optional[float] = None
    df_threshold_dbm: Optional[float] = None
    antenna_hpbw_deg: Optional[float] = None
    environment: Optional[str] = None
    enabled: Optional[bool] = None
    use_gps: Optional[bool] = None
    auto_coverage: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class CotTargets(BaseModel):
    targets: list[str]


class GpsFix(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    alt_m: float = 0.0
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None
    source: str = "manual"


class ManualLob(BaseModel):
    device_id: str
    lat: float
    lon: float
    azimuth_deg: float
    frequency_hz: float
    rssi_dbm: float = -80.0
    confidence_pct: float = 80.0
    observer_height_m: float = 1.5
    environment: str = "suburban"
    target_device_id: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/devices")
async def list_devices(principal: dict = Depends(require_auth)):
    return {"devices": sdr_manager.list()}


@router.post("/devices")
async def create_device(body: DeviceCreate, principal: dict = Depends(require_auth)):
    try:
        dev = sdr_manager.add(body.dict())
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("sdr.device.add", id=dev.id, name=dev.name, type=dev.type, source_class=dev.source_class,
          channels=dev.channels, host=dev.host, by=principal.get("sub"))
    return dev.public()


@router.get("/devices/{device_id}")
async def get_device(device_id: str, principal: dict = Depends(require_auth)):
    dev = sdr_manager.get(device_id)
    if dev is None:
        raise HTTPException(404, "no such device")
    return dev.public()


@router.put("/devices/{device_id}")
async def update_device(device_id: str, body: DeviceUpdate, principal: dict = Depends(require_auth)):
    patch = {k: v for k, v in body.dict().items() if v is not None}
    try:
        dev = sdr_manager.update(device_id, patch)
    except KeyError:
        raise HTTPException(404, "no such device")
    return dev.public()


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, principal: dict = Depends(require_auth)):
    if not sdr_manager.remove(device_id):
        raise HTTPException(404, "no such device")
    audit("sdr.device.remove", id=device_id, by=principal.get("sub"))
    return {"status": "deleted", "id": device_id}


@router.post("/devices/{device_id}/test")
async def test_device(device_id: str, principal: dict = Depends(require_auth)):
    dev = sdr_manager.get(device_id)
    if dev is None:
        raise HTTPException(404, "no such device")
    host = dev.host
    if host.startswith("tcp://"):
        host = host[6:]
    if ":" in host:
        host, _, p = host.partition(":")
        port = int(p)
    else:
        port = dev.port or (8080 if dev.type == "krakensdr" else 8400)
    # quick TCP probe (KrakenSDR HTTP runs over TCP too)
    loop = asyncio.get_event_loop()
    def _probe():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        try:
            s.connect((host, port))
            return True, ""
        except OSError as e:
            return False, str(e)
        finally:
            s.close()
    ok, err = await loop.run_in_executor(None, _probe)
    return {"ok": ok, "host": host, "port": port, "error": err}


# ─── compass: 3 modes + calibration ──────────────────────────────────────────
@router.get("/compass/modes")
async def compass_modes(principal: dict = Depends(require_auth)):
    from app.core.geolocation import COMPASS_MODES
    return {
        "modes": [{"id": k, **v} for k, v in COMPASS_MODES.items()],
        "default": "absolute",
        "calibration": {
            "why": "A Relative LOB is the bearing to the energy across the physical antenna elements (0° = the front of the DF antenna). To plot it on a map you need the antenna's true heading: Absolute LOB = (0° + heading) + Relative LOB. Calibration finds that heading.",
            "steps": [
                "1. Identify a reference emitter (or landmark with a known transmitter) whose TRUE bearing from your position you know — read it off a map, a hand-held compass corrected for declination, or a known-location beacon.",
                "2. Physically aim the DF antenna's front (its 0° mark) however it's mounted — it does NOT need to point at the reference.",
                "3. Tune to the reference signal and shoot a LoB. Note the RELATIVE LOB the DF reports (degrees off the antenna front).",
                "4. Call POST /api/v1/sdr/devices/{id}/calibrate with the known true bearing (and the relative LOB, or omit it to use the last shot). Ares sets antenna_heading_deg = (true − relative) mod 360.",
                "5. Verify: switch the device to Absolute LOB mode and re-shoot the reference — the plotted LOB should now point at it. Re-calibrate if you remount/rotate the antenna or change vehicles.",
            ],
        },
    }


class CalibrateRequest(BaseModel):
    known_true_bearing_deg: float = Field(..., ge=0, le=360)
    measured_relative_lob_deg: Optional[float] = None   # omit ⇒ use the device's most recent LoB
    set_mode_absolute: bool = True                       # also switch the device's output to "absolute" after calibrating


@router.post("/devices/{device_id}/calibrate")
async def calibrate_compass(device_id: str, body: CalibrateRequest, principal: dict = Depends(require_auth)):
    try:
        res = sdr_manager.calibrate_device(device_id, body.known_true_bearing_deg, body.measured_relative_lob_deg)
    except KeyError:
        raise HTTPException(404, "no such device")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.set_mode_absolute:
        try:
            sdr_manager.update(device_id, {"azimuth_reference": "absolute"})
            res["azimuth_reference"] = "absolute"
        except Exception:
            pass
    audit("sdr.calibrate", device=device_id, heading_deg=res.get("antenna_heading_deg"),
          known_true_bearing_deg=res.get("known_true_bearing_deg"), by=principal.get("sub"))
    return {"status": "ok", **res}


# ─────────────────────────────────────────────────────────────────────────────
# State + CoT targets
# ─────────────────────────────────────────────────────────────────────────────
def _mesh_status():
    try:
        from app.core.sdr.mesh import peer_mesh
        return peer_mesh.status()
    except Exception:
        return {"node_id": "local", "node_label": "ares", "peers": []}


@router.get("/state")
async def get_state(principal: dict = Depends(require_auth)):
    return {
        "devices": sdr_manager.list(),
        "lobs": [asdict(l) for l in sdr_manager._recent_lobs()],
        "fixes": list(sdr_manager._fixes),
        "cot_targets": cot.list_targets(),
        "mesh": _mesh_status(),
        "gps": sdr_manager.gps_fix(),
    }


# ─── distributed sensing — peer Ares nodes on the MANET ──────────────────────
class PeerList(BaseModel):
    peers: list[str]


@router.get("/mesh")
async def get_mesh(principal: dict = Depends(require_auth)):
    return _mesh_status()


@router.get("/peers")
async def get_peers(principal: dict = Depends(require_auth)):
    from app.core.sdr.mesh import peer_mesh
    return {"node_id": peer_mesh.node_id, "node_label": peer_mesh.node_label,
            "peers": peer_mesh.list_peers(), "status": peer_mesh.status()["peers"]}


@router.put("/peers")
async def set_peers(body: PeerList, principal: dict = Depends(require_auth)):
    from app.core.sdr.mesh import peer_mesh
    return {"peers": peer_mesh.set_peers(body.peers), "status": peer_mesh.status()}


@router.post("/peers")
async def add_peer(body: dict, principal: dict = Depends(require_auth)):
    from app.core.sdr.mesh import peer_mesh
    url = body.get("url") or body.get("peer") or ""
    try:
        added = peer_mesh.add_peer(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("mesh.peer.add", peer=added, by=principal.get("sub"))
    return {"added": added, "peers": peer_mesh.list_peers()}


@router.delete("/peers")
async def del_peer(url: str, principal: dict = Depends(require_auth)):
    from app.core.sdr.mesh import peer_mesh
    removed = peer_mesh.remove_peer(url)
    audit("mesh.peer.remove", peer=url, removed=removed, by=principal.get("sub"))
    return {"removed": removed, "peers": peer_mesh.list_peers()}


@router.get("/cot/targets")
async def get_targets(principal: dict = Depends(require_auth)):
    return {"targets": cot.list_targets()}


@router.put("/cot/targets")
async def set_cot_targets(body: CotTargets, principal: dict = Depends(require_auth)):
    parsed = cot.set_targets(body.targets)
    return {"targets": parsed}


# ─── GPS (operator location → map marker + observer for LoBs) ─────────────────
@router.get("/gps")
async def get_gps(principal: dict = Depends(require_auth)):
    return {"fix": sdr_manager.gps_fix()}


@router.post("/gps")
async def set_gps(body: GpsFix, principal: dict = Depends(require_auth)):
    return {"status": "ok", "fix": sdr_manager.set_gps_fix(body.lat, body.lon, body.alt_m,
                                                           body.heading_deg, body.speed_mps, body.source)}


# ─── live GPS source: this computer (browser) / a USB GPS (gpsd or serial NMEA) / an SDR's GPSDO ──
class GpsSourceRequest(BaseModel):
    kind: str = Field(..., pattern="^(off|manual|browser|gpsd|serial|sdr)$")
    host: str = "127.0.0.1"            # gpsd host
    port: int = Field(2947, ge=1, le=65535)   # gpsd port
    path: str = "/dev/ttyUSB0"          # serial device path (also accepts /dev/ttyACM0 etc.)
    baud: int = Field(9600, ge=1200, le=921600)
    device_args: str = ""               # SoapySDR device args for the 'sdr' GPSDO source (blank = first device)


@router.get("/gps/source")
async def get_gps_source(principal: dict = Depends(require_auth)):
    from app.core.sdr import gps_source
    return gps_source.status()


@router.post("/gps/source")
async def set_gps_source(body: GpsSourceRequest, principal: dict = Depends(require_auth)):
    """Pick where the live operator fix comes from. 'browser' / 'manual' need no backend poller —
    the UI pushes those fixes to POST /sdr/gps directly; 'gpsd' / 'serial' / 'sdr' start a poller
    that streams fixes in. Nothing runs automatically — a poller starts only on this call."""
    from app.core.sdr import gps_source
    try:
        return gps_source.start(body.kind, host=body.host, port=body.port,
                                path=body.path, baud=body.baud, device_args=body.device_args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ─── spectrum / DF-accuracy / audio decode ───────────────────────────────────
@router.get("/devices/{device_id}/spectrum")
async def device_spectrum(device_id: str, center_hz: Optional[float] = None,
                          span_hz: float = 2.4e6, n_bins: int = 1024, channel: int = 0,
                          principal: dict = Depends(require_auth)):
    dev = sdr_manager.get(device_id)
    if dev is None:
        raise HTTPException(404, "no such device")
    from app.core.sdr import dsp
    c = float(center_hz) if center_hz else float(dev.frequency_hz or 100e6)
    nch = max(1, int(getattr(dev, "channels", 1)))
    ch = max(0, min(nch - 1, int(channel)))
    fr = dsp.spectrum_frame(dev.public(), c, span_hz, n_bins, ch)
    fr["device_id"] = device_id
    fr["channels"] = nch
    fr["df_threshold_dbm"] = dev.df_threshold_dbm
    return fr


@router.get("/accuracy_estimate")
async def accuracy_estimate(channels: int = 5, array_type: str = "uca",
                            spacing_wavelengths: float = 0.4, frequency_hz: float = 433.92e6,
                            snr_db: float = 15.0, snapshots: int = 256,
                            principal: dict = Depends(require_auth)):
    from app.core.sdr import dsp
    return dsp.lob_accuracy_estimate(channels, array_type=array_type, spacing_wavelengths=spacing_wavelengths,
                                     frequency_hz=frequency_hz, snr_db=snr_db, snapshots=snapshots)


@router.get("/audio/modes")
async def audio_modes(principal: dict = Depends(require_auth)):
    from app.core.sdr import dsp
    return dsp.audio_mode_info()


class AudioDecodeRequest(BaseModel):
    frequency_hz: float
    mode: str


@router.post("/devices/{device_id}/audio")
async def device_audio(device_id: str, body: AudioDecodeRequest, principal: dict = Depends(require_auth)):
    dev = sdr_manager.get(device_id)
    if dev is None:
        raise HTTPException(404, "no such device")
    from app.core.sdr import dsp
    return dsp.start_audio_decode(dev.public(), body.frequency_hz, body.mode)


# ─────────────────────────────────────────────────────────────────────────────
# Manual LoB push (testing / external pipelines that prefer REST over TCP)
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/lob")
async def push_lob(body: ManualLob, principal: dict = Depends(require_auth)):
    if sdr_manager.get(body.device_id) is None:
        raise HTTPException(404, "no such device")
    ev = LobEvent(
        device_id=body.device_id, lat=body.lat, lon=body.lon,
        azimuth_deg=body.azimuth_deg % 360.0, frequency_hz=body.frequency_hz,
        rssi_dbm=body.rssi_dbm, confidence_pct=body.confidence_pct,
        observer_height_m=body.observer_height_m, environment=body.environment,
        device_type=sdr_manager.get(body.device_id).type,
        target_device_id=body.target_device_id, t=time.time(),
    )
    await sdr_manager._on_lob(ev)
    return {"status": "ok", "id": ev.id}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket stream — snapshot + live events
# ─────────────────────────────────────────────────────────────────────────────
@router.websocket("/stream")
async def stream_ws(ws: WebSocket):
    # WS auth: when ARES_AUTH is on, require either a valid bearer token
    # (`?token=<jwt>` — a UI client) or `?mesh_secret=<secret>` (a peer Ares node).
    from app.config import settings
    if settings.auth_enabled:
        from app.core.auth import decode_token
        from app.core import meshsec
        qp = ws.query_params
        ok = (decode_token(qp.get("token", "")) is not None) or meshsec.ws_secret_ok(qp.get("mesh_secret"))
        if not ok:
            await ws.close(code=4401)         # 4401 ≈ "unauthorized" for WS
            return
    await ws.accept()
    q = await sdr_manager.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping", "t": time.time()})
                continue
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("sdr ws error")
    finally:
        sdr_manager.unsubscribe(q)
