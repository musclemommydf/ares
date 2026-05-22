"""
sdr/soapy.py — SoapySDR capture shim (Workstream D).

If the **SoapySDR** Python bindings are installed (`pip install soapysdr` /
the distro's `python3-soapysdr` — drives RTL-SDR, HackRF, Airspy, USRP/UHD,
LimeSDR, PlutoSDR, BladeRF, **Epiq Sidekiq/Matchstiq X40** via SoapySidekiq,
and the KrakenSDR's individual tuners), this registers a real
``app.core.sdr.dsp.SPECTRUM_PROVIDER`` so the SDR console's spectrum / DF panel
shows **live RF** instead of the synthetic placeholder. When SoapySDR isn't
present, ``register()`` is a no-op and the synthetic provider stays — fully
optional, no hard dependency.

It opens the device named by ``device.metadata["soapy"]`` (a SoapySDR device
args string, e.g. ``"driver=rtlsdr"``, ``"driver=sidekiq"``, ``"driver=uhd"``;
falls back to the first device SoapySDR enumerates), tunes to the requested
centre, grabs a short IQ burst per channel, and returns an averaged Welch PSD
frame in the same shape ``dsp.spectrum_frame`` expects. Coherent multi-channel
capture for actual DF (phase-aligned across channels) needs the radio's own
coherent front-end / DAQ (the KrakenSDR DAQ, a clocked-together USRP set, …) —
that path stays the JSON-lines / interferometry ingest in :mod:`adapters`; this
shim is the per-channel *spectrum* feed.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import SoapySDR                                   # type: ignore
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32  # type: ignore
    _HAVE = True
except Exception:
    SoapySDR = None
    _HAVE = False

_CACHE: dict = {}      # device-args string → (Soapy device handle, stream handle, channel)
_SAMP_RATE = 2.4e6     # default capture sample rate (≈ span)
_BURST = 1 << 16       # IQ samples per burst (→ a few-ms snapshot)


def available() -> bool:
    return _HAVE


def list_devices() -> list[dict]:
    if not _HAVE:
        return []
    try:
        return [dict(d) for d in SoapySDR.Device.enumerate()]
    except Exception as e:  # pragma: no cover
        log.debug("SoapySDR.enumerate failed: %s", e)
        return []


def _open(args: str, ch: int):
    key = (args or "", ch)
    if key in _CACHE:
        return _CACHE[key]
    dev = SoapySDR.Device(args) if args else SoapySDR.Device()
    st = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [ch])
    dev.activateStream(st)
    _CACHE[key] = (dev, st)
    return dev, st


def _psd(samples: np.ndarray, fs: float, n_bins: int) -> np.ndarray:
    """Averaged (Welch) power spectral density in dB, length n_bins, fftshift'd."""
    nseg = max(1, len(samples) // n_bins)
    win = np.hanning(n_bins)
    acc = np.zeros(n_bins)
    cnt = 0
    for i in range(nseg):
        seg = samples[i * n_bins:(i + 1) * n_bins]
        if len(seg) < n_bins:
            break
        sp = np.fft.fftshift(np.fft.fft(seg * win))
        acc += (np.abs(sp) ** 2)
        cnt += 1
    if cnt == 0:
        return np.full(n_bins, -120.0)
    p = acc / (cnt * np.sum(win ** 2))
    # dBm into 50 Ω from a full-scale-relative power, with a coarse front-end offset
    return 10.0 * np.log10(np.maximum(p, 1e-20)) - 30.0


def _provider(device: dict, center_hz: float, span_hz: float, n_bins: int, channel: int) -> Optional[dict]:
    if not _HAVE:
        return None
    # Resolve the SoapySDR device args from the registered device — an explicit
    # metadata.soapy string, else the device's kind/model/built-in driver_id (so a
    # Pluto added via the built-in-driver flow opens the *Pluto*, not the first SDR).
    try:
        from . import iq_capture
        args = iq_capture.soapy_args_for(device)
    except Exception:
        args = ((device.get("metadata") or {}).get("soapy") or "").strip()
    ch = max(0, int(channel))
    fs = max(2e5, min(_SAMP_RATE if span_hz <= _SAMP_RATE else span_hz, 30e6))
    try:
        dev, st = _open(args, ch)
        dev.setSampleRate(SOAPY_SDR_RX, ch, fs)
        dev.setFrequency(SOAPY_SDR_RX, ch, float(center_hz))
        try:
            g = (device.get("metadata") or {}).get("gain_db")
            if g is None:
                dev.setGainMode(SOAPY_SDR_RX, ch, True)              # AGC
            else:
                dev.setGainMode(SOAPY_SDR_RX, ch, False)
                dev.setGain(SOAPY_SDR_RX, ch, float(g))
        except Exception:
            pass
        buf = np.empty(_BURST, np.complex64)
        got = 0
        deadline = time.time() + 0.3
        while got < _BURST and time.time() < deadline:
            sr = dev.readStream(st, [buf[got:].view(np.complex64)], _BURST - got, timeoutUs=int(2e5))
            n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, (list, tuple)) else 0)
            if n is None or n <= 0:
                break
            got += int(n)
        if got < n_bins:
            return None
        psd = _psd(buf[:got], fs, n_bins)
        # if the requested span < the captured fs, crop to the centre
        if span_hz < fs:
            keep = int(round(n_bins * span_hz / fs))
            lo = (n_bins - keep) // 2
            psd = psd[lo:lo + keep]
            psd = np.interp(np.linspace(0, len(psd) - 1, n_bins), np.arange(len(psd)), psd)
        peak_i = int(np.argmax(psd))
        f0 = float(center_hz) - span_hz / 2.0
        return {
            "source": "hardware", "driver": args or "auto", "channel": ch,
            "center_hz": float(center_hz), "span_hz": float(span_hz), "n_bins": int(n_bins),
            "sample_rate_hz": fs,
            "power_dbm": [round(float(v), 2) for v in psd],
            "noise_floor_dbm": round(float(np.percentile(psd, 20.0)), 2),
            "peak_hz": round(f0 + (peak_i / max(1, n_bins - 1)) * span_hz, 1),
            "peak_dbm": round(float(psd[peak_i]), 2), "t": time.time(),
        }
    except Exception as e:  # pragma: no cover - depends on real hardware
        log.debug("SoapySDR capture failed (%s): %s", args, e)
        return None


def register() -> bool:
    """Wire the SoapySDR PSD provider into ``dsp.SPECTRUM_PROVIDER`` if available.
    Returns True if a real provider was registered, False if SoapySDR is absent
    (the synthetic provider then remains active)."""
    if not _HAVE:
        log.info("SoapySDR not installed — SDR spectrum uses the synthetic provider (pip install soapysdr to go live)")
        return False
    from app.core.sdr import dsp
    dsp.set_spectrum_provider(_provider)
    devs = list_devices()
    log.info("SoapySDR registered as the SDR spectrum provider (%d device(s) enumerated)", len(devs))
    return True
