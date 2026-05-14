#!/usr/bin/env python3
"""
docs/capture_screenshots.py — capture the screenshots embedded in the flyer +
tutorial PDFs by driving a headless Chromium against a running Ares instance.

Usage
-----
    # 1) Make sure Playwright + Chromium are available (one-shot, in the venv):
    .venv/bin/pip install playwright
    .venv/bin/playwright install --with-deps chromium

    # 2) Start the backend + frontend dev or built server in another shell:
    ./start-web.sh         # or: ./start-backend.sh + cd frontend && npm run dev

    # 3) Capture every panel covered in the tutorial / flyer:
    .venv/bin/python docs/capture_screenshots.py

    # 4) Rebuild the PDFs (they auto-embed any PNG that's present here):
    .venv/bin/python docs/build_pdfs.py

The captures land in docs/screenshots/<key>.png at 1600×1000 each. Missing
captures are fine — build_pdfs.py falls back to the diagrammatic mockups it
ships with. So you can drop your own hand-curated PNGs here too; just name
them the same as the keys listed in `SHOTS` below.

Connecting to a running app
---------------------------
By default we point at  http://localhost:3000  (start-web.sh's bundled UI)
and authenticate as the admin password emitted on first backend startup
(read from data/.auth_admin if present). Override with environment vars:

    ARES_DOCS_URL          http://localhost:8000   (the API server, no UI)
    ARES_DOCS_USERNAME     admin
    ARES_DOCS_PASSWORD     <whatever the backend printed>

The script is best-effort: it waits for each panel's headline element to
exist, snaps a region around it, and moves on. If a particular panel isn't
in your current routing it's skipped with a warning rather than failing.
"""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
OUT_DIR = DOCS_DIR / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

URL = os.environ.get("ARES_DOCS_URL", "http://localhost:3000")
USERNAME = os.environ.get("ARES_DOCS_USERNAME", "admin")

# Password fallback chain: env → data/.auth_admin → blank (auth off)
_PW_FILE = DOCS_DIR.parent / "backend" / "data" / ".auth_admin"
PASSWORD = os.environ.get("ARES_DOCS_PASSWORD")
if PASSWORD is None and _PW_FILE.exists():
    try:
        PASSWORD = _PW_FILE.read_text(encoding="utf-8").strip().split("\n")[0]
    except Exception:
        PASSWORD = ""
PASSWORD = PASSWORD or ""

VIEWPORT = {"width": 1600, "height": 1000}

# Each tuple: (key, route_or_click_steps, post_load_wait_ms, screenshot_target)
# - key:           filename (PNG) in docs/screenshots/
# - steps:         a list of {action: 'goto'|'click'|'wait_for'|'sleep', ...}
# - target:        css-selector to clip to, or "viewport" to grab the whole window
SHOTS = [
    {"key": "map_overview",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": ".leaflet-container", "timeout_ms": 30000},
                {"action": "sleep", "ms": 2000}],
     "target": "viewport",
     "caption": "Main view — 2-D Leaflet map with the bottom-panel tabs"},

    {"key": "tab_algorithms",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "Algorithms"},
                {"action": "sleep", "ms": 1000}],
     "target": "viewport",
     "caption": "Algorithms tab — single-channel DF + multi-method fusion"},

    {"key": "tab_df",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "DF"},
                {"action": "sleep", "ms": 1000}],
     "target": "viewport",
     "caption": "DF tab — stacked spectrum + LoB compass + Live AoA solver + Advanced fusion"},

    {"key": "tab_video",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "Video"},
                {"action": "sleep", "ms": 1500}],
     "target": "viewport",
     "caption": "UAS Video panel — FPV demod with colormaps, snapshot & record"},

    {"key": "tab_emitters",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "Emitter Summary"},
                {"action": "sleep", "ms": 800}],
     "target": "viewport",
     "caption": "Emitter Summary — geolocated emitters with origin badges"},

    {"key": "tab_passive_radar",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "Passive Radar"},
                {"action": "sleep", "ms": 800}],
     "target": "viewport",
     "caption": "Passive radar — cross-ambiguity surface + range-Doppler hits"},

    {"key": "tab_3d",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "3D View"},
                {"action": "sleep", "ms": 2500}],
     "target": "viewport",
     "caption": "3-D View — coverage on real terrain (CesiumJS)"},

    {"key": "tab_terrain",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "Terrain Profile"},
                {"action": "sleep", "ms": 800}],
     "target": "viewport",
     "caption": "Terrain profile — link cross-section with Fresnel zone"},

    {"key": "tab_results",
     "steps": [{"action": "goto", "url": "/"},
                {"action": "wait_for", "selector": "button.tab", "timeout_ms": 20000},
                {"action": "click", "text": "Results"},
                {"action": "sleep", "ms": 800}],
     "target": "viewport",
     "caption": "Results tab — link budget / coverage metadata"},
]


def _auth_header() -> dict:
    """Get a bearer token from the backend if auth is on; else empty."""
    if not PASSWORD:
        return {}
    try:
        import urllib.request as ur, urllib.error as ue
        api_root = URL if URL.endswith(":8000") else URL.rsplit(":", 1)[0] + ":8000"
        req = ur.Request(api_root.rstrip("/") + "/api/v1/auth/login",
                          data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
                          headers={"Content-Type": "application/json"})
        with ur.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
            tok = d.get("access_token") or d.get("token")
            return {"Authorization": f"Bearer {tok}"} if tok else {}
    except Exception as e:
        print(f"[!] auth probe skipped: {e}")
        return {}


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[!] playwright is not installed in this Python.")
        print("    Install it with:")
        print("        .venv/bin/pip install playwright")
        print("        .venv/bin/playwright install --with-deps chromium")
        return 2

    headers = _auth_header()
    print(f"[+] capturing from {URL}  (auth: {'on' if headers else 'off'})")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            print(f"[!] couldn't launch Chromium: {e}")
            print("    Try:  .venv/bin/playwright install --with-deps chromium")
            return 3
        context = browser.new_context(viewport=VIEWPORT, extra_http_headers=headers)
        page = context.new_page()

        # If the UI relies on a token in localStorage instead of an Authorization header,
        # seed it now (matches the frontend's convention).
        if headers:
            tok = headers["Authorization"].split(" ", 1)[-1]
            page.goto(URL, wait_until="domcontentloaded")
            page.evaluate(f"window.localStorage.setItem('ares_token', {json.dumps(tok)})")

        successes = 0
        for spec in SHOTS:
            key = spec["key"]
            out = OUT_DIR / f"{key}.png"
            print(f"  · {key} …", end=" ", flush=True)
            try:
                for step in spec["steps"]:
                    a = step["action"]
                    if a == "goto":
                        page.goto(URL + step["url"], wait_until="networkidle", timeout=30_000)
                    elif a == "wait_for":
                        page.wait_for_selector(step["selector"], timeout=step.get("timeout_ms", 15_000))
                    elif a == "click":
                        # Click by exact text or by selector
                        if "selector" in step:
                            page.click(step["selector"], timeout=step.get("timeout_ms", 8_000))
                        else:
                            # tabs are <button> with role; click by exact text
                            page.get_by_role("button", name=step["text"]).first.click(
                                timeout=step.get("timeout_ms", 8_000))
                    elif a == "sleep":
                        time.sleep(step["ms"] / 1000.0)
                # Final shot
                if spec.get("target") and spec["target"] != "viewport":
                    el = page.query_selector(spec["target"])
                    if el is None:
                        raise PWTimeout(f"target {spec['target']!r} not found")
                    el.screenshot(path=str(out))
                else:
                    page.screenshot(path=str(out), full_page=False)
                print("✓")
                successes += 1
            except Exception as e:
                print(f"skipped ({type(e).__name__}: {e})")
        browser.close()

    print(f"[+] {successes}/{len(SHOTS)} screenshots written to {OUT_DIR}/")
    if successes < len(SHOTS):
        print("[i] Missing ones get a placeholder in the PDFs. Re-run when the app is up,")
        print("    or drop your own PNGs into docs/screenshots/<key>.png to override.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
