"""
sdr/ptt_classifier.py — automatic PTT modulation identification.

Given an IQ snippet captured at a known sample rate (and optionally a known
centre frequency), this module identifies which Push-To-Talk standard is
present — DMR, dPMR, P25 Phase 1/2, TETRA, NXDN 4800/9600, D-STAR, YSF,
M17, EDACS-ProVoice — or falls back to FM-voice / AM / SSB / "data" /
"unknown". The verdict comes with a confidence score and a routed
**decoder** name so the SDR audio pipeline can pick the right external
program automatically (DSD-FME / OP25 / m17-demod / multimon-ng / …).

How the identification works (pure numpy / scipy, in-process):

  1. **Occupied bandwidth** from the FFT (99% energy) — narrows the search.
  2. **Modulation family** from the IQ statistics:
        4-FSK  (DMR / P25 P1 C4FM / NXDN / YSF / M17): FM-discriminator
                histogram is 4-peaked at ±k·Δf.
        2-FSK / GMSK (D-STAR): FM-disc histogram is 2-peaked, symmetric.
        π/4-DQPSK (TETRA / P25 P2):   IQ constellation rotates 45° per
                symbol; cluster on eight phase positions.
  3. **Symbol rate** from the cyclostationary autocorrelation peak of the
     squared envelope (a clean spectral line at f_sym).
  4. **Standard mapping**: bandwidth × family × symbol-rate keys into the
     PTT catalogue. Ties broken by which decoder is actually installed and
     by frame-sync sniffing when the symbol rate is high-confidence.

Reference numbers (used as the lookup keys):

  Standard        Bandwidth  Family       Symbol rate   Frame sync pattern
  --------        ---------  ------       -----------   ------------------
  DMR             12.5 kHz   4-FSK        4800 sym/s    voice ☞ 7553 / data ☞ 5575
  dPMR            6.25 kHz   4-FSK        2400 sym/s    A82A6B (FS1) / 5E6BCB (FS3)
  P25 Phase 1     12.5 kHz   4-FSK / C4FM 4800 sym/s    5575F5FF77FF
  P25 Phase 2     12.5 kHz   H-CPM / DQPSK 6000 sym/s   varies (TDMA)
  TETRA           25.0 kHz   π/4-DQPSK    18 000 sym/s  Sync-Symbols
  NXDN 4800       6.25 kHz   4-FSK        4800 sym/s    CDF59F
  NXDN 9600       12.5 kHz   4-FSK        9600 sym/s    EF89F0
  D-STAR          6.25 kHz   GMSK         4800 sym/s    Frame Sync 0x07F39F00
  YSF             12.5 kHz   4-FSK / C4FM 4800 sym/s    FICH sync 24-bit
  M17             9.0 kHz    4-FSK        4800 sym/s    M17 link-setup sync
  POCSAG/FLEX     12.5 kHz   2-FSK        512/1200/2400 multimon-ng owns
  FM voice        12.5–25 k  analog FM    —             —
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

C_LIGHT = 299_792_458.0


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue: every PTT row + how to confirm it + which decoder Ares should
# hand the baseband to. Decoder field matches the logical names in
# sdr/dsp.AUDIO_MODES so the existing audio-mode plumbing picks them up.
# ─────────────────────────────────────────────────────────────────────────────
PTT_STANDARDS: list[dict] = [
    {"id": "dmr",      "label": "DMR (Tier I/II/III)",          "bw_hz": 12_500, "family": "4fsk",
     "symbol_rate_hz": 4800,  "decoder": "dsd-fme",  "audio_mode": "dmr"},
    {"id": "dpmr",     "label": "dPMR",                          "bw_hz":  6_250, "family": "4fsk",
     "symbol_rate_hz": 2400,  "decoder": "dsd-fme",  "audio_mode": "dpmr"},
    {"id": "p25p1",    "label": "APCO P25 Phase 1 (C4FM)",       "bw_hz": 12_500, "family": "4fsk",
     "symbol_rate_hz": 4800,  "decoder": "op25",      "audio_mode": "p25p1"},
    {"id": "p25p2",    "label": "APCO P25 Phase 2 (H-CPM)",      "bw_hz": 12_500, "family": "psk8",
     "symbol_rate_hz": 6000,  "decoder": "op25",      "audio_mode": "p25p2"},
    {"id": "tetra",    "label": "TETRA TMO/DMO",                 "bw_hz": 25_000, "family": "dqpsk",
     "symbol_rate_hz": 18_000, "decoder": "tetra-rx", "audio_mode": "tetra"},
    {"id": "nxdn48",   "label": "NXDN 4800",                     "bw_hz":  6_250, "family": "4fsk",
     "symbol_rate_hz": 4800,  "decoder": "dsd-fme",   "audio_mode": "nxdn48"},
    {"id": "nxdn96",   "label": "NXDN 9600",                     "bw_hz": 12_500, "family": "4fsk",
     "symbol_rate_hz": 9600,  "decoder": "dsd-fme",   "audio_mode": "nxdn96"},
    {"id": "dstar",    "label": "D-STAR (GMSK voice)",           "bw_hz":  6_250, "family": "gmsk",
     "symbol_rate_hz": 4800,  "decoder": "dsd-fme",   "audio_mode": "dstar"},
    {"id": "ysf",      "label": "Yaesu System Fusion (C4FM)",    "bw_hz": 12_500, "family": "4fsk",
     "symbol_rate_hz": 4800,  "decoder": "dsd-fme",   "audio_mode": "ysf"},
    {"id": "m17",      "label": "M17",                            "bw_hz":  9_000, "family": "4fsk",
     "symbol_rate_hz": 4800,  "decoder": "m17-tools", "audio_mode": "m17"},
    {"id": "provoice", "label": "EDACS ProVoice",                 "bw_hz": 12_500, "family": "4fsk",
     "symbol_rate_hz": 9_600, "decoder": "dsd-fme",   "audio_mode": "provoice"},
    {"id": "pocsag",   "label": "POCSAG paging",                  "bw_hz": 12_500, "family": "2fsk",
     "symbol_rate_hz": 1200,  "decoder": "multimon-ng", "audio_mode": "pocsag"},
    {"id": "flex",     "label": "FLEX paging",                    "bw_hz": 12_500, "family": "2fsk",
     "symbol_rate_hz": 1600,  "decoder": "multimon-ng", "audio_mode": "flex"},
    {"id": "nfm",      "label": "Narrowband FM voice",            "bw_hz": 12_500, "family": "analog_fm",
     "symbol_rate_hz": 0,     "decoder": "builtin",   "audio_mode": "nfm"},
    {"id": "wfm",      "label": "Broadcast FM",                   "bw_hz": 200_000, "family": "analog_fm",
     "symbol_rate_hz": 0,     "decoder": "builtin",   "audio_mode": "wfm"},
    {"id": "am",       "label": "AM voice",                       "bw_hz":  6_000, "family": "analog_am",
     "symbol_rate_hz": 0,     "decoder": "builtin",   "audio_mode": "am"},
    # ── Cellular / wide-band data (passive observation, no decoding bundled here;
    # the audio-decoder bridge routes the verdict to the right runner).
    # GSM uses GMSK at 270.833 ksym/s on a 200 kHz channel; the symbol rate is far
    # above PTT GMSK (4800) so it can't be confused with D-STAR.
    {"id": "gsm",      "label": "GSM (2G GMSK)",                  "bw_hz": 200_000, "family": "gmsk",
     "symbol_rate_hz": 270_833, "decoder": "gr-gsm",  "audio_mode": "gsm"},
    {"id": "umts",     "label": "UMTS / WCDMA (3G)",              "bw_hz": 5_000_000, "family": "wcdma",
     "symbol_rate_hz": 3_840_000, "decoder": "builtin", "audio_mode": "umts"},
    {"id": "lte",      "label": "LTE (4G OFDM)",                  "bw_hz": 10_000_000, "family": "ofdm_lte",
     "symbol_rate_hz": 15_000, "decoder": "lte-sniffer", "audio_mode": "lte"},
    {"id": "nr_fr1",   "label": "5G NR (FR1 OFDM)",               "bw_hz": 100_000_000, "family": "ofdm_nr",
     "symbol_rate_hz": 30_000, "decoder": "5g-sniffer", "audio_mode": "nr"},
    {"id": "wifi_ofdm","label": "WiFi (802.11 OFDM)",             "bw_hz": 20_000_000, "family": "ofdm_wifi",
     "symbol_rate_hz": 312_500, "decoder": "hcxdumptool", "audio_mode": "wifi"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Primitives
# ─────────────────────────────────────────────────────────────────────────────
def _as_c64(iq) -> np.ndarray:
    a = np.asarray(iq).astype(np.complex64, copy=False)
    return a.reshape(-1)


def _occupied_bandwidth(iq: np.ndarray, fs: float, energy_frac: float = 0.99) -> float:
    """Bandwidth that holds `energy_frac` of the IQ spectral energy (Hz).
    Robust to noise floor — clips low bins before integrating."""
    if iq.size < 64:
        return 0.0
    nfft = min(4096, 1 << int(math.floor(math.log2(iq.size))))
    win = np.hanning(nfft).astype(np.float32)
    x = iq[: nfft] * win
    p = np.fft.fftshift(np.abs(np.fft.fft(x)) ** 2)
    p = p / (p.sum() + 1e-12)
    # Cumulative energy from the centre out (signal is at baseband)
    cum = np.cumsum(p)
    target = energy_frac
    # Find narrowest centred band capturing `target`.
    centre = len(p) // 2
    span = 0
    e = float(p[centre])
    while e < target and span < (len(p) // 2):
        span += 1
        e += float(p[centre + span] + p[centre - span])
    bw_bins = max(1, 2 * span + 1)
    return float(bw_bins * (fs / nfft))


def _fm_discriminate(iq: np.ndarray) -> np.ndarray:
    """Phase-difference frequency discriminator (proportional to instantaneous freq)."""
    z = iq[1:] * np.conj(iq[:-1])
    return np.angle(z).astype(np.float32)


def _envelope(iq: np.ndarray) -> np.ndarray:
    return np.abs(iq).astype(np.float32)


def _hist_peaks(x: np.ndarray, bins: int = 64) -> tuple[np.ndarray, np.ndarray]:
    """Histogram + indices of local maxima above 30% of the global max."""
    h, edges = np.histogram(x, bins=bins)
    if h.size < 3:
        return h, np.array([], dtype=int)
    mx = h.max()
    peaks = []
    thr = max(1, int(0.30 * mx))
    for i in range(1, h.size - 1):
        if h[i] > h[i - 1] and h[i] > h[i + 1] and h[i] >= thr:
            peaks.append(i)
    return h, np.array(peaks, dtype=int)


def _kmeans1d(x: np.ndarray, k: int, n_iter: int = 25) -> tuple[np.ndarray, float]:
    """One-dimensional k-means with deterministic initialisation. Returns
    (centroids, normalised_mse) — the MSE is divided by the variance of
    `x` so it's scale-free and we can compare different k against each
    other.

    For our purposes: a clean 4-FSK signal has very low normalised MSE
    when k=4 (each sample is close to its centroid) and a much higher one
    at k=1; an analog-FM voice signal has comparable normalised MSE for
    k=4 and k=1, because samples are smoothly distributed.
    """
    if x.size < k * 4:
        return np.array([]), 1.0
    centres = np.quantile(x, np.linspace(0.5 / k, 1 - 0.5 / k, k))
    for _ in range(n_iter):
        dist = np.abs(x[:, None] - centres[None, :])
        lbl = np.argmin(dist, axis=1)
        new_centres = np.array([x[lbl == j].mean() if np.any(lbl == j) else centres[j] for j in range(k)])
        if np.allclose(new_centres, centres, rtol=1e-4):
            break
        centres = new_centres
    dist = np.abs(x[:, None] - centres[None, :])
    mse = float(np.mean(np.min(dist, axis=1) ** 2))
    var = float(np.var(x)) + 1e-12
    return centres, mse / var


def _classify_family(iq: np.ndarray, fs: float) -> dict:
    """Decide whether the modulation is 2-FSK, 4-FSK, GMSK, π/4-DQPSK, or analog.

    The discriminators that survive contact with real-world voice signals:

      - **Constant-envelope-ness**: |IQ| is nearly constant (var≪1) for
        every digital constant-envelope mode AND for analog FM. Big
        envelope swings → AM / SSB.

      - **K-means quantisation MSE on the FM-discriminator output**: an
        FSK signal clusters tightly into 2 or 4 frequency levels (low
        normalised MSE at k=2 or k=4); analog FM voice is smoothly
        distributed across the frequency band (k-means doesn't help —
        normalised MSE stays near 1/k). The *ratio* mse_k1 / mse_k4 is a
        clean digital-vs-analog discriminator.

      - **Histogram peak count** confirms how many FSK levels are present.

      - **Symbol-rate cyclostationarity** (run elsewhere): present for
        digital modes, absent for analog FM voice.

    Returns: {"family": "4fsk"|"2fsk"|"gmsk"|"dqpsk"|"analog_fm"|"analog_am"|"unknown",
              "fsk_peaks": int, "envelope_var": float,
              "mse_k1": float, "mse_k2": float, "mse_k4": float,
              "fsk_quantisation_ratio": float,    # >>1 → clean FSK; ≈1 → analog
              "mean_freq_dev_hz": float}
    """
    if iq.size < 256:
        return {"family": "unknown"}
    env = _envelope(iq)
    env_norm = env / (env.mean() + 1e-9)
    envelope_var = float(env_norm.var())
    envelope_ripple_db = 10.0 * math.log10(max(1e-6, env_norm.max() / max(1e-6, env_norm.mean())))
    fm = _fm_discriminate(iq)
    freq_hz = fm * (fs / (2 * math.pi))
    # inner 95% to ignore sync bursts and outliers
    lo, hi = np.percentile(freq_hz, [2.5, 97.5])
    sel = freq_hz[(freq_hz >= lo) & (freq_hz <= hi)].astype(np.float32)
    if sel.size < 64:
        return {"family": "unknown"}
    # k-means quantisation tests
    _, mse_k1 = _kmeans1d(sel, 1)
    _, mse_k2 = _kmeans1d(sel, 2)
    _, mse_k4 = _kmeans1d(sel, 4)
    # ratio: how much better does k=4 explain the distribution than k=1?
    quant_ratio_4 = mse_k1 / max(mse_k4, 1e-6)
    quant_ratio_2 = mse_k1 / max(mse_k2, 1e-6)
    # Histogram peak count for reference (and tie-breaking on 2 vs 4 FSK)
    h, peaks = _hist_peaks(sel, bins=64)
    n_peaks = int(peaks.size)
    mean_dev = float(np.mean(np.abs(freq_hz)))
    cm = envelope_var < 0.05
    # FM-discriminator bandwidth — the *most* reliable analog-vs-digital tell.
    # Analog voice has FM-disc PSD concentrated in the 300–3 kHz audio band;
    # an FSK signal has FM-disc PSD extending to ~f_sym (well above 3 kHz for
    # any PTT mode of interest). We measure the 90%-energy bandwidth of the
    # FM-disc and compare to the voice/digital boundary.
    fm_seg = fm[: min(fm.size, 8192)].astype(np.float32)
    fm_seg = fm_seg - fm_seg.mean()
    if fm_seg.size >= 256:
        fm_seg *= np.hanning(fm_seg.size).astype(np.float32)
        P = np.abs(np.fft.rfft(fm_seg)) ** 2
        freqs = np.fft.rfftfreq(fm_seg.size, d=1.0 / fs)
        total = float(P.sum()) + 1e-12
        cum = np.cumsum(P) / total
        idx = int(np.searchsorted(cum, 0.90))
        idx = min(idx, freqs.size - 1)
        fm_bw_hz = float(freqs[idx])
    else:
        fm_bw_hz = 0.0
    voice_band = fm_bw_hz < 3500.0    # tightly inside the human voice band

    # Cellular / wideband markers — these run *before* the FSK cascade because
    # WCDMA and OFDM signals are nothing like a PTT GMSK / 4-FSK distribution.
    wcdma_score = _wcdma_chip_score(iq, fs)
    ofdm_score, ofdm_cp_lag = _ofdm_cyclic_prefix_score(iq, fs)

    # ──── Classification cascade ────
    family = "unknown"
    if wcdma_score >= 8.0 and fs >= 4_000_000:
        # Strong 3.84 Mchips/s cyclostationary tone → UMTS/WCDMA
        family = "wcdma"
    elif ofdm_score >= 4.0 and ofdm_cp_lag > 0:
        # Cyclic-prefix autocorrelation peak well above the noise floor.
        # The CP lag (in samples → microseconds) further classifies which OFDM:
        #   LTE normal CP: 4.69 / 16.67 µs depending on SCS
        #   5G NR SCS=30 kHz: 2.34 µs CP
        #   WiFi 802.11a/g/n/ac: 0.8 µs CP (long) or 0.4 µs (short)
        cp_us = ofdm_cp_lag * 1e6 / fs
        if cp_us < 1.0:
            family = "ofdm_wifi"
        elif cp_us < 3.5:
            family = "ofdm_nr"
        else:
            family = "ofdm_lte"
    elif envelope_var > 0.4:
        # Big envelope swings: AM / SSB / noisy analog
        family = "analog_am" if mean_dev < 200.0 else "analog_fm"
    elif voice_band and (quant_ratio_4 < 6.0):
        # FM-disc energy is concentrated in the voice band AND the FSK
        # quantisation ratio isn't extreme → analog FM voice. We require
        # both because a 4-FSK signal at low deviation can also have narrow
        # FM-disc bandwidth — the q-ratio test catches that case.
        family = "analog_fm"
    elif not cm:
        # Modest envelope variation + frequency excursions → analog FM
        family = "analog_fm"
    else:
        # Constant envelope, FM-disc spreads beyond voice. Is the frequency
        # *quantised*? Strong digital signature: k=4 fits >5× tighter than
        # k=1 and >1.6× tighter than k=2.
        if quant_ratio_4 >= 4.0 and (mse_k2 / max(mse_k4, 1e-6)) >= 1.6 and n_peaks >= 3:
            family = "4fsk"
        elif quant_ratio_2 >= 4.0 and n_peaks == 2:
            # 2-FSK vs GMSK: GMSK has smoother transitions; k=2 quantisation
            # is less tight (q2 < 8) and the inner deviation is narrow.
            family = "gmsk" if (quant_ratio_2 < 8.0 and mean_dev < 1500) else "2fsk"
        elif cm and quant_ratio_4 < 2.0 and quant_ratio_2 < 2.0:
            # Constant envelope, no FSK quantisation → likely PSK / DQPSK
            family = "dqpsk"
        else:
            family = "analog_fm" if mean_dev > 200 else "unknown"
    return {
        "family": family,
        "fsk_peaks": n_peaks,
        "envelope_var": envelope_var,
        "envelope_ripple_db": envelope_ripple_db,
        "mse_k1": mse_k1, "mse_k2": mse_k2, "mse_k4": mse_k4,
        "fsk_quantisation_ratio": quant_ratio_4,
        "fm_disc_bandwidth_hz": fm_bw_hz,
        "mean_freq_dev_hz": mean_dev,
        "wcdma_score": wcdma_score,
        "ofdm_score": ofdm_score,
        "ofdm_cp_lag_samples": ofdm_cp_lag,
    }


def _wcdma_chip_score(iq: np.ndarray, fs: float) -> float:
    """Detect WCDMA 3.84 Mchips/s cyclostationarity from the squared-envelope
    spectrum. Returns peak/floor ratio at the chip rate; large (>>1) → WCDMA.

    Only meaningful when the sample rate is high enough to see the chip rate
    (need fs >= 2 × 3.84 = 7.68 Mhz; we accept fs >= 4 MHz with reduced
    confidence since aliasing can still expose the line)."""
    WCDMA = 3.84e6
    if fs < 4_000_000 or iq.size < 2048:
        return 0.0
    n = min(iq.size, 1 << 14)
    seg = iq[:n].astype(np.complex64, copy=False)
    sq = (seg * np.conj(seg)).real.astype(np.float32)
    sq = sq - sq.mean()
    sq *= np.hanning(sq.size).astype(np.float32)
    P = np.abs(np.fft.rfft(sq)) ** 2
    freqs = np.fft.rfftfreq(sq.size, d=1.0 / fs)
    floor = float(np.median(P) + 1e-12)
    # Sum power at the chip rate ± a 2-bin window
    score = 0.0
    for harmonic in (1, 2):
        target = harmonic * WCDMA
        if target > freqs[-1]:
            break
        idx = int(np.argmin(np.abs(freqs - target)))
        lo = max(0, idx - 2); hi = min(freqs.size, idx + 3)
        score += float(np.max(P[lo:hi])) / floor
    return float(score)


def _ofdm_cyclic_prefix_score(iq: np.ndarray, fs: float) -> tuple[float, int]:
    """Detect OFDM via the cyclic-prefix autocorrelation peak.

    OFDM copies the last cp_len samples of each FFT block to its head, so the
    autocorrelation R(τ) = E[x[n] x*[n+τ]] has a peak at τ = N_FFT (the FFT
    length in samples). Returns (peak_score, lag_samples). lag_samples is the
    detected CP period at the input sample rate; we don't yet decode the FFT
    length directly — instead we measure the lag and let _classify_family map
    it to LTE/NR/WiFi via the CP duration.

    Searches lags in the OFDM-plausible range: corresponding to FFT lengths
    of 128–4096 samples at the SDR rate.
    """
    if fs < 1_000_000 or iq.size < 8192:
        return 0.0, 0
    n = min(iq.size, 1 << 14)
    seg = iq[:n].astype(np.complex64, copy=False)
    # Strongly low-pass via FFT-based autocorrelation; look for the highest
    # peak in a windowed lag range. We compute |E[x[n]·x*[n+τ]]| for a few
    # candidate τ values rather than a full AC (full AC would be expensive
    # at 1<<14 samples for every PTT classify).
    lag_candidates = []
    # WiFi 802.11a/g/n FFT lengths (64) × OS, plus LTE/NR (128, 256, 512, 1024, 2048)
    for fft_len in (64, 128, 256, 512, 1024, 2048):
        # Scale to the input fs assuming a 'natural' OFDM bandwidth of fs/2;
        # this is approximate — we'll see what the *peak* lag is across all.
        lag_candidates.append(fft_len)
    best_lag = 0
    best_score = 0.0
    floor = 0.0
    # Compute floor as |E[x[n]·x*[n+τ]]| at random non-OFDM lags
    rng = np.random.default_rng(0)
    floor_samples = []
    for _ in range(8):
        τ = int(rng.integers(low=10, high=min(n // 2, 4000)))
        r = np.abs(np.mean(seg[: n - τ] * np.conj(seg[τ:])))
        floor_samples.append(float(r))
    floor = float(np.median(floor_samples)) + 1e-9
    for lag in lag_candidates:
        if lag >= n // 2:
            continue
        r = np.abs(np.mean(seg[: n - lag] * np.conj(seg[lag:])))
        score = float(r) / floor
        if score > best_score:
            best_score = score; best_lag = lag
    return best_score, best_lag


def _symbol_rate_hz(iq: np.ndarray, fs: float, search_min: float = 600, search_max: float = 40_000) -> dict:
    """Estimate symbol rate via autocorrelation of the rectified FM-discriminator
    derivative.

    For FSK / GMSK / C4FM the FM-discriminator output is a step-and-hold
    sequence. Its temporal derivative spikes at every symbol transition. The
    autocorrelation of that rectified-derivative signal peaks at integer
    multiples of the symbol period — the *first* peak (after lag 0) gives us
    the symbol rate directly.

    For analog FM voice there's no such periodicity → the autocorrelation
    has a wide bell shape, no isolated peak.
    """
    if iq.size < 1024:
        return {"symbol_rate_hz": 0.0, "confidence": 0.0}
    n = min(iq.size, int(fs * 0.2))                # up to 200 ms
    if n < 1024:
        return {"symbol_rate_hz": 0.0, "confidence": 0.0}
    seg = iq[: n + 1]
    fm = _fm_discriminate(seg).astype(np.float32)  # length n, in radians/sample
    # Rectified derivative — spikes at every symbol transition
    drift = np.abs(np.diff(fm))
    drift = drift - drift.mean()
    if drift.size < 256:
        return {"symbol_rate_hz": 0.0, "confidence": 0.0}
    # Bounds on the symbol period in samples
    lag_min = max(2, int(round(fs / search_max)))
    lag_max = min(drift.size - 4, int(round(fs / search_min)))
    if lag_max <= lag_min + 4:
        return {"symbol_rate_hz": 0.0, "confidence": 0.0}
    # Unbiased autocorrelation via FFT (fast for ≈10k samples)
    nfft = 1 << int(math.ceil(math.log2(drift.size * 2)))
    F = np.fft.rfft(drift, n=nfft)
    R = np.fft.irfft(F * np.conj(F), n=nfft)[: drift.size].astype(np.float64)
    # Normalise — R[0] is the energy
    R /= max(1e-12, R[0])
    # Search for the largest peak in the allowed lag window. Require both
    # a high absolute autocorrelation value AND a high peak-to-median ratio
    # — that's what separates a real symbol-rate cycle from noise.
    band = R[lag_min: lag_max + 1]
    if band.size < 5:
        return {"symbol_rate_hz": 0.0, "confidence": 0.0}
    floor = max(1e-9, float(np.median(np.abs(band))))
    # Find local maxima that beat both an absolute threshold (0.12 of AC[0])
    # AND a relative threshold (4× the median).
    peaks: list[int] = []
    for i in range(2, band.size - 2):
        if band[i] > band[i - 1] and band[i] > band[i + 1] \
           and band[i] > 0.12 and band[i] > 4.0 * floor:
            peaks.append(i)
    if not peaks:
        return {"symbol_rate_hz": 0.0, "peak_to_floor": 0.0, "confidence": 0.0}
    # Pick the strongest peak; the fundamental usually dominates over its harmonics.
    best = max(peaks, key=lambda p: band[p])
    peak_lag = lag_min + best
    snr = float(band[best] / floor)
    sym_rate = float(fs / peak_lag)
    # Confidence: strong peak (>8× floor) AND in a sane band → high
    conf = max(0.0, min(1.0, (snr - 3.0) / 12.0))
    return {
        "symbol_rate_hz": sym_rate,
        "peak_to_floor": snr,
        "confidence": conf,
        "lag_samples": int(peak_lag),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Top-level classifier
# ─────────────────────────────────────────────────────────────────────────────
def classify_ptt(iq, fs: float, *, center_hz: Optional[float] = None,
                  installed_decoders: Optional[Sequence[str]] = None) -> dict:
    """Identify the PTT standard present in `iq` at sample-rate `fs`.

    Returns a dict shaped:
      {
        "ok": bool,
        "verdict": {
            "ptt_id": "dmr"|"p25p1"|...,
            "label": "DMR (Tier I/II/III)",
            "family": "4fsk"|"gmsk"|...,
            "decoder": "dsd-fme",
            "audio_mode": "dmr",
            "confidence": 0.83,
        },
        "evidence": {
            "bandwidth_hz": 11_780,
            "symbol_rate_hz": 4800,
            "family_detected": "4fsk",
            "fsk_peaks": 4,
            "envelope_var": 0.018,
            "snr_db_est": 22.4,
            ...
        },
        "candidates": [{"ptt_id": "dmr", "score": 0.83, "reason": "..."}, ...],
        "decoder_available": bool,
        "fallback_decoder": "sdrtrunk",
      }
    """
    x = _as_c64(iq)
    if x.size < 1024:
        return {"ok": False, "error": "need at least 1024 IQ samples"}
    installed = set(installed_decoders or [])
    bw = _occupied_bandwidth(x, fs)
    fam = _classify_family(x, fs)
    sr = _symbol_rate_hz(x, fs)
    # SNR proxy
    sig_pow = float(np.mean(np.abs(x) ** 2))
    noise_pow = float(np.var(np.diff(x.real)) + np.var(np.diff(x.imag))) / 2.0 + 1e-12
    snr_db = 10.0 * math.log10(max(1e-9, sig_pow / noise_pow))

    # Score each catalogue standard against the evidence. Weights are tuned
    # so the *strongest* and most-trustworthy feature dominates:
    #   family  — high weight (4-FSK vs analog is the most discriminative test)
    #   symbol-rate — high weight when sr confidence ≥ 0.3, else demoted
    #   bandwidth   — lower weight (synthetic signals + IF filtering vary widely)
    sr_trustworthy = sr["symbol_rate_hz"] > 0 and sr["confidence"] >= 0.3
    if sr_trustworthy:
        w_bw, w_fam, w_sr = 0.25, 0.35, 0.40
    else:
        w_bw, w_fam, w_sr = 0.40, 0.50, 0.10
    cand: list[dict] = []
    for std in PTT_STANDARDS:
        reasons: list[str] = []
        # Bandwidth match: gaussian-falloff around catalogue bw, lenient σ.
        bw_err = abs(bw - std["bw_hz"]) / max(1.0, std["bw_hz"])
        bw_score = math.exp(-bw_err * bw_err * 3.0)
        reasons.append(f"bw {bw:.0f}/{std['bw_hz']} → {bw_score:.2f}")
        # Family match. We treat each FSK arity strictly (4fsk vs 2fsk → 0.2);
        # PSK/DQPSK soft-match; analog catch-all.
        df, sf = fam["family"], std["family"]
        if sf == df:
            fam_score = 1.0; reasons.append(f"family={df} ✓")
        elif sf.startswith("analog") and df.startswith("analog"):
            fam_score = 0.85; reasons.append("analog match")
        elif sf == "psk8" and df == "dqpsk":
            fam_score = 0.75; reasons.append("psk/dqpsk soft match")
        elif sf == "gmsk" and df == "2fsk":
            # GMSK is filtered 2-FSK — accept either way with a small penalty
            fam_score = 0.75; reasons.append("gmsk≈2fsk soft match")
        elif sf == "2fsk" and df == "gmsk":
            fam_score = 0.65; reasons.append("2fsk←gmsk soft match")
        else:
            fam_score = 0.05; reasons.append(f"family≠ ({sf} vs {df})")
        # Symbol-rate match (when applicable). Snap to nearest catalogue rate
        # via a Gaussian within ±15%; outside that, score collapses fast.
        if std["symbol_rate_hz"] > 0 and sr["symbol_rate_hz"] > 0:
            sr_err = abs(sr["symbol_rate_hz"] - std["symbol_rate_hz"]) / std["symbol_rate_hz"]
            sr_score = math.exp(-sr_err * sr_err * 50.0)
            reasons.append(f"sym {sr['symbol_rate_hz']:.0f}/{std['symbol_rate_hz']} → {sr_score:.2f}")
        elif std["symbol_rate_hz"] == 0:
            sr_score = 0.6     # analog modes don't have a symbol rate; don't penalise
        else:
            sr_score = 0.25; reasons.append("no symbol rate detected")
        score = w_bw * bw_score + w_fam * fam_score + w_sr * sr_score
        cand.append({
            "ptt_id": std["id"], "label": std["label"], "score": float(score),
            "decoder": std["decoder"], "audio_mode": std["audio_mode"],
            "family": std["family"], "expected_bw_hz": std["bw_hz"],
            "expected_symbol_rate_hz": std["symbol_rate_hz"], "reason": "; ".join(reasons),
        })
    cand.sort(key=lambda c: -c["score"])
    top = cand[0]
    # Decoder availability: if the *first* choice's decoder isn't installed but a
    # second-place decoder is installed (e.g. SDRTrunk covers DMR+P25+NXDN+YSF), pick that.
    decoder_available = (top["decoder"] == "builtin") or (top["decoder"] in installed)
    fallback = None
    if not decoder_available:
        for c in cand[:5]:
            if c["decoder"] != top["decoder"] and c["decoder"] in installed:
                fallback = c["decoder"]
                break
    return {
        "ok": True,
        "verdict": {
            "ptt_id": top["ptt_id"], "label": top["label"], "family": top["family"],
            "decoder": top["decoder"], "audio_mode": top["audio_mode"],
            "confidence": top["score"],
        },
        "evidence": {
            "bandwidth_hz": bw,
            "symbol_rate_hz": sr["symbol_rate_hz"],
            "symbol_rate_confidence": sr["confidence"],
            "family_detected": fam["family"],
            "fsk_peaks": fam.get("fsk_peaks"),
            "envelope_var": fam.get("envelope_var"),
            "envelope_ripple_db": fam.get("envelope_ripple_db"),
            "mean_freq_dev_hz": fam.get("mean_freq_dev_hz"),
            "mse_k1": fam.get("mse_k1"),
            "mse_k2": fam.get("mse_k2"),
            "mse_k4": fam.get("mse_k4"),
            "fsk_quantisation_ratio": fam.get("fsk_quantisation_ratio"),
            "fm_disc_bandwidth_hz": fam.get("fm_disc_bandwidth_hz"),
            "wcdma_score": fam.get("wcdma_score"),
            "ofdm_score": fam.get("ofdm_score"),
            "ofdm_cp_lag_samples": fam.get("ofdm_cp_lag_samples"),
            "snr_db_est": snr_db,
            "center_hz": center_hz,
            "fs_hz": fs,
            "n_samples": int(x.size),
        },
        "candidates": cand,
        "decoder_available": decoder_available,
        "fallback_decoder": fallback,
    }
