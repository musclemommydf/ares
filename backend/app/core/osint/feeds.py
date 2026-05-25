# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
osint/feeds.py — OSINT feed registry, fetchers, normalisers, filtering + cache.

Every source is normalised to a GeoJSON FeatureCollection so the frontend renders
it as an ordinary user layer (toggle on/off like any other). Filtering is layered
so a feed can never flood the UI: source-native query params → server-side bbox
clip → hard ``max_features`` cap. Results cache to ``DATA_DIR/osint`` so feeds keep
working offline (and honour ``network_policy == offline_only``).

Keyed sources (NASA FIRMS, ACLED, aisstream) are *operator-provided* — the key is
stored via :func:`set_config` or read from an env var; until present the feed
reports ``unavailable`` with the signup URL (never fake data). DeepState / GDELT /
ADS-B / airplanes.live / GPSJam work with no key; LiveUAMap / Signal Cockpit are
best-effort scrapes that degrade to ``unavailable`` when the site's structure changes.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from xml.etree import ElementTree as ET

import aiohttp

from app.config import settings, DATA_DIR
from app.core import net_state

log = logging.getLogger(__name__)

OSINT_DIR = DATA_DIR / "osint"
_STORE = OSINT_DIR / "feeds.json"          # {"custom": {...}, "config": {...}}
_UA = "Ares-OSINT/1.0 (+https://github.com/musclemommydf/ares)"
_DEFAULT_MAX_FEATURES = 2000
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────
# `params` are rendered by the UI as filter controls and passed back on fetch.
BUILTIN_FEEDS: dict[str, dict] = {
    "deepstate": {
        "id": "deepstate", "name": "DeepState Map (Ukraine)", "category": "conflict",
        "description": "Ukraine frontline + unit positions, updated continuously.",
        "attribution": "deepstatemap.live", "color": "#ef4444", "requires_config": False,
        "big": False, "params": [], "url": "https://deepstatemap.live/",
    },
    "gdelt": {
        "id": "gdelt", "name": "GDELT GEO (global events)", "category": "events",
        "description": "Geolocated global news events from the GDELT project.",
        "attribution": "gdeltproject.org", "color": "#f59e0b", "requires_config": False,
        "big": True, "url": "https://www.gdeltproject.org/",
        "params": [
            {"key": "query", "label": "Query", "type": "text", "default": "conflict"},
            {"key": "timespan", "label": "Timespan", "type": "select", "default": "1d",
             "options": ["15min", "1h", "6h", "1d", "3d", "1w"]},
        ],
    },
    "flights": {
        "id": "flights", "name": "Global flights (airplanes.live)", "category": "tracks",
        "description": "Live aircraft from the airplanes.live community ADS-B network — no key, "
                       "unfiltered (airliners, GA, military), with registration/type/squawk and "
                       "7500/7600/7700 emergency flags. Viewport-based: covers up to a 250 NM "
                       "radius of the map centre per refresh.",
        "attribution": "airplanes.live", "color": "#38bdf8", "requires_config": False,
        "big": True, "wants_bbox": True, "url": "https://airplanes.live/",
        "params": [
            {"key": "military", "label": "Military only (global)", "type": "bool", "default": False},
        ],
    },
    "gpsjam": {
        "id": "gpsjam", "name": "GPSJam (GNSS interference)", "category": "signals",
        "description": "Daily GPS/GNSS interference map: H3 hexagons coloured by the share of "
                       "aircraft reporting degraded nav accuracy (≤2% green, 2–10% amber, >10% red). "
                       "Derived from ADS-B Exchange NIC data.",
        "attribution": "gpsjam.org (John Wiseman) · data: ADS-B Exchange", "color": "#ef4444",
        "requires_config": False, "big": True, "wants_bbox": True, "url": "https://gpsjam.org/",
        "params": [
            {"key": "date", "label": "Date (UTC; blank = latest)", "type": "text", "default": ""},
            {"key": "min_frac", "label": "Min degraded fraction", "type": "number",
             "default": 0.02, "min": 0, "max": 1},
        ],
    },
    "nasa_firms": {
        "id": "nasa_firms", "name": "NASA FIRMS (active fires)", "category": "events",
        "description": "Active fire / thermal anomaly detections (VIIRS/MODIS).",
        "attribution": "NASA FIRMS", "color": "#fb7185", "requires_config": True,
        "config_fields": ["api_key"], "big": True, "wants_bbox": True,
        "signup": "https://firms.modaps.eosdis.nasa.gov/api/area/  → 'Get MAP_KEY' (self-serve, instant)",
        "env": {"api_key": "ARES_NASA_FIRMS_KEY"},
        "params": [
            {"key": "source", "label": "Sensor", "type": "select", "default": "VIIRS_SNPP_NRT",
             "options": ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT"]},
            {"key": "days", "label": "Days back", "type": "number", "default": 1, "min": 1, "max": 10},
        ],
    },
    "acled": {
        "id": "acled", "name": "ACLED (armed conflict events)", "category": "conflict",
        "description": "Armed-conflict & protest events worldwide (OAuth2: account email + password).",
        "attribution": "acleddata.com", "color": "#f43f5e", "requires_config": True,
        "config_fields": ["email", "password"], "big": True,
        "signup": "https://acleddata.com/register/  → then use your myACLED account email + password (OAuth2)",
        "env": {"email": "ARES_ACLED_EMAIL", "password": "ARES_ACLED_PASSWORD"},
        "params": [
            {"key": "days", "label": "Days back", "type": "number", "default": 7, "min": 1, "max": 90},
            {"key": "country", "label": "Country (optional)", "type": "text", "default": ""},
        ],
    },
    "aisstream": {
        "id": "aisstream", "name": "Ship AIS (aisstream.io)", "category": "tracks",
        "description": "Live vessel positions (collected over a short window). Advanced.",
        "attribution": "aisstream.io", "color": "#34d399", "requires_config": True,
        "config_fields": ["api_key"], "big": True, "wants_bbox": True,
        "signup": "https://aisstream.io  → create account → API key",
        "env": {"api_key": "ARES_AISSTREAM_KEY"},
        "params": [
            {"key": "seconds", "label": "Collect seconds", "type": "number", "default": 6, "min": 2, "max": 20},
        ],
    },
    "liveuamap": {
        "id": "liveuamap", "name": "LiveUAMap (best-effort)", "category": "conflict",
        "description": "Conflict event markers scraped from a LiveUAMap region page. Fragile.",
        "attribution": "liveuamap.com", "color": "#a78bfa", "requires_config": False,
        "best_effort": True, "url": "https://liveuamap.com/",
        "params": [
            {"key": "region", "label": "Region subdomain", "type": "text", "default": "ukraine"},
        ],
    },
    "signalcockpit": {
        "id": "signalcockpit", "name": "Signal Cockpit (data URL)", "category": "conflict",
        "description": "Point Ares at a Signal Cockpit GeoJSON/KML data export URL.",
        "attribution": "signalcockpit", "color": "#60a5fa", "requires_config": True,
        "config_fields": ["url"], "best_effort": True,
        "signup": "Open the Signal Cockpit map, find its data/export endpoint, paste the URL here.",
        "params": [],
    },
    "aprs": {
        "id": "aprs", "name": "APRS stations (local decode)", "category": "tracks",
        "description": "Amateur APRS stations decoded locally from RF / a TNC (POST /aprs/decode). "
                       "No external service — strictly local decode.",
        "attribution": "local APRS decoder", "color": "#84cc16", "requires_config": False,
        "big": True, "local": True, "params": [],
    },
}

_FORMATS = ("auto", "geojson", "kml", "georss", "gpx")


# ─────────────────────────────────────────────────────────────────────────────
# Persistence (custom feeds + per-feed config) + env fallback
# ─────────────────────────────────────────────────────────────────────────────
def _load_store() -> dict:
    if _STORE.exists():
        try:
            d = json.loads(_STORE.read_text())
            d.setdefault("custom", {})
            d.setdefault("config", {})
            return d
        except Exception:
            log.warning("osint store unreadable — starting empty")
    return {"custom": {}, "config": {}}


def _save_store(store: dict) -> None:
    OSINT_DIR.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(store, indent=2))


def _feed_def(feed_id: str) -> Optional[dict]:
    if feed_id in BUILTIN_FEEDS:
        return BUILTIN_FEEDS[feed_id]
    return _load_store()["custom"].get(feed_id)


def _config_for(feed_id: str) -> dict:
    """Stored config merged over env-var fallback (stored wins)."""
    fd = _feed_def(feed_id) or {}
    cfg = dict(_load_store()["config"].get(feed_id, {}))
    for field, env in (fd.get("env") or {}).items():
        if not cfg.get(field):
            v = os.environ.get(env)
            if v:
                cfg[field] = v
    return cfg


def _is_configured(feed_id: str) -> bool:
    fd = _feed_def(feed_id) or {}
    if not fd.get("requires_config"):
        return True
    cfg = _config_for(feed_id)
    return all(cfg.get(f) for f in (fd.get("config_fields") or []))


# ─────────────────────────────────────────────────────────────────────────────
# Normalisers → GeoJSON FeatureCollection
# ─────────────────────────────────────────────────────────────────────────────
def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def detect_format(text: str) -> str:
    t = text.lstrip()[:512].lower()
    if t.startswith("{") or t.startswith("["):
        return "geojson"
    if "<kml" in t or "<gpx" in t and "kml" in t:
        return "kml"
    if "<gpx" in t:
        return "gpx"
    if "<rss" in t or "<feed" in t or "georss" in t or "<rdf" in t:
        return "georss"
    if "<kml" in t:
        return "kml"
    return "geojson"


def _geojson_passthrough(obj: Any) -> dict:
    if isinstance(obj, str):
        obj = json.loads(obj)
    if isinstance(obj, dict):
        t = obj.get("type")
        if t == "FeatureCollection":
            return {"type": "FeatureCollection", "features": list(obj.get("features") or [])}
        if t == "Feature":
            return _fc([obj])
        if t in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString",
                 "MultiPolygon", "GeometryCollection"):
            return _fc([{"type": "Feature", "geometry": obj, "properties": {}}])
        # some APIs wrap it
        for k in ("map", "geojson", "data"):
            if isinstance(obj.get(k), dict):
                return _geojson_passthrough(obj[k])
    if isinstance(obj, list):
        return _fc([f for f in obj if isinstance(f, dict) and f.get("type") == "Feature"])
    raise ValueError("not GeoJSON")


def _kml_to_geojson(text: str) -> dict:
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    feats: list[dict] = []

    def _coords(s: str) -> list[list[float]]:
        out = []
        for tok in s.replace("\n", " ").split():
            parts = tok.split(",")
            if len(parts) >= 2:
                try:
                    out.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    pass
        return out

    for el in root.iter():
        if _strip_ns(el.tag) != "placemark":
            continue
        name = ""
        for c in el:
            if _strip_ns(c.tag) == "name":
                name = (c.text or "").strip()
        geom = None
        for g in el.iter():
            tag = _strip_ns(g.tag)
            if tag == "point":
                for c in g.iter():
                    if _strip_ns(c.tag) == "coordinates":
                        cs = _coords(c.text or "")
                        if cs:
                            geom = {"type": "Point", "coordinates": cs[0]}
            elif tag == "linestring":
                for c in g.iter():
                    if _strip_ns(c.tag) == "coordinates":
                        cs = _coords(c.text or "")
                        if len(cs) >= 2:
                            geom = {"type": "LineString", "coordinates": cs}
            elif tag == "polygon":
                ring = None
                for c in g.iter():
                    if _strip_ns(c.tag) == "coordinates":
                        cs = _coords(c.text or "")
                        if len(cs) >= 3:
                            ring = cs
                            break
                if ring:
                    geom = {"type": "Polygon", "coordinates": [ring]}
            if geom:
                break
        if geom:
            feats.append({"type": "Feature", "geometry": geom, "properties": {"name": name}})
    return _fc(feats)


def _georss_to_geojson(text: str) -> dict:
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    feats: list[dict] = []
    for item in root.iter():
        if _strip_ns(item.tag) not in ("item", "entry"):
            continue
        title = ""
        lat = lon = None
        geom = None
        for c in item.iter():
            tag = _strip_ns(c.tag)
            txt = (c.text or "").strip()
            if tag == "title":
                title = txt
            elif tag == "point" and txt:                    # georss:point "lat lon"
                p = txt.split()
                if len(p) >= 2:
                    lat, lon = float(p[0]), float(p[1])
            elif tag == "lat" and txt:
                lat = float(txt)
            elif tag in ("long", "lon") and txt:
                lon = float(txt)
            elif tag == "polygon" and txt:                  # georss:polygon "lat lon lat lon ..."
                nums = [float(x) for x in txt.split() if _isnum(x)]
                ring = [[nums[i + 1], nums[i]] for i in range(0, len(nums) - 1, 2)]
                if len(ring) >= 3:
                    geom = {"type": "Polygon", "coordinates": [ring]}
        if geom is None and lat is not None and lon is not None:
            geom = {"type": "Point", "coordinates": [lon, lat]}
        if geom:
            feats.append({"type": "Feature", "geometry": geom, "properties": {"name": title}})
    return _fc(feats)


def _gpx_to_geojson(text: str) -> dict:
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    feats: list[dict] = []
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag == "wpt":
            lat, lon = el.get("lat"), el.get("lon")
            if lat and lon:
                name = ""
                for c in el:
                    if _strip_ns(c.tag) == "name":
                        name = (c.text or "").strip()
                feats.append({"type": "Feature", "properties": {"name": name},
                              "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]}})
        elif tag in ("trkseg", "rte"):
            pts = []
            for c in el.iter():
                if _strip_ns(c.tag) in ("trkpt", "rtept"):
                    la, lo = c.get("lat"), c.get("lon")
                    if la and lo:
                        pts.append([float(lo), float(la)])
            if len(pts) >= 2:
                feats.append({"type": "Feature", "properties": {},
                              "geometry": {"type": "LineString", "coordinates": pts}})
    return _fc(feats)


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def normalize(text: str, fmt: str = "auto") -> dict:
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = detect_format(text)
    if fmt == "geojson":
        return _geojson_passthrough(text)
    if fmt == "kml":
        return _kml_to_geojson(text)
    if fmt == "georss":
        return _georss_to_geojson(text)
    if fmt == "gpx":
        return _gpx_to_geojson(text)
    return _geojson_passthrough(text)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry filtering (bbox clip + cap)
# ─────────────────────────────────────────────────────────────────────────────
def _coord_extent(coords: Any, acc: list[float]) -> None:
    """Walk nested coordinate arrays; accumulate [w, s, e, n] into acc in-place."""
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        lon, lat = float(coords[0]), float(coords[1])
        acc[0] = min(acc[0], lon); acc[1] = min(acc[1], lat)
        acc[2] = max(acc[2], lon); acc[3] = max(acc[3], lat)
        return
    for c in coords:
        _coord_extent(c, acc)


def _feature_extent(feat: dict) -> Optional[list[float]]:
    geom = (feat or {}).get("geometry") or {}
    coords = geom.get("coordinates")
    if coords is None:
        return None
    acc = [180.0, 90.0, -180.0, -90.0]
    _coord_extent(coords, acc)
    if acc[0] > acc[2]:
        return None
    return acc


def _clip_bbox(features: list[dict], bbox: list[float]) -> list[dict]:
    """Keep features whose extent overlaps bbox=[west, south, east, north]."""
    w, s, e, n = bbox
    out = []
    for f in features:
        ext = _feature_extent(f)
        if ext is None:
            continue
        fw, fs, fe, fn = ext
        if not (fe < w or fw > e or fn < s or fs > n):
            out.append(f)
    return out


def _cap(features: list[dict], max_features: int) -> tuple[list[dict], bool]:
    if max_features and len(features) > max_features:
        return features[:max_features], True
    return features, False


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _http_text(url: str, headers: Optional[dict] = None) -> str:
    h = {"User-Agent": _UA, **(headers or {})}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(url, headers=h) as r:
            r.raise_for_status()
            return await r.text()


async def _http_json(url: str, headers: Optional[dict] = None) -> Any:
    h = {"User-Agent": _UA, "Accept": "application/json", **(headers or {})}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(url, headers=h) as r:
            r.raise_for_status()
            raw = await r.read()                  # bytes — some feeds (GDELT GKG) emit latin-1
            try:
                return json.loads(raw)
            except UnicodeDecodeError:
                return json.loads(raw.decode("utf-8", "replace"))


async def _http_post_form(url: str, data: dict) -> Any:
    h = {"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.post(url, data=data, headers=h) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


# ─────────────────────────────────────────────────────────────────────────────
# Per-source fetchers → FeatureCollection
# ─────────────────────────────────────────────────────────────────────────────
async def _fetch_deepstate(fd, params, bbox, cfg) -> dict:
    data = await _http_json("https://deepstatemap.live/api/history/last")
    if isinstance(data, list) and data:
        data = data[-1]
    return _geojson_passthrough(data)


async def _fetch_gdelt(fd, params, bbox, cfg) -> dict:
    # The v2 GEO API (/api/v2/geo/geo) is returning 404 server-side, so use the v1
    # GKG GeoJSON API — the last 15-min window of geolocated GKG events as GeoJSON,
    # with an optional keyword filter.
    query = (params.get("query") or "").strip()
    url = "https://api.gdeltproject.org/api/v1/gkg_geojson"
    if query:
        url += f"?QUERY={quote(query)}"
    return _geojson_passthrough(await _http_json(url))


def _emergency_label(squawk: str, emergency: Any) -> Optional[str]:
    """Map a transponder code / ADS-B emergency field to a human label."""
    code = {"7500": "hijack", "7600": "radio-failure", "7700": "general-emergency"}.get(squawk)
    if code:
        return code
    if emergency and str(emergency).lower() not in ("none", "no", ""):
        return str(emergency)
    return None


async def _fetch_flights(fd, params, bbox, cfg) -> dict:
    """Global flight tracking via airplanes.live (ADSBExchange-v2 schema; no key).

    ``military`` uses the global ``/v2/mil`` endpoint; otherwise the viewport bbox is
    turned into a centre + radius (NM, capped at the API's 250 NM limit) — the outer
    bbox clip in :func:`fetch_feed` then trims the circle to the exact view.
    """
    if params.get("military"):
        data = await _http_json("https://api.airplanes.live/v2/mil")
        is_mil = True
    else:
        is_mil = False
        if bbox:
            w, s, e, n = bbox
            lat, lon = (s + n) / 2.0, (w + e) / 2.0
            import math
            d_lat = (n - lat) * 60.0
            d_lon = (e - lon) * 60.0 * max(0.01, math.cos(math.radians(lat)))
            radius = min(250.0, max(5.0, math.hypot(d_lat, d_lon)))
        else:
            lat, lon, radius = 0.0, 0.0, 250.0     # no viewport → central 250 NM (UI sends bbox)
        data = await _http_json(
            f"https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{int(round(radius))}")
    feats = []
    for ac in (data.get("ac") or []):
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        squawk = str(ac.get("squawk") or "")
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"name": (ac.get("flight") or ac.get("r") or ac.get("hex") or "").strip(),
                                     "icao": ac.get("hex"), "reg": ac.get("r"), "type": ac.get("t"),
                                     "desc": ac.get("desc"), "alt_ft": ac.get("alt_baro"),
                                     "speed_kt": ac.get("gs"), "heading_deg": ac.get("track"),
                                     "vert_rate_fpm": ac.get("baro_rate"), "squawk": squawk or None,
                                     "category": ac.get("category"),
                                     "emergency": _emergency_label(squawk, ac.get("emergency")),
                                     "military": is_mil, "kind": "aircraft"}})
    return _fc(feats)


async def _fetch_nasa_firms(fd, params, bbox, cfg) -> dict:
    key = cfg.get("api_key")
    if not key:
        raise _NeedsKey(fd)
    source = params.get("source") or "VIIRS_SNPP_NRT"
    days = max(1, min(10, int(params.get("days") or 1)))
    if bbox:
        w, s, e, n = bbox
    else:
        w, s, e, n = -180, -90, 180, 90
    area = f"{w},{s},{e},{n}"
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/{days}"
    text = await _http_text(url)
    feats = []
    rdr = csv.DictReader(io.StringIO(text))
    for row in rdr:
        try:
            lat = float(row["latitude"]); lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"name": "fire", "confidence": row.get("confidence"),
                                     "frp": row.get("frp"), "acq_date": row.get("acq_date"),
                                     "acq_time": row.get("acq_time"), "sensor": source, "kind": "fire"}})
    return _fc(feats)


_ACLED_TOKENS: dict[str, dict] = {}      # email → {"access_token", "exp"}


async def _acled_token(email: str, password: str) -> str:
    """OAuth2 password grant → bearer token (24 h), cached in-process per email.
    ACLED retired the legacy key+email scheme on 2025-09-15."""
    tok = _ACLED_TOKENS.get(email)
    if tok and tok["exp"] - time.time() > 60:
        return tok["access_token"]
    # ACLED's docs show the credential field as `email`; OAuth2 password grant uses
    # `username` — send both so either server expectation works.
    data = await _http_post_form("https://acleddata.com/oauth/token", {
        "username": email, "email": email, "password": password, "grant_type": "password",
        "client_id": "acled", "scope": "authenticated",
    })
    access = data.get("access_token")
    if not access:
        raise RuntimeError("ACLED auth returned no access_token (check email/password)")
    _ACLED_TOKENS[email] = {"access_token": access, "exp": time.time() + float(data.get("expires_in") or 86400)}
    return access


async def _fetch_acled(fd, params, bbox, cfg) -> dict:
    email = cfg.get("email"); password = cfg.get("password")
    if not (email and password):
        raise _NeedsKey(fd)
    token = await _acled_token(email, password)
    days = max(1, min(365, int(params.get("days") or 7)))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    url = (f"https://acleddata.com/api/acled/read?_format=json&limit=5000"
           f"&event_date={start}|{end}&event_date_where=BETWEEN")
    country = (params.get("country") or "").strip()
    if country:
        url += f"&country={quote(country)}"
    data = await _http_json(url, headers={"Authorization": f"Bearer {token}"})
    rows = data.get("data") if isinstance(data, dict) else data
    if rows is None and isinstance(data, dict):
        rows = data.get("results") or []
    feats = []
    for row in (rows or []):
        try:
            lat = float(row["latitude"]); lon = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"name": row.get("event_type") or "event",
                                     "sub_event": row.get("sub_event_type"), "date": row.get("event_date"),
                                     "fatalities": row.get("fatalities"), "actor1": row.get("actor1"),
                                     "location": row.get("location"), "notes": row.get("notes"),
                                     "kind": "acled"}})
    return _fc(feats)


async def _fetch_aisstream(fd, params, bbox, cfg) -> dict:
    key = cfg.get("api_key")
    if not key:
        raise _NeedsKey(fd)
    secs = max(2, min(20, int(params.get("seconds") or 6)))
    if bbox:
        w, s, e, n = bbox
    else:
        w, s, e, n = -180, -90, 180, 90
    sub = {"APIKey": key, "BoundingBoxes": [[[s, w], [n, e]]],
           "FilterMessageTypes": ["PositionReport"]}
    ships: dict[str, dict] = {}
    deadline = time.time() + secs
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=secs + 10)) as session:
        async with session.ws_connect("wss://stream.aisstream.io/v0/stream") as ws:
            await ws.send_json(sub)
            while time.time() < deadline:
                try:
                    msg = await ws.receive(timeout=max(0.5, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    continue                          # ping/pong etc.
                raw = msg.data
                if isinstance(raw, (bytes, bytearray)):    # aisstream sends BINARY JSON frames
                    raw = raw.decode("utf-8", "replace")
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                if isinstance(m, dict) and (m.get("error") or m.get("Error")):
                    raise RuntimeError(f"aisstream: {m.get('error') or m.get('Error')}")
                pr = (m.get("Message") or {}).get("PositionReport") or {}
                meta = m.get("MetaData") or {}
                lat = pr.get("Latitude") if pr else meta.get("latitude")
                lon = pr.get("Longitude") if pr else meta.get("longitude")
                mmsi = str(meta.get("MMSI") or pr.get("UserID") or "")
                if lat is None or lon is None or not mmsi:
                    continue
                ships[mmsi] = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                               "properties": {"name": (meta.get("ShipName") or mmsi).strip(),
                                              "mmsi": mmsi, "heading_deg": pr.get("TrueHeading"),
                                              "speed_kt": pr.get("Sog"), "kind": "ship"}}
    return _fc(list(ships.values()))


_LAT_LNG_RE = re.compile(r'data-lat(?:itude)?\s*=\s*["\'](-?\d+\.\d+)["\'][^>]*?data-l(?:ng|on|ongitude)\s*=\s*["\'](-?\d+\.\d+)["\']', re.I)
_JSON_LATLNG_RE = re.compile(r'"lat(?:itude)?"\s*:\s*(-?\d+\.\d+)\s*,\s*"l(?:ng|on|ongitude)"\s*:\s*(-?\d+\.\d+)', re.I)


async def _fetch_liveuamap(fd, params, bbox, cfg) -> dict:
    region = re.sub(r"[^a-z0-9-]", "", (params.get("region") or "ukraine").lower()) or "ukraine"
    _cf = ("LiveUAMap is behind a Cloudflare bot challenge that blocks server-side scraping. "
           "Use the generic feed importer with a direct data URL, or a browser-based exporter.")
    try:
        text = await _http_text(f"https://{region}.liveuamap.com/")
    except aiohttp.ClientResponseError as e:
        if e.status in (403, 503):                # Cloudflare returns 403/503 for blocked bots
            raise RuntimeError(_cf) from e
        raise
    # Some CF blocks return 200 with a challenge interstitial — detect that too.
    if "just a moment" in text.lower() or "cf-browser-verification" in text.lower() or "challenge-platform" in text.lower():
        raise RuntimeError(_cf)
    pairs = _LAT_LNG_RE.findall(text) or _JSON_LATLNG_RE.findall(text)
    feats = []
    seen = set()
    for la, lo in pairs:
        key = (la, lo)
        if key in seen:
            continue
        seen.add(key)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(lo), float(la)]},
                      "properties": {"name": "event", "kind": "liveuamap"}})
    if not feats:
        raise RuntimeError("could not parse markers from the LiveUAMap page (its layout may have changed) — "
                           "use the generic feed importer with a direct data URL instead")
    return _fc(feats)


async def _fetch_signalcockpit(fd, params, bbox, cfg) -> dict:
    url = (cfg.get("url") or "").strip()
    if not url:
        raise _NeedsKey(fd)
    text = await _http_text(url)
    return normalize(text, "auto")


async def _fetch_custom(fd, params, bbox, cfg) -> dict:
    url = fd.get("url")
    if not url:
        raise RuntimeError("custom feed has no URL")
    text = await _http_text(url)
    return normalize(text, fd.get("format") or "auto")


async def _fetch_aprs(fd, params, bbox, cfg) -> dict:
    """Locally-decoded APRS stations (fed via POST /aprs/decode) → GeoJSON points."""
    from app.core.decoders import aprs
    feats = []
    for st in aprs.decoder.stations.values():
        if st.lat is None or st.lon is None:
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [st.lon, st.lat]},
                      "properties": {"name": st.callsign, "symbol": st.symbol, "comment": st.comment,
                                     "course_deg": st.course_deg, "speed_kt": st.speed_kt,
                                     "altitude_ft": st.altitude_ft, "object": st.is_object,
                                     "n_msgs": st.n_msgs, "kind": "aprs"}})
    return _fc(feats)


def _h3_ring_lnglat(cell: str) -> list[list[float]]:
    """H3 cell → closed GeoJSON ring ``[[lon, lat], ...]`` (h3 v4, with a v3 fallback).
    Both API versions return the boundary as ``(lat, lng)`` vertices."""
    import h3
    try:
        verts = h3.cell_to_boundary(cell)          # h3 >= 4
    except AttributeError:
        verts = h3.h3_to_geo_boundary(cell)        # h3 3.x
    ring = [[lng, lat] for (lat, lng) in verts]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])                       # GeoJSON polygons must close
    return ring


def _gpsjam_severity(frac: float) -> tuple[str, str]:
    """GPSJam's own colour tiers: >10% degraded → red, 2–10% → amber, ≤2% → green."""
    if frac > 0.10:
        return "#ef4444", "high"
    if frac >= 0.02:
        return "#f59e0b", "medium"
    return "#22c55e", "low"


async def _fetch_gpsjam(fd, params, bbox, cfg) -> dict:
    """GPSJam daily GNSS-interference hexagons → coloured GeoJSON polygons.

    GPSJam publishes one CSV per UTC day (``hex,count_good_aircraft,count_bad_aircraft``
    over H3 res-4 cells). The file lands ~00:00 UTC, so a blank date tries today then
    yesterday. ``min_frac`` drops near-clean cells before we build polygons — the default
    keeps only amber/red (actual interference); set it to 0 to render the full coverage.
    """
    try:
        import h3  # noqa: F401  — fail loud-but-graceful if the optional dep is absent
    except ImportError as e:
        raise RuntimeError("the 'h3' package is required for the GPSJam feed (pip install h3)") from e

    date = (params.get("date") or "").strip()
    if date:
        candidates = [date]
    else:
        now = datetime.now(timezone.utc)
        candidates = [now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")]
    try:
        min_frac = max(0.0, min(1.0, float(params.get("min_frac", 0.02))))
    except (TypeError, ValueError):
        min_frac = 0.02

    text = used = None
    last_err: Optional[Exception] = None
    for d in candidates:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise RuntimeError(f"bad date {d!r} — use YYYY-MM-DD")
        try:
            text = await _http_text(f"https://gpsjam.org/data/{d}-h3_4.csv")
            used = d
            break
        except aiohttp.ClientResponseError as e:
            last_err = e
            if e.status == 404:                    # day not published yet → try the day before
                continue
            raise
    if text is None:
        raise RuntimeError(f"no GPSJam file for {candidates} ({last_err})")

    feats = []
    for row in csv.DictReader(io.StringIO(text)):
        cell = (row.get("hex") or "").strip()
        if not cell:
            continue
        try:
            good = int(row.get("count_good_aircraft") or 0)
            bad = int(row.get("count_bad_aircraft") or 0)
        except ValueError:
            continue
        total = good + bad
        if total <= 0:
            continue
        frac = bad / total
        if frac < min_frac:
            continue
        color, sev = _gpsjam_severity(frac)
        try:
            ring = _h3_ring_lnglat(cell)
        except Exception:
            continue
        if len(ring) < 4:
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"name": f"{round(frac * 100)}% degraded", "kind": "gnss_interference",
                                     "date": used, "h3": cell, "aircraft": total, "good": good, "bad": bad,
                                     "bad_fraction": round(frac, 4), "severity": sev,
                                     "stroke": color, "stroke-opacity": 0.9, "stroke-width": 1,
                                     "fill": color, "fill-opacity": 0.35}})
    return _fc(feats)


_FETCHERS = {
    "deepstate": _fetch_deepstate, "gdelt": _fetch_gdelt,
    "flights": _fetch_flights, "gpsjam": _fetch_gpsjam,
    "nasa_firms": _fetch_nasa_firms, "acled": _fetch_acled, "aisstream": _fetch_aisstream,
    "liveuamap": _fetch_liveuamap, "signalcockpit": _fetch_signalcockpit, "aprs": _fetch_aprs,
}


class _NeedsKey(Exception):
    def __init__(self, fd: dict):
        self.fd = fd
        super().__init__(f"{fd.get('name')} needs configuration")


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────
def _cache_paths(feed_id: str) -> tuple[Path, Path]:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", feed_id)
    return OSINT_DIR / f"{safe}.geojson", OSINT_DIR / f"{safe}.meta.json"


def _write_cache(feed_id: str, fc: dict, meta: dict) -> None:
    OSINT_DIR.mkdir(parents=True, exist_ok=True)
    gj, mj = _cache_paths(feed_id)
    gj.write_text(json.dumps(fc))
    mj.write_text(json.dumps(meta))


def get_cached(feed_id: str) -> Optional[dict]:
    gj, mj = _cache_paths(feed_id)
    if not gj.exists():
        return None
    try:
        fc = json.loads(gj.read_text())
        meta = json.loads(mj.read_text()) if mj.exists() else {}
        return {"geojson": fc, "meta": meta}
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_feed(feed_id: str, *, bbox: Optional[list[float]] = None,
                     params: Optional[dict] = None, max_features: int = _DEFAULT_MAX_FEATURES,
                     force: bool = False) -> dict:
    """Fetch + normalise + filter one feed → ``{geojson, source, as_of, count, total,
    truncated, attribution, error?}``. Falls back to cache when offline; ``unavailable``
    with a signup note when a keyed source isn't configured."""
    fd = _feed_def(feed_id)
    if fd is None:
        return {"source": "unavailable", "error": f"unknown feed {feed_id!r}", "geojson": _fc([])}
    params = params or {}
    max_features = int(max_features or _DEFAULT_MAX_FEATURES)
    attribution = fd.get("attribution", "")

    if fd.get("requires_config") and not _is_configured(feed_id):
        return {"source": "unavailable", "geojson": _fc([]), "attribution": attribution,
                "needs_config": True, "config_fields": fd.get("config_fields", []),
                "error": f"{fd.get('name')} needs configuration",
                "signup": fd.get("signup", "")}

    cached = get_cached(feed_id)

    # Offline / policy → serve cache only. Local feeds (e.g. APRS decoded on-box)
    # are not network-bound, so they always run their fetcher.
    if not fd.get("local") and (settings.network_policy == "offline_only" or net_state.is_online() is not True):
        if cached:
            m = cached["meta"]
            return {**m, "geojson": cached["geojson"], "source": "cache", "stale": True,
                    "attribution": attribution}
        return {"source": "unavailable", "geojson": _fc([]), "attribution": attribution,
                "error": "offline and no cached copy yet"}

    fetcher = _FETCHERS.get(feed_id, _fetch_custom)
    try:
        fc = await fetcher(fd, params, bbox, _config_for(feed_id))
        feats = list(fc.get("features") or [])
        if bbox:
            feats = _clip_bbox(feats, bbox)
        total = len(feats)
        feats, truncated = _cap(feats, max_features)
        out_fc = _fc(feats)
        as_of = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta = {"as_of": as_of, "count": len(feats), "total": total, "truncated": truncated,
                "attribution": attribution}
        _write_cache(feed_id, out_fc, meta)
        return {**meta, "geojson": out_fc, "source": "live"}
    except _NeedsKey:
        return {"source": "unavailable", "geojson": _fc([]), "attribution": attribution,
                "needs_config": True, "config_fields": fd.get("config_fields", []),
                "error": f"{fd.get('name')} needs configuration", "signup": fd.get("signup", "")}
    except Exception as e:
        log.info("osint fetch %s failed: %s", feed_id, e)
        if cached:
            m = cached["meta"]
            return {**m, "geojson": cached["geojson"], "source": "cache", "stale": True,
                    "attribution": attribution, "error": f"{type(e).__name__}: {e}"}
        return {"source": "unavailable", "geojson": _fc([]), "attribution": attribution,
                "error": f"{type(e).__name__}: {e}"}


def list_feeds() -> list[dict]:
    """Public catalogue: built-ins + customs with status (configured, cached count/as-of)."""
    store = _load_store()
    out = []
    for fd in {**BUILTIN_FEEDS, **store["custom"]}.values():
        fid = fd["id"]
        cached = get_cached(fid)
        meta = (cached or {}).get("meta", {}) if cached else {}
        out.append({
            "id": fid, "name": fd.get("name", fid), "category": fd.get("category", "other"),
            "description": fd.get("description", ""), "attribution": fd.get("attribution", ""),
            "color": fd.get("color", "#06d6a0"), "requires_config": bool(fd.get("requires_config")),
            "config_fields": fd.get("config_fields", []), "configured": _is_configured(fid),
            "best_effort": bool(fd.get("best_effort")), "big": bool(fd.get("big")),
            "wants_bbox": bool(fd.get("wants_bbox")), "params": fd.get("params", []),
            "signup": fd.get("signup", ""), "url": fd.get("url", ""),
            "custom": fid in store["custom"],
            "cached": bool(cached), "cached_count": meta.get("count"), "cached_as_of": meta.get("as_of"),
        })
    return out


def add_custom_feed(name: str, url: str, fmt: str = "auto", color: str = "#06d6a0") -> dict:
    if not url:
        raise ValueError("a feed URL is required")
    fmt = fmt if fmt in _FORMATS else "auto"
    store = _load_store()
    fid = "custom_" + re.sub(r"[^a-z0-9]+", "_", (name or url).lower()).strip("_")[:32]
    base, i = fid, 1
    while fid in store["custom"] or fid in BUILTIN_FEEDS:
        i += 1; fid = f"{base}_{i}"
    fd = {"id": fid, "name": name or url, "category": "custom", "description": f"Custom feed ({fmt})",
          "attribution": url.split("/")[2] if "//" in url else url, "color": color,
          "requires_config": False, "url": url, "format": fmt, "params": [], "custom": True}
    store["custom"][fid] = fd
    _save_store(store)
    return fd


def remove_feed(feed_id: str) -> bool:
    store = _load_store()
    changed = False
    if feed_id in store["custom"]:
        del store["custom"][feed_id]; changed = True
    if feed_id in store["config"]:
        del store["config"][feed_id]; changed = True
    if changed:
        _save_store(store)
    # also drop the cache files
    for p in _cache_paths(feed_id):
        try: p.unlink()
        except OSError: pass
    return changed


def set_config(feed_id: str, cfg: dict) -> dict:
    """Store per-feed config (api_key/email/url/region). Empty/None values clear a field."""
    if _feed_def(feed_id) is None:
        raise KeyError(feed_id)
    store = _load_store()
    cur = dict(store["config"].get(feed_id, {}))
    for k, v in (cfg or {}).items():
        if v in (None, ""):
            cur.pop(k, None)
        else:
            cur[k] = v
    store["config"][feed_id] = cur
    _save_store(store)
    return {"id": feed_id, "configured": _is_configured(feed_id)}
