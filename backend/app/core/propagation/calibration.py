# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
calibration.py — calibrate/validate the propagation models against measured drive-test data.

Measured path loss is the ground truth a predictor should match. This module:
  * ships a small set of **published, attributed reference anchors** from real
    drive-test campaigns so the harness runs out of the box;
  * ingests a full drive-test CSV (the Covenant smart-campus column layout, or a
    generic one) for per-point calibration;
  * reports, per model, the error statistics (bias / MAE / RMSE / σ) and the
    best-fit constant offset — i.e. how far each model is from reality and how
    much a clutter/offset correction closes the gap.

Reference anchors (route means; coarse — they pin the offset near ~430 m, not the
distance slope — drop the full dataset for that):
  Covenant University smart-campus, Ota NG — 1800 MHz urban macro, 3 routes.
  Oyetunji & Akinwumi, *Data in Brief* 17 (2018) 1264, DOI 10.1016/j.dib.2018.02.026.
  Receiver ~1.5 m; macro BTS height taken as 30 m (typical 1800 MHz macro; the
  data article does not state it, so absolute bias carries that assumption).

This is calibration against *real measured* path loss — distinct from
validation.py (closed-form/invariant self-checks).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from app.core.propagation.models import PropagationModel, select_model


@dataclass
class Measurement:
    freq_mhz: float
    distance_m: float
    path_loss_db: float
    tx_height_m: float = 30.0
    rx_height_m: float = 1.5
    environment: str = "urban"      # urban | suburban | rural
    weight: float = 1.0             # e.g. number of samples behind a route mean
    source: str = ""


# Published reference anchors.
#  • Covenant U. — route MEANS (weighted by sample count); pin the offset at ~430 m.
#  • Gadze et al. (IJWMN 11(6) 2019) — measured PL at near/far distance, 800 & 2600
#    MHz, urban + suburban Ghana, TX 46 dBm; values DIGITIZED from Figs 4–13 (±~3 dB),
#    so they're coarse but add the distance slope and two more bands/environments.
_DIB = "DOI 10.1016/j.dib.2018.02.026"
_GH = "Gadze et al., IJWMN 11(6) 2019 (figs, digitized ±3 dB)"
REFERENCE_ANCHORS: list[Measurement] = [
    Measurement(1800, 399.81, 142.42, 30, 1.5, "urban", 937, f"Covenant U. route X ({_DIB})"),
    Measurement(1800, 460.49, 139.72, 30, 1.5, "urban", 1229, f"Covenant U. route Y ({_DIB})"),
    Measurement(1800, 447.42, 146.34, 30, 1.5, "urban", 1450, f"Covenant U. route Z ({_DIB})"),
    # Ghana 800 MHz urban (Adum hb≈24 m; Techiman hb≈35 m)
    Measurement(800, 50,  133, 24, 1.5, "urban", 100, f"Adum 50m ({_GH})"),
    Measurement(800, 500, 144, 24, 1.5, "urban", 100, f"Adum 500m ({_GH})"),
    Measurement(800, 50,  131, 35, 1.5, "urban", 100, f"Techiman 50m ({_GH})"),
    Measurement(800, 500, 141, 35, 1.5, "urban", 100, f"Techiman 500m ({_GH})"),
    # Ghana 800 MHz suburban (Agogo hb≈25 m)
    Measurement(800, 50,  128, 25, 1.5, "suburban", 100, f"Agogo 50m ({_GH})"),
    Measurement(800, 500, 143, 25, 1.5, "suburban", 100, f"Agogo 500m ({_GH})"),
    # Ghana 2600 MHz urban / suburban (hb≈30 m)
    Measurement(2600, 50,  135, 30, 1.5, "urban", 100, f"2600 urban 50m ({_GH})"),
    Measurement(2600, 500, 150, 30, 1.5, "urban", 100, f"2600 urban 500m ({_GH})"),
    Measurement(2600, 200, 133, 30, 1.5, "suburban", 100, f"2600 suburban 200m ({_GH})"),
    Measurement(2600, 500, 143, 30, 1.5, "suburban", 100, f"2600 suburban 500m ({_GH})"),
]

# Models worth comparing at cellular UHF (terrain-free empirical predictors).
_CANDIDATE_MODELS = [
    PropagationModel.COST231_HATA, PropagationModel.COST231_WI, PropagationModel.ECC33,
    PropagationModel.HATA_URBAN, PropagationModel.ITU_P1546,
    PropagationModel.SUI, PropagationModel.EGLI, PropagationModel.FSPL,
]

_ENV_CONTEXT = {"urban": 1, "suburban": 2, "rural": 3}


def _predict(model: PropagationModel, m: Measurement) -> Optional[float]:
    try:
        return select_model(model, m.distance_m, m.freq_mhz * 1e6,
                            m.tx_height_m, m.rx_height_m,
                            context=_ENV_CONTEXT.get(m.environment, 2))
    except Exception:
        return None


def _stats(errors: list[float], weights: list[float]) -> dict:
    """Weighted bias / MAE / RMSE / σ for prediction errors (predicted − measured)."""
    W = sum(weights) or 1.0
    bias = sum(e * w for e, w in zip(errors, weights)) / W
    mae = sum(abs(e) * w for e, w in zip(errors, weights)) / W
    rmse = math.sqrt(sum((e * e) * w for e, w in zip(errors, weights)) / W)
    var = sum(((e - bias) ** 2) * w for e, w in zip(errors, weights)) / W
    return {"bias_db": round(bias, 2), "mae_db": round(mae, 2),
            "rmse_db": round(rmse, 2), "std_db": round(math.sqrt(var), 2),
            # RMSE after removing the best-fit constant offset (= the corrected fit)
            "rmse_after_offset_db": round(math.sqrt(max(0.0, rmse * rmse - bias * bias)), 2),
            "suggested_offset_db": round(-bias, 2)}


def _clutter_db(m: Measurement) -> float:
    """ITU-R P.2108 terminal clutter for this measurement's environment (median)."""
    from app.core import clutter as _clutter
    return _clutter.terminal_clutter_loss_p2108_db(
        m.freq_mhz / 1000.0, m.distance_m / 1000.0, percent=50.0, environment=m.environment)


def calibrate(measurements: Optional[list[Measurement]] = None) -> dict:
    """Per-model error stats vs measured path loss (defaults to the reference anchors),
    each model evaluated bare and with the ITU-R P.2108 clutter correction added.
    best_fit = lowest raw RMSE (absolute accuracy), since clustered-distance anchors
    can't rank by post-offset RMSE alone."""
    ms = measurements or REFERENCE_ANCHORS
    results = {}
    for model in dict.fromkeys(_CANDIDATE_MODELS):     # de-dup, keep order
        for variant, add_clutter in (("", False), ("+clutter(P.2108)", True)):
            errs, wts, ok = [], [], True
            for m in ms:
                p = _predict(model, m)
                if p is None:
                    ok = False; break
                if add_clutter:
                    p += _clutter_db(m)
                errs.append(p - m.path_loss_db); wts.append(m.weight)
            if ok and errs:
                results[model.value + variant] = _stats(errs, wts)
    best = min(results.items(), key=lambda kv: kv[1]["rmse_db"]) if results else None

    # Per-regime (freq band × environment) best fit, so multi-band data is actionable.
    regimes = {}
    groups: dict[tuple, list[Measurement]] = {}
    for m in ms:
        groups.setdefault((m.freq_mhz, m.environment), []).append(m)
    for (f, env), gms in sorted(groups.items()):
        sub = calibrate_regime(gms)
        regimes[f"{int(f)}MHz/{env}"] = sub

    return {
        "n_points": len(ms),
        "n_samples": int(sum(m.weight for m in ms)),
        "regime": {"freq_mhz": sorted({m.freq_mhz for m in ms}),
                   "env": sorted({m.environment for m in ms}),
                   "dist_m_range": [round(min(m.distance_m for m in ms), 1),
                                    round(max(m.distance_m for m in ms), 1)]},
        "by_regime": regimes,
        "models_overall": results,
        "best_fit_overall": ({"model": best[0], **best[1]} if best else None),
        "note": ("Negative bias ⇒ the model under-predicts measured loss. Covenant anchors "
                 "are route means (pin offset, not slope); Ghana anchors are digitized from "
                 "figures (±3 dB). Use by_regime for per-band/-environment best fit; drop a "
                 "full per-point CSV via load_covenant_csv() for tight per-distance work."),
        "sources": sorted({m.source.split(" (")[0] if "route" not in m.source else m.source
                           for m in ms if m.source}),
    }


def calibrate_regime(ms: list[Measurement]) -> dict:
    """Compact best-fit (bare + clutter) for a homogeneous set of measurements."""
    res = {}
    for model in dict.fromkeys(_CANDIDATE_MODELS):
        for variant, add_clutter in (("", False), ("+clutter", True)):
            errs, wts, ok = [], [], True
            for m in ms:
                p = _predict(model, m)
                if p is None:
                    ok = False; break
                if add_clutter:
                    p += _clutter_db(m)
                errs.append(p - m.path_loss_db); wts.append(m.weight)
            if ok and errs:
                res[model.value + variant] = _stats(errs, wts)
    best = min(res.items(), key=lambda kv: kv[1]["rmse_db"]) if res else None
    return {"n_points": len(ms),
            "dist_m_range": [round(min(m.distance_m for m in ms), 1),
                             round(max(m.distance_m for m in ms), 1)],
            "best_fit": ({"model": best[0], **best[1]} if best else None)}


# ── CSV ingest (Covenant column layout, or a generic header) ─────────────────
def load_covenant_csv(path: str, *, freq_mhz: float = 1800.0, tx_height_m: float = 30.0,
                      rx_height_m: float = 1.5, environment: str = "urban") -> list[Measurement]:
    """Load the Covenant smart-campus layout: columns longitude, latitude, elevation,
    altitude, clutter, distance(m), path_loss(dB). Header names are matched loosely;
    a generic file just needs 'distance' and 'path_loss' columns."""
    import csv
    out: list[Measurement] = []
    with open(path, newline="") as f:
        rdr = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in (rdr.fieldnames or [])}

        def col(*names):
            for n in names:
                for k, orig in cols.items():
                    if n in k:
                        return orig
            return None
        dcol = col("distance", "dist", "range")
        pcol = col("path_loss", "pathloss", "pl", "loss")
        fcol = col("freq")
        if not dcol or not pcol:
            raise ValueError("CSV needs distance and path_loss columns")
        for row in rdr:
            try:
                d = float(row[dcol]); pl = float(row[pcol])
            except (TypeError, ValueError):
                continue
            if d <= 0 or not math.isfinite(pl):
                continue
            fm = float(row[fcol]) if fcol and row.get(fcol) else freq_mhz
            out.append(Measurement(fm, d, pl, tx_height_m, rx_height_m, environment, 1.0,
                                   f"csv:{path}"))
    return out


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1:
        ms = load_covenant_csv(sys.argv[1])
        print(f"loaded {len(ms)} measured points from {sys.argv[1]}")
        rep = calibrate(ms)
    else:
        print("calibrating against built-in published reference anchors "
              "(pass a drive-test CSV path for per-point calibration)\n")
        rep = calibrate()
    print(json.dumps(rep, indent=2))
