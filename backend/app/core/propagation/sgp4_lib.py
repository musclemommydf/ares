# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sgp4_lib.py — SGP4 satellite propagation (Workstream C).

If the canonical ``sgp4`` package (Brandon Rhodes' port of Vallado's reference
code, with SDP4 deep-space) is installed, it is used. Otherwise this module
provides a vendored, faithful **SGP4 near-earth** propagator (WGS-72, Hoots &
Roehrich / SPACETRACK REPORT #3 — the same algorithm CelesTrak/STK/GPredict use),
which is exact for the LEO regime (orbital period < ~225 min: ISS, Starlink,
imaging birds, weather sats). For deep-space orbits (Molniya, GEO, period ≥ 225
min) ``pip install sgp4`` to pick up SDP4; this module flags those cases.

Public API:
  * ``Satellite.from_tle(name, line1, line2)`` — parse a TLE
  * ``sat.propagate(dt_utc)`` → ``SatState`` (ECI position/velocity TEME, geodetic
    lat/lon/alt, footprint radius)
  * ``look_angles(obs_lat, obs_lon, obs_alt_m, sat_lat, sat_lon, sat_alt_m)`` →
    (azimuth_deg, elevation_deg, slant_range_km)
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Optional

# ── try the reference package first ─────────────────────────────────────────
try:
    from sgp4.api import Satrec as _Satrec, jday as _jday   # type: ignore
    _HAVE_SGP4_PKG = True
except Exception:
    _HAVE_SGP4_PKG = False

# WGS-72 constants (the geodetic model TLEs are referenced to)
_XKMPER = 6378.135            # earth equatorial radius, km
_F = 1.0 / 298.26             # WGS-72 flattening
_MU = 398600.8                # km^3/s^2
_XKE = 60.0 / math.sqrt(_XKMPER ** 3 / _MU)   # sqrt(GM) in (er^1.5)/min  ≈ 0.0743669161
_J2 = 1.082616e-3
_J3 = -2.53881e-6
_J4 = -1.65597e-6
_CK2 = 0.5 * _J2
_CK4 = -0.375 * _J4
_A3OVK2 = -_J3 / _CK2
_QO = 120.0
_SO = 78.0
_S0 = 1.0 + _SO / _XKMPER
_QOMS2T = ((_QO - _SO) / _XKMPER) ** 4
_TWOPI = 2.0 * math.pi
_MIN_PER_DAY = 1440.0
_DE2RA = math.pi / 180.0
_OMEGA_E = 7.29211514670698e-5 * 60.0   # earth rotation, rad/min  (sidereal)


def _epoch_to_datetime(epochyr: int, epochdays: float) -> _dt.datetime:
    yr = 2000 + epochyr if epochyr < 57 else 1900 + epochyr
    base = _dt.datetime(yr, 1, 1, tzinfo=_dt.timezone.utc)
    return base + _dt.timedelta(days=epochdays - 1.0)


def _jd(d: _dt.datetime) -> float:
    d = d.astimezone(_dt.timezone.utc)
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    jdn = d.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    frac = (d.hour - 12) / 24.0 + d.minute / 1440.0 + (d.second + d.microsecond / 1e6) / 86400.0
    return jdn + frac


def _gmst_rad(jd_ut1: float) -> float:
    """Greenwich Mean Sidereal Time (rad) — IAU-82, sufficient for tracking."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    g = (67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t * t * t)
    g = math.radians((g % 86400.0) / 240.0)   # seconds → degrees → rad
    return g % _TWOPI


@dataclass
class SatState:
    lat_deg: float
    lon_deg: float
    alt_km: float
    eci_km: tuple
    eci_vel_kmps: tuple
    footprint_radius_km: float       # great-circle radius of the visibility circle (0° mask)
    deep_space: bool = False
    error: int = 0                   # 0 ok; >0 = SGP4 error code (decay, etc.)


class Satellite:
    def __init__(self):
        self.name = ""
        self.satnum = 0
        self.epoch: Optional[_dt.datetime] = None
        self._satrec = None           # the sgp4-package object, if used
        self._vendored = False
        self.deep_space = False

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_tle(cls, name: str, line1: str, line2: str) -> "Satellite":
        s = cls()
        s.name = (name or "").strip()
        if _HAVE_SGP4_PKG:
            try:
                s._satrec = _Satrec.twoline2rv(line1.strip(), line2.strip())
                s.satnum = int(getattr(s._satrec, "satnum", 0) or 0)
                # the package exposes jdsatepoch (+ frac); reconstruct a datetime for display
                jd = float(s._satrec.jdsatepoch) + float(getattr(s._satrec, "jdsatepochF", 0.0))
                s.epoch = _jd_to_datetime(jd)
                no_kozai = float(s._satrec.no_kozai)             # rad/min
                period_min = _TWOPI / no_kozai if no_kozai else 0.0
                s.deep_space = period_min >= 225.0
                return s
            except Exception:
                s._satrec = None  # fall through to the vendored implementation
        s._vendored = True
        s._init_vendored(line1.strip(), line2.strip())
        return s

    # ── propagation ──────────────────────────────────────────────────────────
    def propagate(self, when: _dt.datetime) -> SatState:
        if self._satrec is not None and not self._vendored:
            jd, fr = _jday(when.year, when.month, when.day, when.hour, when.minute,
                            when.second + when.microsecond / 1e6)
            e, r, v = self._satrec.sgp4(jd, fr)
            gmst = _gmst_rad(jd + fr)
            lat, lon, alt = _eci_to_geodetic(r, gmst)
            return SatState(lat, lon, alt, tuple(r), tuple(v),
                            _footprint_km(alt), self.deep_space, int(e))
        # vendored SGP4
        tsince = (when.astimezone(_dt.timezone.utc) - self.epoch).total_seconds() / 60.0
        r, v, err = self._sgp4_vendored(tsince)
        jd = _jd(when)
        gmst = _gmst_rad(jd)
        lat, lon, alt = _eci_to_geodetic(r, gmst)
        return SatState(lat, lon, alt, tuple(r), tuple(v), _footprint_km(alt), self.deep_space, err)

    # ── vendored SGP4 (near-earth) ───────────────────────────────────────────
    def _init_vendored(self, l1: str, l2: str) -> None:
        self.satnum = int(l1[2:7])
        epochyr = int(l1[18:20])
        epochdays = float(l1[20:32])
        self.epoch = _epoch_to_datetime(epochyr, epochdays)
        self.bstar = _expfield(l1[53:61])
        self.inclo = float(l2[8:16]) * _DE2RA
        self.nodeo = float(l2[17:25]) * _DE2RA
        self.ecco = float("0." + l2[26:33].strip())
        self.argpo = float(l2[34:42]) * _DE2RA
        self.mo = float(l2[43:51]) * _DE2RA
        self.no = float(l2[52:63]) * _TWOPI / _MIN_PER_DAY     # rad/min
        period_min = _TWOPI / self.no if self.no else 0.0
        self.deep_space = period_min >= 225.0

        # Brouwer mean-motion / SMA recovery (SPACETRACK #3)
        cosio = math.cos(self.inclo)
        theta2 = cosio * cosio
        x3thm1 = 3.0 * theta2 - 1.0
        eosq = self.ecco * self.ecco
        betao2 = 1.0 - eosq
        betao = math.sqrt(betao2)
        a1 = (_XKE / self.no) ** (2.0 / 3.0)
        del1 = 1.5 * _CK2 * x3thm1 / (a1 * a1 * betao * betao2)
        ao = a1 * (1.0 - del1 * (0.5 * (2.0 / 3.0) + del1 * (1.0 + 134.0 / 81.0 * del1)))
        delo = 1.5 * _CK2 * x3thm1 / (ao * ao * betao * betao2)
        self.no_dp = self.no / (1.0 + delo)        # "n0''"
        self.a_dp = ao / (1.0 - delo)              # "a0''"
        self.perigee_km = (self.a_dp * (1.0 - self.ecco) - 1.0) * _XKMPER

        # s & qoms2t adjustment for low perigee
        s = _S0
        qoms24 = _QOMS2T
        if self.perigee_km < 156.0:
            s = (self.perigee_km - _SO) / _XKMPER + 1.0 if self.perigee_km > 98.0 else 20.0 / _XKMPER + 1.0
            qoms24 = ((120.0 - (s - 1.0) * _XKMPER) / _XKMPER) ** 4
        self._isimp = (self.a_dp * (1.0 - self.ecco) / 1.0) < (220.0 / _XKMPER + 1.0)

        pinvsq = 1.0 / (self.a_dp * self.a_dp * betao2 * betao2)
        tsi = 1.0 / (self.a_dp - s)
        eta = self.a_dp * self.ecco * tsi
        etasq = eta * eta
        eeta = self.ecco * eta
        psisq = abs(1.0 - etasq)
        coef = qoms24 * tsi ** 4
        coef1 = coef / psisq ** 3.5
        c2 = (coef1 * self.no_dp * (self.a_dp * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
              + 0.75 * _CK2 * tsi / psisq * x3thm1 * (8.0 + 3.0 * etasq * (8.0 + etasq))))
        self.c1 = self.bstar * c2
        sinio = math.sin(self.inclo)
        a3ovk2 = -_J3 / _CK2
        c3 = coef * tsi * a3ovk2 * self.no_dp * sinio / max(1e-12, self.ecco)
        x1mth2 = 1.0 - theta2
        self.c4 = (2.0 * self.no_dp * coef1 * self.a_dp * betao2 * (
            eta * (2.0 + 0.5 * etasq) + self.ecco * (0.5 + 2.0 * etasq)
            - 2.0 * _CK2 * tsi / (self.a_dp * psisq) * (
                -3.0 * x3thm1 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                + 0.75 * x1mth2 * (2.0 * etasq - eeta * (1.0 + etasq)) * math.cos(2.0 * self.argpo))))
        self.c5 = 2.0 * coef1 * self.a_dp * betao2 * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
        theta4 = theta2 * theta2
        temp1 = 3.0 * _CK2 * pinvsq * self.no_dp
        temp2 = temp1 * _CK2 * pinvsq
        temp3 = 1.25 * _CK4 * pinvsq * pinvsq * self.no_dp
        self.xmdot = (self.no_dp + 0.5 * temp1 * betao * x3thm1
                      + 0.0625 * temp2 * betao * (13.0 - 78.0 * theta2 + 137.0 * theta4))
        self.argpdot = (-0.5 * temp1 * (1.0 - 5.0 * theta2)
                        + 0.0625 * temp2 * (7.0 - 114.0 * theta2 + 395.0 * theta4)
                        + temp3 * (3.0 - 36.0 * theta2 + 49.0 * theta4))
        xhdot1 = -temp1 * cosio
        self.nodedot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * theta2) + 2.0 * temp3 * (3.0 - 7.0 * theta2)) * cosio
        self.omgcof = self.bstar * c3 * math.cos(self.argpo)
        self.xmcof = -(2.0 / 3.0) * coef * self.bstar / max(1e-12, eeta)
        self.nodecf = 3.5 * betao2 * xhdot1 * self.c1
        self.t2cof = 1.5 * self.c1
        self.xlcof = 0.125 * a3ovk2 * sinio * (3.0 + 5.0 * cosio) / max(1e-12, 1.0 + cosio)
        self.aycof = 0.25 * a3ovk2 * sinio
        delmo = (1.0 + eta * math.cos(self.mo)) ** 3
        self.delmo = delmo
        self.sinmo = math.sin(self.mo)
        if not self._isimp:
            c1sq = self.c1 * self.c1
            self.d2 = 4.0 * self.a_dp * tsi * c1sq
            temp = self.d2 * tsi * self.c1 / 3.0
            self.d3 = (17.0 * self.a_dp + s) * temp
            self.d4 = 0.5 * temp * self.a_dp * tsi * (221.0 * self.a_dp + 31.0 * s) * self.c1
            self.t3cof = self.d2 + 2.0 * c1sq
            self.t4cof = 0.25 * (3.0 * self.d3 + self.c1 * (12.0 * self.d2 + 10.0 * c1sq))
            self.t5cof = 0.2 * (3.0 * self.d4 + 12.0 * self.c1 * self.d3 + 6.0 * self.d2 * self.d2
                                + 15.0 * c1sq * (2.0 * self.d2 + c1sq))
        # save a few for propagate
        self._x3thm1 = x3thm1
        self._x1mth2 = x1mth2
        self._x7thm1 = 7.0 * theta2 - 1.0
        self._cosio = cosio
        self._sinio = sinio
        self._eta = eta
        self._betao2 = betao2

    def _sgp4_vendored(self, tsince: float):
        # secular gravity + atmospheric drag
        xmdf = self.mo + self.xmdot * tsince
        argpdf = self.argpo + self.argpdot * tsince
        nodedf = self.nodeo + self.nodedot * tsince
        omega = argpdf
        xmp = xmdf
        tsq = tsince * tsince
        node = nodedf + self.nodecf * tsq
        tempa = 1.0 - self.c1 * tsince
        tempe = self.bstar * self.c4 * tsince
        templ = self.t2cof * tsq
        if not self._isimp:
            delomg = self.omgcof * tsince
            delm = self.xmcof * ((1.0 + self._eta * math.cos(xmdf)) ** 3 - self.delmo)
            temp = delomg + delm
            xmp = xmdf + temp
            omega = argpdf - temp
            t3 = tsq * tsince
            t4 = t3 * tsince
            tempa = tempa - self.d2 * tsq - self.d3 * t3 - self.d4 * t4
            tempe = tempe + self.bstar * self.c5 * (math.sin(xmp) - self.sinmo)
            templ = templ + self.t3cof * t3 + t4 * (self.t4cof + tsince * self.t5cof)
        a = self.a_dp * tempa * tempa
        e = self.ecco - tempe
        if e < 1.0e-6:
            e = 1.0e-6
        if e >= 1.0 or a < 1.0:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1   # decayed / unstable
        xl = xmp + omega + node + self.no_dp * templ
        beta = math.sqrt(1.0 - e * e)
        xn = _XKE / a ** 1.5
        # long-period periodics
        axn = e * math.cos(omega)
        temp = 1.0 / (a * beta * beta)
        xll = temp * self.xlcof * axn
        aynl = temp * self.aycof
        xlt = xl + xll
        ayn = e * math.sin(omega) + aynl
        # Kepler's equation for (E+ω)
        capu = (xlt - node) % _TWOPI
        epw = capu
        for _ in range(12):
            sinepw = math.sin(epw)
            cosepw = math.cos(epw)
            ecose = axn * cosepw + ayn * sinepw
            esine = axn * sinepw - ayn * cosepw
            f = capu - epw + esine
            if abs(f) < 1.0e-12:
                break
            df = 1.0 - ecose
            dpw = f / df
            # damp the step (Vallado limits to 0.95 rad)
            if dpw > 0.95:
                dpw = 0.95
            elif dpw < -0.95:
                dpw = -0.95
            epw = epw + dpw
        # short-period preliminaries
        ecose = axn * math.cos(epw) + ayn * math.sin(epw)
        esine = axn * math.sin(epw) - ayn * math.cos(epw)
        elsq = axn * axn + ayn * ayn
        pl = a * (1.0 - elsq)
        if pl < 0.0:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 4
        r = a * (1.0 - ecose)
        rdot = _XKE * math.sqrt(a) / r * esine
        rfdot = _XKE * math.sqrt(pl) / r
        betal = math.sqrt(1.0 - elsq)
        temp = esine / (1.0 + betal)
        cosu = a / r * (math.cos(epw) - axn + ayn * temp)
        sinu = a / r * (math.sin(epw) - ayn - axn * temp)
        u = math.atan2(sinu, cosu)
        sin2u = 2.0 * sinu * cosu
        cos2u = 2.0 * cosu * cosu - 1.0
        temp = 1.0 / pl
        temp1 = _CK2 * temp
        temp2 = temp1 * temp
        # update for short-period periodics
        rk = r * (1.0 - 1.5 * temp2 * betal * self._x3thm1) + 0.5 * temp1 * self._x1mth2 * cos2u
        uk = u - 0.25 * temp2 * self._x7thm1 * sin2u
        nodek = node + 1.5 * temp2 * self._cosio * sin2u
        xinck = self.inclo + 1.5 * temp2 * self._cosio * self._sinio * cos2u
        rdotk = rdot - xn * temp1 * self._x1mth2 * sin2u
        rfdotk = rfdot + xn * temp1 * (self._x1mth2 * cos2u + 1.5 * self._x3thm1)
        # orientation vectors
        sinuk = math.sin(uk)
        cosuk = math.cos(uk)
        sinik = math.sin(xinck)
        cosik = math.cos(xinck)
        sinnok = math.sin(nodek)
        cosnok = math.cos(nodek)
        xmx = -sinnok * cosik
        xmy = cosnok * cosik
        ux = xmx * sinuk + cosnok * cosuk
        uy = xmy * sinuk + sinnok * cosuk
        uz = sinik * sinuk
        vx = xmx * cosuk - cosnok * sinuk
        vy = xmy * cosuk - sinnok * sinuk
        vz = sinik * cosuk
        # ECI position (km) and velocity (km/s) — TEME frame
        rx = rk * ux * _XKMPER
        ry = rk * uy * _XKMPER
        rz = rk * uz * _XKMPER
        velconv = _XKMPER / 60.0
        vxk = (rdotk * ux + rfdotk * vx) * velconv
        vyk = (rdotk * uy + rfdotk * vy) * velconv
        vzk = (rdotk * uz + rfdotk * vz) * velconv
        return (rx, ry, rz), (vxk, vyk, vzk), 0


# ── helpers ──────────────────────────────────────────────────────────────────
def _expfield(s: str) -> float:
    """Parse a TLE exponential field like ' 12345-3' → 0.12345e-3."""
    s = s.strip()
    if not s or s in ("00000-0", "00000+0", "0"):
        return 0.0
    sign = 1.0
    if s[0] in "+-":
        sign = -1.0 if s[0] == "-" else 1.0
        s = s[1:]
    if s and s[-2] in "+-":
        mant = "0." + s[:-2]
        exp = int(s[-2:])
        return sign * float(mant) * (10.0 ** exp)
    try:
        return sign * float("0." + s)
    except ValueError:
        return 0.0


def _jd_to_datetime(jd: float) -> _dt.datetime:
    jd2 = jd + 0.5
    Z = math.floor(jd2)
    F = jd2 - Z
    if Z < 2299161:
        A = Z
    else:
        alpha = math.floor((Z - 1867216.25) / 36524.25)
        A = Z + 1 + alpha - math.floor(alpha / 4)
    B = A + 1524
    C = math.floor((B - 122.1) / 365.25)
    D = math.floor(365.25 * C)
    E = math.floor((B - D) / 30.6001)
    day = B - D - math.floor(30.6001 * E) + F
    month = E - 1 if E < 14 else E - 13
    year = C - 4716 if month > 2 else C - 4715
    di = int(day)
    secs = (day - di) * 86400.0
    h = int(secs // 3600); secs -= h * 3600
    mn = int(secs // 60); secs -= mn * 60
    return _dt.datetime(int(year), int(month), di, h, mn, int(secs),
                        int((secs % 1) * 1e6), tzinfo=_dt.timezone.utc)


def _eci_to_geodetic(eci_km, gmst_rad: float):
    """TEME/ECI (km) + GMST → geodetic lat/lon (deg) and altitude (km), WGS-72 ellipsoid."""
    x, y, z = eci_km
    lon = (math.atan2(y, x) - gmst_rad) % _TWOPI
    if lon > math.pi:
        lon -= _TWOPI
    r = math.hypot(x, y)
    e2 = 2.0 * _F - _F * _F
    lat = math.atan2(z, r)
    for _ in range(8):
        sin_lat = math.sin(lat)
        C = 1.0 / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        lat = math.atan2(z + _XKMPER * C * e2 * sin_lat, r)
    sin_lat = math.sin(lat)
    C = 1.0 / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    alt = r / math.cos(lat) - _XKMPER * C
    return math.degrees(lat), math.degrees(lon), alt


def _footprint_km(alt_km: float) -> float:
    """Great-circle radius of the 0°-mask visibility circle for a sat at alt_km."""
    Re = _XKMPER
    if alt_km <= 0:
        return 0.0
    return Re * math.acos(min(1.0, Re / (Re + alt_km)))


def look_angles(obs_lat_deg: float, obs_lon_deg: float, obs_alt_m: float,
                sat_lat_deg: float, sat_lon_deg: float, sat_alt_km: float):
    """Topocentric azimuth (deg, from N), elevation (deg, +up), and slant range (km)
    from a ground observer to a sub-point/altitude. Spherical earth (Re = WGS-72)."""
    Re = _XKMPER
    o_lat, o_lon = math.radians(obs_lat_deg), math.radians(obs_lon_deg)
    s_lat, s_lon = math.radians(sat_lat_deg), math.radians(sat_lon_deg)
    ro = Re + obs_alt_m / 1000.0
    rs = Re + sat_alt_km
    # ECEF (sphere)
    ox, oy, oz = ro * math.cos(o_lat) * math.cos(o_lon), ro * math.cos(o_lat) * math.sin(o_lon), ro * math.sin(o_lat)
    sx, sy, sz = rs * math.cos(s_lat) * math.cos(s_lon), rs * math.cos(s_lat) * math.sin(s_lon), rs * math.sin(s_lat)
    dx, dy, dz = sx - ox, sy - oy, sz - oz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    # rotate to local ENU at the observer
    east = -math.sin(o_lon) * dx + math.cos(o_lon) * dy
    north = (-math.sin(o_lat) * math.cos(o_lon) * dx - math.sin(o_lat) * math.sin(o_lon) * dy + math.cos(o_lat) * dz)
    up = (math.cos(o_lat) * math.cos(o_lon) * dx + math.cos(o_lat) * math.sin(o_lon) * dy + math.sin(o_lat) * dz)
    az = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
    el = math.degrees(math.atan2(up, math.hypot(east, north)))
    return az, el, rng


def propagation_backend() -> str:
    return "sgp4 (Vallado reference package)" if _HAVE_SGP4_PKG else "vendored SGP4 near-earth (WGS-72)"
