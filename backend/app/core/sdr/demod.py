# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/demod.py — in-process analog demodulators (NFM / WFM / AM / SSB) → PCM audio.

Turns a complex baseband stream (the wideband SDR capture) into mono 16-bit PCM
for the SDR console's "Listen". Pure numpy/scipy, no external decoder — the
analog voice modes (the common PMR/LMR/marine/airband case) are decoded here;
the *digital* trunked modes (DMR/P25/TETRA/…) still shell out to op25/dsd-fme
(licensed vocoders can't be vendored).

`AudioDemod` is **stateful** so back-to-back chunks decode without a click at the
seams: the NCO phase, the two decimation FIR states (carried via a sample
counter so the decimation grid never shifts between blocks), the FM
discriminator's last sample, the de-emphasis one-pole, and the AGC all persist
across `process()` calls.

Chain:  mix tune→DC · channel low-pass · ↓decim1 → fs_if · demod · de-emphasis ·
        audio low-pass · ↓decim2 → audio_rate · DC-block · AGC · int16
"""
from __future__ import annotations

import numpy as np
from scipy import signal


# mode → (channel_bw_hz, audio_rate_hz, deemphasis_us)
_MODE = {
    "nfm": (12_500.0, 16_000, 750.0), "fm": (12_500.0, 16_000, 750.0),
    "wfm": (180_000.0, 48_000, 75.0),
    "am":  (10_000.0, 16_000, 0.0),
    "usb": (3_000.0, 16_000, 0.0), "lsb": (3_000.0, 16_000, 0.0),
    "ssb": (3_000.0, 16_000, 0.0), "cw": (1_500.0, 16_000, 0.0),
}


def mode_params(mode: str) -> tuple[float, int, float]:
    return _MODE.get((mode or "nfm").lower(), _MODE["nfm"])


class AudioDemod:
    def __init__(self, mode: str, fs_in: float, offset_hz: float, *,
                 channel_bw_hz: float | None = None, audio_rate: int | None = None,
                 discriminator: bool = False):
        self.mode = (mode or "nfm").lower()
        self.fs_in = float(fs_in)
        self.offset = float(offset_hz)
        # discriminator mode: emit the raw FM-discriminated baseband at a fixed level
        # (no de-emphasis, minimal post-filter) to feed an external digital decoder.
        self.discriminator = bool(discriminator)
        bw, arate, deemph_us = mode_params(self.mode)
        if self.discriminator:
            bw = float(channel_bw_hz or 12500.0)
            arate = int(audio_rate or 48000)
            deemph_us = 0.0
        self.channel_bw = float(channel_bw_hz or bw)
        want_audio = int(audio_rate or arate)

        # stage-1: integer-decimate fs_in → fs_if (≥ ~2.2× channel bw, and ≥ audio rate)
        target_if = max(want_audio, 2.2 * self.channel_bw)
        self.decim1 = max(1, int(self.fs_in // target_if))
        self.fs_if = self.fs_in / self.decim1
        # stage-2: integer-decimate fs_if → audio_rate
        self.decim2 = max(1, int(round(self.fs_if / want_audio)))
        self.audio_rate = self.fs_if / self.decim2

        # NCO (continuous phase across chunks)
        self._dphi = -2.0 * np.pi * self.offset / self.fs_in
        self._phase = 0.0
        # stage-1 channel-select low-pass (complex), with filter + decimation-grid state
        c1 = float(np.clip((self.channel_bw / 2.0) / self.fs_in, 1e-4, 0.49))
        self._b1 = signal.firwin(129, c1).astype(np.float64)
        self._zi1 = np.zeros(len(self._b1) - 1, dtype=np.complex128)
        self._n1 = 0                                  # samples fed to stage-1 (decimation phase)
        # FM discriminator memory
        self._last = np.complex128(0)
        # de-emphasis one-pole (NFM/WFM)
        self._deemph_a = None
        if deemph_us > 0:
            tau, dt = deemph_us * 1e-6, 1.0 / self.audio_rate
            self._deemph_a = dt / (tau + dt)
            self._deemph_y = 0.0
        # stage-2 audio low-pass (real) + decimation-grid state. Discriminator feeds
        # stay wide-open (stage-1 already band-limited; the decoder wants the symbols).
        c2 = 0.45 if self.discriminator else float(np.clip((min(self.audio_rate, self.channel_bw) / 2.0) / self.fs_if, 1e-3, 0.49))
        self._b2 = signal.firwin(129, c2).astype(np.float64)
        self._zi2 = np.zeros(len(self._b2) - 1, dtype=np.float64)
        self._n2 = 0
        self._dc = 0.0                                # DC blocker (AM/SSB envelope)
        self._agc = 1e-6                              # slow peak tracker
        # discriminator: a fixed deviation→level map (no AGC, so symbol levels are stable)
        self._fixed_gain = (0.6 / max(1.0, self.channel_bw / 2.0)) if self.discriminator else None

    def _decim(self, y, decim, n_prev):
        """Decimate keeping a continuous grid across chunks (start so global index % decim == 0)."""
        if decim <= 1:
            return y, n_prev + len(y)
        start = (-n_prev) % decim
        return y[start::decim], n_prev + len(y)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.complex128).ravel()
        if x.size == 0:
            return np.zeros(0, dtype=np.int16)
        n = x.size
        # NCO mix (tune → DC), continuous phase
        mix = np.exp(1j * (self._phase + self._dphi * np.arange(n)))
        self._phase = float((self._phase + self._dphi * n) % (2 * np.pi))
        xm = x * mix
        # channel select + decimate to fs_if
        y, self._zi1 = signal.lfilter(self._b1, [1.0], xm, zi=self._zi1)
        y, self._n1 = self._decim(y, self.decim1, self._n1)
        if y.size == 0:
            return np.zeros(0, dtype=np.int16)

        if self.discriminator or self.mode in ("nfm", "fm", "wfm"):
            prev = np.empty_like(y); prev[0] = self._last; prev[1:] = y[:-1]
            self._last = y[-1]
            audio = np.angle(y * np.conj(prev))            # instantaneous freq (rad/sample)
            audio = audio * (self.fs_if / (2 * np.pi))      # → Hz
        elif self.mode == "am":
            env = np.abs(y)
            self._dc = self._dc + 0.001 * (float(env.mean()) - self._dc)
            audio = env - self._dc                          # remove the carrier DC
        else:                                               # usb / lsb / ssb / cw — product detection at the tuned carrier
            audio = np.real(y)

        audio = audio.astype(np.float64)
        # de-emphasis (post-discriminator) for FM
        if self._deemph_a is not None:
            a = self._deemph_a
            # one-pole IIR y[n] = y[n-1] + a*(x[n]-y[n-1]) == lfilter([a],[1,-(1-a)])
            audio, zf = signal.lfilter([a], [1.0, -(1.0 - a)], audio, zi=[self._deemph_y * (1.0 - a)])
            self._deemph_y = float(audio[-1]) if audio.size else self._deemph_y
        # audio band-limit + decimate to audio_rate
        audio, self._zi2 = signal.lfilter(self._b2, [1.0], audio, zi=self._zi2)
        audio, self._n2 = self._decim(audio, self.decim2, self._n2)
        if audio.size == 0:
            return np.zeros(0, dtype=np.int16)
        if self._fixed_gain is not None:                   # discriminator feed — stable level, no AGC
            out = np.clip(audio * self._fixed_gain, -1.0, 1.0)
        else:                                              # voice — AGC to ~0.5 full scale
            peak = float(np.percentile(np.abs(audio), 99.0)) or 1e-6
            self._agc = max(self._agc * 0.95, peak)
            out = np.clip(audio * (0.5 / max(self._agc, 1e-6)), -1.0, 1.0)
        return (out * 32767.0).astype(np.int16)
