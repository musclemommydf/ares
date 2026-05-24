# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
packet_modem.py — an in-process, software-defined packet modem (numpy only).

This is the DSP that lets *any* SDR carry network frames: it turns a byte
buffer (an Ethernet/IP frame off a TAP/TUN interface) into a baseband IQ
waveform the radio transmits, and turns received IQ back into the original
bytes. No external GNU Radio flowgraph, no compiled blocks — same "everything
bundled in Ares" rule the DF pipeline follows.

Scheme: **differential BPSK** with root-raised-cosine pulse shaping.

  * Differential encoding (DBPSK) removes the need for absolute carrier-phase
    recovery — a short frame survives a residual phase/frequency offset, which
    is exactly what a cheap SDR pair (e.g. two Plutos) hands you. The decision
    is ``bit = real(s[k+1] · conj(s[k])) < 0``, with the very first symbol an
    explicit ``+1`` reference that carries no data.
  * Acquisition is by **cross-correlating the received IQ against the known
    transmitted preamble waveform** (a complex matched filter). The magnitude
    peak gives the frame's sample offset regardless of carrier phase; the
    differential slicer that follows is phase-blind, so no separate carrier
    recovery is needed. This is the standard preamble-acquisition receiver and
    is robust on real hardware, not a toy.

Frame on the wire (before the leading reference symbol, DBPSK + RRC):

    [ PREAMBLE 0xAA·n ][ SYNC 16-bit ][ LEN u16-BE ][ PAYLOAD ][ CRC32-BE ]
                                       └──────── CRC covers LEN+PAYLOAD ────┘

LEN is the payload byte count; CRC32 is computed over LEN||PAYLOAD so a bit
error anywhere in the header or body is caught and the frame dropped.

Throughput ≈ sample_rate / sps  bits/s  (DBPSK = 1 bit/symbol). At
sample_rate=2.4 MHz, sps=8 → ~300 kbit/s, enough for an IP control link.

The TX side is stateless; the RX side keeps a small residual carry-over so a
frame split across two IQ reads is still recovered. It is exercised end-to-end
by the synthetic driver's TX→RX loopback, so the offline demo proves the
framing + DSP actually round-trip.
"""
from __future__ import annotations

import binascii
import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

# Default sync word — 16 bits with a sharp autocorrelation peak. Prefixed by a
# run of 0xAA (…1010…) preamble for AGC settle + a strong correlation template.
_DEFAULT_SYNC = 0x1ACF
_SYNC_BITS = 16
_LEN_BYTES = 2          # u16 big-endian payload length
_CRC_BYTES = 4          # CRC32 big-endian over LEN||PAYLOAD
_MAX_PAYLOAD = 2048     # generous: covers a 1500-byte MTU + tunnel overhead


def _rrc_taps(sps: int, span_syms: int = 8, beta: float = 0.35) -> np.ndarray:
    """Root-raised-cosine filter taps, unit-energy normalised.

    span_syms symbols either side → len = span_syms*sps + 1. beta is the
    roll-off (0.35 is a common compromise between bandwidth and ISI).
    """
    n = span_syms * sps
    t = (np.arange(n + 1) - n / 2.0) / float(sps)   # time in symbol units
    h = np.zeros_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-8:
            h[i] = 1.0 - beta + 4.0 * beta / np.pi
        elif beta > 0 and abs(abs(ti) - 1.0 / (4.0 * beta)) < 1e-6:
            h[i] = (beta / np.sqrt(2.0)) * (
                (1 + 2.0 / np.pi) * np.sin(np.pi / (4 * beta))
                + (1 - 2.0 / np.pi) * np.cos(np.pi / (4 * beta))
            )
        else:
            num = (np.sin(np.pi * ti * (1 - beta))
                   + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta)))
            den = np.pi * ti * (1 - (4 * beta * ti) ** 2)
            h[i] = num / den
    h /= np.sqrt(np.sum(h ** 2))
    return h.astype(np.float64)


def _bytes_to_bits(data: bytes) -> np.ndarray:
    """MSB-first bit unpacking → uint8 array of 0/1."""
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def _bits_to_bytes(bits: np.ndarray) -> bytes:
    """MSB-first bit packing. ``bits`` length must be a multiple of 8."""
    return np.packbits(bits.astype(np.uint8)).tobytes()


@dataclass
class ModemConfig:
    sps: int = 8                     # samples per symbol (sets the bit rate)
    preamble_bytes: int = 6          # 0xAA run before the sync word
    sync_word: int = _DEFAULT_SYNC
    rrc_beta: float = 0.35
    rrc_span: int = 8                # RRC half-span in symbols
    # acquisition gate: normalised correlation (0…1) must exceed this for a
    # candidate to be tried — keeps pure noise from minting phantom packets.
    detect_threshold: float = 0.55


class PacketModem:
    """Differential-BPSK + RRC packet modem. ``modulate`` is reentrant;
    ``demodulate`` keeps a residual buffer across calls."""

    def __init__(self, config: ModemConfig | None = None):
        self.cfg = config or ModemConfig()
        self.sps = int(self.cfg.sps)
        self._rrc = _rrc_taps(self.sps, self.cfg.rrc_span, self.cfg.rrc_beta).astype(np.complex64)
        self._rrc_delay = (len(self._rrc) - 1) // 2
        # Header bits common to every frame: preamble + sync word.
        pre = np.tile(_bytes_to_bits(b"\xAA"), self.cfg.preamble_bytes)
        sync = np.array([(self.cfg.sync_word >> (_SYNC_BITS - 1 - i)) & 1
                         for i in range(_SYNC_BITS)], dtype=np.uint8)
        self._header_bits = np.concatenate([pre, sync]).astype(np.uint8)
        # Acquisition template = TX waveform of (reference symbol + header bits).
        self._template = self._modulate_bits(self._header_bits)
        self._template_conj = np.conj(self._template[::-1]).astype(np.complex64)
        self._template_energy = float(np.sum(np.abs(self._template) ** 2))
        # RX state
        self._rx_residual = np.zeros(0, dtype=np.complex64)

    # ── geometry helpers ────────────────────────────────────────────────────
    @property
    def header_len_bits(self) -> int:
        return int(self._header_bits.size)

    def bits_per_second(self, sample_rate_hz: float) -> float:
        return float(sample_rate_hz) / float(self.sps)

    # ── modulation ──────────────────────────────────────────────────────────
    def _symbols_from_bits(self, bits: np.ndarray) -> np.ndarray:
        """DBPSK: leading +1 reference, then each bit toggles the phase.
        Returns 1 + len(bits) complex symbols."""
        flips = np.where(bits.astype(np.int8) == 1, -1.0, 1.0)
        syms = np.cumprod(np.concatenate([[1.0], flips]))
        return syms.astype(np.complex64)

    def _modulate_bits(self, bits: np.ndarray) -> np.ndarray:
        """Differential-BPSK encode bits → RRC-shaped complex64 baseband."""
        syms = self._symbols_from_bits(bits)
        up = np.zeros(len(syms) * self.sps, dtype=np.complex64)
        up[::self.sps] = syms
        return np.convolve(up, self._rrc, mode="full").astype(np.complex64)

    def modulate(self, payload: bytes) -> np.ndarray:
        """Frame ``payload`` and return the complex64 baseband to transmit."""
        if len(payload) > _MAX_PAYLOAD:
            raise ValueError(f"payload {len(payload)} > max {_MAX_PAYLOAD}")
        length = len(payload).to_bytes(_LEN_BYTES, "big")
        crc = binascii.crc32(length + payload) & 0xFFFFFFFF
        body = length + payload + crc.to_bytes(_CRC_BYTES, "big")
        bits = np.concatenate([self._header_bits, _bytes_to_bits(body)])
        return self._modulate_bits(bits)

    # ── demodulation ────────────────────────────────────────────────────────
    def _n_symbols(self, payload_len: int) -> int:
        """Total transmitted symbols incl. the leading reference."""
        body_bits = (_LEN_BYTES + payload_len + _CRC_BYTES) * 8
        return 1 + self.header_len_bits + body_bits

    def _frame_sample_len(self, payload_len: int) -> int:
        return self._n_symbols(payload_len) * self.sps

    def demodulate(self, iq: np.ndarray) -> list[bytes]:
        """Feed a chunk of received complex IQ; return any complete, CRC-valid
        payloads found. Frames straddling chunk boundaries are recovered via an
        internal residual buffer."""
        if iq.ndim > 1:
            iq = iq[0]                          # NIC uses a single RX channel
        buf = np.concatenate([self._rx_residual, iq.astype(np.complex64)])
        max_frame = self._frame_sample_len(_MAX_PAYLOAD)
        frames: list[bytes] = []

        tlen = self._template_conj.size
        if buf.size < tlen + self.sps:
            self._rx_residual = buf[-max_frame:] if buf.size > max_frame else buf
            return frames

        # Normalised cross-correlation against the known header waveform:
        #   nc[i] = |Σ buf[i+k]·conj(tmpl[k])| / sqrt(E_win[i] · E_tmpl)
        # which sits near 1.0 at a real frame start and is small for noise — a
        # fixed threshold then works at any signal amplitude, and we only run
        # the (costly) full decode at the sparse correlation peaks.
        corr = np.convolve(buf, self._template_conj, mode="valid")   # len = N-tlen+1
        e = np.abs(buf).astype(np.float64) ** 2
        csum = np.concatenate([[0.0], np.cumsum(e)])
        win_e = csum[tlen:] - csum[:-tlen]                           # energy of buf[i:i+tlen]
        nc = np.abs(corr) / np.sqrt(win_e * self._template_energy + 1e-12)

        thr = self.cfg.detect_threshold
        cand = np.flatnonzero(nc > thr)

        consumed = 0
        guard = self.sps
        prev_end = -1
        for c in cand:
            if c < prev_end:
                continue
            lo, hi = max(0, c - self.sps), min(nc.size, c + self.sps + 1)
            if nc[c] < np.max(nc[lo:hi]):
                continue                                   # not the local peak
            decoded = self._try_decode(buf, int(c))
            if decoded is not None:
                payload, frame_samples = decoded
                frames.append(payload)
                consumed = c + frame_samples
                prev_end = consumed
            else:
                prev_end = c + guard
        tail_start = max(consumed, buf.size - max_frame)
        self._rx_residual = buf[tail_start:].copy()
        return frames

    def _decode_bits(self, mf: np.ndarray, base: int, n_bits: int) -> np.ndarray:
        """Differentially slice ``n_bits`` bits from matched-filtered ``mf``,
        symbol[0] at index ``base`` being the +1 reference."""
        idx = base + np.arange(n_bits + 1) * self.sps
        if idx[-1] >= mf.size:
            return np.zeros(0, dtype=np.uint8)
        s = mf[idx]
        d = s[1:] * np.conj(s[:-1])
        return (np.real(d) < 0).astype(np.uint8)

    def _try_decode(self, buf: np.ndarray, start: int):
        """Attempt to decode a frame whose header waveform begins at ``start``.
        Returns (payload_bytes, frame_sample_len) or None."""
        need = self._frame_sample_len(_MAX_PAYLOAD) + len(self._rrc)
        seg = buf[start:start + need]
        min_syms = (1 + self.header_len_bits + (_LEN_BYTES + _CRC_BYTES) * 8)
        if seg.size < min_syms * self.sps:
            return None
        mf = np.convolve(seg, self._rrc, mode="full").astype(np.complex64)
        base = self._rrc_delay * 2          # TX-RRC delay + RX-RRC delay

        # confirm the header (preamble+sync) before trusting the frame
        n_hdr = self.header_len_bits
        hdr = self._decode_bits(mf, base, n_hdr)
        if hdr.size < n_hdr or int(np.sum(hdr != self._header_bits)) > 1:
            return None

        body_base = base + n_hdr * self.sps   # next symbol is body bit 0's ref…
        # …which is the last header symbol; _decode_bits treats index `body_base`
        # as the reference, i.e. the (n_hdr)-th transmitted symbol. Correct.
        len_bits = self._decode_bits(mf, body_base, _LEN_BYTES * 8)
        if len_bits.size < _LEN_BYTES * 8:
            return None
        payload_len = int.from_bytes(_bits_to_bytes(len_bits), "big")
        if payload_len > _MAX_PAYLOAD:
            return None

        total_body_bits = (_LEN_BYTES + payload_len + _CRC_BYTES) * 8
        body_bits = self._decode_bits(mf, body_base, total_body_bits)
        if body_bits.size < total_body_bits:
            return None
        body = _bits_to_bytes(body_bits)
        length_field = body[:_LEN_BYTES]
        payload = body[_LEN_BYTES:_LEN_BYTES + payload_len]
        crc_field = body[_LEN_BYTES + payload_len:_LEN_BYTES + payload_len + _CRC_BYTES]
        if len(payload) != payload_len or len(crc_field) != _CRC_BYTES:
            return None
        got = binascii.crc32(length_field + payload) & 0xFFFFFFFF
        if got != int.from_bytes(crc_field, "big"):
            return None
        return payload, self._frame_sample_len(payload_len)

    def reset(self) -> None:
        self._rx_residual = np.zeros(0, dtype=np.complex64)
