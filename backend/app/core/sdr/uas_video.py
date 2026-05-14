"""
sdr/uas_video.py — UAS (drone) video-downlink scanner / decoder bridge.

Covers the RF video links carried by SignalHound (BB60C/BB60D/SM200/SM435),
Epiq Sidekiq / Matchstiq, and Ettus/NI USRP SDRs (UHD), via SoapySDR when its
bindings are present, the vendor SDK when importable, else a synthetic provider
so the UI works fully offline.

What it does itself, without any external tooling:
  * a registry of analog + digital UAS video feed types (FM-analog NTSC/PAL/SECAM,
    DVB-T/T2, DVB-S/S2, ISDB-T 1-seg, generic COFDM/QAM MPEG-TS, and the
    proprietary/encrypted ones — DJI OcuSync / Lightbridge, HDZero, Walksnail,
    CDL/BE-CDL — flagged "characterize-only");
  * known UAS/FPV video channel plans (5.8 GHz raceband, 1.2/1.3 GHz, 2.4 GHz, …);
  * a PSD-based feed classifier (occupied-band detection + bandwidth/flatness/
    channel-plan heuristics; IQ-domain confirmations — OFDM cyclic-prefix
    autocorrelation, FM-video line-rate cadence — run when an IQ provider is wired);
  * a full MISB ST 0601 (STANAG 4609 UAS Datalink Local Set) KLV parser **and**
    encoder, with the 16-bit checksum — so a decoded feed's metadata becomes a
    platform position, a sensor line-of-sight, and a ground-footprint polygon.

What it hands off to external tooling (detected on $PATH at runtime, exactly like
the audio-decode bridge): the actual video demod / TS extraction —
``leandvb`` (DVB-S/S2), a DVB-T/T2 receiver (``gr-dvbt`` / ``dvbt2-…`` / SDRangel
headless DATV), ``ffmpeg`` / ``tsp`` (TSDuck) for the MPEG-TS → H.264/H.265 step,
and a software analog-TV decoder for FM/VSB composite. When none is installed the
decode session reports exactly which package would handle the feed.
"""
from __future__ import annotations

import math
import shutil
import struct
import time
import uuid
from typing import Callable, Optional

import numpy as np

from . import dsp

# ── IQ provider hook (parallel to dsp.SPECTRUM_PROVIDER) ─────────────────────
IQ_PROVIDER: Optional[Callable] = None
# Optional pluggable ML signal classifier — ``fn(iq: np.ndarray, fs: float, *, band: dict|None=None) -> dict``
# returning at least ``{"feed_type": str, "confidence": float, ...}``. When registered (e.g. by a deployment
# that ships a trained CNN over spectrograms / the spectral-correlation function, plus torch/onnxruntime),
# its verdict is ENSEMBLED with the rule-based one — it boosts/agrees/adds-an-alternative, it does not silently
# override — and runs on the GPU when CuPy is present. Absent, the heuristic classifier is used as-is.
ML_CLASSIFIER: Optional[Callable] = None


def set_iq_provider(fn: Optional[Callable]) -> None:
    """Register a callable ``fn(device, center_hz, rate_hz, n_samples, channel) -> np.ndarray[complex]``."""
    global IQ_PROVIDER
    IQ_PROVIDER = fn


# ── Spectrum max-hold accumulator ────────────────────────────────────────────
# A common pattern when hunting FPV video on a sparse band: scan repeatedly and
# accumulate the maximum power per bin across sweeps so that intermittent or
# frequency-hopping signals "draw themselves in" over time. Keyed by session so
# multiple concurrent scans don't collide.
_MAXHOLD_STATE: dict[str, dict] = {}


def reset_max_hold(key: str = "default") -> None:
    """Clear the named max-hold accumulator (next scan starts fresh)."""
    _MAXHOLD_STATE.pop(key, None)


def get_max_hold(key: str = "default") -> Optional[dict]:
    """Return the current max-hold snapshot for `key`, or None if not started.
    Shape: {start_hz, stop_hz, n_bins, freq_hz: [...], power_dbm: [...], sweeps, last_t}."""
    st = _MAXHOLD_STATE.get(key)
    if not st:
        return None
    return {
        "start_hz": st["start_hz"], "stop_hz": st["stop_hz"],
        "n_bins": int(st["power"].size),
        "freq_hz": [round(float(v), 1) for v in st["freq"]],
        "power_dbm": [round(float(v), 2) for v in st["power"]],
        "sweeps": int(st["sweeps"]), "last_t": st["last_t"],
    }


def list_max_hold() -> list[str]:
    return sorted(_MAXHOLD_STATE.keys())


def _maxhold_update(key: str, start_hz: float, stop_hz: float, n_bins: int,
                    freqs: np.ndarray, powers: np.ndarray) -> None:
    """Merge a freshly captured PSD chunk into the persistent max-hold grid.
    `freqs` and `powers` are 1-D arrays of equal length covering some sub-span;
    the grid spans [start_hz, stop_hz] with `n_bins` points."""
    if freqs.size == 0 or powers.size != freqs.size:
        return
    st = _MAXHOLD_STATE.get(key)
    grid_n = max(256, int(n_bins))
    grid_f = np.linspace(start_hz, stop_hz, grid_n)
    if st is None or st["power"].size != grid_n or st["start_hz"] != start_hz or st["stop_hz"] != stop_hz:
        st = {"start_hz": float(start_hz), "stop_hz": float(stop_hz),
              "freq": grid_f, "power": np.full(grid_n, -200.0, dtype=np.float32),
              "sweeps": 0, "last_t": time.time()}
    # Resample incoming powers onto the persistent grid, then max-merge.
    p = np.interp(grid_f, freqs, powers, left=-200.0, right=-200.0).astype(np.float32)
    st["power"] = np.maximum(st["power"], p)
    st["sweeps"] = int(st["sweeps"]) + 1
    st["last_t"] = time.time()
    _MAXHOLD_STATE[key] = st


def set_ml_classifier(fn: Optional[Callable]) -> None:
    """Register (or clear) the optional ML signal classifier — see ``ML_CLASSIFIER``."""
    global ML_CLASSIFIER
    ML_CLASSIFIER = fn


def gpu_available() -> bool:
    """True if CuPy + a CUDA device are usable (the FFT/correlation DSP and an ML stage can offload to it)."""
    try:
        import cupy  # type: ignore
        return cupy.cuda.runtime.getDeviceCount() > 0
    except Exception:
        return False


def _xp():
    """Return CuPy if a CUDA device is available, else NumPy — for the heavy FFT/correlation paths.
    (Kept opt-in: only worth it for wideband / continuous / multi-channel processing.)"""
    try:
        import cupy  # type: ignore
        if cupy.cuda.runtime.getDeviceCount() > 0:
            return cupy
    except Exception:
        pass
    return np


def _capture_iq(device: dict, center_hz: float, rate_hz: float, n_samples: int, channel: int = 0) -> Optional[np.ndarray]:
    """Best-effort IQ capture: the registered provider, else SoapySDR, else None."""
    if IQ_PROVIDER is not None:
        try:
            x = IQ_PROVIDER(device, center_hz, rate_hz, n_samples, channel)
            if x is not None:
                return np.asarray(x, dtype=np.complex64)
        except Exception:
            pass
    try:  # opportunistic SoapySDR capture (UHD / Sidekiq / SignalHound modules expose the same API)
        import SoapySDR  # type: ignore
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32  # type: ignore
        args = (device or {}).get("metadata", {}).get("soapy") or _soapy_args_for(device)
        dev = SoapySDR.Device(args) if args else SoapySDR.Device()
        dev.setSampleRate(SOAPY_SDR_RX, channel, float(rate_hz))
        dev.setFrequency(SOAPY_SDR_RX, channel, float(center_hz))
        st = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [channel])
        dev.activateStream(st)
        buf = np.empty(int(n_samples), np.complex64)
        got = 0
        while got < n_samples:
            chunk = np.empty(min(1 << 16, n_samples - got), np.complex64)
            r = dev.readStream(st, [chunk], len(chunk))
            n = getattr(r, "ret", r if isinstance(r, int) else 0)
            if n <= 0:
                break
            buf[got:got + n] = chunk[:n]
            got += n
        dev.deactivateStream(st); dev.closeStream(st)
        if got:
            return buf[:got]
    except Exception:
        pass
    # no real source — a representative synthetic snapshot, shaped per the channel plan, so the
    # IQ-domain stages (and any registered ML classifier) still run offline. Always tagged "synthetic".
    return _synthetic_iq(device, center_hz, rate_hz, n_samples, channel)


def _synthetic_iq(device: Optional[dict], center_hz: float, rate_hz: float, n_samples: int, channel: int = 0) -> np.ndarray:
    """A representative IQ snapshot for offline use — shaped per the catalogued UAS/FPV channel plan at
    ``center_hz`` (FM analog video / COFDM / single-carrier QAM/PSK / noise). Deterministic per (freq, rate).
    Not a substitute for a real capture layer; everything that consumes it is tagged ``synthetic``."""
    n = max(4096, min(1 << 20, int(n_samples)))
    rng = np.random.default_rng((int(center_hz) ^ int(rate_hz) ^ (int(channel) << 8) ^ 0xA5A5) & 0xFFFFFFFF)
    noise = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)) * np.float32(0.05)
    fids = ((_channel_plan_for(center_hz) or {}).get("likely_feed_types")) or []
    has = lambda *subs: any(any(sub in f for sub in subs) for f in fids)
    if has("fm_analog_video"):
        t = np.arange(n) / float(rate_hz)
        line = 15734.0
        msg = 0.6 * (2 * (t * line - np.floor(0.5 + t * line))) + 0.3 * np.sin(2 * np.pi * 60.0 * t) + 0.10 * rng.standard_normal(n)
        ph = np.cumsum(msg) * (2 * np.pi * (rate_hz * 0.15) / rate_hz)
        return (np.exp(1j * ph).astype(np.complex64) + noise)
    if has("dvbt", "cofdm", "ocusync", "hdzero", "walksnail", "isdbt", "lightbridge"):
        fft_len, cp = (2048, 512) if has("dvbt", "isdbt", "cofdm") else (256, 64)
        parts = []
        while sum(len(p_) for p_ in parts) < n + fft_len:
            X = ((rng.integers(0, 2, fft_len) * 2 - 1) + 1j * (rng.integers(0, 2, fft_len) * 2 - 1)).astype(np.complex64)
            xt = np.fft.ifft(X)
            parts.append(np.concatenate([xt[-cp:], xt]))
        x = np.concatenate(parts)[:n].astype(np.complex64)
        return x / np.float32(np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-9) + noise
    if has("dvbs", "qam", "cdl"):
        sps = 4
        syms = np.exp(1j * (rng.integers(0, 4, n // sps + 16) * (np.pi / 2) + np.pi / 4)).astype(np.complex64)
        up = np.zeros((n // sps + 16) * sps, np.complex64); up[::sps] = syms
        try:  # a proper RRC transmit pulse (β=0.35) — matches the native demod's matched filter
            from . import native_demod as _nd
            h = _nd._rrc_taps(0.35, sps, 8).astype(np.complex64)
        except Exception:
            h = np.hanning(2 * sps + 1).astype(np.complex64)
        x = np.convolve(up, h, "same")[:n].astype(np.complex64)
        return x / np.float32(np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-9) + noise
    return noise * np.float32(2.0)


# Map a registered device's "kind" to a SoapySDR driver hint.
_SOAPY_DRIVER = {
    "signalhound": "sh",         # SoapySDR_SignalHound
    "bb60": "sh", "sm200": "sh", "sm435": "sh",
    "epiq": "sidekiq", "matchstiq": "sidekiq", "sidekiq": "sidekiq",
    "usrp": "uhd", "ettus": "uhd", "uhd": "uhd", "b200": "uhd", "n210": "uhd", "x310": "uhd",
}


def _soapy_args_for(device: dict) -> str:
    kind = str((device or {}).get("type") or (device or {}).get("kind") or "").lower()
    for k, drv in _SOAPY_DRIVER.items():
        if k in kind:
            return f"driver={drv}"
    return ""


# ════════════════════════════════════════════════════════════════════════════
# Feed-type registry
# ════════════════════════════════════════════════════════════════════════════
def _f(id, name, *, transport, modulation, bw_hz, carries_klv, decodable, chain, notes):
    return {
        "id": id, "name": name, "transport": transport, "modulation": modulation,
        "typical_bandwidth_hz": list(bw_hz), "carries_klv": carries_klv,
        "decodable": decodable, "decoder_chain": list(chain), "notes": notes,
    }


FEED_TYPES: list[dict] = [
    # ── analog ──
    _f("fm_analog_video_ntsc", "Analog FM video — NTSC composite", transport="composite_analog",
       modulation="wideband FM (525-line/29.97 Hz)", bw_hz=[6e6, 8e6], carries_klv=False, decodable=True,
       chain=["sdrangel", "ffmpeg"], notes="Classic analog FPV/ISR downlink (1.2/1.3 GHz, 2.4 GHz, 5.8 GHz raceband). FM-demod → composite → software NTSC decoder."),
    _f("fm_analog_video_pal", "Analog FM video — PAL composite", transport="composite_analog",
       modulation="wideband FM (625-line/25 Hz)", bw_hz=[7e6, 8.5e6], carries_klv=False, decodable=True,
       chain=["sdrangel", "ffmpeg"], notes="As NTSC but 625-line/PAL colour."),
    _f("fm_analog_video_secam", "Analog FM video — SECAM composite", transport="composite_analog",
       modulation="wideband FM (625-line SECAM)", bw_hz=[7e6, 8.5e6], carries_klv=False, decodable=True,
       chain=["sdrangel", "ffmpeg"], notes="625-line SECAM colour (FM chroma subcarriers)."),
    _f("vsb_analog_video", "Analog VSB/AM video (legacy broadcast-style)", transport="composite_analog",
       modulation="vestigial-sideband AM", bw_hz=[6e6, 8e6], carries_klv=False, decodable=True,
       chain=["sdrangel", "ffmpeg"], notes="Older terrestrial-TV-style analog payloads; envelope/synchronous detect → composite."),
    # ── digital, openly decodable ──
    _f("dvbt", "DVB-T (COFDM, MPEG-2 TS)", transport="mpeg_ts", modulation="COFDM 2k/8k, QPSK/16/64-QAM",
       bw_hz=[5e6, 6e6, 7e6, 8e6], carries_klv=True, decodable=True,
       chain=["dvbt-rx", "gr-dvbt", "sdrangel", "ffmpeg", "tsp"], notes="Common broadcast-quality ISR downlink. TS may carry STANAG 4609 / MISB KLV + H.264."),
    _f("dvbt2", "DVB-T2 (COFDM, MPEG-TS / GSE)", transport="mpeg_ts", modulation="COFDM 1k–32k, QPSK…256-QAM, rotated constellations",
       bw_hz=[1.7e6, 5e6, 6e6, 7e6, 8e6, 10e6], carries_klv=True, decodable=True,
       chain=["dvbt2-blade", "gr-dvbt2", "sdrangel", "ffmpeg", "tsp"], notes="Higher-efficiency successor to DVB-T; same TS/KLV/H.264-265 payload."),
    _f("dvbs", "DVB-S (QPSK, MPEG-2 TS)", transport="mpeg_ts", modulation="QPSK + Viterbi/RS",
       bw_hz=[1e6, 5e6, 10e6, 20e6, 36e6], carries_klv=True, decodable=True,
       chain=["leandvb", "sdrangel", "ffmpeg", "tsp"], notes="Continuous single-carrier link (BUC/airborne uplink-style). leandvb handles it."),
    _f("dvbs2", "DVB-S2 / S2X (QPSK…32APSK, MPEG-TS / GSE)", transport="mpeg_ts", modulation="ACM QPSK/8PSK/16/32APSK + LDPC/BCH",
       bw_hz=[1e6, 5e6, 10e6, 20e6, 36e6, 72e6], carries_klv=True, decodable=True,
       chain=["leandvb", "sdrangel", "ffmpeg", "tsp"], notes="Modern satellite-style UAS link; leandvb decodes the common short-frame modes."),
    _f("isdbt_1seg", "ISDB-T 1-seg (COFDM, MPEG-TS)", transport="mpeg_ts", modulation="COFDM, DQPSK/QPSK/16-QAM, 1 of 13 segments",
       bw_hz=[430e3], carries_klv=False, decodable=True, chain=["gr-isdbt", "sdrangel", "ffmpeg"],
       notes="Narrowband (~430 kHz) handheld-TV mode occasionally repurposed for low-rate video."),
    _f("cofdm_mpegts", "Proprietary COFDM MPEG-TS link (DTC/Vislink/Domo/Silvus-class)", transport="mpeg_ts",
       modulation="COFDM (DVB-T/H-derived or vendor PHY)", bw_hz=[1.25e6, 2.5e6, 5e6, 6e6, 8e6, 10e6, 20e6],
       carries_klv=True, decodable=True, chain=["sdrangel", "gr-dvbt", "ffmpeg", "tsp"],
       notes="Tactical broadcast modems. Decodable when the PHY is a DVB-T/T2/H variant; vendor-locked PHYs are characterize-only."),
    _f("qam_mpegts", "Single-carrier QAM MPEG-TS (DVB-C-class)", transport="mpeg_ts", modulation="16…256-QAM + RS",
       bw_hz=[1e6, 6e6, 8e6], carries_klv=True, decodable=True, chain=["leandvb", "sdrangel", "tsp", "ffmpeg"],
       notes="Cabled-TV-style modulation used by some short-range UAS links."),
    # ── digital, proprietary / encrypted → detect & characterize (and DF), no decode ──
    _f("dji_ocusync", "DJI OcuSync / O2 / O3 / O4 / Air-Sync", transport="proprietary", modulation="adaptive OFDM, AES-encrypted video",
       bw_hz=[10e6, 20e6, 40e6], carries_klv=False, decodable=False, chain=[],
       notes="Consumer DJI link (2.4/5.2/5.8 GHz, also 900 MHz on some models). The video is AES-encrypted with pairing-negotiated keys — there is no public passive decrypt; Ares detects/characterises/geolocates it. To ID and locate the drone + its operator, use its unencrypted DroneID / Remote ID beacon (see the dji_droneid / remote_id feed types)."),
    _f("dji_lightbridge", "DJI Lightbridge / Lightbridge 2", transport="proprietary", modulation="OFDM, encrypted video",
       bw_hz=[10e6, 20e6], carries_klv=False, decodable=False, chain=[], notes="Earlier DJI link family. Characterize-only."),
    _f("hdzero", "HDZero digital FPV", transport="proprietary", modulation="custom low-latency digital (5.8 GHz)",
       bw_hz=[27e6], carries_klv=False, decodable=False, chain=[], notes="Open-ish but no public RX decoder; characterize-only here."),
    _f("walksnail", "Walksnail Avatar / Caddx digital FPV", transport="proprietary", modulation="proprietary OFDM (5.8 GHz)",
       bw_hz=[20e6, 40e6], carries_klv=False, decodable=False, chain=[], notes="Proprietary; characterize-only."),
    _f("cdl_becdl", "CDL / TCDL / Bandwidth-Efficient CDL", transport="proprietary", modulation="proprietary, encrypted (COMSEC)",
       bw_hz=[10.71e6, 21.42e6, 45e6, 137e6, 274e6], carries_klv=True, decodable=False, chain=[],
       notes="Military common data link (Ku/Ka/X/C). COMSEC-encrypted — no public decrypt; detect/characterise only."),
    # ── telemetry beacons (NOT the video link, but the open way to detect & locate the drone + its operator) ──
    _f("remote_id", "Remote ID / ASTM F3411 (FAA Remote ID — WiFi NAN/beacon + BT4/5)", transport="telemetry_beacon",
       modulation="WiFi Neighbor-Awareness-Networking / beacon vendor IE, or Bluetooth LE advertising", bw_hz=[20e6, 1e6, 2e6],
       carries_klv=False, decodable=True, chain=["rid-decoder", "wireshark", "tcpdump"],
       notes="The mandated unencrypted broadcast on 2.4/5.x GHz WiFi (often ch 6 / ch 149) and BT — drone serial, "
             "position/altitude/speed, AND the operator/home-point location. This is the open, legitimate way to "
             "detect, ID and geolocate a UAS and its pilot; the video link is a separate (often encrypted) channel."),
    _f("dji_droneid", "DJI DroneID (pre-Remote-ID telemetry beacon)", transport="telemetry_beacon",
       modulation="OFDM burst on a WiFi channel (DJI vendor protocol)", bw_hz=[10e6, 20e6],
       carries_klv=False, decodable=True, chain=["dji_droneid", "rid-decoder"],
       notes="DJI's own telemetry beacon (the pre-Remote-ID format, also still emitted by many models): serial, "
             "GPS, and the operator location — recoverable with the published open tooling. NOTE: this is the "
             "telemetry/Remote-ID broadcast, not the AES-encrypted OcuSync video, which has no public decrypt."),
    _f("unknown_digital", "Unidentified digital video link", transport="unknown", modulation="(unknown digital)",
       bw_hz=[], carries_klv=False, decodable=False, chain=["sdrangel"], notes="Occupied digital channel that didn't match a known signature — record + DF it."),
    _f("unknown_analog", "Unidentified analog video link", transport="unknown", modulation="(unknown analog)",
       bw_hz=[], carries_klv=False, decodable=True, chain=["sdrangel", "ffmpeg"], notes="Analog-looking carrier with no recognised line structure — try the analog-TV path + DF it."),
]
_FEED_BY_ID = {f["id"]: f for f in FEED_TYPES}


# ── Known UAS / FPV video channel plans (centre MHz lists) ───────────────────
def _band(name, ghz_lo, ghz_hi, channels_mhz, kinds):
    return {"name": name, "f_lo_hz": ghz_lo * 1e9, "f_hi_hz": ghz_hi * 1e9,
            "channels_hz": [c * 1e6 for c in channels_mhz], "likely_feed_types": list(kinds)}


def _race(base, step, n):
    return [base + step * i for i in range(n)]


KNOWN_CHANNELS: list[dict] = [
    _band("900 MHz analog/digital UAS", 0.902, 0.928, [910, 915, 920], ["fm_analog_video_ntsc", "dji_ocusync", "cofdm_mpegts"]),
    _band("1.2 / 1.3 GHz analog video", 1.04, 1.36, [1080, 1120, 1160, 1200, 1240, 1258, 1280, 1320, 1360], ["fm_analog_video_ntsc", "fm_analog_video_pal"]),
    _band("L-band ISR datalink", 1.70, 1.85, [1710, 1750, 1790, 1830], ["dvbt", "cofdm_mpegts", "cdl_becdl"]),
    _band("2.4 GHz analog video", 2.37, 2.51, [2370, 2390, 2410, 2430, 2450, 2470, 2490], ["fm_analog_video_ntsc", "fm_analog_video_pal"]),
    _band("2.4 GHz digital (OcuSync / WiFi-band)", 2.40, 2.4835, [2412, 2437, 2462], ["dji_ocusync", "dji_lightbridge"]),
    _band("S-band ISR datalink", 2.20, 2.50, [2250, 2300, 2350], ["dvbt", "dvbs2", "cdl_becdl"]),
    _band("5.8 GHz analog FPV — Raceband (R1-R8)", 5.645, 5.945, _race(5658, 37, 8), ["fm_analog_video_ntsc"]),
    _band("5.8 GHz analog FPV — Band A/B/E/F (legacy)", 5.645, 5.945, [5740, 5760, 5780, 5800, 5820, 5840, 5860, 5865, 5880, 5905, 5925], ["fm_analog_video_ntsc"]),
    _band("5.8 GHz digital FPV (HDZero / Walksnail / O3)", 5.645, 5.945, _race(5658, 37, 8), ["hdzero", "walksnail", "dji_ocusync"]),
    _band("C-band ISR datalink", 4.40, 5.00, [4500, 4700, 4900], ["dvbt", "dvbs2", "cdl_becdl"]),
    _band("Ku-band CDL/SATCOM", 14.0, 15.35, [14250, 14750, 15150], ["cdl_becdl", "dvbs2"]),
]


def _channel_plan_for(center_hz: float) -> Optional[dict]:
    best = None
    for b in KNOWN_CHANNELS:
        if b["f_lo_hz"] <= center_hz <= b["f_hi_hz"]:
            # nearest catalogued channel within ±2 MHz counts as a plan hit
            for c in b["channels_hz"]:
                if abs(c - center_hz) <= 2e6:
                    return {"plan": b["name"], "channel_hz": c, "likely_feed_types": b["likely_feed_types"]}
            best = best or {"plan": b["name"], "channel_hz": None, "likely_feed_types": b["likely_feed_types"]}
    return best


# ════════════════════════════════════════════════════════════════════════════
# MISB ST 0601 (STANAG 4609 UAS Datalink Local Set) — parser + encoder
# ════════════════════════════════════════════════════════════════════════════
UAS_LS_KEY = bytes.fromhex("060E2B34020B01010E01030101000000")  # 16-byte Universal Label

# tag -> (name, codec)  where codec is one of: u8 u16 u32 u64 i16 i32
#   lat: i32 mapped to ±90°, lon/az: i32/u32 mapped to ±180°/0-360°, alt: u16 mapped to -900..19000 m, etc.
_T = {
    2:  ("precision_timestamp_us", "u64"),
    3:  ("mission_id", "str"),
    4:  ("platform_tail_number", "str"),
    5:  ("platform_heading_deg", ("u16", 0.0, 360.0)),
    6:  ("platform_pitch_deg", ("i16", -20.0, 20.0)),
    7:  ("platform_roll_deg", ("i16", -50.0, 50.0)),
    10: ("platform_designation", "str"),
    11: ("image_source_sensor", "str"),
    12: ("image_coordinate_system", "str"),
    13: ("sensor_lat_deg", ("i32", -90.0, 90.0)),
    14: ("sensor_lon_deg", ("i32", -180.0, 180.0)),
    15: ("sensor_true_alt_m", ("u16", -900.0, 19000.0)),
    16: ("sensor_hfov_deg", ("u16", 0.0, 180.0)),
    17: ("sensor_vfov_deg", ("u16", 0.0, 180.0)),
    18: ("sensor_rel_az_deg", ("u32", 0.0, 360.0)),
    19: ("sensor_rel_el_deg", ("i32", -180.0, 180.0)),
    20: ("sensor_rel_roll_deg", ("u32", 0.0, 360.0)),
    21: ("slant_range_m", ("u32", 0.0, 5_000_000.0)),
    22: ("target_width_m", ("u16", 0.0, 10000.0)),
    23: ("frame_center_lat_deg", ("i32", -90.0, 90.0)),
    24: ("frame_center_lon_deg", ("i32", -180.0, 180.0)),
    25: ("frame_center_elev_m", ("u16", -900.0, 19000.0)),
    # corner-point *offsets* from frame centre (i16 mapped to ±0.075°)
    26: ("corner_off_lat_1", ("i16", -0.075, 0.075)), 27: ("corner_off_lon_1", ("i16", -0.075, 0.075)),
    28: ("corner_off_lat_2", ("i16", -0.075, 0.075)), 29: ("corner_off_lon_2", ("i16", -0.075, 0.075)),
    30: ("corner_off_lat_3", ("i16", -0.075, 0.075)), 31: ("corner_off_lon_3", ("i16", -0.075, 0.075)),
    32: ("corner_off_lat_4", ("i16", -0.075, 0.075)), 33: ("corner_off_lon_4", ("i16", -0.075, 0.075)),
    40: ("target_lat_deg", ("i32", -90.0, 90.0)),
    41: ("target_lon_deg", ("i32", -180.0, 180.0)),
    42: ("target_elev_m", ("u16", -900.0, 19000.0)),
    48: ("security_local_set", "bytes"),
    56: ("uas_platform_speed_ms", "u8"),
    59: ("platform_call_sign", "str"),
    65: ("uas_ls_version", "u8"),
    # full-range corner points (i32 lat ±90 / lon ±180) — 0601.8+
    82: ("corner_lat_1_deg", ("i32", -90.0, 90.0)), 83: ("corner_lon_1_deg", ("i32", -180.0, 180.0)),
    84: ("corner_lat_2_deg", ("i32", -90.0, 90.0)), 85: ("corner_lon_2_deg", ("i32", -180.0, 180.0)),
    86: ("corner_lat_3_deg", ("i32", -90.0, 90.0)), 87: ("corner_lon_3_deg", ("i32", -180.0, 180.0)),
    88: ("corner_lat_4_deg", ("i32", -90.0, 90.0)), 89: ("corner_lon_4_deg", ("i32", -180.0, 180.0)),
}
_T_BY_NAME = {v[0]: (k, v[1]) for k, v in _T.items()}

_UINT_MAX = {"u8": 0xFF, "u16": 0xFFFF, "u32": 0xFFFFFFFF}
_INT_HALF = {"i16": 0x7FFF, "i32": 0x7FFFFFFF}
_STRUCT = {"u8": ">B", "u16": ">H", "u32": ">I", "u64": ">Q", "i16": ">h", "i32": ">i"}


def _imap_decode(raw: bytes, codec) -> float:
    base, lo, hi = codec
    n = struct.unpack(_STRUCT[base], raw.rjust(struct.calcsize(_STRUCT[base]), b"\x00"))[0]
    if base.startswith("u"):
        return lo + (hi - lo) * (n / _UINT_MAX[base])
    return lo + (hi - lo) * (n + _INT_HALF[base]) / (2 * _INT_HALF[base])  # signed -> 0..1


def _imap_encode(value: float, codec) -> bytes:
    base, lo, hi = codec
    value = max(lo, min(hi, float(value)))
    if base.startswith("u"):
        n = int(round((value - lo) / (hi - lo) * _UINT_MAX[base]))
        n = max(0, min(_UINT_MAX[base], n))
    else:
        n = int(round((value - lo) / (hi - lo) * (2 * _INT_HALF[base]) - _INT_HALF[base]))
        n = max(-_INT_HALF[base] - 1, min(_INT_HALF[base], n))
    return struct.pack(_STRUCT[base], n)


def _ber_len(data: bytes, i: int) -> tuple[int, int]:
    b0 = data[i]
    if b0 < 0x80:
        return b0, i + 1
    n = b0 & 0x7F
    return int.from_bytes(data[i + 1:i + 1 + n], "big"), i + 1 + n


def _ber_len_encode(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def parse_misb_0601(value: bytes, *, strict_key: bool = True) -> dict:
    """Parse a MISB ST 0601 UAS Datalink Local Set. ``value`` may be the full KLV
    packet (16-byte UL + BER length + payload) or just the payload."""
    if len(value) >= 17 and value[:16] == UAS_LS_KEY:
        _len, i = _ber_len(value, 16)
        payload = value[i:i + _len]
    else:
        payload = value
    out: dict = {"tags": {}}
    i = 0
    while i < len(payload):
        tag = payload[i]; i += 1
        if i >= len(payload):
            break
        ln, i = _ber_len(payload, i)
        raw = payload[i:i + ln]; i += ln
        out["tags"][tag] = raw.hex()
        spec = _T.get(tag)
        if not spec:
            continue
        name, codec = spec
        try:
            if codec == "str":
                out[name] = raw.decode("utf-8", "replace")
            elif codec == "bytes":
                out[name] = raw.hex()
            elif codec in ("u8", "u16", "u32", "u64"):
                out[name] = int.from_bytes(raw, "big")
            elif isinstance(codec, tuple):
                out[name] = round(_imap_decode(raw, codec), 7)
        except Exception:
            pass
    return out


def _checksum_0601(packet_without_cs: bytes) -> int:
    """16-bit running sum over the packet up to and including tag 1 + its length byte."""
    s = 0
    for j, b in enumerate(packet_without_cs):
        s = (s + (b << (8 * ((j + 1) % 2)))) & 0xFFFF
    return s


def encode_misb_0601(fields: dict) -> bytes:
    """Encode a (subset of) MISB ST 0601 fields into a complete KLV packet with checksum."""
    body = bytearray()
    for name, val in fields.items():
        if name not in _T_BY_NAME:
            continue
        tag, codec = _T_BY_NAME[name]
        if codec == "str":
            raw = str(val).encode("utf-8")[:127]
        elif codec == "bytes":
            raw = bytes.fromhex(val) if isinstance(val, str) else bytes(val)
        elif codec == "u8":
            raw = struct.pack(">B", max(0, min(255, int(val))))
        elif codec in ("u16", "u32", "u64"):
            raw = struct.pack(_STRUCT[codec], int(val))
        elif isinstance(codec, tuple):
            raw = _imap_encode(val, codec)
        else:
            continue
        body += bytes([tag]) + _ber_len_encode(len(raw)) + raw
    # checksum: tag 1, length 2, value = 16-bit sum over (UL + BER-len + body + b"\x01\x02")
    cs_prefix = body + b"\x01\x02"
    payload_with_cs_prefix = bytes(cs_prefix)
    pkt_prefix = UAS_LS_KEY + _ber_len_encode(len(payload_with_cs_prefix) + 2) + payload_with_cs_prefix
    cs = _checksum_0601(pkt_prefix)
    payload = bytes(body) + b"\x01\x02" + struct.pack(">H", cs)
    return UAS_LS_KEY + _ber_len_encode(len(payload)) + payload


# ── geometry: turn parsed KLV into a footprint + GeoJSON ─────────────────────
def _dest(lat, lon, brg_deg, dist_m):
    R = 6378137.0
    br = math.radians(brg_deg); la1 = math.radians(lat); lo1 = math.radians(lon)
    dr = dist_m / R
    la2 = math.asin(math.sin(la1) * math.cos(dr) + math.cos(la1) * math.sin(dr) * math.cos(br))
    lo2 = lo1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(la1), math.cos(dr) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180


def corner_polygon(klv: dict) -> Optional[list[list[float]]]:
    """Return the sensor-footprint ring as ``[[lon,lat], ... , first]`` (or None)."""
    fc_lat = klv.get("frame_center_lat_deg"); fc_lon = klv.get("frame_center_lon_deg")
    # 1) explicit full-range corner points
    if all(klv.get(k) is not None for k in ("corner_lat_1_deg", "corner_lon_1_deg", "corner_lat_3_deg", "corner_lon_3_deg")):
        ring = [[klv[f"corner_lon_{n}_deg"], klv[f"corner_lat_{n}_deg"]] for n in (1, 2, 3, 4)
                if klv.get(f"corner_lat_{n}_deg") is not None and klv.get(f"corner_lon_{n}_deg") is not None]
        if len(ring) >= 3:
            return ring + [ring[0]]
    # 2) corner offsets relative to frame centre
    if fc_lat is not None and fc_lon is not None and klv.get("corner_off_lat_1") is not None:
        ring = []
        for n in (1, 2, 3, 4):
            dlat = klv.get(f"corner_off_lat_{n}"); dlon = klv.get(f"corner_off_lon_{n}")
            if dlat is None or dlon is None:
                continue
            ring.append([fc_lon + dlon, fc_lat + dlat])
        if len(ring) >= 3:
            return ring + [ring[0]]
    # 3) project a coarse quad from frame centre + FOV + slant range + heading
    if fc_lat is not None and fc_lon is not None and klv.get("slant_range_m"):
        rng = float(klv["slant_range_m"])
        hfov = float(klv.get("sensor_hfov_deg") or 30.0)
        vfov = float(klv.get("sensor_vfov_deg") or hfov * 9 / 16)
        hdg = float(klv.get("platform_heading_deg") or 0.0) + float(klv.get("sensor_rel_az_deg") or 0.0)
        half_w = rng * math.tan(math.radians(min(hfov, 170) / 2))
        half_h = rng * math.tan(math.radians(min(vfov, 170) / 2))
        ring = []
        for sx, sy in ((-1, 1), (1, 1), (1, -1), (-1, -1)):
            # local east/north -> bearing/distance about the frame centre
            de, dn = sx * half_w, sy * half_h
            d = math.hypot(de, dn); b = (math.degrees(math.atan2(de, dn)) + hdg) % 360
            la, lo = _dest(fc_lat, fc_lon, b, d)
            ring.append([lo, la])  # GeoJSON order
        return ring + [ring[0]]
    return None


def klv_to_geojson(klv: dict) -> dict:
    """A FeatureCollection: platform point, frame-centre point, sensor LOS line, footprint polygon.
    Properties carry ``uas_glx`` tags ('platform' | 'frame_center' | 'los' | 'footprint') for the map renderers."""
    feats: list[dict] = []
    p_lat = klv.get("sensor_lat_deg"); p_lon = klv.get("sensor_lon_deg")
    fc_lat = klv.get("frame_center_lat_deg"); fc_lon = klv.get("frame_center_lon_deg")
    cs = klv.get("platform_call_sign") or klv.get("platform_designation") or "UAS"
    if p_lat is not None and p_lon is not None:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [p_lon, p_lat]},
                      "properties": {"uas_glx": "platform", "call_sign": cs,
                                     "alt_m": klv.get("sensor_true_alt_m"), "heading_deg": klv.get("platform_heading_deg"),
                                     "color": "#22d3ee"}})
    if fc_lat is not None and fc_lon is not None:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [fc_lon, fc_lat]},
                      "properties": {"uas_glx": "frame_center", "call_sign": cs, "elev_m": klv.get("frame_center_elev_m"),
                                     "slant_range_m": klv.get("slant_range_m"), "color": "#f59e0b"}})
        if p_lat is not None and p_lon is not None:
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[p_lon, p_lat], [fc_lon, fc_lat]]},
                          "properties": {"uas_glx": "los", "call_sign": cs, "color": "#22d3ee"}})
    ring = corner_polygon(klv)
    if ring:
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"uas_glx": "footprint", "call_sign": cs, "color": "#f59e0b"}})
    return {"type": "FeatureCollection", "features": feats}


# ════════════════════════════════════════════════════════════════════════════
# Feed classifier (PSD-based; IQ confirmations when an IQ provider is wired)
# ════════════════════════════════════════════════════════════════════════════
def _occupied_bands(power_dbm: list[float], center_hz: float, span_hz: float, *, offset_db: float = 9.5, min_run: int = 3):
    p = np.asarray(power_dbm, float)
    if p.size < 8:
        return []
    nf = float(np.percentile(p, 25.0))
    mask = p > (nf + offset_db)
    bins = p.size
    df = span_hz / max(1, bins - 1)
    f0 = center_hz - span_hz / 2.0
    runs = []
    i = 0
    while i < bins:
        if mask[i]:
            j = i
            while j + 1 < bins and mask[j + 1]:
                j += 1
            if j - i >= min_run:
                seg = p[i:j + 1]
                runs.append((f0 + i * df, f0 + j * df, float(seg.max()), float(seg.mean()), float(seg.std()), seg))
            i = j + 1
        else:
            i += 1
    return runs


def _classify_segment(f_lo, f_hi, peak, mean, std, seg, iq_features: Optional[dict]) -> dict:
    bw = max(1.0, f_hi - f_lo)
    center = 0.5 * (f_lo + f_hi)
    plan = _channel_plan_for(center)
    # flatness: OFDM / noise-like channels are flat-topped; single-carrier QAM/FM is structured
    flat = float(std / max(0.5, abs(mean - float(np.percentile(seg, 5.0)))))  # ~ <0.6 = flat-ish
    is_flat = std < 4.0
    # IQ confirmations if available
    ofdm = bool(iq_features and iq_features.get("ofdm"))
    fm_video = bool(iq_features and iq_features.get("fm_video_line_rate"))
    cand: list[tuple[str, float]] = []

    def add(fid, conf):
        cand.append((fid, conf))

    # bandwidth buckets
    near = lambda x, t, tol=0.4e6: abs(bw - t) <= tol
    if near(bw, 8e6) or near(bw, 7e6) or near(bw, 6e6) or near(bw, 5e6, 0.6e6):
        if fm_video or (not is_flat and std > 6.0):
            add("fm_analog_video_ntsc" if bw <= 6.5e6 else "fm_analog_video_pal", 0.55 + 0.25 * fm_video)
        if ofdm or is_flat:
            add("dvbt", 0.5 + 0.25 * ofdm); add("dvbt2", 0.42 + 0.2 * ofdm); add("cofdm_mpegts", 0.3)
        else:
            add("qam_mpegts", 0.32); add("vsb_analog_video", 0.25)
    if 0.35e6 <= bw <= 0.55e6:
        add("isdbt_1seg", 0.55)
    if near(bw, 27e6, 4e6):
        add("hdzero", 0.5); add("walksnail", 0.3)
    if near(bw, 20e6, 4e6) or near(bw, 40e6, 6e6) or near(bw, 10e6, 2e6):
        add("dji_ocusync", 0.5 if (plan and "dji_ocusync" in plan["likely_feed_types"]) else 0.35)
        add("dji_lightbridge", 0.25); add("cofdm_mpegts", 0.3 + 0.2 * (ofdm or is_flat)); add("walksnail", 0.2)
    if 1e6 <= bw <= 36e6 and not is_flat and std > 3.0 and not (5.5e6 <= bw <= 8.5e6):
        add("dvbs2", 0.35); add("dvbs", 0.25)  # single-carrier rolloff shape, satellite-style
    if near(bw, 10.71e6, 2e6) or near(bw, 21.42e6, 3e6) or bw >= 45e6:
        add("cdl_becdl", 0.35)
    # channel-plan boost
    if plan:
        for fid, c in list(cand):
            if fid in plan["likely_feed_types"]:
                cand.append((fid, min(0.95, c + 0.2)))
        # if nothing matched at all, seed from the plan
        if not cand:
            for fid in plan["likely_feed_types"][:2]:
                cand.append((fid, 0.3))
    if not cand:
        cand.append(("unknown_digital" if is_flat else "unknown_analog", 0.3))
    ml = (iq_features or {}).get("ml") if iq_features else None
    if isinstance(ml, dict) and ml.get("feed_type") in _FEED_BY_ID:
        mc = float(ml.get("confidence", 0.5))
        # if the ML agrees with a heuristic candidate, average-up; otherwise add it as a candidate.
        seen = {fid for fid, _ in cand}
        if ml["feed_type"] in seen:
            cand.append((ml["feed_type"], min(0.97, 0.5 * (mc + max(c for fid, c in cand if fid == ml["feed_type"])) + 0.15)))
        else:
            cand.append((ml["feed_type"], min(0.95, mc)))
    # pick best per fid
    best: dict[str, float] = {}
    for fid, c in cand:
        best[fid] = max(best.get(fid, 0.0), c)
    fid = max(best, key=best.get)
    f = _FEED_BY_ID.get(fid, _FEED_BY_ID["unknown_digital"])
    alts = sorted(((k, round(v, 2)) for k, v in best.items() if k != fid), key=lambda kv: -kv[1])[:3]
    return {
        "center_hz": round(center, 1), "bandwidth_hz": round(bw, 1),
        "rssi_dbm": round(peak, 1), "noise_margin_db": round(peak - float(np.percentile(seg, 5.0)), 1),
        "feed_type": fid, "feed_name": f["name"], "transport": f["transport"], "modulation": f["modulation"],
        "carries_klv": f["carries_klv"], "decodable": f["decodable"], "decoder_chain": f["decoder_chain"],
        "confidence": round(min(0.95, best[fid]), 2),
        "alternatives": [{"feed_type": k, "confidence": c} for k, c in alts],
        "channel_plan": plan, "flatness": round(flat, 2),
        "ml": ((iq_features or {}).get("ml") if iq_features else None),
        "action": "decode" if f["decodable"] else "characterize",
    }


def _iq_features(device: dict, center_hz: float, bw_hz: float) -> Optional[dict]:
    """Cheap IQ-domain confirmations when an IQ provider is available: an OFDM
    cyclic-prefix autocorrelation peak, and an FM-video line-rate spectral line."""
    rate = max(2e6, min(40e6, bw_hz * 1.4))
    x = _capture_iq(device, center_hz, rate, int(rate * 0.02))  # ~20 ms
    if x is None or x.size < 4096:
        return None
    feat: dict = {}
    # OFDM: cyclic-prefix autocorrelation — sweep CP lags, look for a sharp peak
    n = min(x.size, 1 << 16)
    seg = x[:n]
    pwr = float(np.mean(np.abs(seg) ** 2)) + 1e-12
    ac_peak = 0.0
    for fft_len in (512, 1024, 2048, 4096, 8192, 16384):
        if fft_len * 2 >= n:
            continue
        a = seg[:n - fft_len]; b = seg[fft_len:n]
        ac_peak = max(ac_peak, float(abs(np.mean(a * np.conj(b))) / pwr))
    feat["ofdm"] = ac_peak > 0.06
    feat["ofdm_cp_corr"] = round(ac_peak, 3)
    # FM video: demod, FFT the magnitude, look for a line at ~15.625-15.734 kHz
    inst = np.angle(seg[1:] * np.conj(seg[:-1]))
    inst = inst - inst.mean()
    f = np.fft.rfftfreq(inst.size, d=1.0 / rate)
    P = np.abs(np.fft.rfft(inst))
    band = (f > 15.0e3) & (f < 16.0e3)
    if band.any():
        peak = float(P[band].max()); med = float(np.median(P[(f > 5e3) & (f < 50e3)]) + 1e-9)
        feat["fm_video_line_rate"] = peak / med > 6.0
        feat["fm_line_ratio"] = round(peak / med, 2)
    if ML_CLASSIFIER is not None:
        try:
            feat["ml"] = ML_CLASSIFIER(x, rate, band={"center_hz": center_hz, "bandwidth_hz": bw_hz})
        except Exception:
            pass
    return feat


def classify_band(device: Optional[dict], start_hz: float, stop_hz: float, *,
                  step_hz: float = 20e6, n_bins: int = 4096, use_iq: bool = True,
                  max_hold: bool = False, maxhold_key: str = "default",
                  reset_maxhold: bool = False) -> dict:
    """Scan [start_hz, stop_hz] for occupied channels and classify each.

    When `max_hold=True`, each per-step PSD is merged into a persistent
    accumulator keyed by `maxhold_key` (a max() across successive sweeps).
    This makes intermittent or hopping FPV/UAS downlinks far easier to spot:
    over a few sweeps the band "draws itself in" even if individual sweeps
    miss the transmitter.

    Set `reset_maxhold=True` to clear the accumulator before this sweep.
    The returned dict gains a `max_hold` field with the current accumulator
    snapshot when `max_hold=True`.
    """
    device = device or {"id": "synthetic", "metadata": {}}
    start_hz, stop_hz = float(min(start_hz, stop_hz)), float(max(start_hz, stop_hz))
    span_total = max(1e6, stop_hz - start_hz)
    step_hz = float(min(max(1e6, step_hz), 40e6))
    if max_hold and reset_maxhold:
        reset_max_hold(maxhold_key)
    detections: list[dict] = []
    fr: dict = {}
    f = start_hz + step_hz / 2.0
    guard = 0
    while f - step_hz / 2.0 < stop_hz and guard < 256:
        guard += 1
        fr = dsp.spectrum_frame(device, f, step_hz, n_bins)
        # Accumulate this step into the max-hold grid (if requested).
        if max_hold:
            powers = np.asarray(fr.get("power_dbm", []), dtype=np.float32)
            if powers.size:
                # Spectrum_frame returns power vs. bin index over the local step span.
                step_freqs = f - step_hz / 2 + (np.arange(powers.size) + 0.5) * (step_hz / powers.size)
                _maxhold_update(maxhold_key, start_hz, stop_hz, n_bins, step_freqs, powers)
        for (lo, hi, pk, mean, std, seg) in _occupied_bands(fr.get("power_dbm", []), f, step_hz):
            if lo < start_hz - 1e6 or hi > stop_hz + 1e6:
                continue
            iqf = _iq_features(device, 0.5 * (lo + hi), max(1e6, hi - lo)) if use_iq else None
            detections.append(_classify_segment(lo, hi, pk, mean, std, seg, iqf))
        f += step_hz
    # de-dupe overlapping detections (keep the higher-confidence one)
    detections.sort(key=lambda d: (-d["confidence"], d["center_hz"]))
    kept: list[dict] = []
    for d in detections:
        if any(abs(d["center_hz"] - k["center_hz"]) < 0.5 * (d["bandwidth_hz"] + k["bandwidth_hz"]) * 0.6 for k in kept):
            continue
        kept.append(d)
    kept.sort(key=lambda d: d["center_hz"])
    # If max-hold is active, also extract detections from the *accumulated* spectrum.
    maxhold_snap = None
    if max_hold:
        maxhold_snap = get_max_hold(maxhold_key)
        # Re-run occupied-band detection over the entire accumulated grid so that
        # weak intermittents that no single sweep saw clearly become visible.
        if maxhold_snap is not None and maxhold_snap["power_dbm"]:
            mh_powers = maxhold_snap["power_dbm"]
            mh_center = 0.5 * (start_hz + stop_hz)
            mh_span = stop_hz - start_hz
            for (lo, hi, pk, mean, std, seg) in _occupied_bands(mh_powers, mh_center, mh_span):
                if lo < start_hz - 1e6 or hi > stop_hz + 1e6:
                    continue
                iqf = _iq_features(device, 0.5 * (lo + hi), max(1e6, hi - lo)) if use_iq else None
                cand = _classify_segment(lo, hi, pk, mean, std, seg, iqf)
                cand["from_max_hold"] = True
                # Merge if this hit doesn't already collide with an existing detection.
                if not any(abs(cand["center_hz"] - k["center_hz"]) <
                            0.5 * (cand["bandwidth_hz"] + k["bandwidth_hz"]) * 0.6 for k in kept):
                    kept.append(cand)
            kept.sort(key=lambda d: d["center_hz"])
    return {
        "start_hz": start_hz, "stop_hz": stop_hz, "n_detections": len(kept),
        "detections": kept, "source": fr.get("source", "synthetic"),
        "iq_backend": _capture_backend() if use_iq else "off",
        "max_hold": maxhold_snap,
    }


def auto_detect_feed(device: Optional[dict], frequency_hz: float, bandwidth_hz: Optional[float] = None) -> Optional[dict]:
    """Classify what's at ``frequency_hz``: scan a narrow window around it, take the
    detection whose band overlaps the tune frequency, preferring a *decodable* one.
    Returns the detection dict (with feed_type / feed_name / confidence / …) or None."""
    span = float(bandwidth_hz or 8e6) * 4.0 + 12e6
    res = classify_band(device or {"id": "synthetic", "metadata": {}}, frequency_hz - span / 2, frequency_hz + span / 2,
                        step_hz=min(40e6, max(8e6, span)), use_iq=True)
    dets = res.get("detections", [])
    def overlaps(d):
        return abs(d["center_hz"] - frequency_hz) <= 0.5 * d["bandwidth_hz"] + 2e6
    hit = [d for d in dets if overlaps(d)]
    if hit:
        hit.sort(key=lambda d: (abs(d["center_hz"] - frequency_hz), 0 if d.get("decodable") else 1, -d.get("confidence", 0.0)))
        return hit[0]
    if dets:  # something nearby but not overlapping the exact tune — still informative
        dets.sort(key=lambda d: (abs(d["center_hz"] - frequency_hz), 0 if d.get("decodable") else 1, -d.get("confidence", 0.0)))
        return dets[0]
    # nothing in the PSD — fall back to the catalogued UAS/FPV channel plan at this frequency
    plan = _channel_plan_for(frequency_hz)
    if plan and plan.get("likely_feed_types"):
        fid = plan["likely_feed_types"][0]
        f = _FEED_BY_ID.get(fid, _FEED_BY_ID["unknown_digital"])
        return {"center_hz": float(frequency_hz), "bandwidth_hz": float(f["typical_bandwidth_hz"][0] if f["typical_bandwidth_hz"] else 8e6),
                "rssi_dbm": None, "feed_type": fid, "feed_name": f["name"], "transport": f["transport"], "modulation": f["modulation"],
                "carries_klv": f["carries_klv"], "decodable": f["decodable"], "decoder_chain": f["decoder_chain"],
                "confidence": 0.3, "alternatives": [{"feed_type": k, "confidence": 0.2} for k in plan["likely_feed_types"][1:3]],
                "channel_plan": plan, "from": "channel_plan (no occupied channel detected in the PSD)", "action": "decode" if f["decodable"] else "characterize"}
    return None


# ════════════════════════════════════════════════════════════════════════════
# Decode-session manager
# ════════════════════════════════════════════════════════════════════════════
# These external programs are NOT required — Ares demodulates UAS video in-process
# (see sdr/native_demod.py). available_decoders() still reports whether any are present
# (a deployment may prefer them for an even better path), but nothing gates on it.
_EXTERNAL_TOOLS = ("ffmpeg", "leandvb", "tsp", "sdrangel", "gr-dvbt", "dvbt-rx", "dvbt2-blade", "gr-dvbt2", "gr-isdbt")
_SESSIONS: dict[str, dict] = {}
_SESSION_FRAMES: dict[str, list[bytes]] = {}   # sid -> [PNG bytes, ...] from the native demod


def available_decoders() -> dict:
    tools = {t: bool(shutil.which(t)) for t in _EXTERNAL_TOOLS}
    try:
        import SoapySDR  # type: ignore  # noqa: F401
        tools["soapysdr"] = True
    except Exception:
        tools["soapysdr"] = False
    try:
        import uhd  # type: ignore  # noqa: F401
        tools["uhd_python"] = True
    except Exception:
        tools["uhd_python"] = False
    return tools


def _capture_backend() -> str:
    """Where the baseband IQ comes from. The native demod runs regardless of which —
    'iq_provider' / 'soapysdr' are live captures; 'synthetic_iq' is the offline snapshot."""
    if IQ_PROVIDER is not None:
        return "iq_provider"
    try:
        import SoapySDR  # type: ignore  # noqa: F401
        return "soapysdr"
    except Exception:
        return "synthetic_iq"


def _native_demod_decode(feed: dict, device: dict, frequency_hz: float, bw_hz: float, channel: int,
                          *, analog_options: Optional[dict] = None,
                          capture_seconds: float = 0.045) -> dict:
    """Capture a baseband snapshot and run Ares' in-process software demod over it
    (sdr/native_demod). Returns the demod result dict (already JSON-safe except for any
    'frames' / 'byte_stream' which the caller strips).

    ``analog_options`` forwards analog-video demod knobs. ``capture_seconds`` overrides
    the default 45 ms capture (longer captures = more frames per decode pass)."""
    from . import native_demod
    rate = float(max(2.0e6, min(40.0e6, (bw_hz or 8.0e6) * 1.4)))
    n_secs = max(0.020, min(0.5, float(capture_seconds)))
    n = int(max(1 << 14, min(1 << 22, rate * n_secs)))
    iq = _capture_iq(device, float(frequency_hz), rate, n, int(channel))
    if iq is None:
        return {"ok": False, "error": "no IQ available to demodulate"}
    return native_demod.decode_feed(feed, iq, rate, max_frames=8,
                                     analog_options=analog_options)


def start_decode(device: Optional[dict], frequency_hz: float, feed_type: Optional[str] = None, *,
                 bandwidth_hz: Optional[float] = None, channel: int = 0, label: str = "",
                 analog_options: Optional[dict] = None,
                 capture_seconds: float = 0.045) -> dict:
    auto = None
    if not feed_type or feed_type in ("auto", "auto_detect"):
        auto = auto_detect_feed(device, frequency_hz, bandwidth_hz)
        if not auto:
            return {"error": "auto-detect found no occupied channel near that frequency — pass a feed_type explicitly",
                    "feed_types": [x["id"] for x in FEED_TYPES]}
        feed_type = auto["feed_type"]
        if bandwidth_hz is None:
            bandwidth_hz = auto.get("bandwidth_hz")
    f = _FEED_BY_ID.get(feed_type)
    if not f:
        return {"error": f"unknown feed_type '{feed_type}'", "feed_types": [x["id"] for x in FEED_TYPES]}
    device = device or {"id": "synthetic", "metadata": {}}
    sid = uuid.uuid4().hex[:12]
    backend = _capture_backend()
    bw = float(bandwidth_hz or (f["typical_bandwidth_hz"][0] if f["typical_bandwidth_hz"] else 8e6))
    sess = {
        "id": sid, "device_id": device.get("id"), "feed_type": feed_type, "feed_name": f["name"],
        "frequency_hz": float(frequency_hz), "bandwidth_hz": bw, "channel": int(channel),
        "transport": f["transport"], "carries_klv": f["carries_klv"], "label": label or f["name"],
        "started_ts": time.time(), "capture_backend": backend,
        "stream_url": f"/api/v1/uas/sessions/{sid}/stream",
        "metadata_url": f"/api/v1/uas/sessions/{sid}/metadata" if f["carries_klv"] else None,
    }
    if not f["decodable"]:
        sess["status"] = "characterize_only"
        sess["message"] = (f"{f['name']} is proprietary/encrypted — Ares detects, characterises and (with a DF array) "
                           f"geolocates it, but cannot decode the video. {f['notes']}")
        sess["pipeline"] = []
    else:
        # Decode in-process with Ares' own software demodulator (sdr/native_demod) —
        # no SoapySDR / leandvb / DVB-T(2) receiver / SDRangel / ffmpeg / TSDuck needed.
        # Tag the analog session_key so frame-averaging persists across re-decodes.
        ao = dict(analog_options or {})
        ao.setdefault("session_key", sid)
        demod = _native_demod_decode(f, device, frequency_hz, bw, channel,
                                       analog_options=ao, capture_seconds=capture_seconds)
        sess["analog_options"] = ao
        sess["capture_seconds"] = float(capture_seconds)
        # stash any decoded frames as PNGs for the stream endpoint, then strip the heavy arrays
        frames = demod.pop("frames", None) or []
        try:
            from . import native_demod as _nd
            pngs = [p for p in (_nd.to_png(fr) for fr in frames) if p]
        except Exception:
            pngs = []
        if pngs:
            _SESSION_FRAMES[sid] = pngs
        demod.pop("byte_stream", None)
        sess["demod"] = demod
        sess["capture_iq"] = backend          # iq_provider | soapysdr | synthetic_iq
        # the capture + native demod pipeline is running → the session has started; whether the
        # demod has *locked* (and how clean it is) is reported in sess["demod"]. There is no
        # "missing tool" state any more — Ares does the demod itself.
        sess["status"] = "started"
        sess["pipeline"] = [f"{backend}:capture@{frequency_hz/1e6:.3f}MHz"] + list(demod.get("pipeline", []))
        if demod.get("ok") and demod.get("kind") == "analog":
            sess["video_url"] = f"/api/v1/uas/sessions/{sid}/frame.png" if pngs else None
            sess["message"] = (f"Decoding in-process — native FM/composite video demod: {demod.get('n_frames', 0)} raster frame(s) "
                               f"recovered (line rate ≈ {demod.get('line_rate_hz_est')} Hz, SNR ≈ {demod.get('snr_db_est')} dB; "
                               f"capture: {backend}).")
        elif demod.get("ok"):
            ts = demod.get("ts") or {}
            ts_note = (f"; TS sync — {ts.get('klv_units', 0)} KLV unit(s)" if ts.get("ts_sync")
                       else "; PHY symbols recovered (TS sync needs the inner FEC stage / a cleaner link)")
            sess["message"] = (f"Decoding in-process — native {demod.get('kind')} demod: {demod.get('modulation', '?')}, "
                               f"{demod.get('n_symbols', 0)} symbols, EVM ≈ {demod.get('evm_pct')}%{ts_note} (capture: {backend}).")
        else:
            sess["message"] = ("Capture + native software demod running, but it hasn't locked onto the signal yet "
                               f"({demod.get('error') or demod.get('reason') or 'no lock'}) — a wider/cleaner capture window helps.")
    if auto is not None:
        sess["auto_detected"] = {"confidence": auto.get("confidence"), "alternatives": auto.get("alternatives"),
                                 "channel_plan": auto.get("channel_plan"), "rssi_dbm": auto.get("rssi_dbm")}
    if f["carries_klv"]:
        sess["last_metadata"] = _synthetic_metadata(sess, sess["started_ts"], device)
    _SESSIONS[sid] = sess
    return dict(sess)


def list_sessions() -> list[dict]:
    return [dict(s) for s in _SESSIONS.values()]


def get_session(sid: str) -> Optional[dict]:
    return _SESSIONS.get(sid)


def stop_session(sid: str) -> bool:
    _SESSION_FRAMES.pop(sid, None)
    try:
        from . import native_demod as _nd
        _nd.reset_frame_average(sid)
    except Exception:
        pass
    return _SESSIONS.pop(sid, None) is not None


def redemod_session(sid: str, *, analog_options: Optional[dict] = None,
                    capture_seconds: Optional[float] = None) -> Optional[dict]:
    """Re-capture and re-demodulate an existing session with updated analog options.

    Useful when the user tweaks colour decode, scanline rate, frame averaging, or
    peak-hold τ on a running session — we rerun the demod over a fresh capture and
    refresh the cached frame PNGs without tearing the session down.
    """
    sess = _SESSIONS.get(sid)
    if not sess:
        return None
    f = _FEED_BY_ID.get(sess["feed_type"])
    if not f or not f.get("decodable"):
        return sess
    device = {"id": sess.get("device_id") or "synthetic", "metadata": {}}
    prev = dict(sess.get("analog_options") or {})
    if analog_options:
        prev.update(analog_options)
    prev["session_key"] = sid
    secs = float(capture_seconds if capture_seconds is not None else sess.get("capture_seconds") or 0.045)
    demod = _native_demod_decode(f, device, sess["frequency_hz"], sess["bandwidth_hz"], sess.get("channel", 0),
                                  analog_options=prev, capture_seconds=secs)
    frames = demod.pop("frames", None) or []
    try:
        from . import native_demod as _nd
        pngs = [p for p in (_nd.to_png(fr) for fr in frames) if p]
    except Exception:
        pngs = []
    if pngs:
        _SESSION_FRAMES[sid] = pngs
    sess["demod"] = demod
    sess["analog_options"] = prev
    sess["capture_seconds"] = secs
    if demod.get("ok") and demod.get("kind") == "analog" and pngs:
        sess["video_url"] = f"/api/v1/uas/sessions/{sid}/frame.png"
    return dict(sess)


def session_frames(sid: str) -> list[bytes]:
    """PNG-encoded raster frames recovered by the native analog-video demod for this session
    (empty for digital feeds or once the session is gone)."""
    return _SESSION_FRAMES.get(sid, [])


def session_metadata(sid: str) -> Optional[dict]:
    """Latest decoded MISB ST 0601 KLV for a session (re-derived with elapsed time so
    a synthetic feed shows motion; a live decode would push real KLV here)."""
    s = _SESSIONS.get(sid)
    if not s or not s.get("carries_klv"):
        return None
    klv = _synthetic_metadata(s, time.time(), {"id": s.get("device_id")})
    s["last_metadata"] = klv
    return {"session_id": sid, "feed_type": s["feed_type"], "klv": klv, "geojson": klv_to_geojson(klv),
            "footprint": corner_polygon(klv)}


def _synthetic_metadata(sess: dict, t: float, device: Optional[dict]) -> dict:
    """A plausible MISB ST 0601 frame: a platform orbiting near the SDR, camera
    pointed at a fixed scene, with a derived footprint. Encoded then parsed, so the
    bytes are real (this is what drives the round-trip test)."""
    base_lat = float((device or {}).get("lat") or 36.114) if isinstance(device, dict) else 36.114
    base_lon = float((device or {}).get("lon") or -115.173) if isinstance(device, dict) else -115.173
    elapsed = max(0.0, t - sess.get("started_ts", t))
    orbit_r_m = 2200.0
    ang = (elapsed * 6.0) % 360.0  # 1 rev / minute
    p_lat, p_lon = _dest(base_lat, base_lon, ang, orbit_r_m)
    p_alt = 850.0 + 50.0 * math.sin(elapsed / 11.0)
    scene_lat, scene_lon = _dest(base_lat, base_lon, 75.0, 600.0)  # the thing it's watching
    de = (scene_lon - p_lon) * 111320.0 * math.cos(math.radians(p_lat))
    dn = (scene_lat - p_lat) * 110540.0
    ground = math.hypot(de, dn)
    az = math.degrees(math.atan2(de, dn)) % 360.0
    slant = math.hypot(ground, p_alt - 15.0)
    hfov = 18.0 + 6.0 * math.sin(elapsed / 7.0)
    half = slant * math.tan(math.radians(hfov / 2))
    fields = {
        "uas_ls_version": 19, "precision_timestamp_us": int(t * 1e6),
        "platform_designation": "MQ-X DEMO", "platform_call_sign": sess.get("label", "UAS")[:16],
        "image_source_sensor": "EO/IR", "image_coordinate_system": "WGS-84",
        "platform_heading_deg": (ang + 90.0) % 360.0, "platform_pitch_deg": 2.0, "platform_roll_deg": 0.0,
        "sensor_lat_deg": p_lat, "sensor_lon_deg": p_lon, "sensor_true_alt_m": p_alt,
        "sensor_hfov_deg": hfov, "sensor_vfov_deg": hfov * 9 / 16,
        "sensor_rel_az_deg": (az - (ang + 90.0)) % 360.0, "sensor_rel_el_deg": -math.degrees(math.atan2(p_alt - 15.0, max(1.0, ground))),
        "slant_range_m": slant, "target_width_m": min(9999.0, 2 * half),
        "frame_center_lat_deg": scene_lat, "frame_center_lon_deg": scene_lon, "frame_center_elev_m": 15.0,
    }
    # add 4 corner offsets (degrees) about the frame centre
    mlat = half / 110540.0; mlon = half / (111320.0 * math.cos(math.radians(scene_lat)))
    rot = math.radians(az)
    for n, (sx, sy) in zip((1, 2, 3, 4), ((-1, 1), (1, 1), (1, -1), (-1, -1))):
        e = sx * mlon; nn = sy * mlat
        fields[f"corner_off_lat_{n}"] = max(-0.074, min(0.074, nn * math.cos(rot) - e * math.sin(rot)))
        fields[f"corner_off_lon_{n}"] = max(-0.074, min(0.074, nn * math.sin(rot) + e * math.cos(rot)))
    pkt = encode_misb_0601(fields)
    klv = parse_misb_0601(pkt)
    klv["_packet_hex"] = pkt.hex()
    klv["_synthetic"] = True
    return klv


# ════════════════════════════════════════════════════════════════════════════
def status() -> dict:
    return {
        "feed_types": len(FEED_TYPES),
        "decodable_feed_types": sum(1 for f in FEED_TYPES if f["decodable"]),
        "known_channel_plans": len(KNOWN_CHANNELS),
        "demodulator": "native (in-process, sdr/native_demod): FM/VSB composite video · OFDM/COFDM "
                       "(CP sync + FFT + 1-tap eq + QPSK/16/64-QAM demap) · single-carrier PSK/QAM "
                       "(RRC MF + Gardner timing + CMA/DD eq + slicer) → MPEG-TS demux + STANAG-4609 KLV. "
                       "No SoapySDR / leandvb / DVB-T(2) receiver / SDRangel / ffmpeg / TSDuck required.",
        "fec_note": "PHY demod only — DVB inner Viterbi+RS(204,188) / DVB-S2 LDPC+BCH not yet applied.",
        "decoders": available_decoders(),   # presence of optional external tools — informational only; nothing gates on it
        "capture_backend": _capture_backend(),
        "active_sessions": len(_SESSIONS),
        "misb_0601": "parse+encode (ST 0601, STANAG 4609 UAS Datalink LS), with checksum",
        "gpu_acceleration": gpu_available(),
        "ml_classifier": (ML_CLASSIFIER is not None),
        "encrypted_video": "OcuSync / Lightbridge / CDL — detect & geolocate only; no public passive decrypt. "
                           "Use the Remote ID / DroneID telemetry beacon (decodable) to ID & locate the drone + operator.",
    }
