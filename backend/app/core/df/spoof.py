"""
DF ↔ Remote-ID cross-check (anti-spoof).

When a DF emitter track sits at a different position from the Remote-ID
broadcast it claims to come from, somebody is lying. Common reasons:
  - spoofed Remote-ID broadcast (drone says it's at lat/lon A but RF leaks
    DF point at B): the drone is at B.
  - genuine Remote-ID + DF accuracy issue (large σ_az, distant emitter):
    not necessarily an alert.
  - multiple emitters in the same band: legitimate divergence.

Logic:
  1. For each DF track t_df and each RID message m_rid (recent, within Δt):
       compute the angular distance from the observer to the RID position,
       compare against t_df.azimuth_deg, and check if it lies inside the
       track's CEP ellipse.
  2. If the RID position is *outside* the 3σ ellipse, flag.
  3. Otherwise, associate (RID gives us identity + drone model + serial).

Returns annotation dicts compatible with the existing UAS Tool feed.
"""

from __future__ import annotations

import math
from typing import Optional


EARTH_R = 6_378_137.0


def _bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) -
         math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def correlate(df_tracks: list[dict], rid_messages: list[dict],
              observer: Optional[dict] = None,
              max_age_s: float = 30.0,
              bearing_tol_deg: float = 8.0,
              cep_multiplier: float = 3.0) -> list[dict]:
    """Return one annotation per matched pair; flags spoof candidates.

    df_tracks    : [{ id, lat, lon, cep_m, frequency_hz, last_update_t, n_obs, ...}]
    rid_messages : [{ uid|serial, lat, lon, alt_m?, t, drone_model?, operator_lat?, operator_lon? }]
    observer     : {lat, lon} of the DF station (used to compute observer→RID bearing
                    when a track has no explicit position yet — e.g. single-LoB cuts).
    """
    out: list[dict] = []
    import time
    now = time.time()
    for tr in df_tracks:
        if (now - tr.get("last_update_t", now)) > max_age_s:
            continue
        cep = float(tr.get("cep_m", 9_999_999))
        gate_m = cep * cep_multiplier
        best = None
        for rid in rid_messages:
            if (now - float(rid.get("t", now))) > max_age_s:
                continue
            d = _distance_m(tr["lat"], tr["lon"], rid["lat"], rid["lon"])
            # Bearing agreement when we have an observer + track is single-LoB
            bearing_err = None
            if observer and "azimuth_deg" in tr:
                rid_bearing = _bearing(observer["lat"], observer["lon"], rid["lat"], rid["lon"])
                bearing_err = abs(((tr["azimuth_deg"] - rid_bearing + 540) % 360) - 180)
            score = d + 1000 * (bearing_err or 0.0)  # heuristic
            if best is None or score < best["score"]:
                best = {"rid": rid, "distance_m": d, "bearing_err_deg": bearing_err, "score": score}
        if best is None:
            continue
        within_cep = best["distance_m"] <= gate_m
        within_bearing = (best["bearing_err_deg"] is None) or (best["bearing_err_deg"] <= bearing_tol_deg)
        agree = within_cep and within_bearing
        out.append({
            "track_id": tr["id"],
            "rid_uid": best["rid"].get("uid") or best["rid"].get("serial") or "?",
            "rid_serial": best["rid"].get("serial"),
            "rid_drone_model": best["rid"].get("drone_model"),
            "distance_m": best["distance_m"],
            "bearing_err_deg": best["bearing_err_deg"],
            "verdict": "agree" if agree else "DISAGREE_spoof_candidate",
            "agree": agree,
            "frequency_hz": tr.get("frequency_hz"),
            "rid_lat": best["rid"]["lat"], "rid_lon": best["rid"]["lon"],
            "df_lat": tr["lat"], "df_lon": tr["lon"],
            "cep_m": cep,
            "explanation": (
                f"RID claims ({best['rid']['lat']:.5f},{best['rid']['lon']:.5f}), DF places at "
                f"({tr['lat']:.5f},{tr['lon']:.5f}) — {best['distance_m']:.0f} m apart, gate "
                f"{gate_m:.0f} m (CEP {cep:.0f} m × {cep_multiplier})."
                + (f" Bearing residual {best['bearing_err_deg']:.1f}° (tol {bearing_tol_deg}°)."
                   if best['bearing_err_deg'] is not None else "")
            ),
        })
    return out
