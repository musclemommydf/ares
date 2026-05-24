# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/gps_source.py — pluggable live-GPS sources for the operator position.

The operator/antenna position (shown on the map, used as the LoB observer, the SDR-device
position, the auto-coverage centre, …) can come from:

  * **this computer** — the browser's Geolocation API; the frontend just POSTs the fix to
    ``/api/v1/sdr/gps`` (no backend poller needed). Source tag ``"browser"``.
  * **a USB GPS dongle** — either via **gpsd** (the standard Linux GPS daemon — works with any
    receiver gpsd supports; install ``gpsd gpsd-clients``, plug the dongle in) on TCP 2947, or by
    reading **NMEA-0183** straight off a serial port (``/dev/ttyUSB0`` / ``/dev/ttyACM0``, 4800/9600/…
    baud) — needs ``pyserial``.
  * **an SDR with a GNSS-disciplined oscillator / GPSDO** — USRP X-series / N-series, Sidekiq,
    SignalHound SM-series, … expose ``gps_*`` sensors via SoapySDR (``gps_locked``, ``gps_gpgga``,
    ``gps_lat``/``gps_long``, …); we poll those.
  * **manual** — typed into the SDR console (the existing ``POST /api/v1/sdr/gps``).

Start/stop a poller with ``start(kind, ...)``; it pushes fixes into ``sdr_manager.set_gps_fix``.
Everything degrades cleanly: no gpsd / no pyserial / no SoapySDR → the source just reports it's
unavailable and the manual + browser paths still work. Nothing runs automatically — a poller is
only ever started by an explicit request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import Optional

log = logging.getLogger(__name__)

_STATE: dict = {"kind": "off", "running": False, "last_fix": None, "last_error": None,
                "started_ts": None, "config": {}}
_TASK: Optional[asyncio.Task] = None


def status() -> dict:
    return {**_STATE,
            "available": {
                "gpsd": True,                                  # we can always try to connect to localhost:2947
                "serial": _have_pyserial(),
                "sdr": _have_soapy(),
                "browser": True,
            },
            "note": "browser geolocation needs no backend poller — the UI POSTs that fix to /sdr/gps directly"}


def _have_pyserial() -> bool:
    try:
        import serial  # noqa: F401
        return True
    except Exception:
        return False


def _have_soapy() -> bool:
    try:
        import SoapySDR  # noqa: F401
        return True
    except Exception:
        return False


def _push(lat, lon, *, alt_m=0.0, heading_deg=None, speed_mps=None, source="gps") -> None:
    if lat is None or lon is None:
        return
    try:
        from app.core.sdr import sdr_manager
        fix = sdr_manager.set_gps_fix(float(lat), float(lon), float(alt_m or 0.0),
                                      heading_deg, speed_mps, source)
        _STATE["last_fix"] = fix
    except Exception as e:  # pragma: no cover
        _STATE["last_error"] = f"push failed: {e}"


# ── gpsd (TCP JSON, port 2947) ───────────────────────────────────────────────
async def _run_gpsd(host: str = "127.0.0.1", port: int = 2947) -> None:
    while True:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=5.0)
        except Exception as e:
            _STATE["last_error"] = f"gpsd connect failed ({host}:{port}): {e}"
            await asyncio.sleep(5.0)
            continue
        _STATE["last_error"] = None
        try:
            writer.write(b'?WATCH={"enable":true,"json":true};\n')
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
                if not line:
                    break
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if obj.get("class") == "TPV" and obj.get("lat") is not None and obj.get("lon") is not None:
                    spd = obj.get("speed")               # m/s
                    trk = obj.get("track")               # deg true
                    _push(obj["lat"], obj["lon"], alt_m=obj.get("altMSL") or obj.get("alt") or 0.0,
                          heading_deg=trk, speed_mps=spd, source=f"gpsd ({obj.get('mode', '?')}D)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _STATE["last_error"] = f"gpsd stream error: {e}"
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        await asyncio.sleep(3.0)


# ── serial NMEA-0183 (USB GPS dongle on /dev/ttyUSB* | /dev/ttyACM*) ─────────
_GGA = re.compile(r"^\$..GGA,")
_RMC = re.compile(r"^\$..RMC,")


def _nmea_deg(val: str, hemi: str) -> Optional[float]:
    # ddmm.mmmm / dddmm.mmmm + N/S/E/W
    if not val:
        return None
    try:
        f = float(val)
    except ValueError:
        return None
    d = math.floor(f / 100.0)
    m = f - d * 100.0
    deg = d + m / 60.0
    if hemi in ("S", "W"):
        deg = -deg
    return deg


async def _run_serial(path: str, baud: int = 9600) -> None:
    try:
        import serial  # type: ignore
    except Exception:
        _STATE["last_error"] = "pyserial not installed — `pip install pyserial`, or use gpsd instead"
        await asyncio.sleep(1.0)
        return
    while True:
        try:
            ser = serial.serial_for_url(path, baudrate=int(baud), timeout=1.0)
        except Exception as e:
            _STATE["last_error"] = f"serial open failed ({path} @ {baud}): {e}"
            await asyncio.sleep(5.0)
            continue
        _STATE["last_error"] = None
        last_speed = None; last_track = None
        try:
            while True:
                raw = await asyncio.get_event_loop().run_in_executor(None, ser.readline)
                line = raw.decode("ascii", "replace").strip()
                if not line.startswith("$"):
                    continue
                p = line.split("*")[0].split(",")
                if _RMC.match(line) and len(p) >= 10 and p[2] == "A":
                    lat = _nmea_deg(p[3], p[4]); lon = _nmea_deg(p[5], p[6])
                    try: last_speed = float(p[7]) * 0.514444 if p[7] else None   # knots → m/s
                    except ValueError: pass
                    try: last_track = float(p[8]) if p[8] else None
                    except ValueError: pass
                    _push(lat, lon, heading_deg=last_track, speed_mps=last_speed, source="USB GPS (NMEA)")
                elif _GGA.match(line) and len(p) >= 10 and p[6] not in ("", "0"):
                    lat = _nmea_deg(p[2], p[3]); lon = _nmea_deg(p[4], p[5])
                    try: alt = float(p[9]) if p[9] else 0.0
                    except ValueError: alt = 0.0
                    _push(lat, lon, alt_m=alt, heading_deg=last_track, speed_mps=last_speed, source="USB GPS (NMEA)")
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _STATE["last_error"] = f"serial read error: {e}"
        finally:
            try: ser.close()
            except Exception: pass
        await asyncio.sleep(3.0)


# ── SDR GPSDO / GNSS sensors (via SoapySDR) ──────────────────────────────────
def _parse_gpgga(s: str) -> Optional[tuple[float, float, float]]:
    p = s.split("*")[0].split(",")
    if len(p) < 10 or p[6] in ("", "0"):
        return None
    lat = _nmea_deg(p[2], p[3]); lon = _nmea_deg(p[4], p[5])
    try: alt = float(p[9]) if p[9] else 0.0
    except ValueError: alt = 0.0
    return (lat, lon, alt) if lat is not None and lon is not None else None


async def _run_sdr(device_args: str = "") -> None:
    try:
        import SoapySDR  # type: ignore
    except Exception:
        _STATE["last_error"] = "SoapySDR not installed — needed to read an SDR's GPSDO sensors"
        await asyncio.sleep(1.0)
        return
    try:
        dev = SoapySDR.Device(device_args) if device_args else SoapySDR.Device()
    except Exception as e:
        _STATE["last_error"] = f"SoapySDR open failed ({device_args or 'auto'}): {e}"
        await asyncio.sleep(5.0)
        return
    _STATE["last_error"] = None
    while True:
        try:
            sensors = {}
            for s in (list(dev.listSensors()) or []):
                try: sensors[s] = dev.readSensor(s)
                except Exception: pass
            lat = lon = alt = None
            # common UHD/GPSDO sensor names
            gga = sensors.get("gps_gpgga") or sensors.get("gpgga")
            if gga:
                parsed = _parse_gpgga(str(gga))
                if parsed: lat, lon, alt = parsed
            if lat is None:
                for la, lo in (("gps_lat", "gps_long"), ("gps_lat", "gps_lon"), ("gpsdo_lat", "gpsdo_lon")):
                    if la in sensors and lo in sensors:
                        try: lat, lon = float(sensors[la]), float(sensors[lo]); break
                        except (TypeError, ValueError): pass
            locked = str(sensors.get("gps_locked", sensors.get("ref_locked", "?"))).lower() in ("true", "1", "yes", "locked")
            if lat is not None and lon is not None:
                _push(lat, lon, alt_m=alt or 0.0, source=f"SDR GPSDO{' (locked)' if locked else ''}")
            elif not _STATE.get("last_fix"):
                _STATE["last_error"] = ("SDR exposes no GPS position sensor" if not sensors
                                        else f"SDR sensors present but no usable GPS fix: {sorted(sensors)[:8]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _STATE["last_error"] = f"SDR sensor read error: {e}"
        await asyncio.sleep(2.0)


# ── controller ───────────────────────────────────────────────────────────────
def stop() -> dict:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
    _TASK = None
    _STATE.update(kind="off", running=False, started_ts=None, config={})
    return status()


def start(kind: str, *, host: str = "127.0.0.1", port: int = 2947,
          path: str = "/dev/ttyUSB0", baud: int = 9600, device_args: str = "") -> dict:
    """Start (or switch to) a live-GPS poller. ``kind`` ∈ {gpsd, serial, sdr, browser, off, manual}.
    'browser' and 'manual' need no backend poller (the UI POSTs those fixes directly) — calling
    start() with them just records the chosen source and stops any running poller."""
    global _TASK
    kind = (kind or "off").lower()
    stop()
    if kind in ("off", "browser", "manual"):
        _STATE.update(kind=kind, running=False, started_ts=time.time(),
                      config={} if kind == "off" else {"hint": "fixes pushed by the UI to POST /sdr/gps"})
        return status()
    if kind == "gpsd":
        coro, cfg = _run_gpsd(host, port), {"host": host, "port": int(port)}
    elif kind == "serial":
        coro, cfg = _run_serial(path, baud), {"path": path, "baud": int(baud)}
    elif kind == "sdr":
        coro, cfg = _run_sdr(device_args), {"device_args": device_args}
    else:
        raise ValueError(f"unknown GPS source kind {kind!r}")
    try:
        loop = asyncio.get_event_loop()
        _TASK = loop.create_task(coro)
    except RuntimeError:  # no running loop (called outside the server) — caller should retry in an async ctx
        raise RuntimeError("GPS poller must be started from within the running event loop")
    _STATE.update(kind=kind, running=True, started_ts=time.time(), config=cfg, last_error=None)
    log.info("GPS source started: %s %s", kind, cfg)
    return status()
