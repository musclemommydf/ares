"""
Polyphase channelizer — split a wideband IQ capture into N narrow uniform
sub-channels in a single FFT pass. Each sub-channel is then DF'd / classified
independently, so one wide tune (e.g. a Matchstiq X40 at 30 MHz BW) covers
dozens of simultaneous signals.

Reference: Crochiere & Rabiner "Multirate Digital Signal Processing"; the
polyphase identity factors an N-point DFT and a low-pass prototype filter so
the per-channel cost is O(N log N) instead of N × FFT.

Two entry points:

    channelize(iq, n_channels, taps_per_channel=32, ...) → (n_channels, n_blocks) complex
    detect_signals(channels, sample_rate_hz, center_hz, ...) → list of detected sub-channels
                                                                with their centre + bw + power
"""

from __future__ import annotations

import numpy as np


def design_prototype(n_channels: int, taps_per_channel: int = 32,
                      window: str = "hamming") -> np.ndarray:
    """Windowed-sinc low-pass prototype with cutoff = π/N. Length = N · taps_per_channel."""
    L = n_channels * taps_per_channel
    n = np.arange(L) - (L - 1) / 2
    cutoff = np.pi / n_channels
    h = np.sinc(cutoff * n / np.pi) * (cutoff / np.pi)
    if window == "hamming":
        w = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(L) / (L - 1))
    elif window == "hann":
        w = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(L) / (L - 1))
    else:
        w = np.ones(L)
    h = h * w
    return h / np.sum(h)


def channelize(iq: np.ndarray, n_channels: int, *,
               taps_per_channel: int = 32) -> np.ndarray:
    """Split a 1-D complex baseband stream into `n_channels` uniform sub-channels.
    Returns an (n_channels, n_blocks) complex array where row k is the demodulated
    sub-band centred at (k - n_channels/2) · fs/n_channels (after fftshift).
    """
    iq = np.asarray(iq, dtype=np.complex64).ravel()
    M = n_channels
    h = design_prototype(M, taps_per_channel).astype(np.complex64)
    # Pad to a whole number of polyphase blocks
    L = h.size
    n_blocks = (iq.size - L) // M + 1
    if n_blocks <= 0:
        return np.empty((M, 0), dtype=np.complex64)
    # Build polyphase coefficient matrix E_k(z): row k = h[k::M]
    E = h.reshape(-1, M).T                              # (M, taps_per_channel)
    out = np.empty((M, n_blocks), dtype=np.complex64)
    # Slide windowed input through the polyphase branches
    # x_k(n) = x(n·M - k) gives row k input; convolve with E_k then IDFT across rows.
    for k in range(M):
        # Reversed branch shift: feed the k-th polyphase component
        signal_k = iq[k :: M][: taps_per_channel + n_blocks - 1] if k > 0 else iq[:: M][: taps_per_channel + n_blocks - 1]
        # Convolve with the branch filter
        conv = np.convolve(signal_k, E[k, ::-1], mode="valid")
        n = min(conv.size, n_blocks)
        out[k, :n] = conv[:n]
    # Per-block N-point IDFT mixes branches into sub-channel outputs
    return np.fft.fftshift(np.fft.ifft(out, n=M, axis=0), axes=0).astype(np.complex64)


def detect_signals(channels: np.ndarray, sample_rate_hz: float, center_hz: float,
                   threshold_db: float = 10.0, min_bins: int = 1) -> list[dict]:
    """Per-channel RMS power → detected-channel list above (median + threshold_db).
    Returns [{ channel_idx, center_hz, bw_hz, power_db }] sorted by power desc.
    Adjacent active channels are merged into one detection."""
    if channels.size == 0:
        return []
    M, N = channels.shape
    if N == 0:
        return []
    rms = np.sqrt(np.mean(np.abs(channels) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-12))
    floor = float(np.median(db))
    active = db >= (floor + threshold_db)
    if active.sum() < min_bins:
        return []
    bw = sample_rate_hz / M
    chan_centers = center_hz + (np.arange(M) - M / 2 + 0.5) * bw
    out = []
    i = 0
    while i < M:
        if not active[i]:
            i += 1; continue
        j = i
        while j + 1 < M and active[j + 1]:
            j += 1
        seg = slice(i, j + 1)
        out.append({
            "channel_idx_start": int(i),
            "channel_idx_end": int(j),
            "center_hz": float((chan_centers[i] + chan_centers[j]) / 2),
            "bw_hz": float((j - i + 1) * bw),
            "power_db": float(db[seg].max()),
            "snr_db": float(db[seg].max() - floor),
        })
        i = j + 1
    out.sort(key=lambda d: d["power_db"], reverse=True)
    return out
