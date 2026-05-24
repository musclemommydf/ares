# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
pattern_import.py — measured antenna-pattern import (Workstream B / antenna).

Parses the NSMA / "Planet" (a.k.a. MSI / `.msi` / `.pln` / `.ant`) antenna-pattern
files that vendors ship and that CloudRF / ATDI / Atoll ingest — header (gain, H/V
beamwidths, F/B, polarisation, frequency) plus a 360-point HORIZONTAL cut and a
360-point VERTICAL cut of attenuation (dB below peak). Reconstructs a 2-D
``gain_dbi[az][el]`` grid (the "summing" cross-section method:
``att(az,el) = att_H(az) + att_V(el)``, the conservative choice the planning tools
default to) and emits it as the JSON the engine's ``custom_pattern`` consumes —
so a real measured pattern flows straight into the coverage path, not just the
analytic approximations.

A best-effort NEC-2 ``RP`` (radiation-pattern card) parser is also provided.
"""
from __future__ import annotations

import json
import math
import re
from typing import Optional

import numpy as np

_DBD_TO_DBI = 2.15


def parse_msi(text: str) -> dict:
    """Parse an NSMA/Planet MSI pattern file → metadata + the two cuts.
    Returns ``{name, make, frequency_mhz, gain_dbi, h_width_deg, v_width_deg,
    front_to_back_db, polarization, comment, horizontal:[(deg,att_db)...],
    vertical:[(deg,att_db)...]}``. Tolerant of casing/whitespace and of GAIN
    given in dBd, dBi or dBd implied."""
    name = make = polarization = comment = ""
    freq_mhz = None
    gain_dbi = h_width = v_width = fb = None
    horizontal: list[tuple[float, float]] = []
    vertical: list[tuple[float, float]] = []
    section = None
    expected = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        if section in ("H", "V") and expected > 0:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    deg = float(parts[0]) % 360.0
                    att = float(parts[1])
                    (horizontal if section == "H" else vertical).append((deg, att))
                    expected -= 1
                    if expected == 0:
                        section = None
                    continue
                except ValueError:
                    pass
            section = None  # malformed row → end the section
        m = re.match(r"^(\w[\w/]*)\s+(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).upper(), m.group(2).strip()
        if key == "NAME":
            name = val
        elif key in ("MAKE", "MANUFACTURER"):
            make = val
        elif key in ("FREQUENCY", "FREQ"):
            mm = re.search(r"([-+]?\d*\.?\d+)", val)
            if mm:
                f = float(mm.group(1))
                freq_mhz = f / 1e6 if f > 1e6 else f          # accept Hz or MHz
        elif key in ("GAIN",):
            mm = re.search(r"([-+]?\d*\.?\d+)", val)
            if mm:
                g = float(mm.group(1))
                gain_dbi = g + (_DBD_TO_DBI if "DBD" in up or "DBI" not in up else 0.0)
        elif key in ("H_WIDTH", "HORIZONTAL_BEAMWIDTH", "HBW"):
            mm = re.search(r"([-+]?\d*\.?\d+)", val)
            if mm:
                h_width = float(mm.group(1))
        elif key in ("V_WIDTH", "VERTICAL_BEAMWIDTH", "VBW"):
            mm = re.search(r"([-+]?\d*\.?\d+)", val)
            if mm:
                v_width = float(mm.group(1))
        elif key in ("FRONT_TO_BACK", "F/B", "FB"):
            mm = re.search(r"([-+]?\d*\.?\d+)", val)
            if mm:
                fb = float(mm.group(1))
        elif key in ("POLARIZATION", "POLARISATION", "POL"):
            polarization = val
        elif key in ("COMMENT", "COMMENTS", "DESCRIPTION"):
            comment = (comment + " " + val).strip()
        elif key == "HORIZONTAL":
            section = "H"
            mm = re.search(r"(\d+)", val)
            expected = int(mm.group(1)) if mm else 360
        elif key == "VERTICAL":
            section = "V"
            mm = re.search(r"(\d+)", val)
            expected = int(mm.group(1)) if mm else 360
    if not horizontal and not vertical:
        raise ValueError("not an MSI/Planet pattern (no HORIZONTAL/VERTICAL cuts found)")
    return {
        "name": name, "make": make, "frequency_mhz": freq_mhz, "gain_dbi": gain_dbi,
        "h_width_deg": h_width, "v_width_deg": v_width, "front_to_back_db": fb,
        "polarization": polarization, "comment": comment,
        "horizontal": horizontal, "vertical": vertical,
    }


def _interp_cut(cut: list[tuple[float, float]], n_az: int) -> np.ndarray:
    """Resample an irregular (deg, att_db) cut onto a regular 0..360 grid (n_az points)."""
    if not cut:
        return np.zeros(n_az)
    pts = sorted(((d % 360.0, a) for d, a in cut))
    # wrap-around: prepend the last point shifted -360 and append the first +360
    xs = [pts[-1][0] - 360.0] + [p[0] for p in pts] + [pts[0][0] + 360.0]
    ys = [pts[-1][1]] + [p[1] for p in pts] + [pts[0][1]]
    grid = np.linspace(0.0, 360.0, n_az, endpoint=False)
    return np.interp(grid, xs, ys)


def msi_to_custom_pattern(parsed: dict, n_az: int = 72, n_el: int = 37) -> dict:
    """Build a 2-D ``gain_dbi[az][el]`` grid from the H/V cuts ("summing"
    cross-section method) → the dict shape :func:`app.core.propagation.antenna._custom_pattern`
    expects: ``{"azimuth":[...], "elevation":[...], "gain_dbi":[[...]] }``."""
    peak = float(parsed.get("gain_dbi") or 0.0)
    az = np.linspace(0.0, 360.0, n_az, endpoint=False)
    el = np.linspace(-90.0, 90.0, n_el)
    h_att = _interp_cut(parsed.get("horizontal") or [], n_az)            # att (dB) vs azimuth
    # the VERTICAL cut is given over 0..360 of elevation angle; map 0..180 → el 0..90 of the
    # front side and 180..360 → boresight-rear; we take the 0..180 half and mirror as needed.
    v_cut = parsed.get("vertical") or []
    # build att vs signed elevation: 0° in the file = boresight (el 0); 90° = straight up; 270° = straight down
    v_lookup = sorted(((d % 360.0, a) for d, a in v_cut))
    def v_att(el_deg: float) -> float:
        if not v_lookup:
            return 0.0
        ang = (el_deg if el_deg >= 0 else 360.0 + el_deg) % 360.0   # signed el → 0..360 of the file convention
        xs = [v_lookup[-1][0] - 360.0] + [p[0] for p in v_lookup] + [v_lookup[0][0] + 360.0]
        ys = [v_lookup[-1][1]] + [p[1] for p in v_lookup] + [v_lookup[0][1]]
        return float(np.interp(ang, xs, ys))
    grid = np.zeros((n_az, n_el))
    for i in range(n_az):
        for j in range(n_el):
            grid[i, j] = peak - (float(h_att[i]) + v_att(float(el[j])))
    return {"azimuth": az.tolist(), "elevation": el.tolist(), "gain_dbi": grid.tolist()}


def msi_to_custom_pattern_json(parsed: dict, **kw) -> str:
    return json.dumps(msi_to_custom_pattern(parsed, **kw))


def parse_nec2_rp(text: str) -> Optional[dict]:
    """Best-effort parse of an NEC-2 ``RP`` (radiation-pattern) listing → the same
    ``{azimuth, elevation, gain_dbi}`` shape. NEC prints rows of
    ``THETA  PHI  ...  GAIN(VERT) GAIN(HORIZ) GAIN(TOTAL) ...`` in dB; we take the
    TOTAL column. Returns None if the table can't be found."""
    rows = []
    in_table = False
    for line in text.splitlines():
        u = line.upper()
        if "RADIATION PATTERNS" in u:
            in_table = True
            continue
        if not in_table:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            theta = float(parts[0]); phi = float(parts[1])
            # the "TOTAL" power-gain column is usually parts[4] (after vert/horiz)
            tot = float(parts[4])
            rows.append((theta, phi, tot))
        except (ValueError, IndexError):
            if rows:        # table ended
                break
            continue
    if not rows:
        return None
    thetas = sorted(set(r[0] for r in rows))
    phis = sorted(set(r[1] for r in rows))
    # NEC theta = angle from zenith ⇒ elevation = 90 - theta
    el = [90.0 - t for t in thetas]
    g = np.full((len(phis), len(thetas)), -50.0)
    ti = {t: i for i, t in enumerate(thetas)}
    pi = {p: i for i, p in enumerate(phis)}
    for theta, phi, tot in rows:
        g[pi[phi], ti[theta]] = tot
    return {"azimuth": list(phis), "elevation": el, "gain_dbi": g.tolist()}
