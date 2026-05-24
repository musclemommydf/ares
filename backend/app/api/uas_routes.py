# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
api/uas_routes.py — UAS (drone) video-downlink scanner / decoder bridge.

  GET  /uas/feed_types                       — the analog + digital feed registry + known channel plans
  GET  /uas/status                           — module status (decoders on PATH, capture backend, sessions)
  GET  /uas/decoders                         — which external decode tools / capture backends are available
  GET  /uas/scan?device_id&start_hz&stop_hz  — sweep a band, classify the UAS video feeds present  [auth]
  POST /uas/decode                           — start a decode/characterize session for one feed       [auth]
  GET  /uas/sessions                         — active decode sessions
  GET  /uas/sessions/{sid}                   — one session
  GET  /uas/sessions/{sid}/metadata          — latest decoded MISB ST 0601 KLV → platform / LOS / footprint
  GET  /uas/sessions/{sid}/stream            — video-stream status / proxy descriptor
  DELETE /uas/sessions/{sid}                 — stop a session                                          [auth]
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.core.security import audit
from app.core.sdr import uas_video
from app.core.sdr import video_exploit
from app.core.sdr import remote_id
from app.core.sdr import ml_signal_classifier
from app.core.sdr import sdr_manager

try:  # CoT push is best-effort — never let an export hiccup fail the request
    from app.core import cot as _cot
except Exception:  # pragma: no cover
    _cot = None

router = APIRouter(tags=["uas"], prefix="/uas")


def _device_dict(device_id: Optional[str]) -> dict:
    """Resolve a registered SDR to a plain dict the dsp/IQ layer can use; falls back
    to a minimal stub (the synthetic path ignores everything but the id)."""
    if not device_id:
        return {"id": "synthetic", "metadata": {}}
    for attr in ("get", "device", "get_device"):
        fn = getattr(sdr_manager, attr, None)
        if callable(fn):
            try:
                d = fn(device_id)
            except Exception:
                d = None
            if d is not None:
                if hasattr(d, "public"):
                    try:
                        return d.public()
                    except Exception:
                        pass
                if isinstance(d, dict):
                    return d
    return {"id": device_id, "metadata": {}}


# ── reference / status ───────────────────────────────────────────────────────
@router.get("/feed_types")
def feed_types():
    return {"feed_types": uas_video.FEED_TYPES, "known_channels": uas_video.KNOWN_CHANNELS}


@router.get("/status")
def uas_status():
    return uas_video.status()


@router.get("/decoders")
def uas_decoders():
    st = uas_video.status()
    return {"decoders": st["decoders"], "capture_backend": st["capture_backend"]}


# ── scan a band ──────────────────────────────────────────────────────────────
@router.get("/scan")
def scan(device_id: Optional[str] = None,
         start_hz: float = Query(..., gt=0),
         stop_hz: float = Query(..., gt=0),
         step_hz: float = Query(20e6, gt=1e5, le=40e6),
         use_iq: bool = True,
         max_hold: bool = False,
         maxhold_key: str = "default",
         reset_maxhold: bool = False,
         _auth=Depends(require_auth)):
    if abs(stop_hz - start_hz) > 6e9:
        raise HTTPException(400, "scan span too wide (max 6 GHz per call)")
    dev = _device_dict(device_id)
    audit("uas.scan", device=device_id or "synthetic", start_hz=start_hz, stop_hz=stop_hz, max_hold=max_hold)
    return uas_video.classify_band(dev, start_hz, stop_hz, step_hz=step_hz, use_iq=use_iq,
                                    max_hold=max_hold, maxhold_key=maxhold_key,
                                    reset_maxhold=reset_maxhold)


@router.post("/scan/maxhold/reset")
def reset_scan_maxhold(maxhold_key: str = "default", _auth=Depends(require_auth)):
    uas_video.reset_max_hold(maxhold_key)
    return {"reset": True, "maxhold_key": maxhold_key}


@router.get("/scan/maxhold")
def get_scan_maxhold(maxhold_key: str = "default", _auth=Depends(require_auth)):
    snap = uas_video.get_max_hold(maxhold_key)
    if snap is None:
        raise HTTPException(404, f"no max-hold accumulator named {maxhold_key!r}")
    return snap


# ── start a decode / characterize session ────────────────────────────────────
class AnalogOptions(BaseModel):
    """Knobs for the in-process analog-video demod (NTSC / PAL / SECAM / VSB).
    All fields are optional; omitted keys fall through to the demod's defaults."""
    system: Optional[str] = None                  # ntsc | pal | secam | vsb
    width_px: Optional[int] = Field(None, ge=64, le=2048)
    max_frames: Optional[int] = Field(None, ge=1, le=64)
    try_all_detectors: Optional[bool] = None
    use_h_sync_pll: Optional[bool] = None
    use_v_sync_detect: Optional[bool] = None
    use_per_line_clamp: Optional[bool] = None
    deinterlace: Optional[bool] = None
    frame_avg_n: Optional[int] = Field(None, ge=0, le=32)
    decode_color: Optional[bool] = None
    peak_hold_tau_s: Optional[float] = Field(None, gt=0.0, le=5.0)
    # operator overrides (set when auto-tune isn't getting it right)
    line_rate_hz: Optional[float] = Field(None, gt=1000, le=200000)    # forces this scanline rate
    frame_rate_hz: Optional[float] = Field(None, gt=1.0, le=240.0)     # forces this frame rate
    pixel_rate_hz: Optional[float] = Field(None, gt=1e5, le=200e6)     # forces this pixel rate
    h_offset_samples: Optional[int] = Field(None, ge=-32768, le=32768) # H-shift active region
    v_offset_lines: Optional[int] = Field(None, ge=-512, le=512)       # V-shift active region
    active_duration_s: Optional[float] = Field(None, gt=1e-5, le=0.1)  # active scanline duration


class DecodeRequest(BaseModel):
    device_id: Optional[str] = None
    frequency_hz: float = Field(..., gt=0)
    feed_type: Optional[str] = None   # None / "auto" → Ares auto-detects from the spectrum + channel plan
    bandwidth_hz: Optional[float] = Field(None, gt=0)
    channel: int = 0
    label: str = ""
    push_to_atak: bool = False
    analog_options: Optional[AnalogOptions] = None
    capture_seconds: float = Field(0.045, ge=0.020, le=0.5)


def _aopts_dict(ao: Optional[AnalogOptions]) -> dict:
    if not ao:
        return {}
    return {k: v for k, v in ao.model_dump().items() if v is not None}


@router.post("/decode")
def decode(req: DecodeRequest, _auth=Depends(require_auth)):
    dev = _device_dict(req.device_id)
    sess = uas_video.start_decode(dev, req.frequency_hz, req.feed_type,
                                  bandwidth_hz=req.bandwidth_hz, channel=req.channel, label=req.label,
                                  analog_options=_aopts_dict(req.analog_options),
                                  capture_seconds=req.capture_seconds)
    if "error" in sess:
        raise HTTPException(400, sess["error"])
    audit("uas.decode", device=req.device_id or "synthetic", feed_type=req.feed_type,
          frequency_hz=req.frequency_hz, status=sess.get("status"), push_to_atak=req.push_to_atak)
    if req.push_to_atak and sess.get("carries_klv") and _cot is not None:
        md = uas_video.session_metadata(sess["id"])
        if md and md.get("klv"):
            sess["cot"] = _push_uas_cot(sess, md)
    return sess


class RedemodRequest(BaseModel):
    analog_options: Optional[AnalogOptions] = None
    capture_seconds: Optional[float] = Field(None, ge=0.020, le=0.5)


@router.post("/sessions/{sid}/redemod")
def session_redemod(sid: str, req: RedemodRequest, _auth=Depends(require_auth)):
    """Re-run the analog-video demod on a fresh capture with updated options.
    Lets the operator tweak colormap-independent params (scanline rate, frame
    averaging, peak-hold τ, colour decode, deinterlace, …) on a live session
    without restarting it."""
    sess = uas_video.redemod_session(sid,
                                      analog_options=_aopts_dict(req.analog_options),
                                      capture_seconds=req.capture_seconds)
    if sess is None:
        raise HTTPException(404, "no such session")
    return sess


def _push_uas_cot(sess: dict, md: dict) -> dict:
    """Best-effort CoT: a sensor point-of-interest at the frame centre + the footprint
    as a drawn polygon, tagged with the platform call sign. Reuses cot._event / _send_all
    when present; otherwise records that no CoT targets are configured."""
    try:
        klv = md["klv"]
        fc_lat = klv.get("frame_center_lat_deg")
        fc_lon = klv.get("frame_center_lon_deg")
        if fc_lat is None or fc_lon is None:
            return {"sent": False, "reason": "no frame-centre in KLV"}
        cs = klv.get("platform_call_sign") or klv.get("platform_designation") or "UAS"
        sent = False
        # point-of-interest at the sensor frame centre
        if hasattr(_cot, "_event") and hasattr(_cot, "_send_all"):
            uid = f"ares-uas-fc-{sess['id']}"
            ev = _cot._event(uid, "a-u-G", float(fc_lat), float(fc_lon),
                             remarks=f"{cs} sensor frame centre · slant {klv.get('slant_range_m', '?')} m · {sess['feed_name']}")
            _cot._send_all(ev)
            sent = True
        return {"sent": sent, "frame_center": [fc_lat, fc_lon], "call_sign": cs,
                "footprint_pts": len(md.get("footprint") or [])}
    except Exception as e:  # pragma: no cover
        return {"sent": False, "reason": str(e)}


# ── sessions ─────────────────────────────────────────────────────────────────
@router.get("/sessions")
def sessions():
    st = uas_video.status()
    return {"sessions": uas_video.list_sessions(), "capture_backend": st["capture_backend"]}


@router.get("/sessions/{sid}")
def session(sid: str):
    s = uas_video.get_session(sid)
    if not s:
        raise HTTPException(404, "no such session")
    return s


@router.get("/sessions/{sid}/metadata")
def session_metadata(sid: str):
    md = uas_video.session_metadata(sid)
    if md is None:
        s = uas_video.get_session(sid)
        if not s:
            raise HTTPException(404, "no such session")
        raise HTTPException(409, "this feed type does not carry MISB/STANAG-4609 metadata")
    return md


@router.get("/sessions/{sid}/stream")
def session_stream(sid: str):
    s = uas_video.get_session(sid)
    if not s:
        raise HTTPException(404, "no such session")
    frames = uas_video.session_frames(sid)
    demod = s.get("demod") or {}
    return {
        "session_id": sid, "status": s.get("status"), "feed_type": s.get("feed_type"),
        "transport": s.get("transport"), "capture_backend": s.get("capture_backend"),
        "pipeline": s.get("pipeline"), "message": s.get("message"),
        "demod": demod,
        "frame_count": len(frames),
        "frame_url": (f"/api/v1/uas/sessions/{sid}/frame.png" if frames else None),
        "note": ("Ares demodulates this feed in-process (sdr/native_demod). Analog feeds yield raster "
                 "frames at frame_url; digital feeds yield recovered PHY symbols + (on a clean link) the "
                 "demuxed MPEG-TS / KLV — see the 'demod' block. A real H.264/H.265 elementary-stream "
                 "decode of a recovered TS still benefits from ffmpeg if it's installed."),
    }


@router.get("/sessions/{sid}/frame.png")
def session_frame(sid: str, i: int = 0):
    """The i-th raster frame recovered by the native analog-video demod, as a PNG.
    With no index the latest is returned; the index wraps so the UI can cycle."""
    s = uas_video.get_session(sid)
    if not s:
        raise HTTPException(404, "no such session")
    frames = uas_video.session_frames(sid)
    if not frames:
        raise HTTPException(409, "no decoded video frames for this session "
                                 "(digital feed, or the demod hasn't produced a raster frame)")
    png = frames[i % len(frames)] if i else frames[-1]
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.delete("/sessions/{sid}")
def stop(sid: str, _auth=Depends(require_auth)):
    ok = uas_video.stop_session(sid)
    if ok:
        audit("uas.session.stop", session=sid)
    return {"removed": ok}

# ── digital-video exploitation (PED) ─────────────────────────────────────────
@router.get("/exploit/status")
def exploit_status():
    return video_exploit.status()


class CharacterizeRequest(BaseModel):
    device_id: Optional[str] = None
    frequency_hz: float = Field(..., gt=0)
    bandwidth_hz: float = Field(8e6, gt=1e5, le=80e6)
    channel: int = 0


@router.post("/exploit/characterize")
def exploit_characterize(req: CharacterizeRequest, _auth=Depends(require_auth)):
    """Cumulant / cyclostationary modulation & OFDM identification on a captured IQ
    snapshot (needs an IQ backend — SoapySDR's SignalHound / Sidekiq / UHD module, or a
    wired IQ provider). Without one this reports what's missing."""
    dev = _device_dict(req.device_id)
    rate = max(2e6, min(40e6, req.bandwidth_hz * 1.4))
    iq = uas_video._capture_iq(dev, req.frequency_hz, rate, int(rate * 0.02), req.channel)
    audit("uas.exploit.characterize", device=req.device_id or "synthetic", frequency_hz=req.frequency_hz,
          bandwidth_hz=req.bandwidth_hz, iq="captured" if (iq is not None and iq.size >= 4096) else "none")
    if iq is None or iq.size < 4096:
        return {"status": "no_iq_backend", "frequency_hz": req.frequency_hz, "bandwidth_hz": req.bandwidth_hz,
                "iq_backend": uas_video._capture_backend(),
                "message": ("No IQ capture available — install SoapySDR with the SignalHound / Sidekiq / UHD module, "
                            "or wire an IQ provider, for live cumulant/cyclostationary modulation classification.")}
    return {"status": "characterized", "frequency_hz": req.frequency_hz, "bandwidth_hz": req.bandwidth_hz,
            "iq_backend": uas_video._capture_backend(), "characterization": video_exploit.classify_modulation(iq, rate)}


@router.post("/sessions/{sid}/exploit")
def session_exploit(sid: str, _auth=Depends(require_auth)):
    """Run a PED pass on a decode session: demux the (decoded or synthesised) MPEG-TS →
    PID map + STANAG-4609 KLV track → platform track / sensor LOS / footprint polygons →
    GeoJSON + (with ffmpeg/tesseract) keyframe + in-frame-OCR plan; plus the digital-signal
    characterization when an IQ backend is available."""
    if not uas_video.get_session(sid):
        raise HTTPException(404, "no such session")
    r = video_exploit.exploit_session(sid)
    if "error" in r:
        raise HTTPException(400, r["error"])
    audit("uas.exploit.session", session=sid, klv_track_len=r.get("klv_track_len"),
          sigchar=(r.get("signal_characterization") or {}).get("family"))
    return r


@router.get("/exploit/{eid}")
def get_exploit(eid: str):
    r = video_exploit.get_exploit(eid)
    if not r:
        raise HTTPException(404, "no such exploit run")
    return r

# ── Remote ID / DJI DroneID — UAS telemetry-beacon demux ─────────────────────
@router.get("/rid/status")
def rid_status():
    return remote_id.status()


class RidParseRequest(BaseModel):
    hex: str
    format: str = "auto"   # "auto" | "f3411" | "dji"


@router.post("/rid/parse")
def rid_parse(req: RidParseRequest):
    """Parse the bytes of a captured Remote-ID / DroneID message (ASTM F3411 message
    or pack, or a de-framed DJI DroneID payload) → structured fields + GeoJSON."""
    try:
        data = bytes.fromhex(req.hex.replace(" ", "").replace(":", ""))
    except Exception:
        raise HTTPException(400, "hex: not valid hex")
    if not data:
        raise HTTPException(400, "hex: empty")
    fmt = req.format
    if fmt == "auto":
        fmt = "f3411" if (((data[0] >> 4) & 0x0F) in (0x0, 0x1, 0x3, 0x4, 0x5, 0xF) and (data[0] & 0x0F) <= 3) else "dji"
    parsed = remote_id.parse_f3411(data) if fmt == "f3411" else remote_id.parse_dji_droneid(data)
    return {"format": fmt, "parsed": parsed, "geojson": remote_id.rid_to_geojson(parsed)}


class RidDecodeRequest(BaseModel):
    device_id: Optional[str] = None
    frequency_hz: float = Field(2.437e9, gt=0)
    kind: str = "f3411"   # "f3411" (WiFi/BT Remote ID) | "dji" (DJI DroneID OFDM burst)
    label: str = ""
    push_to_atak: bool = False


@router.post("/rid/decode")
def rid_decode(req: RidDecodeRequest, _auth=Depends(require_auth)):
    dev = _device_dict(req.device_id)
    sess = remote_id.decode_rid(dev, frequency_hz=req.frequency_hz, kind=req.kind, label=req.label)
    audit("uas.rid.decode", device=req.device_id or "synthetic", kind=req.kind, frequency_hz=req.frequency_hz,
          status=sess.get("status"), push_to_atak=req.push_to_atak)
    if req.push_to_atak and sess.get("last"):
        sess["cot"] = remote_id.rid_to_cot(sess["last"])
    return sess


@router.get("/rid/sessions")
def rid_sessions():
    return {"sessions": remote_id.list_rid_sessions()}


@router.get("/rid/sessions/{sid}/metadata")
def rid_session_metadata(sid: str):
    md = remote_id.rid_session_metadata(sid)
    if md is None:
        raise HTTPException(404, "no such Remote-ID session")
    return md


@router.delete("/rid/sessions/{sid}")
def rid_stop(sid: str, _auth=Depends(require_auth)):
    ok = remote_id.stop_rid_session(sid)
    if ok:
        audit("uas.rid.session.stop", session=sid)
    return {"removed": ok}

@router.get("/ml/status")
def ml_status():
    """Status of the optional ML signal-classifier stage: which runtimes (onnxruntime /
    torch) are available, the feature names, the default class set, and whether a model
    is registered. A trained model is deployment-supplied — see ml_signal_classifier's
    docstring for how to train and register one."""
    return ml_signal_classifier.status()

