"""
SDR / DF device manager (Workstream D).

A *device* is a registered RF source that produces lines-of-bearing — a
KrakenSDR running krakensdr_doa, an external DF process feeding an Epiq
Matchstiq X40, a generic JSON-lines TCP stream, etc. Each device owns one
:class:`SDRAdapter` task. Whenever an adapter emits a :class:`LobEvent`, the
manager:

  1. Stores it in a rolling per-frequency ring buffer.
  2. Re-runs the existing :func:`app.core.geolocation.solve_fix` solver across
     recent same-frequency LoBs (and other devices' LoBs at the same frequency)
     to (re-)compute Cuts / Fixes / CAP-CEP ellipses.
  3. Broadcasts every LoB / fix / device-status change as a JSON event to all
     WebSocket subscribers (the web globe + the ATAK plugin's `/ws/sdr` client).
  4. Pushes a CoT event to any configured TAK targets (UDP multicast,
     TCP unicast, …) so the LoB and fix show up in ATAK natively.
  5. (Optionally, on a new fix) kicks off a coverage simulation centred on the
     fix and broadcasts the resulting GeoJSON — the operator gets a propagation
     prediction that *updates live as the emitter is located*.

Devices and policy persist to ``data/sdr_devices.json``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.config import DATA_DIR
from app.core import geolocation
from app.core import cot

log = logging.getLogger(__name__)

DEVICES_FILE = DATA_DIR / "sdr_devices.json"
_LOB_BUFFER_MAX = 256          # ring per frequency bucket
_FIX_HISTORY_MAX = 64          # ring of recent fix-summary events
_FREQ_BUCKET_HZ = 5_000.0      # group LoBs into 5 kHz bins for matching
_DEFAULT_LOB_TTL_S = 90.0      # an LoB stops contributing to fixes after this
_AUTO_COVERAGE_COOLDOWN_S = 8.0


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SDRDevice:
    id: str
    name: str
    type: str                       # "krakensdr" | "matchstiq_x40" | "generic"
    host: str                       # IP / hostname (or "tcp://host:port" for generic)
    port: int = 0                   # adapter-specific (8081 default for kraken)
    # Source class: a single-channel SDR can monitor a spectrum / decode audio but
    # cannot produce a line of bearing (DF needs ≥2 coherent channels). A
    # multi-channel source declares its `channels` count — more channels ⇒ tighter LoBs.
    source_class: str = "multi_channel"   # "single_channel" | "multi_channel"
    channels: int = 5
    array_type: str = "uca"               # "ula" | "uca" | "custom" — for the DF-accuracy estimate & geometry
    array_spacing_wavelengths: float = 0.4
    # DF azimuth reference: "true" (degrees from true north) or "relative" (clock
    # position off the antenna front — `antenna_heading_deg` then maps it to true).
    azimuth_reference: str = "absolute"   # "absolute" (true north) | "relative" (deg off the antenna front) | "clock"
    antenna_heading_deg: float = 0.0
    lat: float = 0.0                # observer / antenna position (overridden by a live GPS fix if one is set)
    lon: float = 0.0
    altitude_m: float = 0.0
    observer_height_m: float = 1.5
    frequency_hz: float = 0.0       # operator-set centre / DF tune frequency; adapters may override per-LoB
    df_threshold_dbm: float = -90.0 # min power for a bin to count as "active" (shoot a LoB)
    antenna_hpbw_deg: Optional[float] = None
    environment: str = "suburban"
    enabled: bool = True
    use_gps: bool = True            # if a live GPS fix is set, use it as this device's position
    auto_coverage: bool = False     # rerun /simulate/coverage on every new fix from this device's group
    metadata: dict[str, Any] = field(default_factory=dict)
    # mutable runtime — not persisted directly via to_persist()
    status: str = "stopped"         # stopped | connecting | streaming | error
    last_error: str = ""
    last_event_ts: float = 0.0
    lob_count: int = 0

    @property
    def can_df(self) -> bool:
        return self.source_class != "single_channel" and self.channels >= 2

    def to_persist(self) -> dict:
        d = asdict(self)
        for k in ("status", "last_error", "last_event_ts", "lob_count"):
            d.pop(k, None)
        return d

    def public(self) -> dict:
        d = asdict(self)
        d["can_df"] = self.can_df
        return d


@dataclass
class LobEvent:
    device_id: str
    lat: float                      # device position when this LoB was taken
    lon: float
    azimuth_deg: float              # the *Absolute* LOB (deg from true north) after any relative→absolute conversion
    frequency_hz: float
    raw_azimuth_deg: Optional[float] = None   # the as-reported azimuth (a Relative LOB if the device is in relative/clock mode); used for compass calibration
    rssi_dbm: float = -80.0
    confidence_pct: float = 80.0
    observer_height_m: float = 1.5
    environment: str = "suburban"
    device_type: str = ""           # propagated to the solver (groups by device_type+device_id)
    target_device_id: str = ""      # the *emitter* identifier when known (DMR/IMSI/MAC/callsign)
    estimated_distance_m: float = 0.0
    origin_node: Optional[str] = None   # the Ares node this LoB originated on (mesh distributed sensing); None ⇒ this node
    origin_device: Optional[str] = None # the originating device id on `origin_node`
    hops: int = 0                       # mesh hop count (TTL-bounded forwarding)
    sig: Optional[str] = None           # HMAC-SHA256 over the LoB content, keyed by ARES_MESH_SECRET (mesh integrity)
    t: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


# ─────────────────────────────────────────────────────────────────────────────
# Manager
# ─────────────────────────────────────────────────────────────────────────────
class SDRManager:
    def __init__(self) -> None:
        self._devices: dict[str, SDRDevice] = {}
        self._adapters: dict[str, asyncio.Task] = {}
        # LoBs indexed by frequency bucket; each bucket is a deque of LobEvent
        self._lobs_by_freq: dict[int, deque[LobEvent]] = {}
        self._tracks: dict[int, "geolocation.EmitterTrack"] = {}   # CV-EKF track per frequency bucket
        self._fixes: deque[dict] = deque(maxlen=_FIX_HISTORY_MAX)
        self._subscribers: set[asyncio.Queue] = set()
        self._auto_cov_runner: Optional[Callable[[dict], Awaitable[None]]] = None
        self._last_auto_cov: dict[int, float] = {}
        self._gps: Optional[dict] = None        # last live GPS fix (operator position)
        self._started = False
        self._lock = asyncio.Lock()
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not DEVICES_FILE.exists():
            return
        try:
            raw = json.loads(DEVICES_FILE.read_text())
            for d in raw.get("devices", []):
                try:
                    self._devices[d["id"]] = SDRDevice(**{
                        k: v for k, v in d.items() if k in SDRDevice.__dataclass_fields__
                    })
                except Exception:
                    log.exception("bad SDR device record: %s", d)
        except Exception:
            log.exception("failed to read %s — starting empty", DEVICES_FILE)

    def _save(self) -> None:
        DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEVICES_FILE.write_text(json.dumps(
            {"devices": [d.to_persist() for d in self._devices.values()]}, indent=2,
        ))

    # ── adapter wiring (lazy import to avoid circulars) ──────────────────────
    def _make_adapter(self, dev: SDRDevice):
        from .adapters import KrakenSdrAdapter, GenericJsonLinesAdapter, MatchstiqX40Adapter
        if dev.type == "krakensdr":
            return KrakenSdrAdapter(dev, self._on_lob)
        if dev.type == "matchstiq_x40":
            return MatchstiqX40Adapter(dev, self._on_lob)
        return GenericJsonLinesAdapter(dev, self._on_lob)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for dev in list(self._devices.values()):
            if dev.enabled:
                self._spawn(dev)
        log.info("SDR manager started (%d device(s))", len(self._devices))

    async def stop(self) -> None:
        self._started = False
        tasks = list(self._adapters.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._adapters.clear()
        log.info("SDR manager stopped")

    def _spawn(self, dev: SDRDevice) -> None:
        if dev.id in self._adapters:
            return
        adapter = self._make_adapter(dev)
        task = asyncio.create_task(adapter.run(), name=f"sdr:{dev.id}")
        self._adapters[dev.id] = task
        dev.status = "connecting"

    def _kill(self, device_id: str) -> None:
        t = self._adapters.pop(device_id, None)
        if t:
            t.cancel()
        if device_id in self._devices:
            self._devices[device_id].status = "stopped"

    def set_auto_coverage_runner(self, runner: Callable[[dict], Awaitable[None]]) -> None:
        """Inject a coroutine the manager calls with each new fix when the group's
        device has ``auto_coverage`` on. Wired by the API layer (it needs the WS
        broadcaster + the existing coverage routine)."""
        self._auto_cov_runner = runner

    # ── device CRUD (sync; the adapters re-read state on each iteration) ─────
    def list(self) -> list[dict]:
        return [d.public() for d in self._devices.values()]

    def get(self, device_id: str) -> Optional[SDRDevice]:
        return self._devices.get(device_id)

    def add(self, payload: dict) -> SDRDevice:
        dev_id = payload.get("id") or uuid.uuid4().hex[:10]
        if dev_id in self._devices:
            raise ValueError(f"device id {dev_id!r} already exists")
        dev = SDRDevice(**{**payload, "id": dev_id})
        self._devices[dev_id] = dev
        self._save()
        if self._started and dev.enabled:
            self._spawn(dev)
        return dev

    def update(self, device_id: str, patch: dict) -> SDRDevice:
        dev = self._devices.get(device_id)
        if dev is None:
            raise KeyError(device_id)
        was_enabled = dev.enabled
        for k, v in patch.items():
            if k in SDRDevice.__dataclass_fields__ and k != "id":
                setattr(dev, k, v)
        self._save()
        # re-spawn on a state change that an adapter wouldn't pick up on its own
        if self._started:
            if dev.enabled != was_enabled or "host" in patch or "port" in patch or "type" in patch:
                self._kill(device_id)
                if dev.enabled:
                    self._spawn(dev)
        return dev

    def remove(self, device_id: str) -> bool:
        if device_id not in self._devices:
            return False
        self._kill(device_id)
        del self._devices[device_id]
        self._save()
        return True

    def last_relative_lob(self, device_id: str) -> Optional[float]:
        """The most recent LoB from this device expressed as a Relative LOB (deg off
        the antenna front) — handy as the 'measured' value for compass calibration."""
        latest = None
        for dq in self._lobs_by_freq.values():
            for ev in dq:
                if ev.device_id == device_id and (latest is None or ev.t > latest.t):
                    latest = ev
        if latest is None:
            return None
        dev = self._devices.get(device_id)
        if latest.raw_azimuth_deg is not None and dev and (dev.azimuth_reference or "absolute").lower() not in ("absolute", "true"):
            return float(latest.raw_azimuth_deg) % 360.0
        # otherwise back out the relative from the absolute using the current heading
        h = (dev.antenna_heading_deg if dev else 0.0) or 0.0
        return (float(latest.azimuth_deg) - h) % 360.0

    def calibrate_device(self, device_id: str, known_true_bearing_deg: float,
                         measured_relative_lob_deg: Optional[float] = None) -> dict:
        """Compass calibration: aim the DF antenna at a target whose *true* bearing is
        known, read the Relative LOB the DF reports, and solve heading = (true − relative).
        Sets ``antenna_heading_deg``; switches the device into "absolute" output so the
        plotted LOBs are now map-correct. If ``measured_relative_lob_deg`` is omitted,
        the most recent LoB from this device is used."""
        dev = self._devices.get(device_id)
        if dev is None:
            raise KeyError(device_id)
        rel = measured_relative_lob_deg
        used_last = False
        if rel is None:
            rel = self.last_relative_lob(device_id)
            used_last = rel is not None
            if rel is None:
                raise ValueError("no recent LoB to calibrate from — aim the antenna at the known target, shoot a LoB, then calibrate (or pass measured_relative_lob_deg)")
        heading = geolocation.calibrate_heading(known_true_bearing_deg, rel)
        dev.antenna_heading_deg = heading
        self._save()
        self._broadcast({"type": "device_status", "device": dev.public()})
        return {"device_id": device_id, "antenna_heading_deg": round(heading, 1),
                "known_true_bearing_deg": round(float(known_true_bearing_deg) % 360.0, 1),
                "measured_relative_lob_deg": round(float(rel) % 360.0, 1),
                "used_last_lob": used_last,
                "formula": "heading = (known_true_bearing − measured_relative_LOB) mod 360 ;  then Absolute LOB = (0 + heading) + Relative LOB"}

    # ── pub/sub (WebSocket fan-out) ──────────────────────────────────────────
    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.add(q)
        # send a synthetic "snapshot" so a fresh client sees current state
        try:
            from app.core.sdr.mesh import NODE_ID, NODE_LABEL
            node_id, node_label = NODE_ID, NODE_LABEL
        except Exception:
            node_id, node_label = "local", "ares"
        try:
            from app.core.chat import chat_hub
            chat_snap = chat_hub.snapshot()
        except Exception:
            chat_snap = {"rooms": ["All"], "messages": []}
        await q.put({"type": "snapshot", "node_id": node_id, "node_label": node_label,
                     "devices": self.list(),
                     "lobs": [asdict(l) for l in self._recent_lobs()],
                     "fixes": list(self._fixes), "gps": self.gps_fix(), "chat": chat_snap})
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, event: dict) -> None:
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    # ── live GPS fix (POST /api/v1/gps) — used as the observer position ──────
    def set_gps_fix(self, lat: float, lon: float, alt_m: float = 0.0, heading_deg: Optional[float] = None,
                    speed_mps: Optional[float] = None, source: str = "manual") -> dict:
        self._gps = {"lat": float(lat), "lon": float(lon), "alt_m": float(alt_m),
                     "heading_deg": (None if heading_deg is None else float(heading_deg)),
                     "speed_mps": (None if speed_mps is None else float(speed_mps)),
                     "source": source, "t": time.time()}
        self._broadcast({"type": "gps", "fix": self._gps})
        return self._gps

    def gps_fix(self) -> Optional[dict]:
        f = getattr(self, "_gps", None)
        return f if (f and time.time() - f["t"] < 30.0) else f   # keep last; UI can show staleness

    # ── ingest from adapters (and, via the mesh, from peer Ares nodes) ──────
    async def _on_lob(self, ev: LobEvent) -> None:
        # stamp the originating node for the mesh (local LoBs ⇒ this node) + sign it
        if ev.origin_node is None:
            try:
                from app.core.sdr.mesh import NODE_ID
                ev.origin_node = NODE_ID
            except Exception:
                ev.origin_node = "local"
            ev.origin_device = ev.device_id
        if ev.sig is None:                     # fresh local LoB → sign (peer LoBs keep the originator's sig)
            try:
                from app.core import meshsec
                ev.sig = meshsec.sign_lob(asdict(ev))
            except Exception:
                ev.sig = None
        dev = self._devices.get(ev.device_id)
        if dev:
            dev.status = "streaming"
            dev.last_error = ""
            dev.last_event_ts = ev.t
            dev.lob_count += 1
            if not ev.device_type:
                ev.device_type = dev.type
            # single-channel sources can monitor a spectrum but never produce a LoB
            if not dev.can_df:
                self._broadcast({"type": "lob_rejected", "device_id": dev.id,
                                 "reason": "single-channel SDR — DF needs ≥2 coherent channels", "device": dev.public()})
                return
            # compass mode: if the device reports a *Relative* LOB (relative / clock
            # mode), convert it to an *Absolute* LOB — Absolute = (0 + heading) + Relative.
            if ev.raw_azimuth_deg is None:
                ev.raw_azimuth_deg = ev.azimuth_deg
            if (dev.azimuth_reference or "absolute").lower() not in ("absolute", "true"):
                ev.azimuth_deg = (0.0 + (dev.antenna_heading_deg or 0.0) + ev.raw_azimuth_deg) % 360.0
            # use a live GPS fix as the observer position when the LoB arrived without one
            if dev.use_gps and not ev.lat and not ev.lon:
                g = self.gps_fix()
                if g is not None:
                    ev.lat, ev.lon = g["lat"], g["lon"]
        # Ring-buffer the LoB
        bucket = int(round(ev.frequency_hz / _FREQ_BUCKET_HZ))
        dq = self._lobs_by_freq.setdefault(bucket, deque(maxlen=_LOB_BUFFER_MAX))
        dq.append(ev)
        # If the LoB carries an emitter identifier, also push it into the
        # per-target tracker so the Targets tab gets a running peak-RSSI +
        # range estimate. Identifier kind: the LoB's free-text `device_type`
        # field is used (DMR/IMSI/MAC/callsign/icao/...); falls back to
        # "other" when the operator didn't tag it.
        if ev.target_device_id:
            try:
                from app.core import targets as _targets
                _targets.record(
                    kind=(ev.device_type or "other").lower(),
                    value=str(ev.target_device_id),
                    observer_lat=float(ev.lat or 0.0),
                    observer_lon=float(ev.lon or 0.0),
                    rssi_dbm=(float(ev.rssi_dbm) if ev.rssi_dbm is not None else None),
                    bearing_deg=float(ev.azimuth_deg),
                    sigma_deg=(float(ev.azimuth_sigma_deg) if getattr(ev, "azimuth_sigma_deg", None) else None),
                    frequency_hz=float(ev.frequency_hz),
                    t=float(ev.t),
                    metadata={"origin_node": ev.origin_node, "source_device_id": ev.device_id},
                )
            except Exception:
                log.debug("targets.record failed for LoB %s", ev.id, exc_info=True)
        self._broadcast({"type": "lob", "lob": asdict(ev), "device": dev.public() if dev else None})
        # CoT push (best-effort)
        try:
            await cot.publish_lob(ev)
        except Exception:
            log.debug("CoT push failed for LoB %s", ev.id, exc_info=True)
        # Recompute fix(es) for this frequency bucket
        await self._solve_and_publish(bucket)

    def _recent_lobs(self) -> list[LobEvent]:
        cutoff = time.time() - _DEFAULT_LOB_TTL_S
        out: list[LobEvent] = []
        for dq in self._lobs_by_freq.values():
            out.extend(l for l in dq if l.t >= cutoff)
        out.sort(key=lambda l: l.t)
        return out[-128:]

    async def _solve_and_publish(self, bucket: int) -> None:
        cutoff = time.time() - _DEFAULT_LOB_TTL_S
        dq = self._lobs_by_freq.get(bucket) or ()
        members = [l for l in dq if l.t >= cutoff]
        if len(members) < 2:
            return
        # `device_id` here is the *emitter*'s identity (DMR/IMSI/MAC/callsign) — left
        # empty when unknown so the solver groups bearings from *every* sensor
        # (local + mesh peers) to the same unidentified emitter at this frequency. The
        # sensor's own id is irrelevant for grouping (it's in `device_type` for display).
        observations = [{
            "lat": l.lat, "lon": l.lon, "azimuth_deg": l.azimuth_deg,
            "frequency_hz": l.frequency_hz, "rssi_dbm": l.rssi_dbm,
            "confidence_pct": l.confidence_pct, "observer_height_m": l.observer_height_m,
            "environment": l.environment,
            "device_type": (l.device_type or "") + (f"@{l.origin_node}" if l.origin_node else ""),
            "device_id": l.target_device_id or "", "id": l.id,
            "estimated_distance_m": l.estimated_distance_m,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(l.t)),
        } for l in members]
        try:
            result = geolocation.solve_fix(observations)
        except Exception:
            log.exception("solve_fix failed")
            return
        # Pull out the latest fix/cut for this frequency
        groups = result.get("groups", [])
        if not groups:
            return
        top = max(groups, key=lambda g: g.get("n_lobs", 0))
        # EKF track — smooth the stream of independent ML fixes (one CV track per
        # frequency bucket); broadcast the smoothed position/velocity alongside.
        track_state = None
        c = top.get("centroid")
        if c and top.get("kind") in ("fix", "cut") and top.get("covariance_enu"):
            trk = self._tracks.get(bucket)
            if trk is None:
                trk = geolocation.EmitterTrack(c["lat"], c["lon"])
                self._tracks[bucket] = trk
            now = time.time()
            trk.predict(now)
            trk.update({"lat": c["lat"], "lon": c["lon"], "covariance_enu": top["covariance_enu"]}, t=now)
            track_state = trk.state()
        fix_event = {
            "type": "fix", "frequency_hz": top.get("frequency_hz"),
            "kind": top.get("kind"), "n_lobs": top.get("n_lobs"),
            "method": top.get("method"), "gdop": top.get("gdop"),
            "position_sigma_m": top.get("position_sigma_m"), "residual_rms_deg": top.get("residual_rms_deg"),
            "centroid": top.get("centroid"), "cep": top.get("cep"), "covariance_enu": top.get("covariance_enu"),
            "track": track_state,
            "lob_ids": top.get("lob_ids"), "groups": groups,
            "geojson": result.get("geojson"),
            "t": time.time(),
        }
        self._fixes.append(fix_event)
        self._broadcast(fix_event)
        try:
            await cot.publish_fix(top)
        except Exception:
            log.debug("CoT push failed for fix", exc_info=True)
        # auto-coverage from the fix (cooldown'd)
        if self._auto_cov_runner is not None and top.get("kind") in ("fix", "cut") and top.get("centroid"):
            contributing_devs = {l.device_id for l in members if l.id in (top.get("lob_ids") or [])}
            if any(self._devices.get(d) and self._devices[d].auto_coverage for d in contributing_devs):
                last = self._last_auto_cov.get(bucket, 0)
                if time.time() - last >= _AUTO_COVERAGE_COOLDOWN_S:
                    self._last_auto_cov[bucket] = time.time()
                    asyncio.create_task(self._auto_cov_runner(top))

    # ── device-status reporting from adapters ────────────────────────────────
    def report_status(self, device_id: str, status: str, error: str = "") -> None:
        dev = self._devices.get(device_id)
        if not dev:
            return
        dev.status = status
        dev.last_error = error
        self._broadcast({"type": "device_status", "device": dev.public()})


sdr_manager = SDRManager()
