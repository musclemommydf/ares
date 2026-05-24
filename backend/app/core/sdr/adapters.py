# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
SDR adapters — each one polls / listens to a specific kind of DF radio and
emits :class:`LobEvent`s back to the manager.

Implemented:
  - **KrakenSdrAdapter** — polls the krakensdr_doa web app's `DOA_value` HTTP
    endpoint (CSV row per DOA estimate; the canonical KrakenSDR DF protocol).
  - **GenericJsonLinesAdapter** — line-delimited JSON over TCP. Any external
    DF pipeline (a Matchstiq X40 process driving the Epiq Sidekiq SDK, a
    GNU Radio flowgraph, a custom Python DF, …) can push bearings to Ares
    in seconds: `{"azimuth_deg":..., "rssi_dbm":..., "frequency_hz":..., …}\\n`.
  - **MatchstiqX40Adapter** — thin wrapper over the generic JSON-lines path,
    since the Matchstiq X40 itself has no built-in DF: a separate Epiq-side
    process is expected to compute bearings and stream them in.

Adapter contract:
  - `run()` is an async coroutine that *should not return* under normal
    operation; it loops with exponential backoff on errors.
  - Adapters call `self.emit(ev)` to deliver one LoB.
  - Adapters call `self.report(status, err="")` whenever connection state
    changes (`connecting` → `streaming` → `error`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from .manager import LobEvent, SDRDevice, sdr_manager

log = logging.getLogger(__name__)


class _Base:
    def __init__(self, dev: SDRDevice, on_lob: Callable[[LobEvent], Awaitable[None]]):
        self.dev = dev
        self._on_lob = on_lob

    async def emit(self, ev: LobEvent) -> None:
        await self._on_lob(ev)

    def report(self, status: str, err: str = "") -> None:
        sdr_manager.report_status(self.dev.id, status, err)


# ─────────────────────────────────────────────────────────────────────────────
# KrakenSDR — krakensdr_doa "DOA data out" CSV row
# Default endpoint: http://<host>:8080/DOA_value
# CSV (current upstream): epoch_ms, max_doa_deg, confidence_dB, RSSI_dBm,
#                         freq_hz, ant_arrangement, lat, lon, gps_heading,
#                         compass_heading, num_doa_samples, doa[0]..doa[N-1]
# Older builds drop the lat/lon/heading fields — the parser tolerates both.
# ─────────────────────────────────────────────────────────────────────────────
class KrakenSdrAdapter(_Base):
    POLL_INTERVAL_S = 0.5
    DEFAULT_PORT = 8080

    async def run(self) -> None:
        port = self.dev.port or self.DEFAULT_PORT
        url = f"http://{self.dev.host}:{port}/DOA_value"
        backoff = 1.0
        timeout = aiohttp.ClientTimeout(total=4, connect=2)
        last_emit_t = 0.0
        last_max_doa: Optional[float] = None
        log.info("KrakenSDR adapter %s polling %s", self.dev.id, url)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            while True:
                try:
                    async with sess.get(url) as r:
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        text = (await r.text()).strip()
                    if not text:
                        self.report("streaming")
                        backoff = 1.0
                        await asyncio.sleep(self.POLL_INTERVAL_S)
                        continue
                    parts = [p.strip() for p in text.split(",")]
                    ev = self._parse(parts)
                    if ev is not None:
                        # de-dup the same DOA value (Kraken serves a static cached row between updates)
                        if ev.azimuth_deg == last_max_doa and time.time() - last_emit_t < self.POLL_INTERVAL_S * 2:
                            pass
                        else:
                            await self.emit(ev)
                            last_emit_t = time.time()
                            last_max_doa = ev.azimuth_deg
                    self.report("streaming")
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.report("error", f"{type(e).__name__}: {e}")
                    await asyncio.sleep(min(30.0, backoff))
                    backoff = min(30.0, backoff * 2)
                await asyncio.sleep(self.POLL_INTERVAL_S)

    def _parse(self, parts: list[str]) -> Optional[LobEvent]:
        if len(parts) < 5:
            return None
        try:
            epoch_ms = float(parts[0])
            max_doa = float(parts[1]) % 360.0
            confidence_db = float(parts[2])
            rssi_dbm = float(parts[3])
            freq_hz = float(parts[4]) or self.dev.frequency_hz
        except ValueError:
            return None
        # newer rows carry GPS + heading at indices 6..9
        dev_lat, dev_lon, heading = self.dev.lat, self.dev.lon, 0.0
        if len(parts) >= 10:
            try:
                lat = float(parts[6]); lon = float(parts[7])
                if lat or lon:        # 0,0 ⇒ no GPS — fall back to configured pos
                    dev_lat, dev_lon = lat, lon
                heading = float(parts[8] or "0") or float(parts[9] or "0")
            except ValueError:
                pass
        # max_doa is *relative to the antenna array boresight*; add the platform
        # heading so the bearing we emit is true-north referenced.
        true_bearing = (max_doa + heading) % 360.0
        # map Kraken's "confidence in dB" to a 0..100% scale (10 dB ≈ 100%)
        conf_pct = max(0.0, min(100.0, confidence_db * 10.0))
        return LobEvent(
            device_id=self.dev.id, lat=dev_lat, lon=dev_lon, azimuth_deg=true_bearing,
            frequency_hz=freq_hz, rssi_dbm=rssi_dbm, confidence_pct=conf_pct,
            observer_height_m=self.dev.observer_height_m, environment=self.dev.environment,
            device_type="krakensdr",
            t=epoch_ms / 1000.0 if epoch_ms > 1e10 else time.time(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Generic JSON-lines TCP — newline-delimited JSON objects per LoB.
# Schema (all fields except azimuth_deg + frequency_hz are optional):
#   {"azimuth_deg": 117.4, "frequency_hz": 433.92e6, "rssi_dbm": -67.3,
#    "confidence_pct": 75, "lat": ..., "lon": ..., "t": <epoch_s>,
#    "target_device_id": "DMR-0xABCD"}
# Any field omitted uses the device's configured default. The device's `host`
# may be a full "tcp://hostname:port" URI (port also taken from dev.port if 0).
# ─────────────────────────────────────────────────────────────────────────────
class GenericJsonLinesAdapter(_Base):
    DEFAULT_PORT = 8400

    async def run(self) -> None:
        host, port = self._parse_endpoint()
        backoff = 1.0
        while True:
            self.report("connecting")
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5.0,
                )
            except (asyncio.TimeoutError, OSError) as e:
                self.report("error", f"connect failed: {e}")
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)
                continue
            self.report("streaming")
            backoff = 1.0
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("peer closed")
                    text = line.decode(errors="replace").strip()
                    if not text or text.startswith("#"):
                        continue
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    ev = self._build(obj)
                    if ev is not None:
                        await self.emit(ev)
            except asyncio.CancelledError:
                writer.close()
                try: await writer.wait_closed()
                except Exception: pass
                raise
            except Exception as e:
                self.report("error", f"{type(e).__name__}: {e}")
                writer.close()
                try: await writer.wait_closed()
                except Exception: pass
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)

    def _parse_endpoint(self) -> tuple[str, int]:
        h = self.dev.host or "127.0.0.1"
        if h.startswith("tcp://"):
            h = h[6:]
        if ":" in h:
            host, _, p = h.partition(":")
            return host, int(p)
        return h, self.dev.port or self.DEFAULT_PORT

    def _build(self, obj: dict) -> Optional[LobEvent]:
        freq = float(obj.get("frequency_hz") or self.dev.frequency_hz or 0)
        conf = float(obj.get("confidence_pct", 80.0))
        # If the message carries an antenna-array snapshot instead of a bearing,
        # run Ares's phase-interferometry DF here to derive the bearing + its σ.
        if "azimuth_deg" not in obj and ("array_phases_rad" in obj or "iq_real" in obj):
            try:
                from app.core.df.interferometry import geometry_from_spec, aoa_interferometry, aoa_from_snapshots
                spec = obj.get("array") or (self.dev.metadata or {}).get("array")
                if not spec or freq <= 0:
                    return None
                geom = geometry_from_spec(spec)
                heading = float(obj.get("heading_deg", (self.dev.metadata or {}).get("heading_deg", 0.0)))
                if "array_phases_rad" in obj:
                    res = aoa_interferometry(geom, freq, obj["array_phases_rad"],
                                             ref=int(obj.get("ref", 0)),
                                             sigma_phase_deg=float(obj.get("sigma_phase_deg", 8.0)),
                                             observer_heading_deg=heading)
                else:
                    import numpy as _np
                    X = _np.asarray(obj["iq_real"], dtype=float) + 1j * _np.asarray(obj["iq_imag"], dtype=float)
                    res = aoa_from_snapshots(geom, freq, X, method=str(obj.get("method", "music")),
                                             n_sources=int(obj.get("n_sources", 1)),
                                             fb_smoothing=bool(obj.get("fb_smoothing", False)),
                                             observer_heading_deg=heading)
                az = (res.az_true_deg if res.az_true_deg is not None else res.az_deg) % 360.0
                conf = max(5.0, min(99.0, 100.0 - res.sigma_az_deg * 3.0))
                obj = {**obj, "azimuth_deg": az, "device_type": obj.get("device_type", "interferometer")}
            except Exception:
                return None
        if "azimuth_deg" not in obj:
            return None
        try:
            az = float(obj["azimuth_deg"]) % 360.0
        except (TypeError, ValueError):
            return None
        if freq <= 0:
            return None
        return LobEvent(
            device_id=self.dev.id,
            lat=float(obj.get("lat", self.dev.lat)),
            lon=float(obj.get("lon", self.dev.lon)),
            azimuth_deg=az, frequency_hz=freq,
            rssi_dbm=float(obj.get("rssi_dbm", -80.0)),
            confidence_pct=conf,
            observer_height_m=float(obj.get("observer_height_m", self.dev.observer_height_m)),
            environment=str(obj.get("environment", self.dev.environment)),
            device_type=str(obj.get("device_type", "generic")),
            target_device_id=str(obj.get("target_device_id", "")),
            estimated_distance_m=float(obj.get("estimated_distance_m", 0.0)),
            t=float(obj.get("t", time.time())),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Epiq Matchstiq X40 — the radio itself is a wideband SDR with no built-in DF;
# the operational pattern is "an Epiq-side DF process (Sidekiq SDK / GNU Radio /
# proprietary) computes bearings and streams them to Ares". So this adapter is
# the generic JSON-lines path with a stable device-type tag, plus header
# metadata so the device-status UI can show it's an X40 (not "generic").
# ─────────────────────────────────────────────────────────────────────────────
class MatchstiqX40Adapter(GenericJsonLinesAdapter):
    DEFAULT_PORT = 8401

    def _build(self, obj: dict) -> Optional[LobEvent]:
        ev = super()._build(obj)
        if ev is not None and not obj.get("device_type"):
            ev.device_type = "matchstiq_x40"
        return ev
