# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/cellular — passive cellular decoders for Ares (2G GSM, 3G UMTS, 4G LTE, 5G NR).

The decoders are deliberately isolated from the rest of `app.core.sdr` so that
the heavyweight GNU Radio dependency (used by the GSM flowgraph) only loads
when the user actively starts a cellular session. Every higher-G decoder runs
as an external process (`LTESniffer`, `5GSniffer`, `srsRAN`) so the package
doesn't drag those into Python either.

Public surface:

    start_decoder(kind, device, frequency_hz, **kwargs) -> CellularSession
    list_sessions() -> list[dict]
    stop_session(sid) -> bool
    get_session(sid) -> CellularSession | None
    capabilities() -> dict

Each ``CellularSession`` exposes ``.events_queue`` (an asyncio queue of decoded
events) and ``.status()``. The cellular_consumer module (see
``app.core.targets.cellular_consumer``) hooks the queue into the target
tracker so cell-IDs / paging TMSIs / RNTIs flow straight to the Targets tab.

Strictly passive: we receive only what the air interface broadcasts in
plaintext. No decryption, no A5/1 break, no IMSI-catcher behaviour.
"""
from __future__ import annotations

import logging
import shutil
import uuid
from typing import Any, Optional

from .session import CellularSession

log = logging.getLogger(__name__)

_SESSIONS: dict[str, CellularSession] = {}


def _has_gnuradio() -> bool:
    try:
        import gnuradio   # noqa: F401
        import grgsm      # noqa: F401
        return True
    except Exception:
        return False


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


def capabilities() -> dict:
    """What this Ares can actually decode right now."""
    gr = _has_gnuradio()
    return {
        "gnuradio_in_process": gr,
        "decoders": {
            "gsm":   {"available": gr,
                       "method": "gnuradio_flowgraph" if gr else None,
                       "needs": ["gnuradio", "gr-gsm (Python: grgsm)"],
                       "passive_outputs": ["MCC/MNC/LAC/CI", "paging TMSI", "reattach IMSI (when plaintext)"]},
            "umts":  {"available": True,
                       "method": "df_only",
                       "needs": [],
                       "passive_outputs": ["WCDMA cell-detection + DF only — no BCCH decoder"]},
            "lte":   {"available": _has_binary("LTESniffer") or _has_binary("srsue"),
                       "method": "external_subprocess",
                       "needs": ["LTESniffer (preferred) or srsue (srsRAN)"],
                       "passive_outputs": ["PDCCH RNTIs", "MCC/MNC/TAC/CellID (SIB1)", "RSRP"]},
            "nr":    {"available": _has_binary("5g_sniffer") or _has_binary("5GSniffer") or _has_binary("5g-sniffer"),
                       "method": "external_subprocess",
                       "needs": ["5GSniffer (spritelab — github.com/spritelab/5GSniffer; binary `5g_sniffer`)"],
                       "passive_outputs": ["PCI", "MCC/MNC/TAC/CellID (MIB+SIB1)", "SSB RSRP", "PDCCH DCI / RNTI"]},
            "wifi":  {"available": _has_binary("hcxdumptool") or _has_binary("airodump-ng"),
                       "method": "external_subprocess",
                       "needs": ["hcxdumptool (preferred) or airodump-ng + monitor-mode adapter"],
                       "passive_outputs": ["BSSIDs", "associating STA MACs (when not randomised)", "SSIDs", "RSSI"]},
            "ble":   {"available": _has_binary("btmon") or _has_binary("bluetoothctl"),
                       "method": "external_subprocess",
                       "needs": ["bluez (btmon / bluetoothctl)"],
                       "passive_outputs": ["BD_ADDRs", "advertising-data UUIDs", "RSSI"]},
        },
    }


def start_decoder(kind: str, device: Optional[dict], frequency_hz: float, **kwargs: Any) -> CellularSession:
    """Start a passive decoder. ``kind`` in {gsm, umts, lte, nr, wifi, ble}.
    Returns the session; the caller stashes the id and can poll/stop later.
    """
    kind = (kind or "").lower()
    sid = uuid.uuid4().hex[:12]
    if kind == "gsm":
        from .gsm import GsmDecoder
        sess = GsmDecoder(sid=sid, device=device, center_hz=float(frequency_hz),
                            sample_rate_hz=float(kwargs.get("sample_rate_hz", 1_000_000)),
                            gain=float(kwargs.get("gain", 30.0)))
    elif kind == "lte":
        from .lte import LteSnifferSession
        sess = LteSnifferSession(sid=sid, device=device, center_hz=float(frequency_hz),
                                   bandwidth_hz=float(kwargs.get("bandwidth_hz", 10_000_000)))
    elif kind == "nr":
        from .nr import NrSnifferSession
        sess = NrSnifferSession(sid=sid, device=device, center_hz=float(frequency_hz),
                                  scs_khz=int(kwargs.get("scs_khz", 30)))
    elif kind == "umts":
        from .umts import UmtsDetectorSession
        sess = UmtsDetectorSession(sid=sid, device=device, center_hz=float(frequency_hz))
    elif kind == "wifi":
        from app.core.sdr.wifi_bt import WifiMonitor
        sess = WifiMonitor(sid=sid, interface=kwargs.get("interface", "wlan0mon"),
                             channel=kwargs.get("channel"))
    elif kind == "ble":
        from app.core.sdr.wifi_bt import BleMonitor
        sess = BleMonitor(sid=sid, interface=kwargs.get("interface", "hci0"))
    else:
        raise ValueError(f"unknown cellular kind: {kind!r}")
    sess.start()
    _SESSIONS[sid] = sess
    log.info("cellular: started %s session %s @ %s Hz", kind, sid, frequency_hz)
    return sess


def list_sessions() -> list[dict]:
    return [s.status() for s in _SESSIONS.values()]


def get_session(sid: str) -> Optional[CellularSession]:
    return _SESSIONS.get(sid)


def stop_session(sid: str) -> bool:
    s = _SESSIONS.pop(sid, None)
    if s is None:
        return False
    try:
        s.stop()
    except Exception as e:
        log.warning("error stopping cellular session %s: %s", sid, e)
    return True
