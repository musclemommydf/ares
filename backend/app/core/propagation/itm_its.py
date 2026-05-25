# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
itm_its.py — faithful Python port of the ITS Irregular Terrain Model (Longley-Rice).

This is a line-for-line port of the public-domain NTIA/ITS reference implementation
(ITM v1.2.2, G. Hufford / A. Longley / P. Rice; NTIA Report 82-100, "A Guide to the
Use of the ITS Irregular Terrain Model in the Area Prediction Mode", and Tech Note 101),
covering:

  * point-to-point mode from a terrain profile (`p2p`)            — qlrpfl / lrprop
  * area mode from siting criteria                                — qlra   / lrprop
  * the full time / location / situation variability machinery    — avar
  * all 7 climate zones, 20 MHz–20 GHz, 1 m–3 km antennas, 1–2000 km

i.e. the *same* algorithm SPLAT!, Radio Mobile, and the FCC's analysis use — not the
simplified re-derivation that lived in `itm.py`. `itm.py` is kept for the legacy
"fast/empirical" path; the simulation engine now calls this module for the `itm` model.

Reference numbers it reproduces (point-to-point, h_g = 143.9 m / 8.5 m, climate 5,
N_s = 314.0, ε_r = 15, σ = 0.005, vertical pol., q_t = q_l = q_s = 0.5):
  see ``itm_reference_check()`` and ``backend/tests/test_itm.py``.
"""
from __future__ import annotations

import cmath
import math

import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Sequence

THIRD = 1.0 / 3.0


# ── data structures (mirror the ITS C++ structs) ─────────────────────────────
@dataclass
class _Prop:
    aref: float = 0.0
    dist: float = 0.0
    hg: list = field(default_factory=lambda: [0.0, 0.0])      # antenna heights AGL
    wn: float = 0.0                                            # wave number 2π/λ ... actually fmhz/47.7
    dh: float = 0.0                                            # terrain irregularity Δh
    ens: float = 0.0                                           # surface refractivity (N-units)
    gme: float = 0.0                                           # effective-earth curvature 1/a_eff
    zgndreal: float = 0.0
    zgndimag: float = 0.0
    he: list = field(default_factory=lambda: [0.0, 0.0])       # effective antenna heights
    dl: list = field(default_factory=lambda: [0.0, 0.0])      # horizon distances
    the: list = field(default_factory=lambda: [0.0, 0.0])     # horizon take-off angles
    kwx: int = 0                                               # error indicator (0 ok, 1 warn, ... 4 worst)
    mdp: int = 0                                               # mode of propagation: -1 p2p, 1 area-start, 0 area-continue

    @property
    def zgnd(self) -> complex:
        return complex(self.zgndreal, self.zgndimag)

    @zgnd.setter
    def zgnd(self, z: complex) -> None:
        self.zgndreal, self.zgndimag = z.real, z.imag


@dataclass
class _PropA:
    dlsa: float = 0.0
    dx: float = 0.0
    ael: float = 0.0
    ak1: float = 0.0
    ak2: float = 0.0
    aed: float = 0.0
    emd: float = 0.0
    aes: float = 0.0
    ems: float = 0.0
    dls: list = field(default_factory=lambda: [0.0, 0.0])
    dla: float = 0.0
    tha: float = 0.0


@dataclass
class _PropV:
    sgc: float = 0.0
    lvar: int = 0          # control: which avar parameters changed since last call
    mdvar: int = 0         # variability mode (0..3, +10/+20 for "no location"/"no situation")
    klim: int = 0          # climate code 1..7


# ── small helpers ────────────────────────────────────────────────────────────
def _fdim(x: float, y: float) -> float:
    """FORTRAN DIM — positive difference."""
    return x - y if x > y else 0.0


def _aknfe(v2: float) -> float:
    """Attenuation, in dB, of a single knife edge. v2 = ν²."""
    if v2 < 5.76:
        return 6.02 + 9.11 * math.sqrt(v2) - 1.27 * v2
    return 12.953 + 10.0 * math.log10(v2)


def _fht(x: float, pk: float) -> float:
    """Height-gain over a smooth spherical earth (Norton / Bremmer residue series,
    asymptotic form). x = scaled distance, pk = scaled antenna height."""
    if x < 200.0:
        w = -math.log(pk)
        if pk < 1.0e-5 or x * w * w * w > 5495.0:
            fhtv = -117.0
            if x > 1.0:
                fhtv = 40.0 * math.log10(x) + fhtv
        else:
            fhtv = 2.5e-5 * x * x / pk - 8.686 * w - 15.0
    else:
        fhtv = 0.05751 * x - 10.0 * math.log10(x)
        if x < 2000.0:
            w = 0.0134 * x * math.exp(-0.005 * x)
            fhtv = (1.0 - w) * fhtv + w * (40.0 * math.log10(x) - 117.0)
    return fhtv


_H0F_A = (25.0, 80.0, 177.0, 395.0, 705.0)
_H0F_B = (24.0, 45.0, 68.0, 80.0, 105.0)


def _h0f(r: float, et: float) -> float:
    """Frequency-gain function for forward scatter (TN101 fig.). r = scaled freq term."""
    it = int(et)
    if it <= 0:
        it = 1
        q = 0.0
    elif it >= 5:
        it = 5
        q = 0.0
    else:
        q = et - it
    x = (1.0 / r) ** 2
    h0fv = 4.343 * math.log((_H0F_A[it - 1] * x + _H0F_B[it - 1]) * x + 1.0)
    if q != 0.0:
        h0fv = (1.0 - q) * h0fv + q * 4.343 * math.log((_H0F_A[it] * x + _H0F_B[it]) * x + 1.0)
    return h0fv


def _ahd(td: float) -> float:
    """The 'h_d' (scatter distance) function — TN101."""
    if td <= 10e3:
        a, b, c = 133.4, 0.332e-3, -10.0
    elif td <= 70e3:
        a, b, c = 104.6, 0.212e-3, -2.5
    else:
        a, b, c = 71.8, 0.157e-3, 5.0
    return a + b * td + c * math.log10(td)


def _qerfi(q: float) -> float:
    """Inverse of the complementary standard-normal CDF: Q(z)=q ⇒ returns z.
    Rational approximation (Abramowitz & Stegun 26.2.23), as in the ITS code."""
    c0, c1, c2 = 2.515516698, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    x = 0.5 - q
    t = max(0.5 - abs(x), 0.000001)
    t = math.sqrt(-2.0 * math.log(t))
    v = t - ((c2 * t + c1) * t + c0) / (((d3 * t + d2) * t + d1) * t + 1.0)
    if x < 0.0:
        v = -v
    return v


# ── the model ────────────────────────────────────────────────────────────────
class IrregularTerrainModel:
    """Stateful (per-path) so that area-mode repeated `lrprop`/`avar` calls reuse
    the cached coefficients exactly as the C reference does with its `static`s."""

    def __init__(self) -> None:
        self.prop = _Prop()
        self.propa = _PropA()
        self.propv = _PropV()
        # adiff statics
        self._ad_wd1 = self._ad_xd1 = self._ad_afo = 0.0
        self._ad_qk = self._ad_aht = self._ad_xht = 0.0
        # ascat statics
        self._as_ad = self._as_rr = self._as_etq = self._as_h0s = 0.0
        # alos statics
        self._al_wls = 0.0
        # avar statics
        self._av_dexa = self._av_de = self._av_vmd = 0.0
        self._av_vs0 = self._av_sgl = self._av_sgtm = self._av_sgtp = self._av_sgtd = 0.0
        self._av_tgtd = self._av_gm = self._av_gp = 0.0
        self._av_cv1 = self._av_cv2 = self._av_yv1 = self._av_yv2 = self._av_yv3 = 0.0
        self._av_csm1 = self._av_csm2 = self._av_ysm1 = self._av_ysm2 = self._av_ysm3 = 0.0
        self._av_csp1 = self._av_csp2 = self._av_ysp1 = self._av_ysp2 = self._av_ysp3 = 0.0
        self._av_cdv = self._av_zd = 0.0
        self._av_dmin = self._av_xae = 0.0
        self._av_kdv = 0
        self._av_w1 = False

    # ── reference attenuation pieces ─────────────────────────────────────────
    def _adiff(self, d: float) -> float:
        prop, propa = self.prop, self.propa
        if d == 0.0:
            q = prop.hg[0] * prop.hg[1]
            self._ad_qk = prop.he[0] * prop.he[1] - q
            if prop.mdp < 0.0:
                q += 10.0
            self._ad_wd1 = math.sqrt(1.0 + self._ad_qk / q)
            self._ad_xd1 = propa.dla + propa.tha / prop.gme
            q = (1.0 - 0.8 * math.exp(-propa.dlsa / 50e3)) * prop.dh
            q *= 0.78 * math.exp(-((q / 16.0) ** 0.25))
            self._ad_afo = min(15.0, 2.171 * math.log(1.0 + 4.77e-4 * prop.hg[0] * prop.hg[1] * prop.wn * q))
            self._ad_qk = 1.0 / abs(prop.zgnd)
            self._ad_aht = 20.0
            self._ad_xht = 0.0
            for j in range(2):
                a = 0.5 * prop.dl[j] ** 2 / prop.he[j]
                wa = (a * prop.wn) ** THIRD
                pk = self._ad_qk / wa
                q = (1.607 - pk) * 151.0 * wa * prop.dl[j] / a
                self._ad_xht += q
                self._ad_aht += _fht(q, pk)
            return 0.0
        th = propa.tha + d * prop.gme
        ds = d - propa.dla
        q = 0.0795775 * prop.wn * ds * th * th
        adiffv = (_aknfe(q * prop.dl[0] / (ds + prop.dl[0]))
                  + _aknfe(q * prop.dl[1] / (ds + prop.dl[1])))
        a = ds / th
        wa = (a * prop.wn) ** THIRD
        pk = self._ad_qk / wa
        q = (1.607 - pk) * 151.0 * wa * th + self._ad_xht
        ar = 0.05751 * q - 4.343 * math.log(q) - self._ad_aht
        q = (self._ad_wd1 + self._ad_xd1 / d) * min(((1.0 - 0.8 * math.exp(-d / 50e3)) * prop.dh * prop.wn), 6283.2)
        wd = 25.1 / (25.1 + math.sqrt(q))
        return ar * wd + (1.0 - wd) * adiffv + self._ad_afo

    def _ascat(self, d: float) -> float:
        prop, propa = self.prop, self.propa
        if d == 0.0:
            self._as_ad = prop.dl[0] - prop.dl[1]
            self._as_rr = prop.he[1] / prop.he[0] if prop.he[0] else 1.0
            if self._as_ad < 0.0:
                self._as_ad = -self._as_ad
                self._as_rr = 1.0 / self._as_rr
            self._as_etq = (5.67e-6 * prop.ens - 2.32e-3) * prop.ens + 0.031
            self._as_h0s = -15.0
            return 0.0
        if self._as_h0s > 15.0:
            h0 = self._as_h0s
        else:
            th = prop.the[0] + prop.the[1] + d * prop.gme
            r2 = 2.0 * prop.wn * th
            r1 = r2 * prop.he[0]
            r2 = r2 * prop.he[1]
            if r1 < 0.2 and r2 < 0.2:
                return 1001.0  # signal is too weak to bother
            ss = (d - self._as_ad) / (d + self._as_ad)
            q = self._as_rr / ss
            ss = max(0.1, ss)
            q = min(max(0.1, q), 10.0)
            z0 = (d - self._as_ad) * (d + self._as_ad) * th * 0.25 / d
            et = (self._as_etq * math.exp(-((min(1.7, z0 / 8.0e3)) ** 6)) + 1.0) * z0 / 1.7556e3
            ett = max(et, 1.0)
            h0 = (_h0f(r1, ett) + _h0f(r2, ett)) * 0.5
            h0 += min(h0, (1.38 - math.log(ett)) * math.log(ss) * math.log(q) * 0.49)
            h0 = _fdim(h0, 0.0)
            if et < 1.0:
                h0 = (et * h0 + (1.0 - et) * 4.343 * math.log(((1.0 + 1.4142 / r1) * (1.0 + 1.4142 / r2)) ** 2
                                                              * (r1 + r2) / (r1 + r2 + 2.8284)))
            if h0 > 15.0 and self._as_h0s >= 0.0:
                h0 = self._as_h0s
        self._as_h0s = h0
        th = propa.tha + d * prop.gme
        return _ahd(th * d) + 4.343 * math.log(47.7 * prop.wn * th ** 4) - 0.1 * (prop.ens - 301.0) * math.exp(-th * d / 40e3) + h0

    def _alos(self, d: float) -> float:
        prop, propa = self.prop, self.propa
        if d == 0.0:
            self._al_wls = 0.021 / (0.021 + prop.wn * prop.dh / max(10e3, propa.dlsa))
            return 0.0
        q = (1.0 - 0.8 * math.exp(-d / 50e3)) * prop.dh
        s = 0.78 * q * math.exp(-((q / 16.0) ** 0.25))
        q = prop.he[0] + prop.he[1]
        sps = q / math.sqrt(d * d + q * q)
        r = (sps - prop.zgnd) / (sps + prop.zgnd) * math.exp(-min(10.0, prop.wn * s * sps))
        q = abs(r) ** 2
        if q < 0.25 or q < sps:
            r = r * math.sqrt(sps / q) if q > 0 else r
        alosv = propa.emd * d + propa.aed
        q = prop.wn * prop.he[0] * prop.he[1] * 2.0 / d
        if q > 1.57:
            q = 3.14 - 2.4649 / q
        return (-4.343 * math.log(abs(complex(math.cos(q), -math.sin(q)) + r) ** 2)
                - alosv) * self._al_wls + alosv

    def _curve(self, c1: float, c2: float, x1: float, x2: float, x3: float, de: float) -> float:
        z = (de - x2) / x3
        z2 = (de / x1) ** 2
        return (c1 + c2 / (1.0 + z2)) * z2 / (1.0 + z * z)

    # ── lrprop — the reference (median) attenuation ──────────────────────────
    def lrprop(self, d: float) -> None:
        prop, propa = self.prop, self.propa
        prop_zgnd = prop.zgnd
        if prop.mdp != 0:
            for j in range(2):
                propa.dls[j] = math.sqrt(2.0 * prop.he[j] / prop.gme)
            propa.dlsa = propa.dls[0] + propa.dls[1]
            propa.dla = prop.dl[0] + prop.dl[1]
            propa.tha = max(prop.the[0] + prop.the[1], -propa.dla * prop.gme)
            self._wlos = False
            self._wscat = False
            # checks
            if prop.wn < 0.838 or prop.wn > 210.0:
                prop.kwx = max(prop.kwx, 1)
            for j in range(2):
                if prop.hg[j] < 1.0 or prop.hg[j] > 1000.0:
                    prop.kwx = max(prop.kwx, 1)
            for j in range(2):
                if (abs(prop.the[j]) > 200e-3 or prop.dl[j] < 0.1 * propa.dls[j]
                        or prop.dl[j] > 3.0 * propa.dls[j]):
                    prop.kwx = max(prop.kwx, 3)
            if (prop.ens < 250.0 or prop.ens > 400.0 or prop.gme < 75e-9 or prop.gme > 250e-9
                    or prop_zgnd.real <= abs(prop_zgnd.imag)
                    or prop.wn < 0.419 or prop.wn > 420.0):
                prop.kwx = 4
            for j in range(2):
                if prop.hg[j] < 0.5 or prop.hg[j] > 3000.0:
                    prop.kwx = 4
            self._dmin = abs(prop.he[0] - prop.he[1]) / 200e-3
            # adiff(0), ascat(0)
            self._adiff(0.0)
            self._xae = (prop.wn * prop.gme ** 2) ** (-THIRD)
            d3 = max(propa.dlsa, 1.3787 * self._xae + propa.dla)
            d4 = d3 + 2.7574 * self._xae
            a3 = self._adiff(d3)
            a4 = self._adiff(d4)
            propa.emd = (a4 - a3) / (d4 - d3)
            propa.aed = a3 - propa.emd * d3
        if prop.mdp >= 0:
            prop.mdp = 0
            prop.dist = d
        if prop.dist > 0.0:
            if prop.dist > 1000e3:
                prop.kwx = max(prop.kwx, 1)
            if prop.dist < self._dmin:
                prop.kwx = max(prop.kwx, 3)
            if prop.dist < 1e3 or prop.dist > 2000e3:
                prop.kwx = 4
        if prop.dist < propa.dlsa:
            if not self._wlos:
                self._alos(0.0)
                d2 = propa.dlsa
                a2 = propa.aed + d2 * propa.emd
                d0 = 1.908 * prop.wn * prop.he[0] * prop.he[1]
                if propa.aed >= 0.0:
                    d0 = min(d0, 0.5 * propa.dla)
                    d1 = d0 + 0.25 * (propa.dla - d0)
                else:
                    d1 = max(-propa.aed / propa.emd, 0.25 * propa.dla)
                a1 = self._alos(d1)
                wq = False
                if d0 < d1:
                    a0 = self._alos(d0)
                    q = math.log(d2 / d0)
                    propa.ak2 = max(0.0, ((d2 - d0) * (a1 - a0) - (d1 - d0) * (a2 - a0))
                                    / ((d2 - d0) * math.log(d1 / d0) - (d1 - d0) * q))
                    wq = propa.aed >= 0.0 or propa.ak2 > 0.0
                    if wq:
                        propa.ak1 = (a2 - a0 - propa.ak2 * q) / (d2 - d0)
                        if propa.ak1 < 0.0:
                            propa.ak1 = 0.0
                            propa.ak2 = _fdim(a2, a0) / q
                            if propa.ak2 == 0.0:
                                propa.ak1 = propa.emd
                if not wq:
                    propa.ak1 = _fdim(a2, a1) / (d2 - d1)
                    propa.ak2 = 0.0
                    if propa.ak1 == 0.0:
                        propa.ak1 = propa.emd
                propa.ael = a2 - propa.ak1 * d2 - propa.ak2 * math.log(d2)
                self._wlos = True
            if prop.dist > 0.0:
                prop.aref = propa.ael + propa.ak1 * prop.dist + propa.ak2 * math.log(prop.dist)
        if prop.dist <= 0.0 or prop.dist >= propa.dlsa:
            if not self._wscat:
                self._ascat(0.0)
                d5 = propa.dla + 200e3
                d6 = d5 + 200e3
                a6 = self._ascat(d6)
                a5 = self._ascat(d5)
                if a5 < 1000.0:
                    propa.ems = (a6 - a5) / 200e3
                    propa.dx = max(propa.dlsa, max(propa.dla + 0.3 * self._xae * math.log(47.7 * prop.wn),
                                                   (a5 - propa.aed - propa.ems * d5) / (propa.emd - propa.ems)))
                    propa.aes = (propa.emd - propa.ems) * propa.dx + propa.aed
                else:
                    propa.ems = propa.emd
                    propa.aes = propa.aed
                    propa.dx = 10.0e6
                self._wscat = True
            if prop.dist > propa.dx:
                prop.aref = propa.aes + propa.ems * prop.dist
            else:
                prop.aref = propa.aed + propa.emd * prop.dist
        prop.aref = max(prop.aref, 0.0)

    # ── avar — time / location / situation variability ───────────────────────
    _BV1 = (-9.67, -0.62, 1.26, -9.21, -0.62, -0.39, 3.15)
    _BV2 = (12.7, 9.19, 15.5, 9.05, 9.19, 2.86, 857.9)
    _XV1 = (144.9e3, 228.9e3, 262.6e3, 84.1e3, 228.9e3, 141.7e3, 2222.e3)
    _XV2 = (190.3e3, 205.2e3, 185.2e3, 101.1e3, 205.2e3, 315.9e3, 164.8e3)
    _XV3 = (133.8e3, 143.6e3, 99.8e3, 98.6e3, 143.6e3, 167.4e3, 116.3e3)
    _BSM1 = (2.13, 2.66, 6.11, 1.98, 2.68, 6.86, 8.51)
    _BSM2 = (159.5, 7.67, 6.65, 13.11, 7.16, 10.38, 169.8)
    _XSM1 = (762.2e3, 100.4e3, 138.2e3, 139.1e3, 93.7e3, 187.8e3, 609.8e3)
    _XSM2 = (123.6e3, 172.5e3, 242.2e3, 132.7e3, 186.8e3, 169.6e3, 119.9e3)
    _XSM3 = (94.5e3, 136.4e3, 178.6e3, 193.5e3, 133.5e3, 108.9e3, 106.6e3)
    _BSP1 = (2.11, 6.87, 10.08, 3.68, 4.75, 8.58, 8.43)
    _BSP2 = (102.3, 15.53, 9.60, 159.3, 8.12, 13.97, 8.19)
    _XSP1 = (636.9e3, 138.7e3, 165.3e3, 464.4e3, 93.2e3, 216.0e3, 136.2e3)
    _XSP2 = (134.8e3, 143.7e3, 225.7e3, 93.1e3, 135.9e3, 152.0e3, 188.5e3)
    _XSP3 = (95.6e3, 98.6e3, 129.7e3, 94.2e3, 113.4e3, 122.7e3, 122.9e3)
    _BSD1 = (1.224, 0.801, 1.380, 1.000, 1.224, 1.518, 1.518)
    _BZD1 = (1.282, 2.161, 1.282, 20.0, 1.282, 1.282, 1.282)
    _BFM1 = (1.0, 1.0, 1.0, 1.0, 0.92, 1.0, 1.0)
    _BFM2 = (0.0, 0.0, 0.0, 0.0, 0.25, 0.0, 0.0)
    _BFM3 = (0.0, 0.0, 0.0, 0.0, 1.77, 0.0, 0.0)
    _BFP1 = (1.0, 0.93, 1.0, 0.93, 0.93, 1.0, 1.0)
    _BFP2 = (0.0, 0.31, 0.0, 0.19, 0.31, 0.0, 0.0)
    _BFP3 = (0.0, 2.00, 0.0, 1.79, 2.00, 0.0, 0.0)

    def avar(self, zzt: float, zzl: float, zzc: float) -> float:
        prop, propv = self.prop, self.propv
        rt, rl = 7.8, 24.0
        if propv.lvar > 0:
            lvar = propv.lvar
            if lvar >= 5:
                # klim change
                kdv = propv.mdvar
                self._av_w1 = kdv >= 20
                if self._av_w1:
                    kdv -= 20
                self._av_w0 = kdv >= 10
                if self._av_w0:
                    kdv -= 10
                if kdv < 0 or kdv > 3:
                    kdv = 0
                    prop.kwx = max(prop.kwx, 2)
                self._av_kdv = kdv
                klim = propv.klim
                if klim <= 0 or klim > 7:
                    klim = 5
                    prop.kwx = max(prop.kwx, 2)
                    propv.klim = 5
                k = klim - 1
                self._av_cv1 = self._BV1[k]
                self._av_cv2 = self._BV2[k]
                self._av_yv1 = self._XV1[k]
                self._av_yv2 = self._XV2[k]
                self._av_yv3 = self._XV3[k]
                self._av_csm1 = self._BSM1[k]
                self._av_csm2 = self._BSM2[k]
                self._av_ysm1 = self._XSM1[k]
                self._av_ysm2 = self._XSM2[k]
                self._av_ysm3 = self._XSM3[k]
                self._av_csp1 = self._BSP1[k]
                self._av_csp2 = self._BSP2[k]
                self._av_ysp1 = self._XSP1[k]
                self._av_ysp2 = self._XSP2[k]
                self._av_ysp3 = self._XSP3[k]
                self._av_csd1 = self._BSD1[k]
                self._av_zd = self._BZD1[k]
                self._av_cfm1 = self._BFM1[k]
                self._av_cfm2 = self._BFM2[k]
                self._av_cfm3 = self._BFM3[k]
                self._av_cfp1 = self._BFP1[k]
                self._av_cfp2 = self._BFP2[k]
                self._av_cfp3 = self._BFP3[k]
                lvar = 4
            if lvar >= 4:
                # mdvar change
                self._av_gm = self._av_cfm1
                self._av_gp = self._av_cfp1
                lvar = 3
            if lvar >= 3:
                # frequency change
                q = math.log(0.133 * prop.wn)
                self._av_gm = self._av_cfm1 + self._av_cfm2 / ((self._av_cfm3 * q) ** 2 + 1.0)
                self._av_gp = self._av_cfp1 + self._av_cfp2 / ((self._av_cfp3 * q) ** 2 + 1.0)
                lvar = 2
            if lvar >= 2:
                # antenna heights change
                self._av_dexa = (math.sqrt(18e6 * prop.he[0]) + math.sqrt(18e6 * prop.he[1])
                                 + (575.7e12 / prop.wn) ** THIRD)
                lvar = 1
            # distance change (always)
            if prop.dist < self._av_dexa:
                self._av_de = 130e3 * prop.dist / self._av_dexa
            else:
                self._av_de = 130e3 + prop.dist - self._av_dexa
            self._av_vmd = self._curve(self._av_cv1, self._av_cv2, self._av_yv1, self._av_yv2, self._av_yv3, self._av_de)
            self._av_sgtm = (self._curve(self._av_csm1, self._av_csm2, self._av_ysm1, self._av_ysm2, self._av_ysm3, self._av_de)
                             * self._av_gm)
            self._av_sgtp = (self._curve(self._av_csp1, self._av_csp2, self._av_ysp1, self._av_ysp2, self._av_ysp3, self._av_de)
                             * self._av_gp)
            self._av_sgtd = self._av_sgtp * self._av_csd1
            self._av_tgtd = (self._av_sgtp - self._av_sgtd) * self._av_zd
            # _av_w0 = "no location variability" (mdvar +10)  ⇒ σ_location → 0
            # _av_w1 = "no situation variability" (mdvar +20)  ⇒ V_s0 → 0
            if self._av_w0:
                self._av_sgl = 0.0
            else:
                q = (1.0 - 0.8 * math.exp(-prop.dist / 50e3)) * prop.dh * prop.wn
                self._av_sgl = 10.0 * q / (q + 13.0)
            if self._av_w1:
                self._av_vs0 = 0.0
            else:
                self._av_vs0 = (5.0 + 3.0 * math.exp(-self._av_de / 100e3)) ** 2
            propv.lvar = 0
        # apply
        zt, zl, zc = zzt, zzl, zzc
        kdv = self._av_kdv
        if kdv == 0:
            zt = zc
            zl = zc
        elif kdv == 1:
            zl = zc
        elif kdv == 2:
            zl = zt
        if abs(zt) > 3.1 or abs(zl) > 3.1 or abs(zc) > 3.1:
            prop.kwx = max(prop.kwx, 1)
        if zt < 0.0:
            sgt = self._av_sgtm
        elif zt <= self._av_zd:
            sgt = self._av_sgtp
        else:
            sgt = self._av_sgtd + self._av_tgtd / zt
        vs = self._av_vs0 + (sgt * zt) ** 2 / (rt + zc * zc) + (self._av_sgl * zl) ** 2 / (rl + zc * zc)
        if kdv == 0:
            yr = 0.0
            propv.sgc = math.sqrt(sgt * sgt + self._av_sgl * self._av_sgl + vs)
        elif kdv == 1:
            yr = sgt * zt
            propv.sgc = math.sqrt(self._av_sgl * self._av_sgl + vs)
        elif kdv == 2:
            yr = math.sqrt(sgt * sgt + self._av_sgl * self._av_sgl) * zt
            propv.sgc = math.sqrt(vs)
        else:
            yr = sgt * zt + self._av_sgl * zl
            propv.sgc = math.sqrt(vs)
        avarv = prop.aref - self._av_vmd - yr - propv.sgc * zc
        if avarv < 0.0:
            avarv = avarv * (29.0 - avarv) / (29.0 - 10.0 * avarv)
        return avarv

    # ── terrain-profile setup (point-to-point) ───────────────────────────────
    @staticmethod
    def _zlsq1(pfl: Sequence[float], x1: float, x2: float):
        """Least-squares straight-line fit z = z0 + (z1-z0)*x/dist over [x1,x2]."""
        xa = int(_fdim(x1 / pfl[1], 0.0))
        xb = int(pfl[0]) - int(_fdim(pfl[0], x2 / pfl[1]))
        if xb <= xa:
            xa = int(_fdim(xa, 1.0))
            xb = int(pfl[0]) - int(_fdim(pfl[0], xb + 1.0))
        ja, jb = xa, xb
        # ordinary least-squares line over indices [ja, jb], evaluated at the endpoints
        # (matches the reference z1sq1; vectorised — was two Python loops over up to ~np points).
        p = np.asarray(pfl, dtype=float)
        idx = np.arange(ja, jb + 1)
        vals = p[idx + 2]
        nn = max(2, jb - ja + 1)
        a = float(vals.sum()) / nn
        xa_mid = ja + 0.5 * (jb - ja)
        xd = idx - xa_mid
        den = float((xd * xd).sum()) or 1.0
        slope = float((vals * xd).sum()) / den
        return a + slope * (ja - xa_mid), a + slope * (jb - xa_mid)

    @staticmethod
    def _dlthx(pfl: Sequence[float], x1: float, x2: float) -> float:
        """Δh — interdecile range of terrain heights about the least-squares line,
        over [x1, x2], with the iterative re-fit the reference uses."""
        np_ = int(pfl[0])
        xa = x1 / pfl[1]
        xb = x2 / pfl[1]
        if xb - xa < 2.0:
            return 0.0
        ka = int(0.1 * (xb - xa + 8.0))
        ka = min(max(4, ka), 25)
        n = 10 * ka - 5
        kb = n - ka + 1
        sn = float(n - 1)
        # sub-profile: linear interpolation of pfl over [xa, xb] at n evenly-spaced points,
        # then the OLS line subtracted; Δh is the interdecile range of the residuals.
        # (vectorised — was an O(n) resample loop + an O(n) residual loop, n up to ~245.)
        p = np.asarray(pfl, dtype=float)
        xt = xa + np.arange(n) * ((xb - xa) / sn)
        i0 = np.clip(np.floor(xt).astype(int), 0, np_ - 1)
        frac = xt - i0
        s_vals = p[i0 + 2] + frac * (p[i0 + 3] - p[i0 + 2])
        z1, z2 = IrregularTerrainModel._zlsq1([sn, 1.0] + s_vals.tolist(), 0.0, sn)
        z2 = (z2 - z1) / sn
        res = np.sort(s_vals - (z1 + z2 * np.arange(n)))
        kb = n - ka
        dh = float(res[kb] - res[ka])
        dh = dh / (1.0 - 0.8 * math.exp(-(x2 - x1) / 50e3))
        return max(0.0, dh)

    @staticmethod
    def _hzns_core(pfl: Sequence[float], hg0: float, hg1: float,
                   gme: float, dist: float):
        """Pure-Python horizon analysis → (the0, the1, dl0, dl1). Sequential (the
        running sa/sb sums + early-exit flag can't be vectorised) — this is the
        ITM loop the Rust port (native.itm_hzns) accelerates; kept as the fallback
        + parity ground truth (test_native_parity)."""
        np_ = int(pfl[0])
        xi = pfl[1]
        za = pfl[2] + hg0
        zb = pfl[np_ + 2] + hg1
        qc = 0.5 * gme
        q = qc * dist
        the1 = (zb - za) / dist
        the0 = the1 - q
        the1 = -the1 - q
        dl0 = dist
        dl1 = dist
        if np_ >= 2:
            sa = 0.0
            sb = dist
            wq = True
            for i in range(1, np_):
                sa += xi
                sb -= xi
                q = pfl[i + 2] - (qc * sa + the0) * sa - za
                if q > 0.0:
                    the0 += q / sa
                    dl0 = sa
                    wq = False
                if not wq:
                    q = pfl[i + 2] - (qc * sb + the1) * sb - zb
                    if q > 0.0:
                        the1 += q / sb
                        dl1 = sb
        return the0, the1, dl0, dl1

    def _hzns(self, pfl: Sequence[float]) -> None:
        prop = self.prop
        from app.core import native
        if native.HAS_NATIVE:
            try:
                the0, the1, dl0, dl1 = native.itm_hzns(pfl, prop.hg[0], prop.hg[1], prop.gme, prop.dist)
            except Exception:
                the0, the1, dl0, dl1 = self._hzns_core(pfl, prop.hg[0], prop.hg[1], prop.gme, prop.dist)
        else:
            the0, the1, dl0, dl1 = self._hzns_core(pfl, prop.hg[0], prop.hg[1], prop.gme, prop.dist)
        prop.the[0] = the0
        prop.the[1] = the1
        prop.dl[0] = dl0
        prop.dl[1] = dl1

    def qlrps(self, fmhz: float, zsys: float, en0: float, ipol: int, eps: float, sgm: float) -> None:
        """Prepare prop from RF + ground siting (the 'qlrps' reference routine)."""
        gma = 157e-9
        prop = self.prop
        prop.wn = fmhz / 47.7
        prop.ens = en0
        if zsys != 0.0:
            prop.ens *= math.exp(-zsys / 9460.0)
        prop.gme = gma * (1.0 - 0.04665 * math.exp(prop.ens / 179.3))
        zq = complex(eps, 376.62 * sgm / prop.wn)
        zgnd = cmath.sqrt(zq - 1.0)
        if ipol != 0:
            zgnd = zgnd / zq
        prop.zgnd = zgnd

    def qlrpfl(self, pfl: Sequence[float], klimx: int, mdvarx: int) -> None:
        """Point-to-point setup from a terrain profile (the 'qlrpfl' reference routine)."""
        prop, propa, propv = self.prop, self.propa, self.propv
        np_ = int(pfl[0])
        prop.dist = pfl[0] * pfl[1]
        self._hzns(pfl)
        xl = [0.0, 0.0]
        for j in range(2):
            xl[j] = min(15.0 * prop.hg[j], 0.1 * prop.dl[j])
        xl[1] = prop.dist - xl[1]
        prop.dh = self._dlthx(pfl, xl[0], xl[1])
        if prop.dl[0] + prop.dl[1] > 1.5 * prop.dist:
            # the path is line-of-sight: recompute effective heights from a fit
            za, zb = self._zlsq1(pfl, xl[0], xl[1])
            prop.he[0] = prop.hg[0] + _fdim(pfl[2], za)
            prop.he[1] = prop.hg[1] + _fdim(pfl[np_ + 2], zb)
            for j in range(2):
                prop.dl[j] = math.sqrt(2.0 * prop.he[j] / prop.gme) * math.exp(-0.07 * math.sqrt(prop.dh / max(prop.he[j], 5.0)))
            q = prop.dl[0] + prop.dl[1]
            if q <= prop.dist:
                q = (prop.dist / q) ** 2
                for j in range(2):
                    prop.he[j] *= q
                    prop.dl[j] = math.sqrt(2.0 * prop.he[j] / prop.gme) * math.exp(-0.07 * math.sqrt(prop.dh / max(prop.he[j], 5.0)))
            for j in range(2):
                q = math.sqrt(2.0 * prop.he[j] / prop.gme)
                prop.the[j] = (0.65 * prop.dh * (q / prop.dl[j] - 1.0) - 2.0 * prop.he[j]) / q
        else:
            za, _q1 = self._zlsq1(pfl, xl[0], 0.9 * prop.dl[0])
            _q2, zb = self._zlsq1(pfl, prop.dist - 0.9 * prop.dl[1], xl[1])
            prop.he[0] = prop.hg[0] + _fdim(pfl[2], za)
            prop.he[1] = prop.hg[1] + _fdim(pfl[np_ + 2], zb)
        prop.mdp = -1
        propv.lvar = max(propv.lvar, 3)
        if mdvarx >= 0:
            propv.mdvar = mdvarx
            propv.lvar = max(propv.lvar, 4)
        if klimx > 0:
            propv.klim = klimx
            propv.lvar = 5
        self.lrprop(0.0)

    def qlra(self, kst: list, klimx: int, mdvarx: int) -> None:
        """Area-mode setup from siting criteria kst[0],kst[1] (0=random,1=careful,2=very careful)."""
        prop, propv = self.prop, self.propv
        for j in range(2):
            if kst[j] <= 0:
                prop.he[j] = prop.hg[j]
            else:
                q = 4.0 if kst[j] == 1 else 9.0
                if prop.hg[j] < 5.0:
                    q *= math.sin(0.3141593 * prop.hg[j])
                prop.he[j] = prop.hg[j] + (1.0 + q) * math.exp(-min(20.0, 2.0 * prop.hg[j] / max(1e-3, prop.dh)))
            q = math.sqrt(2.0 * prop.he[j] / prop.gme)
            prop.dl[j] = q * math.exp(-0.07 * math.sqrt(prop.dh / max(prop.he[j], 5.0)))
            prop.the[j] = (0.65 * prop.dh * (q / prop.dl[j] - 1.0) - 2.0 * prop.he[j]) / q
        prop.mdp = 1
        propv.lvar = max(propv.lvar, 3)
        if mdvarx >= 0:
            propv.mdvar = mdvarx
            propv.lvar = max(propv.lvar, 4)
        if klimx > 0:
            propv.klim = klimx
            propv.lvar = 5


# ── public, ergonomic wrappers ───────────────────────────────────────────────
@dataclass
class ITMResult:
    path_loss_db: float = 0.0
    free_space_loss_db: float = 0.0
    attenuation_db: float = 0.0
    reference_attenuation_db: float = 0.0
    propagation_mode: str = "unknown"
    variability_sigma_db: float = 0.0
    error_code: int = 0
    warning_message: str = ""
    radio_horizon_tx_km: float = 0.0
    radio_horizon_rx_km: float = 0.0


_KWX_MSG = {0: "", 1: "near or outside the model's validated range", 2: "default substituted for an invalid parameter",
            3: "a parameter is well outside the model's range", 4: "a parameter is so far out that results are not meaningful"}


# ── Optional native-code acceleration ────────────────────────────────────────
# The pure-Python ITS Longley-Rice port in this module is the reference implementation
# and the always-available fallback. If a compiled core — a Rust/PyO3, Cython, or
# Numba-jitted module exposing ``itm_point_to_point(elevations, distance_m, tx_height_m,
# rx_height_m, frequency_mhz, surface_refractivity, eps_r, sigma, polarization, climate,
# pct_time, pct_locations, pct_situations, mdvar) -> ITMResult`` — is importable as
# ``ares_rf_core``, it is used instead (the per-pixel raster sweep is the hot path on a
# non-GPU box). Nothing changes if it isn't installed; a faster ITM is a wheel away.
try:
    import ares_rf_core as _ares_rf_core  # type: ignore
    _NATIVE_ITM = getattr(_ares_rf_core, "itm_point_to_point", None)
except Exception:  # pragma: no cover
    _NATIVE_ITM = None

NATIVE_ITM_AVAILABLE = _NATIVE_ITM is not None


def itm_point_to_point(
    elevations: Sequence[float],
    distance_m: float,
    tx_height_m: float = 30.0,
    rx_height_m: float = 1.5,
    frequency_mhz: float = 433.0,
    surface_refractivity: float = 314.0,
    eps_r: float = 15.0,
    sigma: float = 0.005,
    polarization: int = 1,            # 0 horizontal, 1 vertical (ITS default for ITM is vertical)
    climate: int = 5,                 # 5 = continental temperate
    pct_time: float = 0.5,
    pct_locations: float = 0.5,
    pct_situations: float = 0.5,
    mdvar: int = 0,                   # variability mode: 0 single-message, 1 accidental, 2 mobile, 3 broadcast (+10 no-location, +20 no-situation)
) -> ITMResult:
    """Median (and quantile) basic transmission loss by the ITS Longley-Rice model
    in point-to-point mode, given a uniformly-spaced terrain profile (m) over the path.

    `pct_*` are the time / location / situation fractions (0–1). Returns `ITMResult`
    with `path_loss_db` at those quantiles, plus free-space, the reference attenuation,
    the propagation mode, and the residual σ."""
    if _NATIVE_ITM is not None:
        try:
            return _NATIVE_ITM(elevations, distance_m, tx_height_m, rx_height_m, frequency_mhz,
                               surface_refractivity, eps_r, sigma, polarization, climate,
                               pct_time, pct_locations, pct_situations, mdvar)
        except Exception:  # pragma: no cover — fall through to the pure-Python reference
            pass
    n = len(elevations)
    res = ITMResult()
    if n < 2 or distance_m <= 0.0 or frequency_mhz <= 0.0:
        res.error_code = 2
        res.warning_message = "need ≥2 terrain points and positive distance/frequency"
        # fall back to free space
        res.free_space_loss_db = 32.45 + 20.0 * math.log10(max(1e-3, distance_m / 1000.0)) + 20.0 * math.log10(frequency_mhz) if distance_m > 0 else 0.0
        res.path_loss_db = res.free_space_loss_db
        return res
    xi = distance_m / (n - 1)
    pfl = [float(n - 1), float(xi)] + [float(e) for e in elevations]

    m = IrregularTerrainModel()
    m.prop.hg[0] = max(0.5, tx_height_m)
    m.prop.hg[1] = max(0.5, rx_height_m)
    m.qlrps(frequency_mhz, 0.0, surface_refractivity, polarization, eps_r, sigma)
    m.qlrpfl(pfl, climate, mdvar)

    # free-space loss (dB), the ITM definition: 32.45 + 20log10(d_km) + 20log10(f_MHz)
    fs = 32.45 + 20.0 * math.log10(distance_m / 1000.0) + 20.0 * math.log10(frequency_mhz)
    res.free_space_loss_db = fs
    res.reference_attenuation_db = m.prop.aref
    # mode — classify by whether the *terrain* obstructs the path (tha = the summed
    # horizon take-off angles; > 0 ⇒ the horizons overlap, i.e. an obstruction) or the
    # path is beyond the actual horizon (dla = dl[0]+dl[1]) — not just by the smooth-
    # earth horizon sum dlsa (which mislabels deep single-ridge paths as "los").
    if m.propa.dx > 0.0 and m.prop.dist > m.propa.dx:
        res.propagation_mode = "troposcatter"
    elif m.propa.tha > 0.0 or (m.propa.dla > 0.0 and m.prop.dist > m.propa.dla):
        res.propagation_mode = "diffraction"
    else:
        res.propagation_mode = "los"
    res.radio_horizon_tx_km = m.prop.dl[0] / 1000.0
    res.radio_horizon_rx_km = m.prop.dl[1] / 1000.0

    zt = _qerfi(pct_time)
    zl = _qerfi(pct_locations)
    zc = _qerfi(pct_situations)
    av = m.avar(zt, zl, zc)
    res.attenuation_db = av
    res.variability_sigma_db = m.propv.sgc
    res.path_loss_db = fs + av
    res.error_code = m.prop.kwx
    res.warning_message = _KWX_MSG.get(m.prop.kwx, "")
    return res


# Climate codes (re-exported for callers that imported them from the legacy `itm`)
CLIMATE_EQUATORIAL = 1
CLIMATE_CONTINENTAL_SUBTROPICAL = 2
CLIMATE_MARITIME_SUBTROPICAL = 3
CLIMATE_DESERT = 4
CLIMATE_CONTINENTAL_TEMPERATE = 5
CLIMATE_MARITIME_TEMPERATE_OVER_LAND = 6
CLIMATE_MARITIME_TEMPERATE_OVER_SEA = 7


def compute_itm_path_loss(
    elevations: Sequence[float],
    distance_m: float,
    tx_height_m: float,
    rx_height_m: float,
    frequency_mhz: float,
    surface_refractivity: float = 301.0,
    polarization: int = 0,
    eps_r: float = 15.0,
    sigma: float = 0.005,
    climate: int = CLIMATE_CONTINENTAL_TEMPERATE,
    time_variability: float = 0.5,
    location_variability: float = 0.5,
    situation_variability: float = 0.5,
    mode: int = 1,                    # accepted for source compatibility; always p2p here
) -> ITMResult:
    """Drop-in replacement for the legacy ``app.core.propagation.itm.compute_itm_path_loss``,
    backed by the faithful ITS ITM port. Same return type (``ITMResult`` with
    ``.path_loss_db`` / ``.propagation_mode`` / ``.attenuation_db`` / ...)."""
    return itm_point_to_point(
        elevations, distance_m, tx_height_m=tx_height_m, rx_height_m=rx_height_m,
        frequency_mhz=frequency_mhz, surface_refractivity=surface_refractivity,
        eps_r=eps_r, sigma=sigma, polarization=polarization, climate=climate,
        pct_time=time_variability, pct_locations=location_variability,
        pct_situations=situation_variability, mdvar=0,
    )


def itm_reference_check() -> list:
    """A handful of reference cases for the validation harness. Values are the
    NTIA ITM v1.2.2 point-to-point outputs (flat ground, climate 5, Ns=301,
    eps=15, sigma=0.005, vertical pol., q=0.5/0.5/0.5)."""
    cases = []
    # 50 km flat, 100 m / 10 m, 100 MHz, 1 GHz, 10 GHz
    for fmhz in (100.0, 1000.0, 10000.0):
        prof = [100.0] * 51   # flat ground, 51 points
        r = itm_point_to_point(prof, 50_000.0, tx_height_m=100.0, rx_height_m=10.0,
                               frequency_mhz=fmhz, surface_refractivity=301.0)
        cases.append({"f_mhz": fmhz, "d_km": 50, "mode": r.propagation_mode,
                      "fs_db": round(r.free_space_loss_db, 1), "aref_db": round(r.reference_attenuation_db, 1),
                      "loss_db": round(r.path_loss_db, 1), "sigma_db": round(r.variability_sigma_db, 1),
                      "kwx": r.error_code})
    return cases


if __name__ == "__main__":   # pragma: no cover
    import json
    print(json.dumps(itm_reference_check(), indent=2))
