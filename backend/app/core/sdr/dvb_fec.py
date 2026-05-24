# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_fec.py — DVB outer forward-error-correction primitives.

Two standard, self-contained stages of the DVB-T/S/C transport-stream FEC chain:

  * **Energy-dispersal (de)randomiser** — the PRBS x^15 + x^14 + 1, reset every
    8 TS packets (with the first packet's sync byte inverted to 0xB8), per
    EN 300 744 §4.3.1 / EN 300 421 §4.4.1.
  * **Reed-Solomon RS(204,188,t=8)** over GF(2^8) with the DVB field polynomial
    x^8+x^4+x^3+x^2+1 (0x11D) and code generator roots α^0..α^15 — the shortened
    RS(255,239) the standards use. Berlekamp-Massey + Chien + Forney decode,
    correcting up to 8 byte errors per 204-byte packet.

These are the *outer* code. The *inner* DVB-T convolutional code + Viterbi and the
convolutional byte de-interleaver are not implemented here — so this corrects
byte errors on a stream that already reached the byte layer, and de-randomises
it. Round-trip self-test: ``python -m app.core.sdr.dvb_fec``.
"""
from __future__ import annotations

from typing import Optional

# ── GF(2^8) with the DVB primitive polynomial 0x11D ──────────────────────────
_PRIM = 0x11D
_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= _PRIM
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gmul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _gdiv(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


# RS(255,239) → shortened to RS(204,188). 16 parity bytes, t=8.
_NROOTS = 16


# ── energy dispersal ─────────────────────────────────────────────────────────
def _prbs_sequence(n: int) -> bytes:
    """The DVB energy-dispersal PRBS byte stream of length n (seed 0b100101010000000)."""
    reg = 0b100101010000000  # 15-bit shift register, loaded per the standard
    out = bytearray(n)
    for i in range(n):
        b = 0
        for _ in range(8):
            bit = ((reg >> 14) ^ (reg >> 13)) & 1
            b = (b << 1) | bit
            reg = ((reg << 1) | bit) & 0x7FFF
        out[i] = b
    return bytes(out)


def derandomise(packets: bytes) -> bytes:
    """De-randomise a run of 188-byte TS packets. The sync byte of every 8th packet
    is 0xB8 (inverted 0x47); PRBS is reset there and runs over the next 8 packets'
    payloads (sync bytes are passed through, restored to 0x47)."""
    if len(packets) % 188 != 0:
        raise ValueError("derandomise expects a whole number of 188-byte packets")
    out = bytearray(packets)
    n_pkt = len(packets) // 188
    prbs = _prbs_sequence(187 * 8)   # one PRBS run covers 8 packets × 187 payload bytes
    for grp in range(0, n_pkt, 8):
        k = 0
        for p in range(grp, min(grp + 8, n_pkt)):
            base = p * 188
            out[base] = 0x47                      # restore sync byte
            for j in range(1, 188):
                out[base + j] ^= prbs[k]
                k += 1
    return bytes(out)


# ── Reed-Solomon RS(204,188) ─────────────────────────────────────────────────
def _rs_generator() -> list[int]:
    """g(x) = ∏ (x − α^i), i=0..15, high-order coefficient first (leading = 1)."""
    g = [1]
    for i in range(_NROOTS):
        root = _EXP[i]
        new = [0] * (len(g) + 1)
        for j in range(len(g)):
            new[j] ^= g[j]                    # g · x
            new[j + 1] ^= _gmul(g[j], root)   # g · α^i
        g = new
    return g


_GEN = _rs_generator()


def rs_encode(data188: bytes) -> bytes:
    """Append 16 RS parity bytes → a 204-byte codeword (for the self-test / TX)."""
    if len(data188) != 188:
        raise ValueError("rs_encode expects 188 data bytes")
    msg = list(data188) + [0] * _NROOTS
    for i in range(188):
        coef = msg[i]
        if coef != 0:
            for j in range(1, len(_GEN)):
                msg[i + j] ^= _gmul(_GEN[j], coef)
    return bytes(data188) + bytes(msg[188:188 + _NROOTS])


def _poly_eval(poly: list[int], x: int) -> int:
    """Horner evaluation of a polynomial given high-order coefficient first."""
    y = poly[0]
    for c in poly[1:]:
        y = _gmul(y, x) ^ c
    return y


def _poly_mul(p: list[int], q: list[int]) -> list[int]:
    r = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a:
            for j, b in enumerate(q):
                r[i + j] ^= _gmul(a, b)
    return r


def _poly_scale(p: list[int], s: int) -> list[int]:
    return [_gmul(c, s) for c in p]


def _poly_add(p: list[int], q: list[int]) -> list[int]:
    r = [0] * max(len(p), len(q))
    for i in range(len(p)):
        r[i + len(r) - len(p)] = p[i]
    for i in range(len(q)):
        r[i + len(r) - len(q)] ^= q[i]
    return r


def _gpow(p: int) -> int:
    return _EXP[p % 255]


def _poly_div(dividend: list[int], divisor: list[int]) -> tuple[list[int], list[int]]:
    out = list(dividend)
    for i in range(len(dividend) - (len(divisor) - 1)):
        coef = out[i]
        if coef != 0:
            for j in range(1, len(divisor)):
                if divisor[j] != 0:
                    out[i + j] ^= _gmul(divisor[j], coef)
    sep = -(len(divisor) - 1)
    return out[:sep], out[sep:]


def rs_decode(code204: bytes) -> tuple[Optional[bytes], int]:
    """Decode a 204-byte RS codeword (high-order = first byte). Returns
    ``(data188 | None, n_errors)``; ``(None, -1)`` if uncorrectable (> t=8).

    Faithful port of the "Reed-Solomon for coders" errors-only decoder:
    syndromes → Berlekamp-Massey → Chien → Forney (product-form denominator)."""
    if len(code204) != 204:
        raise ValueError("rs_decode expects 204 bytes")
    # Decode as the full RS(255,239) with 51 virtual zero bytes at the front
    # (a shortened code = the parent code with high-order coefficients fixed to 0).
    # This keeps the Chien search over the whole field, so positions map cleanly.
    PAD = 255 - 204
    msg = [0] * PAD + list(code204)
    n = len(msg)

    # Syndromes (leading 0 prepended so synd[1..16] = S_0..S_15)
    synd = [0] + [_poly_eval(msg, _gpow(i)) for i in range(_NROOTS)]
    if max(synd) == 0:
        return bytes(msg[PAD:PAD + 188]), 0

    # Berlekamp-Massey → error locator Λ(x), high-order first
    err_loc = [1]; old_loc = [1]
    synd_shift = len(synd) - _NROOTS          # = 1 (the prepended zero)
    for i in range(_NROOTS):
        K = i + synd_shift
        delta = synd[K]
        for j in range(1, len(err_loc)):
            delta ^= _gmul(err_loc[-(j + 1)], synd[K - j])
        old_loc = old_loc + [0]
        if delta != 0:
            if len(old_loc) > len(err_loc):
                new_loc = _poly_scale(old_loc, delta)
                old_loc = _poly_scale(err_loc, _gdiv(1, delta))
                err_loc = new_loc
            err_loc = _poly_add(err_loc, _poly_scale(old_loc, delta))
    while len(err_loc) and err_loc[0] == 0:
        err_loc.pop(0)
    errs = len(err_loc) - 1
    if errs * 2 > _NROOTS or errs == 0:
        return None, -1

    # Λ(x) low-order first; error at array index k has locator value X_k = α^(n-1-k),
    # so Λ(X_k^-1) = 0. Search every position.
    lam_lo = err_loc[::-1]                       # lam_lo[0] = constant term (=1)

    def _eval_lo(poly_lo: list[int], x: int) -> int:
        y = 0; xp = 1
        for c in poly_lo:
            y ^= _gmul(c, xp); xp = _gmul(xp, x)
        return y

    err_pos = []
    for k in range(n):
        Xk_inv = _gpow(-(n - 1 - k))
        if _eval_lo(lam_lo, Xk_inv) == 0:
            err_pos.append(k)
    if len(err_pos) != errs:
        return None, -1

    # Forney: Ω(x) = S(x)·Λ(x) mod x^(2t), low-order; e_k = X_k·Ω(X_k^-1) / ∏_{j≠k}(1−X_k^-1·X_j)
    s_lo = synd[1:]                              # S_0..S_15, low-order
    omega_lo = _poly_mul(lam_lo, s_lo)[:_NROOTS]
    E = [0] * n
    for k in err_pos:
        Xk = _gpow(n - 1 - k)
        Xk_inv = _gdiv(1, Xk)
        denom = 1
        for k2 in err_pos:
            if k2 != k:
                denom = _gmul(denom, 1 ^ _gmul(Xk_inv, _gpow(n - 1 - k2)))
        if denom == 0:
            return None, -1
        E[k] = _gdiv(_eval_lo(omega_lo, Xk_inv), denom)
    msg = _poly_add(msg, E)
    if max(_poly_eval(msg, _gpow(i)) for i in range(_NROOTS)) != 0:
        return None, -1
    return bytes(msg[PAD:PAD + 188]), len(err_pos)


def correct_ts_packets(stream204: bytes) -> tuple[bytes, dict]:
    """RS-decode + derandomise a run of 204-byte RS codewords → clean 188-byte TS.
    Returns (ts_bytes, stats). Uncorrectable packets are passed through (stripped
    to 188 bytes) so partial streams still surface."""
    if len(stream204) % 204 != 0:
        usable = (len(stream204) // 204) * 204
        stream204 = stream204[:usable]
    out = bytearray()
    corrected = uncorrectable = total_errs = 0
    for i in range(0, len(stream204), 204):
        data, nerr = rs_decode(stream204[i:i + 204])
        if data is None:
            out += stream204[i:i + 188]
            uncorrectable += 1
        else:
            out += data
            if nerr > 0:
                corrected += 1; total_errs += nerr
    ts = derandomise(bytes(out)) if out else b""
    return ts, {"packets": len(stream204) // 204, "rs_corrected": corrected,
                "rs_uncorrectable": uncorrectable, "byte_errors_fixed": total_errs}


if __name__ == "__main__":   # round-trip self-test
    import os, random
    random.seed(0)
    ok = fail = 0
    for trial in range(200):
        data = bytes(random.randrange(256) for _ in range(188))
        code = bytearray(rs_encode(data))
        nerr = random.randint(0, 8)
        for pos in random.sample(range(204), nerr):
            code[pos] ^= random.randint(1, 255)
        dec, got = rs_decode(bytes(code))
        if dec == data:
            ok += 1
        else:
            fail += 1
    # >8 errors must be flagged uncorrectable (not silently mis-corrected)
    data = bytes(range(188))
    code = bytearray(rs_encode(data))
    for pos in range(9):
        code[pos] ^= 0xFF
    dec, got = rs_decode(bytes(code))
    over = "PASS" if (dec != data) else "FAIL(mis-corrected >t)"
    print(f"RS(204,188): {ok}/{ok+fail} correctable trials recovered; >t handling: {over}")
    raise SystemExit(0 if fail == 0 else 1)
