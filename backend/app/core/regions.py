# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
core/regions.py — named admin-regions → bounding boxes, for the "download mapping data for a
state / country / region" feature in the layer manager and the map's right-click menu.

Two levels of granularity:
  * **Parents**: US states / European countries / other countries / hand-split sub-regions of the
    giant countries (Russia, China, …). These are the "larger area at once" option — useful at
    low-to-mid zoom (z0–z15) where a whole state/country imagery pack is still feasible.
  * **Cells**: 0.5° × 0.5° sub-cells of any parent, computed on demand. A 0.5° cell at z17 is
    ~150–800 MB — the "reasonably sized for z17" download unit. Right-click "download this region"
    returns the cell at the click point so the default behaviour is already z17-friendly.

Cell codes are synthetic and parse to a SW corner: ``c{lat_di:+04d}{lon_di:+05d}`` where the
ints are deci-degrees of the cell's SW corner (e.g. ``c+0300-0975`` = lat 30.0, lon -97.5). The
``get_region`` / ``cells_for`` / ``region_at`` helpers all accept cell codes; the download
pipeline (``core.packs.start_download`` → terrain / imagery / OSM / buildings) gets the same
``bbox`` shape it always has.

bbox format throughout: ``[min_lon, min_lat, max_lon, max_lat]``.
"""
from __future__ import annotations

import math
import re
from typing import Optional

CELL_DEG = 0.5  # sub-cell size; a 0.5° z17 imagery pack is ~150–800 MB depending on latitude


def _r(name: str, code: str, country: str, bbox: list[float], *, group: Optional[str] = None,
       parent: Optional[str] = None) -> dict:
    return {"name": name, "code": code, "country": country, "bbox": [float(v) for v in bbox],
            "group": group or country, "parent": parent}


# ── US states + DC (rough land bboxes; AK/HI/island areas as their main extent) ──
_US = [
    ("Alabama", "US-AL", [-88.5, 30.2, -84.9, 35.0]), ("Alaska", "US-AK", [-179.1, 51.2, -129.9, 71.4]),
    ("Arizona", "US-AZ", [-114.8, 31.3, -109.0, 37.0]), ("Arkansas", "US-AR", [-94.6, 33.0, -89.6, 36.5]),
    ("California", "US-CA", [-124.5, 32.5, -114.1, 42.0]), ("Colorado", "US-CO", [-109.1, 37.0, -102.0, 41.0]),
    ("Connecticut", "US-CT", [-73.7, 40.9, -71.8, 42.1]), ("Delaware", "US-DE", [-75.8, 38.4, -75.0, 39.8]),
    ("District of Columbia", "US-DC", [-77.2, 38.8, -76.9, 39.0]),
    ("Florida", "US-FL", [-87.6, 24.4, -80.0, 31.0]), ("Georgia", "US-GA", [-85.6, 30.3, -80.8, 35.0]),
    ("Hawaii", "US-HI", [-160.3, 18.9, -154.8, 22.3]), ("Idaho", "US-ID", [-117.2, 42.0, -111.0, 49.0]),
    ("Illinois", "US-IL", [-91.5, 36.9, -87.0, 42.5]), ("Indiana", "US-IN", [-88.1, 37.8, -84.8, 41.8]),
    ("Iowa", "US-IA", [-96.6, 40.4, -90.1, 43.5]), ("Kansas", "US-KS", [-102.1, 37.0, -94.6, 40.0]),
    ("Kentucky", "US-KY", [-89.6, 36.5, -81.9, 39.1]), ("Louisiana", "US-LA", [-94.0, 28.9, -88.8, 33.0]),
    ("Maine", "US-ME", [-71.1, 43.0, -66.9, 47.5]), ("Maryland", "US-MD", [-79.5, 37.9, -75.0, 39.7]),
    ("Massachusetts", "US-MA", [-73.5, 41.2, -69.9, 42.9]), ("Michigan", "US-MI", [-90.4, 41.7, -82.4, 48.3]),
    ("Minnesota", "US-MN", [-97.2, 43.5, -89.5, 49.4]), ("Mississippi", "US-MS", [-91.7, 30.1, -88.1, 35.0]),
    ("Missouri", "US-MO", [-95.8, 35.9, -89.1, 40.6]), ("Montana", "US-MT", [-116.1, 44.3, -104.0, 49.0]),
    ("Nebraska", "US-NE", [-104.1, 39.9, -95.3, 43.0]), ("Nevada", "US-NV", [-120.0, 35.0, -114.0, 42.0]),
    ("New Hampshire", "US-NH", [-72.6, 42.7, -70.6, 45.3]), ("New Jersey", "US-NJ", [-75.6, 38.9, -73.9, 41.4]),
    ("New Mexico", "US-NM", [-109.1, 31.3, -103.0, 37.0]), ("New York", "US-NY", [-79.8, 40.5, -71.8, 45.0]),
    ("North Carolina", "US-NC", [-84.4, 33.8, -75.4, 36.6]), ("North Dakota", "US-ND", [-104.1, 45.9, -96.5, 49.0]),
    ("Ohio", "US-OH", [-84.9, 38.4, -80.5, 42.0]), ("Oklahoma", "US-OK", [-103.1, 33.6, -94.4, 37.0]),
    ("Oregon", "US-OR", [-124.6, 42.0, -116.5, 46.3]), ("Pennsylvania", "US-PA", [-80.6, 39.7, -74.7, 42.3]),
    ("Rhode Island", "US-RI", [-71.9, 41.1, -71.1, 42.1]), ("South Carolina", "US-SC", [-83.4, 32.0, -78.5, 35.2]),
    ("South Dakota", "US-SD", [-104.1, 42.5, -96.4, 45.9]), ("Tennessee", "US-TN", [-90.3, 35.0, -81.6, 36.7]),
    ("Texas", "US-TX", [-106.6, 25.8, -93.5, 36.5]), ("Utah", "US-UT", [-114.1, 37.0, -109.0, 42.0]),
    ("Vermont", "US-VT", [-73.4, 42.7, -71.5, 45.0]), ("Virginia", "US-VA", [-83.7, 36.5, -75.2, 39.5]),
    ("Washington", "US-WA", [-124.8, 45.5, -116.9, 49.0]), ("West Virginia", "US-WV", [-82.6, 37.2, -77.7, 40.6]),
    ("Wisconsin", "US-WI", [-92.9, 42.5, -86.8, 47.1]), ("Wyoming", "US-WY", [-111.1, 41.0, -104.1, 45.0]),
]

# ── Europe (mainland-ish bboxes; overseas territories omitted) ──
_EU = [
    ("Albania", "AL", [19.3, 39.6, 21.1, 42.7]), ("Andorra", "AD", [1.4, 42.4, 1.8, 42.7]),
    ("Austria", "AT", [9.5, 46.4, 17.2, 49.0]), ("Belgium", "BE", [2.5, 49.5, 6.4, 51.5]),
    ("Bosnia and Herzegovina", "BA", [15.7, 42.5, 19.6, 45.3]), ("Bulgaria", "BG", [22.4, 41.2, 28.6, 44.2]),
    ("Croatia", "HR", [13.5, 42.4, 19.4, 46.6]), ("Czechia", "CZ", [12.1, 48.5, 18.9, 51.1]),
    ("Denmark", "DK", [8.0, 54.5, 15.2, 57.8]), ("Estonia", "EE", [21.8, 57.5, 28.2, 59.7]),
    ("Finland", "FI", [20.5, 59.7, 31.6, 70.1]), ("France", "FR", [-5.2, 41.3, 9.6, 51.1]),
    ("Germany", "DE", [5.9, 47.3, 15.0, 55.1]), ("Greece", "GR", [19.3, 34.8, 28.3, 41.8]),
    ("Hungary", "HU", [16.1, 45.7, 22.9, 48.6]), ("Iceland", "IS", [-24.6, 63.3, -13.5, 66.6]),
    ("Ireland", "IE", [-10.6, 51.4, -5.9, 55.4]), ("Italy", "IT", [6.6, 36.6, 18.5, 47.1]),
    ("Kosovo", "XK", [20.0, 41.8, 21.8, 43.3]), ("Latvia", "LV", [21.0, 55.7, 28.2, 58.1]),
    ("Liechtenstein", "LI", [9.5, 47.0, 9.6, 47.3]), ("Lithuania", "LT", [20.9, 53.9, 26.8, 56.5]),
    ("Luxembourg", "LU", [5.7, 49.4, 6.5, 50.2]), ("Malta", "MT", [14.2, 35.8, 14.6, 36.1]),
    ("Moldova", "MD", [26.6, 45.5, 30.2, 48.5]), ("Monaco", "MC", [7.4, 43.7, 7.5, 43.8]),
    ("Montenegro", "ME", [18.4, 41.8, 20.4, 43.6]), ("Netherlands", "NL", [3.3, 50.7, 7.2, 53.6]),
    ("North Macedonia", "MK", [20.4, 40.8, 23.0, 42.4]), ("Norway", "NO", [4.6, 57.9, 31.1, 71.2]),
    ("Poland", "PL", [14.1, 49.0, 24.2, 54.9]), ("Portugal", "PT", [-9.6, 36.9, -6.2, 42.2]),
    ("Romania", "RO", [20.2, 43.6, 29.7, 48.3]), ("Serbia", "RS", [18.8, 42.2, 23.0, 46.2]),
    ("Slovakia", "SK", [16.8, 47.7, 22.6, 49.6]), ("Slovenia", "SI", [13.4, 45.4, 16.6, 46.9]),
    ("Spain", "ES", [-9.4, 36.0, 3.4, 43.8]), ("Sweden", "SE", [11.0, 55.3, 24.2, 69.1]),
    ("Switzerland", "CH", [5.9, 45.8, 10.5, 47.8]), ("Ukraine", "UA", [22.1, 44.4, 40.2, 52.4]),
    ("United Kingdom", "GB", [-8.6, 49.9, 1.8, 60.9]),
]

# ── other countries handled whole (broad world coverage of commonly-referenced AOIs) ──
_WORLD = [
    # East / Southeast / South Asia
    ("Japan", "JP", [122.9, 24.0, 145.8, 45.6]), ("South Korea", "KR", [125.0, 33.1, 130.0, 38.7]),
    ("North Korea", "KP", [124.2, 37.7, 130.7, 43.0]), ("Mongolia", "MN", [87.7, 41.6, 119.9, 52.2]),
    ("Taiwan", "TW", [120.0, 21.9, 122.0, 25.3]), ("Hong Kong SAR", "HK", [113.8, 22.1, 114.5, 22.6]),
    ("Vietnam", "VN", [102.1, 8.2, 109.5, 23.4]), ("Thailand", "TH", [97.3, 5.6, 105.6, 20.5]),
    ("Cambodia", "KH", [102.3, 10.4, 107.6, 14.7]), ("Laos", "LA", [100.1, 13.9, 107.7, 22.5]),
    ("Myanmar", "MM", [92.2, 9.7, 101.2, 28.5]), ("Malaysia", "MY", [99.6, 0.8, 119.3, 7.4]),
    ("Singapore", "SG", [103.6, 1.2, 104.1, 1.5]), ("Indonesia", "ID", [95.0, -11.0, 141.0, 6.1]),
    ("Philippines", "PH", [116.9, 4.6, 126.6, 21.1]), ("Pakistan", "PK", [60.9, 23.7, 77.8, 37.1]),
    ("Bangladesh", "BD", [88.0, 20.7, 92.7, 26.6]), ("Sri Lanka", "LK", [79.6, 5.9, 81.9, 9.9]),
    ("Nepal", "NP", [80.0, 26.3, 88.2, 30.5]), ("Afghanistan", "AF", [60.5, 29.4, 74.9, 38.5]),
    # Middle East + North Africa
    ("Türkiye", "TR", [25.7, 35.8, 44.8, 42.1]), ("Israel", "IL", [34.2, 29.4, 35.9, 33.4]),
    ("Palestine", "PS", [34.2, 31.2, 35.6, 32.6]), ("Lebanon", "LB", [35.1, 33.0, 36.7, 34.7]),
    ("Jordan", "JO", [34.9, 29.1, 39.3, 33.4]), ("Syria", "SY", [35.7, 32.3, 42.4, 37.3]),
    ("Iraq", "IQ", [38.8, 29.0, 48.6, 37.4]), ("Iran", "IR", [44.0, 25.0, 63.4, 39.8]),
    ("Saudi Arabia", "SA", [34.5, 16.0, 55.7, 32.2]), ("Yemen", "YE", [42.5, 12.1, 54.5, 19.0]),
    ("UAE", "AE", [51.0, 22.6, 56.4, 26.1]), ("Oman", "OM", [51.9, 16.6, 59.8, 26.4]),
    ("Qatar", "QA", [50.7, 24.4, 51.7, 26.2]), ("Bahrain", "BH", [50.4, 25.8, 50.7, 26.3]),
    ("Kuwait", "KW", [46.6, 28.5, 48.5, 30.1]), ("Egypt", "EG", [24.7, 21.7, 36.9, 31.7]),
    ("Libya", "LY", [9.4, 19.5, 25.2, 33.2]), ("Tunisia", "TN", [7.5, 30.2, 11.6, 37.5]),
    ("Algeria", "DZ", [-8.7, 19.0, 11.9, 37.1]), ("Morocco", "MA", [-13.2, 21.4, -1.0, 35.9]),
    # Sub-Saharan Africa
    ("South Africa", "ZA", [16.5, -34.9, 32.9, -22.1]), ("Nigeria", "NG", [2.7, 4.2, 14.7, 13.9]),
    ("Kenya", "KE", [33.9, -4.7, 41.9, 5.5]), ("Ethiopia", "ET", [33.0, 3.4, 48.0, 14.9]),
    ("Sudan", "SD", [21.8, 8.7, 38.6, 22.0]), ("Somalia", "SO", [40.9, -1.7, 51.4, 12.0]),
    ("Tanzania", "TZ", [29.3, -11.7, 40.4, -1.0]), ("Uganda", "UG", [29.6, -1.5, 35.0, 4.2]),
    ("Ghana", "GH", [-3.3, 4.7, 1.2, 11.2]), ("Ivory Coast", "CI", [-8.6, 4.4, -2.5, 10.7]),
    ("Senegal", "SN", [-17.6, 12.3, -11.4, 16.7]), ("Mali", "ML", [-12.3, 10.1, 4.3, 25.0]),
    ("DR Congo", "CD", [12.2, -13.5, 31.3, 5.4]), ("Angola", "AO", [11.7, -18.0, 24.1, -4.4]),
    ("Mozambique", "MZ", [30.2, -26.9, 40.8, -10.5]), ("Madagascar", "MG", [43.2, -25.7, 50.5, -11.9]),
    # Oceania
    ("New Zealand", "NZ", [166.4, -47.3, 178.6, -34.4]), ("Papua New Guinea", "PG", [140.8, -11.7, 156.0, -0.9]),
    ("Fiji", "FJ", [177.0, -19.2, -178.0, -16.0]),
    # Americas (those not in _SUBREGIONS / _US)
    ("Mexico", "MX", [-118.4, 14.5, -86.7, 32.7]), ("Guatemala", "GT", [-92.3, 13.7, -88.2, 17.8]),
    ("Honduras", "HN", [-89.4, 12.9, -83.1, 16.5]), ("El Salvador", "SV", [-90.1, 13.1, -87.7, 14.5]),
    ("Nicaragua", "NI", [-87.7, 10.7, -83.1, 15.0]), ("Costa Rica", "CR", [-85.9, 8.0, -82.5, 11.2]),
    ("Panama", "PA", [-83.0, 7.2, -77.2, 9.6]), ("Cuba", "CU", [-85.0, 19.8, -74.1, 23.3]),
    ("Dominican Republic", "DO", [-72.0, 17.5, -68.3, 19.9]), ("Haiti", "HT", [-74.5, 18.0, -71.6, 20.1]),
    ("Jamaica", "JM", [-78.4, 17.7, -76.2, 18.6]), ("Puerto Rico (US)", "PR", [-67.3, 17.9, -65.6, 18.5]),
    ("Trinidad and Tobago", "TT", [-61.9, 10.0, -60.5, 11.4]),
    ("Colombia", "CO", [-79.0, -4.2, -66.9, 12.5]), ("Venezuela", "VE", [-73.4, 0.7, -59.8, 12.2]),
    ("Ecuador", "EC", [-81.0, -5.0, -75.2, 1.5]), ("Peru", "PE", [-81.3, -18.3, -68.7, 0.0]),
    ("Bolivia", "BO", [-69.6, -22.9, -57.5, -9.7]), ("Paraguay", "PY", [-62.6, -27.6, -54.3, -19.3]),
    ("Uruguay", "UY", [-58.4, -34.9, -53.1, -30.1]), ("Chile", "CL", [-75.6, -55.9, -66.4, -17.5]),
    ("Guyana", "GY", [-61.4, 1.2, -56.5, 8.6]), ("Suriname", "SR", [-58.1, 1.8, -53.9, 6.0]),
]

# ── big countries split into sub-regions (so a pack stays a sane size) ──
_SUBREGIONS = {
    "Russia": [
        ("Russia — Western (European)", "RU-W", [27.0, 43.0, 50.0, 70.0]),
        ("Russia — Urals & West Siberia", "RU-U", [50.0, 50.0, 80.0, 73.0]),
        ("Russia — Central & East Siberia", "RU-S", [80.0, 50.0, 130.0, 78.0]),
        ("Russia — Far East", "RU-FE", [130.0, 42.0, 180.0, 73.0]),
        ("Russia — Kaliningrad", "RU-K", [19.6, 54.3, 22.9, 55.3]),
    ],
    "China": [
        ("China — North (Beijing/Hebei/NE)", "CN-N", [113.0, 35.0, 135.0, 53.6]),
        ("China — East (Shanghai/Jiangsu)", "CN-E", [113.0, 27.0, 123.0, 35.0]),
        ("China — South (Guangdong/HK)", "CN-S", [104.0, 17.0, 123.0, 27.0]),
        ("China — Southwest (Sichuan/Yunnan)", "CN-SW", [97.0, 21.0, 110.0, 34.0]),
        ("China — Northwest (Xinjiang/Gansu)", "CN-NW", [73.5, 31.0, 104.0, 49.2]),
        ("China — Tibet", "CN-T", [78.4, 26.8, 99.1, 36.5]),
    ],
    "Canada": [
        ("Canada — British Columbia", "CA-BC", [-139.1, 48.3, -114.0, 60.0]),
        ("Canada — Prairies (AB/SK/MB)", "CA-PR", [-120.0, 49.0, -89.0, 60.0]),
        ("Canada — Ontario", "CA-ON", [-95.2, 41.7, -74.3, 56.9]),
        ("Canada — Québec", "CA-QC", [-79.8, 45.0, -57.1, 62.6]),
        ("Canada — Atlantic (NB/NS/PE/NL)", "CA-AT", [-67.8, 43.4, -52.6, 60.4]),
        ("Canada — North (YT/NT/NU)", "CA-NO", [-141.0, 60.0, -61.0, 83.1]),
    ],
    "Australia": [
        ("Australia — New South Wales", "AU-NSW", [141.0, -37.5, 153.6, -28.2]),
        ("Australia — Victoria", "AU-VIC", [140.9, -39.2, 150.0, -34.0]),
        ("Australia — Queensland", "AU-QLD", [137.9, -29.2, 153.6, -10.0]),
        ("Australia — South Australia", "AU-SA", [129.0, -38.1, 141.0, -25.9]),
        ("Australia — Western Australia", "AU-WA", [112.9, -35.2, 129.0, -13.7]),
        ("Australia — Northern Territory", "AU-NT", [129.0, -26.0, 138.0, -10.9]),
        ("Australia — Tasmania", "AU-TAS", [143.8, -43.7, 148.5, -39.5]),
    ],
    "Brazil": [
        ("Brazil — Southeast (SP/RJ/MG)", "BR-SE", [-53.1, -25.3, -39.7, -14.2]),
        ("Brazil — South (PR/SC/RS)", "BR-S", [-58.0, -33.8, -48.0, -22.5]),
        ("Brazil — Northeast", "BR-NE", [-48.8, -18.4, -34.8, -1.0]),
        ("Brazil — North (Amazon)", "BR-N", [-73.9, -13.7, -46.0, 5.3]),
        ("Brazil — Central-West", "BR-CW", [-61.6, -24.0, -45.9, -7.3]),
    ],
    "India": [
        ("India — North (Delhi/Punjab/UP)", "IN-N", [73.9, 26.0, 84.7, 34.6]),
        ("India — West (Maharashtra/Gujarat)", "IN-W", [68.1, 15.6, 80.9, 26.1]),
        ("India — South (TN/KA/AP/Kerala)", "IN-S", [72.6, 6.7, 84.8, 19.1]),
        ("India — East (WB/Bihar/Odisha)", "IN-E", [82.0, 17.8, 89.9, 27.5]),
        ("India — Northeast", "IN-NE", [88.0, 21.9, 97.4, 29.5]),
    ],
    "Kazakhstan": [
        ("Kazakhstan — West", "KZ-W", [46.5, 40.6, 62.0, 55.4]),
        ("Kazakhstan — East", "KZ-E", [62.0, 40.6, 87.4, 55.4]),
    ],
    "Argentina": [
        ("Argentina — North", "AR-N", [-73.6, -33.0, -53.6, -21.8]),
        ("Argentina — Center (Buenos Aires)", "AR-C", [-66.0, -41.0, -56.7, -33.0]),
        ("Argentina — Patagonia", "AR-P", [-73.6, -55.1, -62.0, -39.0]),
    ],
}


def _build_catalog() -> list[dict]:
    out: list[dict] = []
    for name, code, bbox in _US:
        out.append(_r(name, code, "United States", bbox, group="United States — states", parent="US"))
    for name, code, bbox in _EU:
        out.append(_r(name, code, name, bbox, group="Europe — countries"))
    for name, code, bbox in _WORLD:
        if code.endswith("2"):
            continue
        out.append(_r(name, code, name, bbox, group="Other countries"))
    for parent_name, subs in _SUBREGIONS.items():
        for name, code, bbox in subs:
            out.append(_r(name, code, parent_name, bbox, group=f"{parent_name} — regions", parent=parent_name))
    # de-dupe by code (keep first)
    seen, uniq = set(), []
    for r in out:
        if r["code"] in seen:
            continue
        seen.add(r["code"]); uniq.append(r)
    return uniq


CATALOG: list[dict] = _build_catalog()
_BY_CODE = {r["code"]: r for r in CATALOG}


# ── 0.5° sub-cells (computed on demand; not in CATALOG) ──────────────────────
_CELL_RE = re.compile(r"^c([+-]\d{3})([+-]\d{4})$")


def _cell_code(sw_lat: float, sw_lon: float) -> str:
    return f"c{int(round(sw_lat * 10)):+04d}{int(round(sw_lon * 10)):+05d}"


def _parse_cell_code(code: str) -> Optional[tuple[float, float]]:
    m = _CELL_RE.match(code or "")
    if not m:
        return None
    return int(m.group(1)) / 10.0, int(m.group(2)) / 10.0


def _floor_cell(v: float) -> float:
    return math.floor(v / CELL_DEG) * CELL_DEG


def _cell_bbox(sw_lat: float, sw_lon: float) -> list[float]:
    return [sw_lon, sw_lat, sw_lon + CELL_DEG, sw_lat + CELL_DEG]


def _format_cell_name(sw_lat: float, sw_lon: float) -> str:
    lat_h = "N" if sw_lat >= 0 else "S"
    lon_h = "E" if sw_lon >= 0 else "W"
    return f"{CELL_DEG:g}° cell · {abs(sw_lat):.1f}°{lat_h} {abs(sw_lon):.1f}°{lon_h}"


def _synth_cell(sw_lat: float, sw_lon: float, parent: Optional[dict] = None) -> dict:
    """Build the region-dict for a 0.5° cell. `parent` (if given) supplies country/group labels."""
    if parent is None:
        # Find the smallest catalogued parent containing the cell centre.
        clat, clon = sw_lat + CELL_DEG / 2, sw_lon + CELL_DEG / 2
        hits = [r for r in CATALOG if _in_bbox(clat, clon, r["bbox"])]
        parent = min(hits, key=lambda r: _bbox_area(r["bbox"])) if hits else None
    return {
        "name": _format_cell_name(sw_lat, sw_lon),
        "code": _cell_code(sw_lat, sw_lon),
        "country": parent["country"] if parent else "(cell)",
        "bbox": _cell_bbox(sw_lat, sw_lon),
        "group": f"{parent['name']} — {CELL_DEG:g}° cells" if parent else f"{CELL_DEG:g}° cells",
        "parent": parent["name"] if parent else None,
        "parent_code": parent["code"] if parent else None,
        "parent_bbox": list(parent["bbox"]) if parent else None,
        "cell": True,
    }


def cells_for(parent_code: str) -> list[dict]:
    """Enumerate all 0.5° cells covering a parent region's bbox. Cells share the parent's
    country/group labels so the UI can render them grouped under the parent."""
    parent = _BY_CODE.get(parent_code)
    if not parent:
        return []
    w, s, e, n = parent["bbox"]
    out: list[dict] = []
    sw_lat = _floor_cell(s)
    while sw_lat < n:
        sw_lon = _floor_cell(w)
        while sw_lon < e:
            out.append(_synth_cell(sw_lat, sw_lon, parent))
            sw_lon += CELL_DEG
        sw_lat += CELL_DEG
    return out


def all_regions() -> list[dict]:
    return [dict(r) for r in CATALOG]


def get_region(code: str) -> Optional[dict]:
    r = _BY_CODE.get(code)
    if r:
        return dict(r)
    # Synthetic sub-cell code (``c{lat_di}{lon_di}``)? Synthesize on demand.
    parsed = _parse_cell_code(code)
    if parsed is None:
        return None
    sw_lat, sw_lon = parsed
    return _synth_cell(sw_lat, sw_lon)


def search(q: str, limit: int = 40) -> list[dict]:
    """Substring/word search over region name + code + country. Returns parents only —
    sub-cells aren't in the search index (use ``cells_for`` after picking a parent, or
    right-click the map for the cell at a point)."""
    q = (q or "").strip().lower()
    if not q:
        return all_regions()[:limit]
    toks = [t for t in re.split(r"[\s,]+", q) if t]
    scored = []
    for r in CATALOG:
        hay = f"{r['name']} {r['code']} {r['country']} {r['group']}".lower()
        if all(t in hay for t in toks):
            # prefer name-prefix matches, then shorter names
            score = (0 if r["name"].lower().startswith(toks[0]) else 1, len(r["name"]))
            scored.append((score, r))
    scored.sort(key=lambda kv: kv[0])
    return [dict(r) for _, r in scored[:limit]]


def _in_bbox(lat: float, lon: float, bbox: list[float]) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def _bbox_area(bbox: list[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def region_at(lat: float, lon: float) -> Optional[dict]:
    """Right-click "download this region" → the 0.5° sub-cell at the click point, labelled with
    its containing state/country (so a click in Austin returns a Texas-grouped cell, not the whole
    state). None if the point isn't in any catalogued parent region."""
    hits = [r for r in CATALOG if _in_bbox(lat, lon, r["bbox"])]
    if not hits:
        return None
    parent = min(hits, key=lambda r: _bbox_area(r["bbox"]))
    return _synth_cell(_floor_cell(lat), _floor_cell(lon), parent)
