# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/dsp.py — spectrum, DF-accuracy estimate, and the audio-decode registry/bridge
for the SDR console (Workstream D).

Real RF capture (a power-spectral-density frame, coherent IQ for DF, baseband for
audio decode) requires the radio's native driver — SoapySDR / rtl-sdr / libsidekiq /
the krakensdr DAQ — which is hardware- and OS-specific and outside a code-only build.
This module therefore:
  * **defines the data path** the DF UI and the SDR pipeline consume — a PSD frame
    is ``{center_hz, span_hz, n_bins, power_dbm:[...], noise_floor_dbm, peak_hz, peak_dbm}``;
  * provides a **synthetic spectrum generator** (a realistic noise floor + a couple
    of signals) so the DF panel works end-to-end before hardware is wired, clearly
    flagged ``source:"synthetic"``;
  * exposes a hook (``SPECTRUM_PROVIDER``) a SoapySDR/rtl-sdr capture layer can
    register to feed real frames;
  * gives the **DF-accuracy estimate** the device-setup UI shows (expected LoB σ
    from channel count + array geometry, via the interferometry CRLB);
  * holds the **audio-decode mode registry** (DMR/dPMR, P25 P1/P2, TETRA, NXDN,
    D-STAR, M17, POCSAG/FLEX, plain analog NFM/AM/SSB, …) and a **bridge** that
    shells out to an installed open-source decoder (op25, dsd-fme, sdrtrunk,
    tetra-rx, multimon-ng, …) — reporting cleanly when none is available rather
    than pretending to decode.
"""
from __future__ import annotations

import math
import shutil
import time
from typing import Callable, Optional

import numpy as np

# A capture layer (SoapySDR/rtl-sdr/libsidekiq/krakensdr DAQ) can register a
# callable (device_dict, center_hz, span_hz, n_bins) -> dict to feed real PSDs.
SPECTRUM_PROVIDER: Optional[Callable] = None


def set_spectrum_provider(fn: Optional[Callable]) -> None:
    global SPECTRUM_PROVIDER
    SPECTRUM_PROVIDER = fn


# A coherent-IQ provider (sdr/iq_capture registers one when SoapySDR is present) — used by the
# native DF / AoA solver to pull phase-aligned multi-channel baseband from a connected SDR.
# Signature: fn(device_dict, center_hz, rate_hz, n_samples, channels=(0,1,...)) -> list[np.ndarray] | np.ndarray | None
IQ_PROVIDER: Optional[Callable] = None


def set_iq_provider(fn: Optional[Callable]) -> None:
    global IQ_PROVIDER
    IQ_PROVIDER = fn


def _synthetic_coherent_iq(geom_n: int, n_samples: int, az_deg: float = 75.0, el_deg: float = 5.0,
                           freq_hz: float = 433.92e6, snr_db: float = 18.0, seed: int = 0) -> np.ndarray:
    """An n_samples × geom_n complex IQ block for an array steered (modelled) at (az,el) — drives the
    native DF path offline. Deterministic per seed; clearly synthetic."""
    rng = np.random.default_rng(int(seed) ^ 0x5DF)
    s = (rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples)).astype(np.complex64)
    # a uniform circular array with ~0.4λ element spacing as the default offline geometry
    lam = 299_792_458.0 / max(1.0, float(freq_hz))
    r = 0.4 * lam / (2.0 * np.sin(np.pi / max(2, geom_n)))
    th = 2 * np.pi * np.arange(geom_n) / max(1, geom_n)
    pos = np.stack([r * np.cos(th), r * np.sin(th), np.zeros(geom_n)], axis=1)
    k = 2 * np.pi / lam
    azr, elr = np.radians(az_deg), np.radians(el_deg)
    u = np.array([np.cos(elr) * np.cos(azr), np.cos(elr) * np.sin(azr), np.sin(elr)])
    steer = np.exp(1j * k * (pos @ u)).astype(np.complex64)             # geom_n phasors
    sig = np.outer(s, steer)                                            # n_samples × geom_n
    npow = 10.0 ** (-snr_db / 10.0)
    noise = (rng.standard_normal(sig.shape) + 1j * rng.standard_normal(sig.shape)).astype(np.complex64) * np.sqrt(npow / 2.0)
    return sig + noise


def solve_aoa_live(device: Optional[dict], frequency_hz: float, geometry_spec: dict, *,
                   n_snapshots: int = 4096, channels: Optional[list[int]] = None,
                   method: str = "music") -> dict:
    """Native, in-process angle-of-arrival: captures coherent multi-channel IQ from the SDR (via the
    registered IQ provider, else a synthetic block), then runs the array DF solver
    (``df.interferometry.aoa_from_snapshots`` — MUSIC / Capon / Bartlett). No external app."""
    try:
        from app.core.df.interferometry import geometry_from_spec, aoa_from_snapshots, ArrayGeometry
    except Exception as e:  # pragma: no cover
        return {"error": f"DF solver unavailable: {e}"}
    spec = geometry_spec or {}
    try:
        geom = geometry_from_spec(spec) if spec.get("type") else ArrayGeometry.uca(int(spec.get("n", 5)), 0.4 * (299_792_458.0 / max(1.0, float(frequency_hz))) / (2.0 * np.sin(np.pi / max(2, int(spec.get("n", 5))))))
    except Exception:
        geom = ArrayGeometry.uca(5, 0.4 * (299_792_458.0 / max(1.0, float(frequency_hz))) / (2.0 * np.sin(np.pi / 5)))
    nch = geom.n
    chans = list(channels) if channels else list(range(nch))
    rate = max(1.0e6, min(20.0e6, geometry_spec.get("sample_rate_hz", 2.4e6)))
    n = int(max(1024, min(1 << 18, n_snapshots)))
    src = "synthetic_iq"
    iq = None
    if IQ_PROVIDER is not None:
        try:
            iq = IQ_PROVIDER(device or {}, float(frequency_hz), float(rate), n, tuple(chans))
            if iq is not None:
                src = "iq_provider"
        except Exception:
            iq = None
    if iq is None:
        # a synthetic coherent block (modelled at a plausible AoA) so the native path runs offline
        iq = _synthetic_coherent_iq(nch, n, freq_hz=float(frequency_hz))
    # normalise to an (n_snapshots × n_channels) array
    arr = np.asarray(iq)
    if isinstance(iq, list):
        m = min(len(a) for a in iq)
        arr = np.stack([np.asarray(a[:m], np.complex64) for a in iq], axis=1)
    elif arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    elif arr.shape[0] < arr.shape[1]:
        arr = arr.T
    arr = arr[:, :nch] if arr.shape[1] >= nch else arr
    try:
        # aoa_from_snapshots wants (geom, freq, snapshots) and is forgiving about the (N×K)/(K×N) orientation
        res = aoa_from_snapshots(geom, float(frequency_hz), arr, method=(method or "music").lower())
    except Exception as e:  # pragma: no cover
        return {"error": f"AoA solve failed: {e}", "iq_source": src, "channels": int(arr.shape[1])}
    out = {
        "azimuth_deg": getattr(res, "az_deg", None), "elevation_deg": getattr(res, "el_deg", None),
        "azimuth_sigma_deg": getattr(res, "sigma_az_deg", None), "elevation_sigma_deg": getattr(res, "sigma_el_deg", None),
        "az_true_deg": getattr(res, "az_true_deg", None),
        "quality": getattr(res, "quality", None), "snr_db": getattr(res, "snr_db", None),
        "ambiguities": getattr(res, "ambiguities", None), "spectrum": getattr(res, "spectrum", None),
        "method": method, "snapshots": int(max(arr.shape)), "channels": int(min(arr.shape)),
        "frequency_hz": float(frequency_hz), "iq_source": src,
        "geometry": {"type": getattr(geom, "name", "custom"), "n": nch},
        "synthetic": src == "synthetic_iq",
    }
    return {k: v for k, v in out.items() if v is not None}


# ── synthetic spectrum (until hardware is wired) ─────────────────────────────
def _synthetic_psd(center_hz: float, span_hz: float, n_bins: int, t: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(int(t) // 2 ^ seed)        # changes every couple of seconds
    floor = -110.0 + 5.0 * math.sin(t / 7.0 + seed)        # gently breathing noise floor
    psd = floor + rng.normal(0.0, 2.0, n_bins)
    f = center_hz + (np.arange(n_bins) / max(1, n_bins - 1) - 0.5) * span_hz
    # a few stable-ish carriers at deterministic offsets from centre + one wandering one
    carriers = [(-0.30, 12e3, -62.0), (0.08, 6.25e3, -54.0), (0.42, 8e3, -71.0),
                (0.18 + 0.05 * math.sin(t / 11.0 + seed), 5e3, -58.0 + 6.0 * math.sin(t / 3.0))]
    for frac, bw, pk in carriers:
        fc = center_hz + frac * span_hz
        psd = np.maximum(psd, pk - 30.0 * ((f - fc) / max(1.0, bw)) ** 2)
    return psd


def spectrum_frame(device: dict, center_hz: float, span_hz: float, n_bins: int = 1024,
                   channel: int = 0) -> dict:
    """Return one PSD frame for a device's channel. Uses the registered hardware
    provider if any, else a synthetic frame (``source:"synthetic"``)."""
    n_bins = max(64, min(8192, int(n_bins)))
    span_hz = max(1e3, float(span_hz))
    if SPECTRUM_PROVIDER is not None:
        try:
            fr = SPECTRUM_PROVIDER(device, center_hz, span_hz, n_bins, channel)
            if isinstance(fr, dict) and "power_dbm" in fr:
                fr.setdefault("source", "hardware")
                fr.setdefault("center_hz", center_hz)
                fr.setdefault("span_hz", span_hz)
                fr.setdefault("channel", channel)
                return fr
        except Exception:
            pass
    psd = _synthetic_psd(float(center_hz), span_hz, n_bins, time.time(), seed=int(channel) * 101)
    peak_i = int(np.argmax(psd))
    f = float(center_hz) + (peak_i / max(1, n_bins - 1) - 0.5) * span_hz
    return {
        "source": "synthetic", "channel": int(channel),
        "center_hz": float(center_hz), "span_hz": span_hz, "n_bins": n_bins,
        "power_dbm": [round(float(v), 2) for v in psd],
        "noise_floor_dbm": round(float(np.percentile(psd, 20.0)), 2),
        "peak_hz": round(f, 1), "peak_dbm": round(float(psd[peak_i]), 2),
        "t": time.time(),
    }


# ── DF accuracy estimate (CRLB) for the device-setup UI ──────────────────────
def lob_accuracy_estimate(channels: int, *, array_type: str = "uca", spacing_wavelengths: float = 0.4,
                          frequency_hz: float = 433.92e6, snr_db: float = 15.0, snapshots: int = 256) -> dict:
    """Expected LoB azimuth σ (deg) and rough CEP-at-1-km for a single observer,
    from the array geometry + channel count via the interferometry CRLB. Single
    channel ⇒ no DF (returns ``can_df: False``)."""
    n = max(1, int(channels))
    if n < 2:
        return {"channels": n, "can_df": False, "sigma_az_deg": None,
                "note": "a single-channel SDR can monitor a spectrum but cannot produce a line of bearing — DF needs ≥2 coherent channels"}
    try:
        from app.core.df.interferometry import ArrayGeometry, model_phase_diff, _crlb_phase
        lam = 299_792_458.0 / max(1.0, float(frequency_hz))
        if array_type.lower() == "ula":
            geom = ArrayGeometry.ula(n, spacing_wavelengths * lam)
        else:
            # a UCA with the requested element spacing → solve for the radius
            r = spacing_wavelengths * lam / (2.0 * math.sin(math.pi / n)) if n >= 3 else spacing_wavelengths * lam
            geom = ArrayGeometry.uca(n, max(0.01, r))
        sigma_phase_rad = 1.0 / math.sqrt(2.0 * max(0.5, 10.0 ** (snr_db / 10.0)) * max(1, snapshots))
        s_az, _ = _crlb_phase(geom, frequency_hz, 90.0, 0.0, sigma_phase_rad, 0,
                              not (geom.is_collinear or geom.is_planar_horizontal))
        # average over a few azimuths so a UCA's slight bearing-dependence is smoothed
        vals = []
        for az in (10.0, 70.0, 130.0, 200.0, 280.0, 340.0):
            sa, _ = _crlb_phase(geom, frequency_hz, az, 0.0, sigma_phase_rad, 0,
                                not (geom.is_collinear or geom.is_planar_horizontal))
            vals.append(sa)
        crlb = float(np.mean(vals))
        # the CRLB is a theoretical lower bound; real arrays carry phase-/amplitude-
        # calibration error and mutual coupling. Fold in a ~2.5° practical floor so the
        # estimate reflects the field, not the textbook (and shrink it a touch with N).
        floor = 2.5 * (5.0 / max(2, n)) ** 0.25
        s_az = math.hypot(crlb, floor)
        cep_1km_m = 1000.0 * math.tan(math.radians(s_az))
        return {"channels": n, "can_df": True, "array_type": array_type, "spacing_wavelengths": spacing_wavelengths,
                "frequency_hz": frequency_hz, "assumed_snr_db": snr_db, "assumed_snapshots": snapshots,
                "crlb_sigma_az_deg": round(crlb, 2), "calibration_floor_deg": round(floor, 2),
                "sigma_az_deg": round(s_az, 2), "cep_at_1km_m": round(cep_1km_m, 0),
                "note": f"≈{round(s_az,1)}° 1-σ bearing accuracy expected with {n} channels at SNR {snr_db:.0f} dB "
                        f"(CRLB floor {round(crlb,1)}° + ~{round(floor,1)}° calibration/coupling); more channels and/or "
                        "longer baselines tighten it"}
    except Exception as e:  # pragma: no cover
        return {"channels": n, "can_df": True, "sigma_az_deg": None, "note": f"estimate unavailable: {e}"}


# ── audio-decode mode registry + external-decoder bridge ─────────────────────
# (mode id, label, family, the open-source decoder programs that can do it)
AUDIO_MODES = [
    {"id": "nfm", "label": "Narrowband FM (analog PMR/LMR, PTT)", "family": "analog", "decoders": ["builtin"]},
    {"id": "wfm", "label": "Wideband FM (broadcast, ~200 kHz, mono)", "family": "analog", "decoders": ["builtin"]},
    {"id": "am", "label": "AM (aero / CB)", "family": "analog", "decoders": ["builtin"]},
    {"id": "usb", "label": "SSB upper sideband", "family": "analog", "decoders": ["builtin"]},
    {"id": "lsb", "label": "SSB lower sideband", "family": "analog", "decoders": ["builtin"]},
    {"id": "dmr", "label": "DMR (Tier I/II/III, incl. Capacity+/Connect+/XPT)", "family": "digital", "decoders": ["dsd-fme", "sdrtrunk", "dsdplus"]},
    {"id": "dpmr", "label": "dPMR", "family": "digital", "decoders": ["dsd-fme", "dsdplus"]},
    {"id": "p25p1", "label": "APCO P25 Phase 1", "family": "digital", "decoders": ["op25", "dsd-fme", "sdrtrunk"]},
    {"id": "p25p2", "label": "APCO P25 Phase 2 (TDMA)", "family": "digital", "decoders": ["op25", "sdrtrunk"]},
    {"id": "tetra", "label": "TETRA (TMO/DMO)", "family": "digital", "decoders": ["tetra-rx", "telive", "sdrtrunk"]},
    {"id": "nxdn48", "label": "NXDN 4800 (IDAS / NEXEDGE narrow)", "family": "digital", "decoders": ["dsd-fme", "sdrtrunk"]},
    {"id": "nxdn96", "label": "NXDN 9600 (IDAS / NEXEDGE wide)", "family": "digital", "decoders": ["dsd-fme", "sdrtrunk"]},
    {"id": "dstar", "label": "D-STAR", "family": "digital", "decoders": ["dsd-fme"]},
    {"id": "ysf", "label": "Yaesu System Fusion (C4FM)", "family": "digital", "decoders": ["dsd-fme"]},
    {"id": "m17", "label": "M17 (open-source digital voice)", "family": "digital", "decoders": ["m17-tools", "dsd-fme"]},
    {"id": "provoice", "label": "EDACS ProVoice", "family": "digital", "decoders": ["dsd-fme", "dsdplus"]},
    {"id": "pocsag", "label": "POCSAG paging", "family": "data", "decoders": ["multimon-ng"]},
    {"id": "flex", "label": "FLEX paging", "family": "data", "decoders": ["multimon-ng"]},
    {"id": "ais", "label": "AIS (marine)", "family": "data", "decoders": ["rtl_ais", "AIS-catcher"]},
    {"id": "acars", "label": "ACARS (aero data)", "family": "data", "decoders": ["acarsdec"]},
    {"id": "ads-b", "label": "ADS-B (1090 MHz)", "family": "data", "decoders": ["dump1090"]},
]
_DECODER_PROGRAMS = sorted({d for m in AUDIO_MODES for d in m["decoders"] if d != "builtin"})

# Logical name → list of binaries that count as "this decoder is installed".
# Necessary because the packaged binary names don't always match the logical
# decoder name (e.g. dump1090 ships as dump1090-fa or dump1090-mutability;
# rtl_ais on some distros is rtl-ais; the m17 demod from mobilinkd installs
# as m17-demod / m17-mod, not "m17-tools").
_DECODER_ALIASES = {
    "dsd-fme":     ["dsd-fme", "dsd_fme"],
    "dsdplus":     ["dsdplus", "DSDPlus"],
    "op25":        ["op25_rx", "op25"],          # boatbod/op25 ships op25_rx; "op25" is a launcher dir name on some distros
    "sdrtrunk":    ["sdrtrunk"],                 # only the launcher symlink — not 'java' (false positive on every Linux)
    "tetra-rx":    ["tetra-rx", "tetra_demod"],
    "telive":      ["telive"],
    "m17-tools":   ["m17-demod", "m17-mod", "m17-tools"],
    "multimon-ng": ["multimon-ng"],
    "acarsdec":    ["acarsdec"],
    "rtl_ais":     ["rtl_ais", "rtl-ais"],
    "AIS-catcher": ["AIS-catcher", "ais-catcher"],
    "dump1090":    ["dump1090", "dump1090-fa", "dump1090-mutability"],
    # Cellular decoders + WiFi/BLE capture tools (Targets-tab feeders)
    "gr-gsm":      ["grgsm_livemon_headless", "grgsm_decode", "grgsm_capture"],
    "lte-sniffer": ["LTESniffer", "lte-sniffer"],
    "5g-sniffer":  ["5g_sniffer", "5GSniffer", "5g-sniffer"],    # spritelab/5GSniffer ships `5g_sniffer`
    "srsran-ue":   ["srsue"],
    "hcxdumptool": ["hcxdumptool"],
    "airodump-ng": ["airodump-ng"],
    "btmon":       ["btmon"],
    "bluetoothctl":["bluetoothctl"],
}


def _decoder_present(logical: str) -> bool:
    """True if any binary matching the logical decoder name is on PATH."""
    cands = _DECODER_ALIASES.get(logical, [logical, logical.replace("-", "_")])
    return any(shutil.which(c) for c in cands)


def available_decoders() -> list[str]:
    """Which external decoder programs are actually on the PATH right now —
    matched via the alias table so 'dump1090' resolves to dump1090-fa /
    dump1090-mutability, 'm17-tools' resolves to mobilinkd's m17-demod, etc."""
    return [p for p in _DECODER_PROGRAMS if _decoder_present(p)]


def audio_mode_info() -> dict:
    avail = set(available_decoders())
    out = []
    for m in AUDIO_MODES:
        ds = m["decoders"]
        ready = ("builtin" in ds) or any(d in avail for d in ds)
        out.append({**m, "ready": ready, "available_decoders": [d for d in ds if d == "builtin" or d in avail]})
    return {"modes": out, "external_decoders_present": sorted(avail),
            "note": ("Analog modes (NFM/AM/SSB) decode in-process once a baseband stream is wired; digital "
                     "modes (DMR/P25/TETRA/NXDN/…) are decoded by shelling out to an installed open-source "
                     "decoder — dsd-fme, op25, sdrtrunk, tetra-rx, multimon-ng — none of which can be vendored "
                     "(AMBE/ACELP vocoders are licensed). Install one and Ares will route the baseband to it.")}


def start_audio_decode(device: dict, frequency_hz: float, mode: str) -> dict:
    """Begin (or describe how to begin) decoding a transmission. With a real
    baseband capture + an installed decoder this would spawn it and return a stream
    handle; without either it returns a clear status so the UI can tell the operator."""
    m = next((x for x in AUDIO_MODES if x["id"] == mode), None)
    if m is None:
        return {"status": "error", "error": f"unknown mode {mode!r}; see GET /api/v1/sdr/audio/modes"}
    avail = available_decoders()
    if "builtin" in m["decoders"]:
        return {"status": "needs_baseband", "mode": mode,
                "detail": f"{m['label']}: ready to decode in-process — needs the SDR baseband stream wired (SoapySDR/rtl-sdr capture layer)."}
    usable = [d for d in m["decoders"] if d in avail]
    if not usable:
        return {"status": "decoder_unavailable", "mode": mode,
                "detail": f"{m['label']}: install one of {m['decoders']} (open-source) — Ares will then route the {frequency_hz/1e6:.4f} MHz baseband to it.",
                "install_hint": {"dsd-fme": "github.com/lwvmobile/dsd-fme", "op25": "osmocom/op25",
                                 "sdrtrunk": "github.com/DSheirer/sdrtrunk", "tetra-rx": "osmocom-tetra",
                                 "multimon-ng": "github.com/EliasOenal/multimon-ng"}}
    return {"status": "needs_baseband", "mode": mode, "decoder": usable[0],
            "detail": f"{m['label']}: {usable[0]} is installed — needs the SDR baseband stream wired to pipe into it."}
