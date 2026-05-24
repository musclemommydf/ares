# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Source-count estimation from a sample covariance matrix.

AIC and MDL (Wax & Kailath 1985) — both penalise more sources to avoid the
all-eigenvalues-look-like-signal failure mode. MDL is more conservative
(under-counts in low SNR); AIC is liberal (over-counts in low SNR). We return
both so the caller can pick the one that matches their operating point.
"""

from __future__ import annotations

import numpy as np


def aic_mdl(R: np.ndarray, n_snapshots: int) -> dict:
    """Return {'aic': k_hat_aic, 'mdl': k_hat_mdl, 'eigenvalues': [...]}.
    Eigenvalues returned in descending order; index = number of assumed sources."""
    R = (R + R.conj().T) / 2
    w = np.sort(np.linalg.eigvalsh(R))[::-1]
    M = len(w)
    aic_vals = []
    mdl_vals = []
    for k in range(M):
        noise_eigs = w[k:]
        if noise_eigs.size == 0 or np.any(noise_eigs <= 0):
            aic_vals.append(np.inf)
            mdl_vals.append(np.inf)
            continue
        geo = noise_eigs.prod() ** (1.0 / (M - k)) if (M - k) > 0 else 1.0
        ari = noise_eigs.mean()
        # Log-likelihood term (positive).
        L = (M - k) * n_snapshots * np.log(ari / geo)
        penalty = k * (2 * M - k)
        aic_vals.append(2 * L + 2 * penalty)
        mdl_vals.append(L + 0.5 * penalty * np.log(n_snapshots))
    return {
        "eigenvalues": w.real.tolist(),
        "aic_curve": [float(v) for v in aic_vals],
        "mdl_curve": [float(v) for v in mdl_vals],
        "aic": int(np.argmin(aic_vals)),
        "mdl": int(np.argmin(mdl_vals)),
    }
