"""
sdr/iq_capture.py — native, in-process RF/IQ capture for the SDR(s) plugged into the host.

This is the single capture layer the rest of Ares pulls baseband IQ from — the UAS-video
software demod (sdr/native_demod) and the native DF / angle-of-arrival solver (df/interferometry
via dsp.solve_aoa_live). Ares does NOT shell out to an external SDR application: it talks to the
radios directly via **SoapySDR** (the vendor-neutral SDR abstraction), which already covers the
radios in scope —

  * **SignalHound** BB60C/BB60D/SM200B/SM435B  → ``driver=sh``     (the SoapySDR_SignalHound module)
  * **Ettus / NI USRP** (B2xx, X3xx, N2xx/N3xx) → ``driver=uhd``    (SoapyUHD)
  * **Epiq Sidekiq / Matchstiq** (X40, NV100, …) → ``driver=sidekiq`` (SoapySidekiq)
  * **RTL-SDR** (RTL2832U dongles, incl. each KrakenSDR tuner) → ``driver=rtlsdr`` (SoapyRTLSDR)
  * plus HackRF / Airspy / LimeSDR / PlutoSDR / BladeRF — anything Soapy enumerates.

Multiple radios at once are supported: ``capture_multi()`` opens several devices and grabs a
snapshot from each (e.g. four RTL dongles, or a SignalHound + a Sidekiq). True *coherent* DF
(phase-locked across channels for interferometry) needs a shared reference clock — a KrakenSDR
DAQ, an Octoclock-disciplined USRP set, a Sidekiq X40's two coherent RX — and SoapySDR exposes
those as one device with N RX channels; ``capture()`` grabs all requested channels of such a
device in one go. The SDR console registers/enables the devices; this module captures from them.

When SoapySDR (and the relevant per-device module) isn't installed, ``register()`` is a no-op and
the synthetic-IQ fallbacks in sdr/uas_video and sdr/dsp stay active — Ares still runs fully
offline. To go live: ``pip install soapysdr`` (or the distro's ``python3-soapysdr``) plus the
device module(s) above.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import SoapySDR                                          # type: ignore
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32        # type: ignore
    _HAVE = True
except Exception:
    SoapySDR = None
    _HAVE = False

# device "kind" / model substring → SoapySDR driver name
DRIVER_FOR_KIND: dict[str, str] = {
    # SignalHound
    "signalhound": "sh", "sh": "sh", "bb60": "sh", "bb60c": "sh", "bb60d": "sh",
    "sm200": "sh", "sm200a": "sh", "sm200b": "sh", "sm200c": "sh", "sm435": "sh", "sm435b": "sh",
    # USRP / UHD
    "usrp": "uhd", "ettus": "uhd", "uhd": "uhd", "ni": "uhd", "b200": "uhd", "b205": "uhd",
    "b210": "uhd", "x300": "uhd", "x310": "uhd", "x410": "uhd", "n200": "uhd", "n210": "uhd",
    "n300": "uhd", "n310": "uhd", "n320": "uhd",
    # Epiq Sidekiq / Matchstiq
    "epiq": "sidekiq", "sidekiq": "sidekiq", "matchstiq": "sidekiq", "x40": "sidekiq",
    "z2": "sidekiq", "nv100": "sidekiq",
    # RTL-SDR (incl. KrakenSDR's individual tuners)
    "rtl": "rtlsdr", "rtlsdr": "rtlsdr", "rtl-sdr": "rtlsdr", "rtl2832": "rtlsdr",
    "rtl2832u": "rtlsdr", "krakensdr": "rtlsdr", "kraken": "rtlsdr",
    # other common SDRs Soapy handles
    "hackrf": "hackrf", "airspy": "airspy", "airspyhf": "airspyhf",
    "lime": "lime", "limesdr": "lime", "pluto": "plutosdr", "adalm": "plutosdr",
    "antsdr": "plutosdr",                     # ANTSDR E200 — AD9361 board, speaks the Pluto/libiio manifold
    "bladerf": "bladerf", "blade": "bladerf",
}

_KNOWN_DRIVERS = sorted(set(DRIVER_FOR_KIND.values()))

_DEV_CACHE: dict = {}     # (args, ch_tuple) -> (dev, stream)


def available() -> bool:
    return _HAVE


def soapy_args_for(device: Optional[dict]) -> str:
    """Build a SoapySDR device-args string from a registered device dict. Honours an explicit
    ``metadata.soapy`` args string, else maps the device's type/kind/model/built-in-driver to a
    SoapySDR driver, else returns "" (Soapy picks the first device it finds).

    Crucially this also resolves the built-in-driver flow: a Pluto added under
    "Direction finding — built-in driver" carries ``metadata.driver_id="plutosdr"``
    (and maybe ``metadata.driver_args.uri``) but no explicit ``soapy`` string — without
    this its spectrum would open the *first* enumerated SDR (or none) and fall back to
    the synthetic placeholder."""
    md = (device or {}).get("metadata") or {}
    explicit = (md.get("soapy") or "").strip()
    if explicit:
        return explicit
    drv_args = md.get("driver_args") or {}

    def _with_extras(args: str) -> str:
        sn = md.get("serial") or (device or {}).get("serial")
        if sn:
            args += f",serial={sn}"
        # a Pluto/ANTSDR URI (ip:… / usb:…) targets a specific board via SoapyPlutoSDR
        uri = (drv_args.get("uri") or "").strip()
        if uri and "plutosdr" in args:
            args += f",uri={uri}"
        return args

    # 1) authoritative: the built-in driver id / explicit driver name. Checked first so
    #    a device merely *named* "Kraken-1" can't hijack a plutosdr's driver_id via the
    #    fuzzy blob below ("kraken" → rtlsdr).
    for cand in (md.get("driver_id"), md.get("driver")):
        c = str(cand or "").lower()
        if not c:
            continue
        if c in _KNOWN_DRIVERS:
            return _with_extras(f"driver={c}")
        for token, drv in DRIVER_FOR_KIND.items():   # e.g. "uhd_usrp" → uhd, "antsdr_e200" → plutosdr
            if token in c:
                return _with_extras(f"driver={drv}")
    # 2) fuzzy: the device's type / kind / model / name
    blob = " ".join(str(device.get(k, "")) for k in ("type", "kind", "model", "name", "driver")).lower() if device else ""
    for token, drv in DRIVER_FOR_KIND.items():
        if token in blob:
            return _with_extras(f"driver={drv}")
    return ""


def enumerate_devices() -> list[dict]:
    """Every SDR SoapySDR can see, normalised to ``{id, driver, label, channels, kind, args}``.
    Empty list when SoapySDR isn't installed (the SDR console then shows only registered/synthetic)."""
    if not _HAVE:
        return []
    out: list[dict] = []
    try:
        for d in SoapySDR.Device.enumerate():
            dd = dict(d)
            drv = dd.get("driver") or "unknown"
            # Skip the SoapySDR `audio` driver — it wraps PortAudio sound cards
            # (e.g. "HDA Intel PCH") which aren't RF SDRs for our purposes.
            if drv == "audio":
                continue
            label = dd.get("label") or f"{drv} {dd.get('serial', '')}".strip()
            args = ",".join(f"{k}={v}" for k, v in dd.items() if k in ("driver", "serial", "addr", "uri", "device", "index"))
            nch = 1
            try:
                handle = SoapySDR.Device(dict(d))
                nch = int(handle.getNumChannels(SOAPY_SDR_RX)) or 1
                # don't keep the handle from enumeration around — it may not be the one we capture with
            except Exception:
                pass
            out.append({
                "id": dd.get("serial") or label, "driver": drv, "label": label,
                "channels": nch, "kind": _kind_for_driver(drv), "args": args or f"driver={drv}",
                "coherent_rx": nch >= 2,
            })
    except Exception as e:  # pragma: no cover - hardware dependent
        log.debug("SoapySDR.enumerate failed: %s", e)
    return out


def _kind_for_driver(drv: str) -> str:
    return {"sh": "SignalHound", "uhd": "USRP/UHD", "sidekiq": "Epiq Sidekiq", "rtlsdr": "RTL-SDR",
            "hackrf": "HackRF", "airspy": "Airspy", "lime": "LimeSDR", "plutosdr": "PlutoSDR",
            "bladerf": "BladeRF"}.get(drv, drv or "SDR")


def _open(args: str, channels: tuple[int, ...]):
    key = (args or "", tuple(channels))
    if key in _DEV_CACHE:
        return _DEV_CACHE[key]
    dev = SoapySDR.Device(args) if args else SoapySDR.Device()
    st = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, list(channels))
    dev.activateStream(st)
    _DEV_CACHE[key] = (dev, st)
    return dev, st


def _set_chain(dev, ch: int, center_hz: float, rate_hz: float, gain_db, bw_hz: Optional[float]):
    dev.setSampleRate(SOAPY_SDR_RX, ch, float(rate_hz))
    if bw_hz:
        try: dev.setBandwidth(SOAPY_SDR_RX, ch, float(bw_hz))
        except Exception: pass
    dev.setFrequency(SOAPY_SDR_RX, ch, float(center_hz))
    try:
        if gain_db is None:
            dev.setGainMode(SOAPY_SDR_RX, ch, True)               # AGC
        else:
            dev.setGainMode(SOAPY_SDR_RX, ch, False)
            dev.setGain(SOAPY_SDR_RX, ch, float(gain_db))
    except Exception:
        pass


def capture(device: Optional[dict], center_hz: float, rate_hz: float, n_samples: int,
            channels=(0,), gain_db=None, bw_hz: Optional[float] = None, timeout_s: float = 1.5):
    """Grab ``n_samples`` of complex64 IQ from ``channels`` of one SoapySDR device, tuned to
    ``center_hz`` at ``rate_hz``. Returns a single ndarray for one channel, or a list of ndarrays
    for several (phase-relationship intact if the device has coherent RX). ``None`` if SoapySDR
    isn't available or the capture fails — callers then fall back to synthetic IQ."""
    if not _HAVE:
        return None
    channels = tuple(int(c) for c in (channels if isinstance(channels, (list, tuple)) else [channels]))
    n_samples = int(max(256, min(1 << 22, n_samples)))
    args = soapy_args_for(device)
    md = (device or {}).get("metadata") or {}
    g = gain_db if gain_db is not None else md.get("gain_db")
    bw = bw_hz if bw_hz is not None else md.get("bandwidth_hz")
    try:
        dev, st = _open(args, channels)
        for ch in channels:
            _set_chain(dev, ch, center_hz, rate_hz, g, bw)
        bufs = [np.empty(n_samples, np.complex64) for _ in channels]
        got = 0
        deadline = time.time() + float(timeout_s)
        while got < n_samples and time.time() < deadline:
            views = [b[got:] for b in bufs]
            sr = dev.readStream(st, views, n_samples - got, timeoutUs=int(2e5))
            n = getattr(sr, "ret", sr if isinstance(sr, int) else (sr[0] if isinstance(sr, (list, tuple)) else 0))
            if n is None or n <= 0:
                break
            got += int(n)
        if got < 256:
            return None
        out = [b[:got] for b in bufs]
        return out[0] if len(out) == 1 else out
    except Exception as e:  # pragma: no cover - hardware dependent
        log.debug("IQ capture failed (%s @ %.3f MHz): %s", args, center_hz / 1e6, e)
        return None


def capture_multi(devices: list[dict], center_hz: float, rate_hz: float, n_samples: int,
                  gain_db=None) -> dict:
    """Capture a snapshot from several radios (one channel each). Returns ``{device_id: ndarray}``
    for the ones that produced data. Note: separate radios aren't phase-coherent unless they share
    a clock — fine for power-difference DF / spectrum fusion / per-radio decode, not for raw-phase
    interferometry (use a single coherent-RX device for that)."""
    out: dict = {}
    for dv in devices or []:
        x = capture(dv, center_hz, rate_hz, n_samples, channels=(0,), gain_db=gain_db)
        if x is not None:
            out[dv.get("id") or soapy_args_for(dv) or f"dev{len(out)}"] = np.asarray(x, np.complex64)
    return out


# ── provider adapters ───────────────────────────────────────────────────────
def _uas_iq_provider(device, center_hz, rate_hz, n_samples, channel=0):
    """Signature expected by sdr/uas_video.set_iq_provider."""
    x = capture(device, center_hz, rate_hz, n_samples, channels=(int(channel or 0),))
    if x is None:
        return None
    return np.asarray(x[0] if isinstance(x, list) else x, np.complex64)


def _df_iq_provider(device, center_hz, rate_hz, n_samples, channels=(0, 1)):
    """Signature expected by sdr/dsp.set_iq_provider — coherent multi-channel grab for DF."""
    return capture(device, center_hz, rate_hz, n_samples, channels=channels)


def register() -> bool:
    """Wire this native capture layer into the UAS demod and the DF solver. Returns True if a
    real SDR backend (SoapySDR) is present, False otherwise (the synthetic fallbacks stay)."""
    try:
        from app.core.sdr import uas_video
        uas_video.set_iq_provider(_uas_iq_provider if _HAVE else None)
    except Exception:
        log.debug("could not register UAS IQ provider", exc_info=True)
    try:
        from app.core.sdr import dsp
        if hasattr(dsp, "set_iq_provider"):
            dsp.set_iq_provider(_df_iq_provider if _HAVE else None)
    except Exception:
        log.debug("could not register DF IQ provider", exc_info=True)
    if _HAVE:
        n = len(enumerate_devices())
        log.info("Native IQ capture (SoapySDR) registered for UAS demod + DF — %d SDR(s) enumerated", n)
    else:
        log.info("SoapySDR not installed — UAS demod + DF use synthetic IQ (install soapysdr + the device module to go live)")
    return _HAVE


def status() -> dict:
    return {
        "backend": "soapysdr" if _HAVE else "synthetic_iq",
        "available": _HAVE,
        "supported_drivers": _KNOWN_DRIVERS,
        "in_scope": {"SignalHound": "sh", "USRP/UHD": "uhd", "Epiq Sidekiq": "sidekiq", "RTL-SDR": "rtlsdr"},
        "devices": enumerate_devices(),
        "note": ("Ares captures IQ from the connected SDR(s) directly via SoapySDR — no external SDR app. "
                 "Coherent multi-channel DF needs a shared-clock front-end (KrakenSDR DAQ / Octoclock USRP set / "
                 "Sidekiq coherent RX). Without SoapySDR a synthetic snapshot drives the offline path."),
    }
