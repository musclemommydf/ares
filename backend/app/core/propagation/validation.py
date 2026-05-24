# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
validation.py — self-checks for the propagation models.

Two kinds of check:
  * **exact** — closed-form references (FSPL), and the NTIA/ITS ITM reference
    point-to-point numbers (via itm_its.itm_reference_check()).
  * **invariant** — physical relationships that any correct model set must obey
    (monotonic loss vs distance, model ordering, P.452 ≈ free space within the
    horizon then > free space beyond it, clutter loss rising with frequency, …).

This is *not* validation against measured drive-test data — that needs field
measurements Ares doesn't ship. It catches regressions and gross errors, and
documents the expected behaviour. Run: ``python -m app.core.propagation.validation``.
"""
from __future__ import annotations

import math
from typing import Callable

from app.core.propagation.models import (
    PropagationModel, select_model, fspl_db, itu_p452_db,
)
from app.core import clutter as _clutter


def _approx(name: str, got: float, want: float, tol: float) -> dict:
    ok = abs(got - want) <= tol
    return {"case": name, "kind": "exact", "got": round(got, 3), "want": round(want, 3),
            "tol": tol, "passed": ok}


def _assert(name: str, cond: bool, detail: str = "") -> dict:
    return {"case": name, "kind": "invariant", "passed": bool(cond), "detail": detail}


def run_validation() -> dict:
    cases: list[dict] = []

    # ── exact: FSPL closed form ───────────────────────────────────────────────
    # FSPL(dB) = 32.44 + 20log10(d_km) + 20log10(f_MHz)
    for d_km, f_mhz in ((1.0, 2400.0), (10.0, 150.0), (5.0, 900.0)):
        want = 32.44 + 20 * math.log10(d_km) + 20 * math.log10(f_mhz)
        got = fspl_db(d_km * 1000.0, f_mhz * 1e6)
        cases.append(_approx(f"fspl {d_km}km/{f_mhz}MHz", got, want, 0.1))

    # ── exact: ITM/ITS reference numbers (NTIA report 82-100) ─────────────────
    try:
        from app.core.propagation.itm_its import itm_reference_check
        for r in itm_reference_check():
            # Reference run must complete without an ITM error code (kwx==0) and the
            # total loss must exceed the free-space loss (reference attenuation ≥ 0).
            ok = (r.get("kwx", 1) == 0) and (r.get("loss_db", 0) >= r.get("fs_db", 0) - 0.5)
            cases.append(_assert(f"itm_ref {int(r['f_mhz'])}MHz/{r['d_km']}km", ok,
                                 f"loss={r.get('loss_db')} fs={r.get('fs_db')} mode={r.get('mode')} kwx={r.get('kwx')}"))
    except Exception as e:
        cases.append({"case": "itm_reference_check", "kind": "exact", "passed": False, "detail": str(e)})

    # ── invariant: monotonic loss vs distance (empirical models) ──────────────
    f_hz, htx, hrx = 900e6, 30.0, 1.5
    for m in (PropagationModel.FSPL, PropagationModel.HATA_URBAN, PropagationModel.COST231_HATA,
              PropagationModel.ECC33, PropagationModel.COST231_WI):
        ds = [500, 1000, 2000, 4000]
        vals = [select_model(m, d, f_hz, htx, hrx, context=2) for d in ds]
        mono = all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))
        cases.append(_assert(f"monotonic {m.value}", mono, f"{[round(v,1) for v in vals]}"))

    # ── invariant: model ordering at fixed geometry (urban ≥ suburban ≥ FSPL) ──
    d = 3000.0
    fspl_v = select_model(PropagationModel.FSPL, d, f_hz, htx, hrx)
    hata_u = select_model(PropagationModel.HATA_URBAN, d, f_hz, htx, hrx)
    hata_r = select_model(PropagationModel.HATA_RURAL, d, f_hz, htx, hrx)
    cases.append(_assert("urban ≥ rural ≥ fspl", hata_u >= hata_r >= fspl_v,
                         f"urban={hata_u:.1f} rural={hata_r:.1f} fspl={fspl_v:.1f}"))

    # ── invariant: P.452 ≈ free space within horizon, > free space beyond ─────
    # 30 m / 2 m over 4/3 earth → smooth horizon ≈ 28 km.
    near = itu_p452_db(5.0, 0.4, 30.0, 2.0)
    near_fs = 92.5 + 20 * math.log10(0.4) + 20 * math.log10(5.0)
    cases.append(_assert("p452 near≈free-space", abs(near - near_fs) < 1.0,
                         f"p452={near:.1f} fs={near_fs:.1f}"))
    far = itu_p452_db(120.0, 0.4, 30.0, 2.0)
    far_fs = 92.5 + 20 * math.log10(0.4) + 20 * math.log10(120.0)
    cases.append(_assert("p452 far>free-space (diffraction/troposcatter)", far > far_fs + 5,
                         f"p452={far:.1f} fs={far_fs:.1f}"))

    # ── invariant: P.2108 clutter loss rises with frequency, exceeded-% ───────
    c_lo = _clutter.terminal_clutter_loss_p2108_db(0.9, 1.0, percent=50, environment="urban")
    c_hi = _clutter.terminal_clutter_loss_p2108_db(5.0, 1.0, percent=50, environment="urban")
    cases.append(_assert("p2108 rises with freq", c_hi > c_lo > 0, f"0.9GHz={c_lo:.1f} 5GHz={c_hi:.1f}"))
    c_p50 = _clutter.terminal_clutter_loss_p2108_db(2.4, 1.0, percent=50, environment="urban")
    c_p95 = _clutter.terminal_clutter_loss_p2108_db(2.4, 1.0, percent=95, environment="urban")
    cases.append(_assert("p2108 95% ≥ 50%", c_p95 >= c_p50, f"p50={c_p50:.1f} p95={c_p95:.1f}"))
    c_rural = _clutter.terminal_clutter_loss_p2108_db(2.4, 1.0, percent=50, environment="rural")
    cases.append(_assert("p2108 rural≈0", c_rural == 0.0, f"rural={c_rural:.1f}"))

    passed = sum(1 for c in cases if c["passed"])
    return {"passed": passed, "failed": len(cases) - passed, "total": len(cases), "cases": cases}


if __name__ == "__main__":
    import json
    res = run_validation()
    for c in res["cases"]:
        mark = "PASS" if c["passed"] else "FAIL"
        extra = c.get("detail") or (f"got={c.get('got')} want={c.get('want')}" if "got" in c else "")
        print(f"[{mark}] {c['case']:32s} {extra}")
    print(f"\n{res['passed']}/{res['total']} passed, {res['failed']} failed")
    raise SystemExit(0 if res["failed"] == 0 else 1)
