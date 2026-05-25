# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
live_df.py — the in-process, IQ-to-bearing DF pipeline.

This is the "everything bundled in Ares" path the ``drivers/`` registry was built
for: instead of consuming pre-computed bearings from an external daemon (the
:mod:`adapters` job — krakensdr_doa over HTTP, an Epiq-side process over TCP), a
:class:`LiveDfAdapter` *instantiates a registry driver* (``drivers.create(...)``),
pulls coherent multi-channel IQ off it, and runs Ares's own array DF solver
(:func:`app.core.df.interferometry.aoa_from_snapshots` — MUSIC / Capon /
Bartlett) to produce the line of bearing — then hands it to the same
:meth:`SDRManager._on_lob` ingest every other adapter uses (ring-buffer →
``solve_fix`` → WebSocket fan-out → CoT push → auto-coverage).

A live-DF device is an :class:`SDRDevice` with ``type="live_df"`` whose
``metadata`` carries the driver wiring::

    metadata = {
      "driver_id":      "plutosdr",          # any id from /df/drivers
      "driver_args":    {"uri": "ip:192.168.2.1"},   # kwargs for drivers.create
      "sample_rate_hz": 4.0e6,
      "gain_db":        50.0,                 # null ⇒ the driver's AGC
      "method":         "music",             # music | capon | bartlett
      "n_snapshots":    4096,
      "dwell_s":        1.0,                  # seconds between bearings
      "n_sources":      1,
      "fb_smoothing":   false,                # forward-backward (ULA multipath)
      "az_step_deg":    1.0,
      "min_snr_db":     3.0,                  # gate: don't shoot a LoB at noise
      "min_quality":    0.10
    }

The array geometry (element positions in metres) is fixed once at start from the
device's ``array_type`` / ``channels`` / ``array_spacing_wavelengths`` at the
configured ``frequency_hz`` — the physical array doesn't resize when you retune,
so the steering vector (not the geometry) carries the operating frequency. The
same geometry is handed to the driver via its ``array=`` kwarg, so the synthetic
driver's phantom emitters are generated and solved against one consistent array
and the offline demo produces a correct, stable bearing.

All blocking driver I/O (``open`` / ``read_iq`` / tuning) and the CPU-bound grid
search run in a thread executor so one slow radio never stalls the event loop or
the other adapters.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
from typing import Awaitable, Callable, Optional

import numpy as np

from .adapters import _Base
from .manager import LobEvent, SDRDevice

log = logging.getLogger(__name__)

_C = 299_792_458.0
_IDLE_DWELL = 2.0    # seconds between heartbeats while idling (no subscribers)
_HW_REPROBE_S = 20.0  # while stuck on the synthetic fallback, retry the real radio this often


class LiveDfAdapter(_Base):
    """Drive a registry SDR driver → in-process DF → LoB stream."""

    def __init__(self, dev: SDRDevice, on_lob: Callable[[LobEvent], Awaitable[None]], has_viewers=None):
        super().__init__(dev, on_lob)
        self._has_viewers = has_viewers or (lambda: True)   # gate the capture/DSP loop on live subscribers
        self._idle = False
        md = dev.metadata or {}
        self.driver_id: str = str(md.get("driver_id") or "synthetic")
        self.driver_args: dict = dict(md.get("driver_args") or {})
        self.sample_rate_hz: float = float(md.get("sample_rate_hz") or 2.4e6)
        self.gain_db = md.get("gain_db", None)                     # None ⇒ AGC
        self.method: str = str(md.get("method") or "music").lower()
        self.n_snapshots: int = int(md.get("n_snapshots") or 4096)
        self.dwell_s: float = max(0.05, float(md.get("dwell_s") or 1.0))
        self.n_sources: int = int(md.get("n_sources") or 1)
        self.fb_smoothing: bool = bool(md.get("fb_smoothing", False))
        self.az_step_deg: float = float(md.get("az_step_deg") or 1.0)
        self.min_snr_db: float = float(md.get("min_snr_db", 3.0))
        self.min_quality: float = float(md.get("min_quality", 0.10))

        # ── multi-VFO: each is a narrowband channel (offset + bw + squelch) carved
        #    out of one wideband capture. Absent ⇒ one implicit full-band VFO at the
        #    device centre (legacy single-emitter behaviour).
        self.vfos: list[dict] = self._parse_vfos(md.get("vfos"))
        self.auto_squelch: bool = bool(md.get("auto_squelch", len(self.vfos) > 1))
        self.squelch_margin_db: float = float(md.get("squelch_margin_db", 8.0))
        # ── auto-calibration (needs a driver with a switchable coherence source)
        self.auto_calibrate: bool = bool(md.get("auto_calibrate", False))
        self.cal_interval_s: float = float(md.get("cal_interval_s", 300.0))
        self.cal_snapshots: int = int(md.get("cal_snapshots", 8192))

        self._driver = None
        self._arrays_geom = None        # df.arrays.ArrayGeometry (handed to the driver)
        self._interf_geom = None        # df.interferometry.ArrayGeometry (the solver)
        self._applied_freq: Optional[float] = None
        self._applied_rate: Optional[float] = None
        self._applied_gain: object = "<unset>"
        self._cal = None                # per-channel complex correction (or None)
        self._cal_time: float = 0.0
        self._cal_capable = False
        self._force_cal_seen = 0.0      # /df/live/{id}/calibrate sets metadata['force_cal']
        self._squelch = {}              # vfo name → vfo.SquelchTracker
        self._floor_hist = None         # df.vfo.SquelchTracker shared noise-floor estimator
        self._backend_label = ""        # driver backend ("pyadi"/"soapy"/"synthetic") for spectrum source
        self._last_hw_probe = 0.0       # last time we retried the real radio while on the synthetic fallback
        self._spectrum = None           # cached wideband PSD from the live capture (feeds the DF panel)
        self._audio = None              # active "Listen" session (carves an audio channel from the capture)

    # ── VFO config ──────────────────────────────────────────────────────────────
    def _parse_vfos(self, raw) -> list[dict]:
        """Normalise the VFO list to absolute RF freq + bandwidth + squelch.
        ``offset_hz`` is relative to the device centre; ``freq_hz`` is absolute."""
        center = float(self.dev.frequency_hz or 100e6)
        out: list[dict] = []
        for i, v in enumerate(raw or []):
            try:
                if v.get("freq_hz") is not None:
                    freq = float(v["freq_hz"]); offset = freq - center
                else:
                    offset = float(v.get("offset_hz") or 0.0); freq = center + offset
                sq = v.get("squelch_db", None)
                out.append({
                    "name": str(v.get("name") or f"vfo{i}"),
                    "freq_hz": freq, "offset_hz": offset,
                    "bandwidth_hz": float(v.get("bandwidth_hz") or 0.0),   # 0 ⇒ full capture band
                    "squelch_db": (None if sq in (None, "", "auto") else float(sq)),
                })
            except Exception:
                log.warning("live-DF %s: bad VFO spec %r — skipped", self.dev.id, v)
        if not out:                                     # implicit full-band VFO (legacy)
            out = [{"name": "vfo0", "freq_hz": center, "offset_hz": 0.0,
                    "bandwidth_hz": 0.0, "squelch_db": None}]
        return out

    # ── geometry (fixed at start; metres, frequency-independent) ────────────────
    def _build_geometry(self, freq_hz: float):
        from app.core.df.arrays import ArrayGeometry as ArraysGeom
        from app.core.df.interferometry import ArrayGeometry as InterfGeom, geometry_from_spec

        md = self.dev.metadata or {}
        spec = md.get("array")
        if isinstance(spec, dict) and spec.get("type"):
            interf = geometry_from_spec(spec)
        else:
            n = max(2, int(self.dev.channels))
            lam = _C / max(1.0, float(freq_hz))
            spacing_wl = float(self.dev.array_spacing_wavelengths or 0.4)
            if (self.dev.array_type or "uca").lower() == "ula":
                interf = InterfGeom.ula(n, spacing_wl * lam)
            else:                                   # uca (default); custom w/o positions falls back here
                r = (spacing_wl * lam / (2.0 * math.sin(math.pi / n))) if n >= 3 else (spacing_wl * lam)
                interf = InterfGeom.uca(n, max(0.01, r))
        # the driver wants a df.arrays geometry (x=east, y=north) — same positions,
        # so the synthetic driver generates against exactly what the solver assumes.
        arrays = ArraysGeom.custom(np.asarray(interf.positions_m)[:, :2], label=interf.name)
        return arrays, interf

    # ── driver lifecycle (blocking → executor) ─────────────────────────────────
    def _open_driver(self) -> str:
        from app.core.sdr import drivers
        kwargs = dict(self.driver_args)
        kwargs.setdefault("channels", int(self.dev.channels))
        if self._arrays_geom is not None:
            kwargs.setdefault("array", self._arrays_geom)
        drv = drivers.create(self.driver_id, **kwargs)
        drv.open()
        self._driver = drv
        self._cal_capable = bool(getattr(getattr(drv, "capabilities", None), "cal_source", False))
        backend = getattr(drv, "_backend", None) or getattr(drv, "device_id", None) or self.driver_id
        self._backend_label = str(backend)
        return str(backend)

    def _close_driver(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
        self._driver = None
        self._applied_freq = self._applied_rate = None
        self._applied_gain = "<unset>"

    def _retune(self) -> None:
        """Apply any frequency / sample-rate / gain change the operator made (cheap; only on change)."""
        freq = float(self.dev.frequency_hz or 100e6)
        if freq != self._applied_freq:
            self._driver.set_frequency(freq); self._applied_freq = freq
        if self.sample_rate_hz != self._applied_rate:
            self._driver.set_sample_rate(self.sample_rate_hz); self._applied_rate = self.sample_rate_hz
        if self.gain_db is not None and self.gain_db != self._applied_gain:
            self._driver.set_gain(float(self.gain_db)); self._applied_gain = self.gain_db

    # ── auto-calibration (blocking → executor) ──────────────────────────────────
    def _calibrate(self) -> None:
        """Switch the driver's coherence source on, capture a reference block,
        estimate the per-channel correction, switch it off, store + apply it."""
        from app.core.df.calibration import calibrate_from_noise_coupling, coherence_metrics
        self._retune()
        try:
            self._driver.set_calibration_source(True)
        except Exception as e:
            log.info("live-DF %s: driver has no calibration source (%s)", self.dev.id, e)
            self._cal_capable = False
            return
        try:
            frame = self._driver.read_iq(int(self.cal_snapshots))
            X = np.asarray(frame.samples)
            nch = self._interf_geom.n
            if X.shape[0] > nch:
                X = X[:nch]
            d = calibrate_from_noise_coupling(X, ref_channel=0)
        finally:
            try:
                self._driver.set_calibration_source(False)
            except Exception:
                pass
        self._cal = d
        self._cal_time = time.time()
        self._publish_cal_status(coherence_metrics(d))
        log.info("live-DF %s: calibrated (%s)", self.dev.id, coherence_metrics(d))

    def _publish_cal_status(self, metrics: dict, state: str = "calibrated") -> None:
        md = self.dev.metadata or {}
        md["cal"] = {"state": state, "t": self._cal_time,
                     "age_s": round(time.time() - self._cal_time, 1) if self._cal_time else None,
                     "auto": self.auto_calibrate, "interval_s": self.cal_interval_s,
                     "capable": self._cal_capable, **metrics}
        self.dev.metadata = md
        self.report("streaming")

    # ── capture (blocking → executor) ───────────────────────────────────────────
    def _capture_raw(self) -> np.ndarray:
        """Grab one wideband coherent block at the device centre + apply calibration.
        When a "Listen" session is active, grab a longer block (~100 ms) so the audio
        demod has a continuous stream, demodulate the audio channel from it, and DF on
        a bounded slice."""
        from app.core.df.calibration import apply_gain
        self._retune()
        n = int(self.n_snapshots)
        if self._audio is not None:
            n = max(n, int(self.sample_rate_hz * 0.10))     # ~100 ms for continuous audio
        frame = self._driver.read_iq(n)
        X = np.asarray(frame.samples)
        if X.ndim == 1:
            X = X[np.newaxis, :]
        # Cache the spectrum from *whatever* channels we captured, before the DF
        # channel-count gate — so a stock single-RX Pluto (which can't DF without the
        # 2R2T MIMO mod) still shows its real RF in the spectrum panel instead of
        # falling through to the synthetic placeholder.
        self._cache_spectrum(X)
        if self._audio is not None:
            try:
                self._feed_audio(X[0])
            except Exception:
                log.warning("live-DF %s: audio feed failed", self.dev.id, exc_info=True)
        if X.shape[0] < 2:
            raise RuntimeError(f"need ≥2 coherent channels for DF, got shape {X.shape} "
                               f"(driver backend {self._backend_label or '?'}); spectrum still works")
        nch = self._interf_geom.n
        if X.shape[0] > nch:
            X = X[:nch]
        if X.shape[1] > self.n_snapshots:                   # bound DF cost when listening grabbed a long block
            X = X[:, :self.n_snapshots]
        if self._cal is not None:
            X = apply_gain(X, self._cal)
        return np.ascontiguousarray(X)

    # ── audio "Listen": carve a channel from the wideband capture + demod ────────
    def start_audio(self, mode: str, tune_hz: float, bw_hz: Optional[float] = None) -> dict:
        """Begin decoding the channel at ``tune_hz`` (must be inside the captured band:
        device centre ± sample_rate/2). Analog modes (nfm/wfm/am/ssb) are demodulated
        in-process to voice PCM; digital modes (DMR/P25/NXDN/D-STAR/YSF/M17/POCSAG/…)
        FM-discriminate the channel and pipe it into an installed external decoder
        (dsd-fme / m17-demod / multimon-ng). Returns the stream format."""
        from .demod import AudioDemod
        c = float(self._applied_freq or self.dev.frequency_hz or 0.0)
        off = float(tune_hz) - c
        if abs(off) > self.sample_rate_hz / 2.0:
            lo, hi = (c - self.sample_rate_hz / 2.0) / 1e6, (c + self.sample_rate_hz / 2.0) / 1e6
            raise ValueError(f"{tune_hz/1e6:.4f} MHz is outside the captured band ({lo:.3f}–{hi:.3f} MHz) — "
                             f"retune the device or widen BW")
        analog = mode.lower() in ("nfm", "fm", "wfm", "am", "usb", "lsb", "ssb", "cw")
        if analog:
            d = AudioDemod(mode, self.sample_rate_hz, off, channel_bw_hz=bw_hz)
            self._audio = {"kind": "analog", "demod": d, "buf": collections.deque(maxlen=64),
                           "mode": mode, "tune_hz": float(tune_hz), "rate": d.audio_rate, "t": time.time()}
            log.info("live-DF %s: audio start (analog) mode=%s tune=%.4f MHz → %.0f Hz PCM",
                     self.dev.id, mode, tune_hz / 1e6, d.audio_rate)
            return {"sample_rate": int(round(d.audio_rate)), "mode": mode, "kind": "analog",
                    "tune_hz": float(tune_hz), "channel_bw_hz": d.channel_bw, "encoding": "pcm_s16le", "channels": 1}
        # digital: pick an installed decoder, feed it the discriminator baseband
        from . import decoder as decmod
        spec = decmod.spec_for(mode)
        if spec is None:
            need = decmod.decoders_for(mode)
            raise ValueError(f"{mode}: no decoder installed (install one of {need or ['dsd-fme']}) — "
                             f"the AMBE/codec2 vocoders can't be bundled")
        dec = decmod.DigitalDecoder(spec).start()
        d = AudioDemod(mode, self.sample_rate_hz, off, channel_bw_hz=(bw_hz or 12500.0),
                       discriminator=True, audio_rate=spec["in_rate"])
        self._audio = {"kind": "digital", "demod": d, "decoder": dec, "buf": collections.deque(maxlen=64),
                       "text": collections.deque(maxlen=256), "mode": mode, "tune_hz": float(tune_hz),
                       "rate": dec.out_rate, "t": time.time()}
        log.info("live-DF %s: audio start (digital) mode=%s tune=%.4f MHz → %s (%s)",
                 self.dev.id, mode, tune_hz / 1e6, spec["decoder"], dec.out_kind)
        return {"sample_rate": int(dec.out_rate), "mode": mode, "kind": "digital", "decoder": spec["decoder"],
                "out": dec.out_kind, "tune_hz": float(tune_hz), "channel_bw_hz": d.channel_bw,
                "encoding": "pcm_s16le", "channels": 1}

    def stop_audio(self) -> None:
        a = self._audio
        if a is not None:
            log.info("live-DF %s: audio stop", self.dev.id)
            dec = a.get("decoder")
            if dec is not None:
                try:
                    dec.stop()
                except Exception:
                    log.debug("live-DF %s: decoder stop failed", self.dev.id, exc_info=True)
        self._audio = None

    def _feed_audio(self, x0: np.ndarray) -> None:
        a = self._audio
        if a is None:
            return
        pcm = a["demod"].process(np.asarray(x0))     # analog: voice PCM · digital: discriminator PCM
        if a["kind"] == "digital":
            dec = a["decoder"]
            if pcm.size:
                dec.feed(pcm.tobytes())
            if dec.out_kind == "audio":
                for chunk in dec.audio_chunks():
                    a["buf"].append(chunk)
            else:
                for line in dec.text_lines():
                    a["text"].append(line)
        elif pcm.size:
            a["buf"].append(pcm.tobytes())

    def audio_chunks(self) -> list:
        """Drain queued voice PCM (bytes) for the WS streamer. Empty when no session."""
        a = self._audio
        if a is None:
            return []
        out = []
        while a["buf"]:
            out.append(a["buf"].popleft())
        return out

    def audio_text(self) -> list:
        """Drain decoded text lines (digital data modes — POCSAG/FLEX/…)."""
        a = self._audio
        t = a.get("text") if a else None
        if not t:
            return []
        out = []
        while t:
            out.append(t.popleft())
        return out

    def capture_baseband(self, center_hz: float, rate_hz: float, n_samples: int,
                         channel: int = 0) -> Optional[np.ndarray]:
        """One-shot real IQ for the PTT classifier: read a block from the (already
        open) driver and DDC the channel at ``center_hz`` down to ``rate_hz``. None if
        the driver isn't open or the centre is outside the captured band. The driver's
        own lock serialises this against the DF capture loop."""
        if self._driver is None:
            return None
        fs = float(self.sample_rate_hz)
        c = float(self._applied_freq or self.dev.frequency_hz or 0.0)
        off = float(center_hz) - c
        if abs(off) > fs / 2.0:
            return None
        rate = float(rate_hz) or fs
        decim = max(1, int(round(fs / max(1.0, rate))))
        need = min(int(n_samples) * decim + 512, 1 << 21)
        frame = self._driver.read_iq(int(need))
        X = np.asarray(frame.samples)
        x0 = X[channel] if (X.ndim == 2 and channel < X.shape[0]) else (X[0] if X.ndim == 2 else X)
        from app.core.df.vfo import ddc
        y = ddc(x0[None, :], fs, off, rate, decim=decim)[0]
        return np.asarray(y[:int(n_samples)], dtype=np.complex64)

    # ── spectrum (so the DF panel shows the *real* radio, not synthetic) ─────────
    def _cache_spectrum(self, X: np.ndarray, n_bins: int = 1024) -> None:
        """Welch PSD per channel from the live capture, cached for the spectrum API.
        This is what makes a configured Pluto's actual RF show in the DF panel even
        when SoapySDR isn't installed (the live path runs over pyadi/libiio)."""
        try:
            win = np.hanning(n_bins)
            wpow = float(np.sum(win ** 2))
            psd_by_ch = []
            for ch in range(X.shape[0]):
                x = X[ch]
                nseg = max(1, len(x) // n_bins)
                acc = np.zeros(n_bins)
                cnt = 0
                for i in range(nseg):
                    seg = x[i * n_bins:(i + 1) * n_bins]
                    if len(seg) < n_bins:
                        break
                    sp = np.fft.fftshift(np.fft.fft(seg * win))
                    acc += np.abs(sp) ** 2
                    cnt += 1
                if cnt == 0:
                    seg = np.zeros(n_bins, dtype=np.complex64); seg[:len(x)] = x[:n_bins]
                    acc = np.abs(np.fft.fftshift(np.fft.fft(seg * win))) ** 2; cnt = 1
                p = acc / (cnt * wpow)
                psd_by_ch.append(10.0 * np.log10(np.maximum(p, 1e-20)) - 30.0)   # ~dBm into 50Ω
            self._spectrum = {
                "center_hz": float(self._applied_freq or self.dev.frequency_hz or 0.0),
                "sample_rate_hz": float(self.sample_rate_hz),
                "n_bins": int(n_bins), "psd_by_ch": psd_by_ch, "t": time.time(),
            }
        except Exception:
            log.debug("live-DF %s: spectrum cache failed", self.dev.id, exc_info=True)

    def spectrum(self, center_hz: float, span_hz: float, n_bins: int, channel: int) -> Optional[dict]:
        """Return a PSD frame (dsp.spectrum_frame shape) from the latest live capture,
        windowed around the *requested* centre (clamped inside the captured band) and
        resampled to the requested span/bins. None if nothing captured yet.

        The wideband capture spans [c-fs/2, c+fs/2] at the device's tuned frequency;
        a requested centre within that band pans the view (no retune), so the DF panel
        can show a different sub-band per channel. To move outside the band, retune the
        device (frequency_hz) — that re-captures around the new centre."""
        s = self._spectrum
        if not s or not s["psd_by_ch"]:
            return None
        ch = max(0, min(len(s["psd_by_ch"]) - 1, int(channel)))
        psd = np.asarray(s["psd_by_ch"][ch], dtype=float)
        N = len(psd)
        fs = float(s["sample_rate_hz"]); c = float(s["center_hz"])
        band_lo = c - fs / 2.0
        hz_per_bin = fs / max(1, N)
        out_span = fs if (not span_hz or span_hz >= fs) else float(span_hz)
        keep = max(2, min(N, int(round(N * out_span / fs))))
        # window centred on the requested centre, clamped so it stays inside the band
        req = float(center_hz) if center_hz else c
        center_bin = int(round((req - band_lo) / hz_per_bin))
        lo = min(max(0, center_bin - keep // 2), max(0, N - keep))
        win = psd[lo:lo + keep]
        eff_span = len(win) * hz_per_bin
        f0 = band_lo + lo * hz_per_bin
        if len(win) != n_bins:
            win = np.interp(np.linspace(0, len(win) - 1, n_bins), np.arange(len(win)), win)
        peak_i = int(np.argmax(win))
        src = "synthetic" if self._backend_label == "synthetic" else "hardware"
        return {
            "source": src, "backend": self._backend_label, "driver": self.driver_id, "channel": ch,
            "center_hz": float(f0 + eff_span / 2.0), "span_hz": float(eff_span), "n_bins": int(n_bins),
            "sample_rate_hz": fs, "power_dbm": [round(float(v), 2) for v in win],
            "noise_floor_dbm": round(float(np.percentile(win, 20.0)), 2),
            "peak_hz": round(f0 + (peak_i / max(1, n_bins - 1)) * eff_span, 1),
            "peak_dbm": round(float(win[peak_i]), 2), "age_s": round(time.time() - s["t"], 2),
            "t": s["t"],
        }

    def _squelch_active(self) -> bool:
        return self.auto_squelch or len(self.vfos) > 1 or any(v["squelch_db"] is not None for v in self.vfos)

    def _df_vfo(self, Xv: np.ndarray, vfo: dict, power_db: float) -> dict:
        """Run the DoA solver on an isolated VFO block."""
        from app.core.df.interferometry import aoa_from_snapshots
        res = aoa_from_snapshots(
            self._interf_geom, float(vfo["freq_hz"]), Xv, method=self.method, n_sources=self.n_sources,
            fb_smoothing=self.fb_smoothing, az_step=self.az_step_deg,
            observer_heading_deg=float(self.dev.antenna_heading_deg or 0.0),
        )
        return {
            "az_true": float(res.az_true_deg if res.az_true_deg is not None else res.az_deg) % 360.0,
            "az_rel": float(res.az_deg) % 360.0, "sigma_az": float(res.sigma_az_deg),
            "snr_db": (None if res.snr_db is None else float(res.snr_db)),
            "quality": float(res.quality), "rssi_dbm": round(power_db, 1),
            "freq_hz": float(vfo["freq_hz"]), "method": res.method, "channels": int(Xv.shape[0]),
        }

    # ── adapter contract ────────────────────────────────────────────────────────
    async def run(self) -> None:
        if not self.dev.can_df:
            self.report("error", "live DF needs ≥2 coherent channels — this device is single-channel "
                                 "(set source_class=multi_channel and channels≥2, e.g. a Pluto with the 2R2T mod)")
            return

        loop = asyncio.get_running_loop()
        center = float(self.dev.frequency_hz or 100e6)
        self._applied_freq = None          # force the first _retune to actually tune the radio
        self._arrays_geom, self._interf_geom = self._build_geometry(center)
        log.info("live-DF %s: driver=%s array=%s method=%s vfos=%d", self.dev.id, self.driver_id,
                 self._interf_geom.name, self.method, len(self.vfos))

        backoff = 1.0
        while True:
            self.report("connecting")
            try:
                backend = await loop.run_in_executor(None, self._open_driver)
            except asyncio.CancelledError:
                self._close_driver(); raise
            except Exception as e:
                self.report("error", f"driver open failed: {type(e).__name__}: {e}")
                await asyncio.sleep(min(30.0, backoff)); backoff = min(30.0, backoff * 2)
                continue
            # initial calibration before streaming, if asked + supported
            if self.auto_calibrate and self._cal_capable:
                try:
                    await loop.run_in_executor(None, self._calibrate)
                except Exception as e:
                    log.warning("live-DF %s: initial calibration failed: %s", self.dev.id, e)
            self.report("streaming")
            log.info("live-DF %s streaming: driver=%s backend=%s cal_capable=%s",
                     self.dev.id, self.driver_id, backend, self._cal_capable)
            backoff = 1.0
            try:
                while True:
                    # Idle when nobody is subscribed (and not actively listening):
                    # skip the capture + MUSIC/Bartlett DSP, just heartbeat. The driver
                    # stays open so a reconnecting client resumes within _IDLE_DWELL.
                    if not self._has_viewers() and self._audio is None:
                        if not self._idle:
                            self._idle = True
                            self.report("idle")
                            log.info("live-DF %s: no subscribers — idling capture/DSP", self.dev.id)
                        await asyncio.sleep(_IDLE_DWELL)
                        continue
                    if self._idle:
                        self._idle = False
                        self.report("streaming")
                        log.info("live-DF %s: client connected — resuming capture", self.dev.id)
                    # Self-heal: if a real radio was configured but the open fell back
                    # to synthetic (e.g. the device was momentarily owned by another
                    # process), periodically retry the real open and hot-swap it in.
                    if (self._backend_label == "synthetic" and self.driver_id != "synthetic"
                            and time.time() - self._last_hw_probe >= _HW_REPROBE_S):
                        self._last_hw_probe = time.time()
                        await loop.run_in_executor(None, self._try_upgrade_to_hardware)
                    # operator-forced recalibration (POST /df/live/{id}/calibrate)
                    force = float((self.dev.metadata or {}).get("force_cal") or 0.0)
                    if force and force != self._force_cal_seen:
                        self._force_cal_seen = force
                        if self._cal_capable:
                            await loop.run_in_executor(None, self._calibrate)
                    # periodic recalibration
                    elif (self.auto_calibrate and self._cal_capable
                            and time.time() - self._cal_time >= self.cal_interval_s):
                        await loop.run_in_executor(None, self._calibrate)
                    X = await loop.run_in_executor(None, self._capture_raw)
                    statuses = await loop.run_in_executor(None, self._solve_all, X)
                    for st in statuses:
                        sol = st.pop("sol", None)
                        if sol is not None and st.get("bearing_deg") is not None:
                            await self.emit(self._to_lob(sol, st["name"]))
                    self._publish_vfo_status(statuses)
                    # listening ⇒ capture back-to-back for continuous audio; else dwell
                    await asyncio.sleep(0.0 if self._audio is not None else self.dwell_s)
            except asyncio.CancelledError:
                await loop.run_in_executor(None, self._close_driver)
                raise
            except Exception as e:
                self.report("error", f"{type(e).__name__}: {e}")
                await loop.run_in_executor(None, self._close_driver)
                await asyncio.sleep(min(30.0, backoff)); backoff = min(30.0, backoff * 2)

    def _try_upgrade_to_hardware(self) -> None:
        """Running on the synthetic fallback though a real driver was configured —
        try to (re)open the real radio and hot-swap it in for the synthetic one.
        No-op if the radio is still unreachable (we stay synthetic and retry on the
        next interval). Runs between captures, so swapping ``self._driver`` is safe.
        BLOCKING: call from an executor, not the event loop."""
        from app.core.sdr import drivers
        try:
            kwargs = dict(self.driver_args)
            kwargs.setdefault("channels", int(self.dev.channels))
            if self._arrays_geom is not None:
                kwargs.setdefault("array", self._arrays_geom)
            probe = drivers.create(self.driver_id, **kwargs)
            probe.open()
        except Exception as e:
            log.debug("live-DF %s: hardware re-probe failed: %s", self.dev.id, e)
            return
        backend = str(getattr(probe, "_backend", None) or self.driver_id)
        if backend == "synthetic":
            try: probe.close()
            except Exception: pass
            return
        old, self._driver = self._driver, probe        # swap the real radio in
        self._backend_label = backend
        self._applied_freq = self._applied_rate = None  # force _retune onto the new handle
        self._applied_gain = "<unset>"
        if old is not None:
            try: old.close()
            except Exception: pass
        log.info("live-DF %s: upgraded synthetic→%s (real radio reachable again)", self.dev.id, backend)
        self.report("streaming")

    def _solve_all(self, X: np.ndarray) -> list[dict]:
        """Down-convert every VFO and DF the open ones. Squelch logic:

          * **manual** ``squelch_db`` → a hard pre-DF power gate (always honoured);
          * **auto** → learn a shared band noise floor from all VFO powers and gate
            a VFO below ``floor + margin`` — but only once the band shows real
            dynamic range (an empty channel proves where the floor is), so a band
            of all-constant signals isn't squelched by its own level;
          * the post-DF **SNR/quality gate** (``_passes_gate``) is the always-on
            backstop that drops phantom bearings on noise that slipped through.
        """
        from app.core.df import vfo as vfomod
        if self._floor_hist is None:
            self._floor_hist = vfomod.SquelchTracker(self.squelch_margin_db,
                                                     warmup=8 * max(1, len(self.vfos)))
        active = self._squelch_active()
        fs = self.sample_rate_hz
        chans = [(v, vfomod.ddc(X, fs, v["offset_hz"], v["bandwidth_hz"])) for v in self.vfos]
        powers = [vfomod.power_dbfs(Xv) for _, Xv in chans]
        floor = self._floor_hist.floor_db
        warm = True
        if active:
            self._floor_hist.observe(powers)
            floor = self._floor_hist.floor_db
            warm = len(self._floor_hist._hist) >= self._floor_hist.warmup
        band_range = (max(powers) - floor) if powers else 0.0
        out: list[dict] = []
        for (v, Xv), power_db in zip(chans, powers):
            manual = v["squelch_db"]
            thr = manual if manual is not None else (floor + self.squelch_margin_db)
            st = {"name": v["name"], "freq_hz": v["freq_hz"], "power_db": round(power_db, 1),
                  "open": True, "threshold_db": (round(thr, 1) if active else None), "bearing_deg": None}
            if active and warm and power_db < thr:
                # manual gate is absolute; auto gate only when a real floor exists
                if manual is not None or band_range > self.squelch_margin_db:
                    st["open"] = False
                    out.append(st); continue
            sol = self._df_vfo(Xv, v, power_db)
            st["bearing_deg"] = round(sol["az_true"], 1) if self._passes_gate(sol) else None
            st["sol"] = sol
            out.append(st)
        return out

    def _publish_vfo_status(self, statuses: list[dict]) -> None:
        md = self.dev.metadata or {}
        md["vfo_status"] = [{k: s.get(k) for k in ("name", "freq_hz", "power_db", "threshold_db", "open", "bearing_deg")} for s in statuses]
        self.dev.metadata = md
        # status broadcast is best-effort; the LoB stream already drives the map
        try:
            self.report("streaming")
        except Exception:
            pass

    def _passes_gate(self, sol: dict) -> bool:
        if sol["quality"] < self.min_quality:
            return False
        if sol["snr_db"] is not None and sol["snr_db"] < self.min_snr_db:
            return False
        return True

    def _to_lob(self, sol: dict, vfo_name: str = "") -> LobEvent:
        conf = max(5.0, min(99.0, 100.0 - sol["sigma_az"] * 3.0))
        return LobEvent(
            device_id=self.dev.id, lat=self.dev.lat, lon=self.dev.lon,
            azimuth_deg=sol["az_true"], raw_azimuth_deg=sol["az_rel"],
            frequency_hz=sol["freq_hz"], rssi_dbm=sol["rssi_dbm"], confidence_pct=conf,
            azimuth_sigma_deg=sol["sigma_az"],
            observer_height_m=self.dev.observer_height_m, environment=self.dev.environment,
            device_type=(f"{self.driver_id}:{vfo_name}" if vfo_name and len(self.vfos) > 1 else self.driver_id),
            t=time.time(),
        )
