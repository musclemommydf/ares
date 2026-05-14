"""
Time-sync status — what's keeping this node's clock honest.

Multi-node DF / TDoA needs sub-µs time agreement between nodes. Three
sources, in order of preference:
  1. GPS PPS — pulse-per-second from a disciplined GPS receiver fed to the
                SDR's PPS input (USRP X310 / Kraken with external GPS-DO etc.).
  2. PTP     — IEEE 1588 hardware time over Ethernet (ptp4l).
  3. NTP / chrony — software, ~ms accuracy; only good enough for sanity, not
                     TDoA.

This module reads each source's status from typical Linux locations and
returns a single uniform dict.

Returns (example):
{
  "preferred": "pps",
  "gps_pps":   { "available": true, "lock": true, "satellites": 11, "tdop": 0.8, "ppm": 0.02, "source": "gpsd" },
  "ptp":       { "available": true, "synchronised": true, "offset_ns": 84, "iface": "eth0", "via": "ptp4l" },
  "chrony":    { "available": true, "synchronised": true, "offset_ms": 0.4, "ref_id": "GPS" },
  "reach_ns":  120,   # estimated 1-σ time error vs UTC, ns
}
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _run(cmd: list[str], timeout_s: float = 1.5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s).stdout
    except Exception:
        return ""


def _gpsd_status() -> dict:
    """Read PPS lock + sat count from gpsd via JSON over the GPS socket.
    Falls back to the optional `gpspipe` CLI if available."""
    out: dict = {"available": False}
    if not shutil.which("gpspipe"):
        return out
    raw = _run(["gpspipe", "-w", "-n", "5"], timeout_s=2.0)
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"): continue
        try: msg = json.loads(line)
        except Exception: continue
        if msg.get("class") == "PPS":
            out.update({"available": True, "lock": True, "source": "gpsd",
                          "real_sec": msg.get("real_sec"), "clock_sec": msg.get("clock_sec")})
        elif msg.get("class") == "TPV":
            out["available"] = True
            out["fix_mode"] = int(msg.get("mode") or 0)
            out["lock"] = out.get("lock", out["fix_mode"] >= 2)
        elif msg.get("class") == "SKY":
            out["available"] = True
            out["satellites"] = sum(1 for s in (msg.get("satellites") or []) if s.get("used"))
            out["tdop"] = msg.get("tdop")
    return out


def _ptp_status() -> dict:
    """Parse `pmc` / `phc_ctl` / `ptp4l` log output. Many distros put ptp4l
    state in `/run/linuxptp/ptp4l.sock` — but the cheap probe is the journal."""
    out: dict = {"available": False}
    if not shutil.which("pmc"):
        return out
    raw = _run(["pmc", "-u", "-b", "0", "GET CURRENT_DATA_SET"], timeout_s=1.0)
    if "offsetFromMaster" in raw:
        m = re.search(r"offsetFromMaster\s+([-\d]+)", raw)
        offset_ns = int(m.group(1)) if m else None
        out.update({"available": True, "synchronised": offset_ns is not None and abs(offset_ns) < 1_000_000,
                      "offset_ns": offset_ns, "via": "pmc/ptp4l"})
    return out


def _chrony_status() -> dict:
    """Lightweight chrony status — `chronyc tracking`."""
    out: dict = {"available": False}
    if not shutil.which("chronyc"):
        return out
    raw = _run(["chronyc", "tracking"], timeout_s=1.0)
    if not raw:
        return out
    out["available"] = True
    for line in raw.splitlines():
        if "Reference ID" in line:
            out["ref_id"] = line.split(":", 1)[1].strip().split(" ")[0]
        elif "Stratum" in line:
            try: out["stratum"] = int(line.split(":", 1)[1].strip())
            except Exception: pass
        elif "Leap status" in line:
            out["leap_status"] = line.split(":", 1)[1].strip()
        elif "System time" in line:
            m = re.search(r"([-+]?\d+\.\d+)", line)
            if m:
                # "System time : 0.000001234 seconds slow of NTP time"
                offset_s = float(m.group(1))
                out["offset_ms"] = offset_s * 1000.0
                out["synchronised"] = abs(offset_s) < 0.1
    return out


def status() -> dict:
    gps = _gpsd_status()
    ptp = _ptp_status()
    chr_ = _chrony_status()
    # Pick the preferred source for fusion / TDoA weighting.
    preferred = "none"; reach_ns = 1_000_000_000
    if gps.get("lock"):
        preferred = "pps"; reach_ns = 100
    elif ptp.get("synchronised"):
        preferred = "ptp"; reach_ns = max(1000, abs(ptp.get("offset_ns") or 1_000))
    elif chr_.get("synchronised"):
        preferred = "ntp"; reach_ns = int(max(1e5, abs(chr_.get("offset_ms") or 1) * 1e6))
    return {
        "preferred": preferred, "reach_ns": int(reach_ns),
        "gps_pps": gps, "ptp": ptp, "chrony": chr_,
        "host": os.uname().nodename if hasattr(os, "uname") else "",
    }
