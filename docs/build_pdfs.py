#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Generate two PDFs in docs/:
  * Ares_Flyer.pdf     — a 4-page overview / capability flyer (portrait Letter)
  * Ares_Tutorial.pdf  — a 14-slide, PowerPoint-style how-to (landscape Letter)

Pure-Python, no LaTeX / browser / Office — built with matplotlib's PdfPages
(the only PDF-capable lib in the env). Re-run after a feature change:
    cd backend && ../.venv/bin/python ../docs/build_pdfs.py
"""
from __future__ import annotations

import os
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle, Ellipse

SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")

# ── palette ──────────────────────────────────────────────────────────────────
NAVY = "#0e2a47"
INK = "#1b2733"
MUTE = "#6b7785"
TEAL = "#0bb88f"
TEALD = "#0a6e57"
AMBER = "#d9830f"
PAPER = "#ffffff"
PANEL = "#f3f6f9"
LINE = "#d6dde4"
VERSION = "Ares v5.2 (alpha)"

plt.rcParams["font.family"] = "DejaVu Sans"

PAGE_W_IN = {True: 11.0, False: 8.5}     # landscape / portrait, inches


# ── low-level helpers ────────────────────────────────────────────────────────
def _fig(landscape: bool):
    fig = plt.figure(figsize=((11.0, 8.5) if landscape else (8.5, 11.0)))
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, color=PAPER, zorder=0))
    return fig, ax


def _cpi(size: float) -> float:
    """Approx. characters per inch for DejaVu Sans at `size` points (conservative)."""
    return 132.0 / size


def _wrap_chars(x_left: float, x_right: float, landscape: bool, size: float, k: float = 0.94) -> int:
    usable_in = max(0.35, (x_right - x_left) * PAGE_W_IN[landscape])
    return max(6, int(usable_in * _cpi(size) * k))


def _round(ax, x, y, w, h, fc, ec="none", lw=1.0, z=2, pad=0.012):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad={pad},rounding_size={pad}",
                                fc=fc, ec=ec, lw=lw, zorder=z))


def _t(ax, x, y, s, size=11, color=INK, weight="normal", ha="left", va="top", z=5):
    ax.text(x, y, s, fontsize=size, color=color, weight=weight, ha=ha, va=va, zorder=z, transform=ax.transAxes)


def _para(ax, x, y, text, x_right, landscape, size=10.0, color=INK, lh=0.024, weight="normal"):
    """A wrapped paragraph starting at (x, y); returns the y just below the last line."""
    n = _wrap_chars(x, x_right, landscape, size)
    lines = []
    for src in text.split("\n"):
        lines += textwrap.fill(src, n).split("\n")
    for i, ln in enumerate(lines):
        _t(ax, x, y - i * lh, ln, size=size, color=color, weight=weight)
    return y - len(lines) * lh


def _bullets(ax, x, y, items, landscape, x_right=0.95, size=10.0, lh=0.024, gap=0.011,
             color=INK, bullet="•", bcolor=TEAL, indent=0.024, head_color=None):
    """Each item is a string, or (head, body). Returns the y after the last."""
    head_color = head_color or color
    for it in items:
        head, body = (it if isinstance(it, tuple) else (None, it))
        _t(ax, x, y, bullet, size=size + 1, color=bcolor, weight="bold")
        if head:
            _t(ax, x + indent, y, head, size=size, color=head_color, weight="bold")
            y2 = _para(ax, x + indent, y - lh, body, x_right, landscape, size=size - 0.5, color=MUTE, lh=lh)
            y = y2 - gap
        else:
            y2 = _para(ax, x + indent, y, body, x_right, landscape, size=size, color=color, lh=lh)
            y = y2 - gap
    return y


def _header_bar(ax, title, subtitle=None, kicker=None, h=0.128, landscape=True):
    ax.add_patch(Rectangle((0, 1 - h), 1, h, color=NAVY, zorder=1))
    ax.add_patch(Rectangle((0, 1 - h - 0.006), 1, 0.006, color=TEAL, zorder=1))
    if kicker:
        _t(ax, 0.055, 1 - 0.028, kicker.upper(), size=9.5, color=TEAL, weight="bold")
    tsize = (20 if landscape else 19)
    if len(title) > 46:
        tsize = max(13, int(tsize * 46 / len(title)))
    _t(ax, 0.055, 1 - (0.050 if kicker else 0.044), title, size=tsize, color="white", weight="bold")
    if subtitle:
        _t(ax, 0.055, 1 - (0.050 if kicker else 0.044) - 0.050, subtitle, size=10.5, color="#c2d3e4")
    _t(ax, 0.945, 1 - 0.028, VERSION, size=9, color="#9fb6cc", ha="right")
    return 1 - h - 0.006


def _footer(ax, doc, page, total):
    ax.add_patch(Rectangle((0, 0), 1, 0.026, color=PANEL, zorder=1))
    _t(ax, 0.055, 0.013, f"{VERSION}  ·  {doc}", size=8, color=MUTE, va="center")
    _t(ax, 0.945, 0.013, f"{page} / {total}", size=8, color=MUTE, ha="right", va="center")


def _section(ax, x, y, label, x_right=0.94, landscape=True):
    ax.add_patch(Rectangle((x, y - 0.004), 0.004, 0.024, color=TEAL, zorder=3))
    _t(ax, x + 0.018, y + 0.016, label, size=13, color=NAVY, weight="bold")
    return y - 0.008


def _screenshot(ax, key, x, y, w, h, *, caption=None, fallback_text=None):
    """Embed docs/screenshots/<key>.png into the axes within the rectangle
    (x, y, w, h). If the file is missing, draws a labelled placeholder so the
    PDF still renders. Returns True if a real screenshot was embedded.

    Caption (if given) is drawn under the image in muted text.
    """
    path = os.path.join(SCREENSHOT_DIR, f"{key}.png")
    drew_image = False
    if os.path.isfile(path):
        try:
            img = mpimg.imread(path)
            # axes-fraction → display so we can use ax.imshow
            ax.imshow(img, extent=[x, x + w, y, y + h], aspect="auto",
                      interpolation="hanning", zorder=4,
                      transform=ax.transAxes)
            ax.add_patch(Rectangle((x, y), w, h, fc="none", ec=LINE, lw=0.8, zorder=5))
            drew_image = True
        except Exception as e:
            print(f"[!] couldn't embed {path}: {e}")
    if not drew_image:
        ax.add_patch(Rectangle((x, y), w, h, fc=PANEL, ec=LINE, lw=1.0, zorder=3))
        ax.add_patch(Rectangle((x, y + h - 0.005), w, 0.005, color=AMBER, zorder=4))
        _t(ax, x + w / 2, y + h / 2 + 0.012,
           fallback_text or f"⊕ screenshot: {key}",
           size=10, color=NAVY, weight="bold", ha="center")
        _t(ax, x + w / 2, y + h / 2 - 0.008,
           "drop a PNG at docs/screenshots/" + key + ".png",
           size=7.5, color=MUTE, ha="center")
        _t(ax, x + w / 2, y + h / 2 - 0.022,
           "or run  python docs/capture_screenshots.py",
           size=7.5, color=MUTE, ha="center")
    if caption:
        _t(ax, x + w / 2, y - 0.018, caption,
           size=8, color=MUTE, ha="center", weight="normal")
    return drew_image


def _logo_mark(ax, cx, cy, r, ring="white"):
    """A stylised 'Ares' mark — a DF/compass dial with bearing rays, an error ellipse, a fix dot."""
    import numpy as np
    ax.add_patch(Circle((cx, cy), r, fc="none", ec=ring, lw=2.0, zorder=6))
    ax.add_patch(Circle((cx, cy), r * 0.6, fc="none", ec="#7fe6cf", lw=1.1, zorder=6))
    for ang, c, lw in ((40, TEAL, 1.4), (118, AMBER, 2.2), (205, TEAL, 1.4), (300, "#7fe6cf", 1.4)):
        a = np.radians(90 - ang)
        ax.plot([cx, cx + r * 1.04 * np.cos(a)], [cy, cy + r * 1.04 * np.sin(a)],
                color=c, lw=lw, zorder=7, solid_capstyle="round", transform=ax.transAxes)
    ax.add_patch(Ellipse((cx + r * 0.18, cy - r * 0.05), r * 0.9, r * 0.42, angle=22,
                         fc="none", ec="#ffce86", lw=1.1, zorder=7))
    ax.add_patch(Circle((cx + r * 0.18, cy - r * 0.05), r * 0.085, fc=AMBER, ec="white", lw=0.8, zorder=8))


# ════════════════════════════════════════════════════════════════════════════
# FLYER  (portrait Letter, 4 pages)
# ════════════════════════════════════════════════════════════════════════════
def build_flyer(path):
    LS = False
    with PdfPages(path) as pdf:
        # ── p1: cover ────────────────────────────────────────────────────────
        fig, ax = _fig(LS)
        ax.add_patch(Rectangle((0, 0.39), 1, 0.61, color=NAVY, zorder=1))
        ax.add_patch(Rectangle((0, 0.384), 1, 0.006, color=TEAL, zorder=1))
        _t(ax, 0.07, 0.945, "ARES  ATAK", size=40, color="white", weight="bold")
        _t(ax, 0.072, 0.892, "RF propagation · passive geolocation · distributed sensing · 3-D globe · ATAK",
           size=10.5, color="#bcd0e4")
        _logo_mark(ax, 0.79, 0.71, 0.082)
        _t(ax, 0.07, 0.83, "From sensing to situational awareness — one platform.", size=15, color="white", weight="bold")
        _para(ax, 0.07, 0.79,
              "Locate an emitter across a mesh of sensors → predict its coverage on real 3-D terrain → "
              "push it to every operator's ATAK, and chat about it — air-gapped if you have to. The terrain "
              "physics is the actual ITS Longley-Rice; the geolocation is a maximum-likelihood fix with a "
              "geometry-correct error ellipse; the radios range from one dongle to a coherent DF array to a "
              "whole networked team of Ares nodes on a MANET.",
              0.585, LS, size=10.5, color="#cdd7e0", lh=0.0205)
        # capability chips
        chips = [
            ("Terrain RF propagation", "ITS Longley-Rice (ITM) + ~12 models, raster"),
            ("Passive geolocation / DF", "ML fix + error ellipse · TDOA/FDOA · array MUSIC"),
            ("Single-channel DF",       "RSS · Doppler-CPA · synthetic aperture · phase int."),
            ("Auto PTT identification", "DMR/P25/TETRA/NXDN/D-STAR/YSF/M17 + auto-decoder"),
            ("UAS / FPV video decode",  "NTSC/PAL FM video · colormaps · snapshot/record"),
            ("3-D globe",               "CesiumJS — RF on real terrain & buildings"),
            ("ATAK / TAK",              "CoT out: LoBs · fixes · GeoChat — UDP/mcast/TCP/TLS"),
            ("Distributed sensing",     "MANET peer fusion, mesh-signed · group chat"),
            ("Self-hostable, offline",  "Kali-ready installer · Jetson · laptop · Pi · cloud"),
        ]
        cw, ch, gx, gy, x0, y0 = 0.285, 0.046, 0.012, 0.009, 0.07, 0.565
        for i, (h_, b_) in enumerate(chips):
            col, row = i % 3, i // 3
            x = x0 + col * (cw + gx); y = y0 - row * (ch + gy)
            _round(ax, x, y - ch, cw, ch, "#16365a", ec="#2c557d", lw=0.8)
            ax.add_patch(Rectangle((x + 0.011, y - ch + 0.006), 0.0028, ch - 0.012, color=TEAL, zorder=3))
            _t(ax, x + 0.024, y - 0.012, h_, size=8.4, color="white", weight="bold")
            _para(ax, x + 0.024, y - 0.027, b_, x + cw - 0.008, LS, size=7.2, color="#aec3d6", lh=0.013)
        # white area below the navy band
        _t(ax, 0.07, 0.345, "Open source · runs fully offline · feature-equal to the SOOTHSAYER ATAK plugin against an Ares server — plus DF.",
           size=9.5, color=MUTE)
        ax.add_patch(Rectangle((0.07, 0.318), 0.40, 0.0025, color=TEAL, zorder=3))
        _t(ax, 0.07, 0.295, "Get started", size=13, color=NAVY, weight="bold")
        gs1 = [
            ("Install — ", "./install.sh  (Linux/macOS) · install.bat  (Windows) · air-gapped: ./install.sh --offline-bundle <dir>"),
            ("Run — ", "./start-backend.sh  (server :8000) · ./start-web.sh  (browser UI :3000) · ./start-desktop.sh  (Electron) · docker compose up -d"),
            ("Explore — ", "http://localhost:8000/docs  (interactive API) · docs/Ares.md · docs/DEPLOYMENT.md · cd backend && python -m tests.test_validation"),
        ]
        yy = 0.262
        for h_, b_ in gs1:
            _t(ax, 0.07, yy, h_, size=9, color=NAVY, weight="bold")
            yy = _para(ax, 0.165, yy, b_, 0.945, LS, size=9, color=INK, lh=0.0185) - 0.008
        _para(ax, 0.07, yy - 0.005,
              "Then read the companion 'Ares Tutorial' PDF (a slide-by-slide walkthrough). New to programming? Ask the project chat for the learning roadmap.",
              0.945, LS, size=8.6, color=MUTE, lh=0.0185)
        pdf.savefig(fig); plt.close(fig)

        # ── p2: what it does ─────────────────────────────────────────────────
        fig, ax = _fig(LS)
        _header_bar(ax, "What Ares does", "Six things most products keep separate — in one self-hostable stack.", kicker="Capabilities", landscape=LS)
        groups = [
            ("Terrain RF propagation",
             "The reference ITS Longley-Rice (ITM) — the SPLAT! / Radio-Mobile / FCC algorithm — plus ~a dozen "
             "empirical & ITU models, real diffraction (Deygout / Bullington / Epstein-Peterson / Giovanelli), "
             "atmospheric and space-weather corrections, a radar equation, 20+ analytic antenna patterns and "
             "measured-pattern import (NSMA/Planet MSI, NEC-2). Coverage as a radial heatmap or a per-pixel raster; "
             "point-to-point link budgets with terrain profile, Fresnel zone and LOS-obstruction; multisite / "
             "best-server / best-site; interference & EMCON; ray-trace; route; MANET coverage."),
            ("Passive geolocation / DF",
             "Maximum-likelihood bearing-only triangulation with a covariance-derived (geometry-correct) error "
             "ellipse, GDOP and an EKF emitter track; TDOA/FDOA hyperbolic multilateration; phase-interferometry / "
             "MUSIC / Capon array DoA with the CRLB. Bearings are terrain-capped via the propagation engine — DF "
             "that respects mountains. Three compass modes (Absolute LOB · Relative LOB · clock position) plus a "
             "guided calibration procedure."),
            ("SDR console & spectrum",
             "Single-channel SDRs monitor a spectrum / decode audio; multi-channel SDRs (declare the channel count) "
             "also produce lines of bearing. A DF panel gives stacked spectrum viewers (scroll-zoom, fixed y-axis) "
             "with a ▦ waterfall, a live LoB compass, and the DF options (tuner, active-power threshold, gain/AGC, "
             "demodulate-and-listen). SoapySDR drives real RF when installed; an audio-decode bridge dispatches "
             "DMR / P25 / TETRA / NXDN / … to op25 / dsd-fme / sdrtrunk when present."),
            ("3-D globe & offline data",
             "A CesiumJS 3-D globe alongside the Leaflet 2-D map: coverage, LOS, Fresnel zones, antenna lobes and "
             "obstruction markers on real heightmap terrain; KMZ import/export persisting across 2-D ↔ 3-D; offline "
             "data packs (terrain / OSM / imagery / buildings / clutter) with a provider chain that grows the pack "
             "online; a 'you-are-here' GPS marker."),
            ("ATAK / TAK integration",
             "Cursor-on-Target out (UDP / multicast / TCP / mutual-TLS) — LoBs as drawn routes, fixes as intel "
             "ground points with a CEP circle, chat as GeoChat — and a CoT receive listener so GeoChat from ATAK "
             "joins the same conversation. Offline data-pack and radio-template management. An open ATAK-CIV plugin "
             "(SDK-blocked for build/signing) for SOOTHSAYER-style parity plus DF."),
            ("Distributed sensing & chat",
             "Multiple SDRs on one box cross-fuse automatically; over a MANET, peer Ares nodes share LoBs / fixes / "
             "chat — mesh-signed (HMAC over a shared secret) and loop-safe (dedup, hop-count) — so the union of every "
             "node's bearings is fused on every node. Group chat with rooms, bridged to ATAK GeoChat. HF circuit "
             "prediction (ITU-R-P.533-style: hops, MUF/FOT/LUF, absorption, reliability) and real-SGP4 satellite "
             "visibility round it out."),
        ]
        y = 0.795
        for head, body in groups:
            _section(ax, 0.055, y, head, landscape=LS)
            y = _para(ax, 0.077, y - 0.014, body, 0.945, LS, size=8.6, color=INK, lh=0.0205) - 0.018
        _footer(ax, "Flyer", 2, 4); pdf.savefig(fig); plt.close(fig)

        # ── p3: how it works ─────────────────────────────────────────────────
        fig, ax = _fig(LS)
        _header_bar(ax, "How it works", "From a radio (or several, networked) to a fix, a coverage prediction, and ATAK.", kicker="The live loop", landscape=LS)
        y = 0.805
        steps = [
            ("1 · Sense", "An SDR (KrakenSDR / Matchstiq X40 / generic — or a manual operator) produces a line of bearing; a coherent array's snapshot is turned into one by interferometry / MUSIC."),
            ("2 · Fuse → Fix", "All bearings at the same frequency, from this node and every mesh peer, feed the ML solver: a maximum-likelihood emitter position with a geometry-correct error ellipse, GDOP, and an EKF track."),
            ("3 · Predict", "Optionally a coverage run reruns from the computed emitter location (ITS Longley-Rice on real terrain) — so you see its predicted reach, updating live as it's located."),
            ("4 · Push", "LoBs, the fix (with its CEP circle) and chat go out as Cursor-on-Target — UDP / multicast / TCP / mutual-TLS — appearing natively in ATAK / WinTAK / on a TAK Server."),
            ("5 · Share", "Over a MANET every Ares node re-broadcasts what it learns (signed, deduplicated, hop-bounded) — so the fused picture and the group chat live on every node, not just the one with the antenna."),
            ("6 · Map", "It renders on the 2-D Leaflet map and the 3-D CesiumJS globe: coverage heatmaps, LoB fans, error ellipses, suspected-emitter markers, KMZ overlays, your GPS position — online or offline."),
        ]
        for h_, b_ in steps:
            _t(ax, 0.06, y, "▸", size=12, color=AMBER, weight="bold")
            _t(ax, 0.085, y, h_, size=11, color=NAVY, weight="bold")
            y = _para(ax, 0.085, y - 0.023, b_, 0.945, LS, size=9.0, color=INK, lh=0.019) - 0.011
        # one server / mesh sketch
        _section(ax, 0.055, y - 0.002, "One server — or a mesh of them", landscape=LS)
        import numpy as np
        nodes = [("Node A", "Jetson + DF array"), ("Node B", "laptop + dongle"), ("Node C", "Pi 5, links-only")]
        ny = 0.305
        for i, (lbl, sub) in enumerate(nodes):
            x = 0.20 + i * 0.30
            _round(ax, x - 0.10, ny - 0.032, 0.20, 0.064, PANEL, ec=LINE, lw=1.2)
            _logo_mark(ax, x - 0.076, ny + 0.008, 0.013, ring=NAVY)
            _t(ax, x + 0.018, ny + 0.020, lbl, size=9.5, color=NAVY, weight="bold", ha="center")
            _t(ax, x + 0.018, ny + 0.003, sub, size=7.2, color=MUTE, ha="center")
            if i < 2:
                ax.add_patch(FancyArrowPatch((x + 0.105, ny), (x + 0.195, ny), arrowstyle="<|-|>",
                                             mutation_scale=11, color=TEAL, lw=1.4, zorder=4))
        ax.add_patch(FancyArrowPatch((0.50, ny - 0.038), (0.50, ny - 0.074), arrowstyle="-|>", mutation_scale=13, color=AMBER, lw=1.6, zorder=4))
        _round(ax, 0.34, ny - 0.142, 0.32, 0.058, NAVY)
        _t(ax, 0.50, ny - 0.106, "ATAK / WinTAK / TAK Server", size=10, color="white", weight="bold", ha="center")
        _t(ax, 0.50, ny - 0.124, "CoT:  LoBs · fixes · CEP circles · GeoChat", size=7.8, color="#bcd0e4", ha="center")
        _para(ax, 0.055, 0.135,
              "Each node shares LoBs / fixes / chat over the mesh — HMAC-signed with a shared secret, deduplicated and "
              "hop-count-bounded; the fusion runs on every node, so losing one node loses a sensor, not the picture. "
              "Same physics throughout; offline-first, with online auto-fetch when reachable.",
              0.945, LS, size=8.4, color=MUTE, lh=0.018)
        _footer(ax, "Flyer", 3, 4); pdf.savefig(fig); plt.close(fig)

        # ── p4: where it stands + deployment + get started ───────────────────
        fig, ax = _fig(LS)
        _header_bar(ax, "Where Ares stands", "No single product spans this — Ares ties the layers together, openly.", kicker="Landscape & deployment", landscape=LS)
        rows = [
            ("Capability", "Ares", "Closest existing tools"),
            ("Terrain propagation", "ITS Longley-Rice + ~12 models, raster", "CloudRF · SPLAT! / Radio Mobile (ITM only)"),
            ("Geolocation / DF", "ML fix + error ellipse, TDOA/FDOA, MUSIC", "KrakenSDR doa app (DoA + basic triangulation)"),
            ("SDR spectrum / audio", "spectrum + waterfall, decode bridge", "SDR# / SDRtrunk / op25 (no propagation, no fix)"),
            ("3-D globe", "CesiumJS, RF on real terrain", "STK (orbits, not RF coverage)"),
            ("Offline / air-gapped", "data packs + provider chain; Jetson/Pi", "SPLAT! (yes, 1990s UI); CloudRF (SaaS-bound)"),
            ("ATAK / CoT out", "LoBs/fixes/GeoChat — UDP/mcast/TCP/TLS", "SOOTHSAYER (CoT via a commercial plugin)"),
            ("Distributed (MANET)", "peer fusion, mesh-signed, + group chat", "— (nothing comparable, open or closed)"),
            ("Licensing", "open source, self-hostable", "CloudRF/STK commercial · SPLAT!/doa-app open"),
        ]
        cx = [0.07, 0.275, 0.585]
        tx_right = [0.27, 0.575, 0.94]
        y0 = 0.81
        rh = 0.029
        ax.add_patch(Rectangle((0.05, y0 - rh * (len(rows) - 1) - 0.017), 0.90, rh * len(rows) + 0.018, fc=PANEL, ec=LINE, lw=1.0, zorder=1))
        for r, row in enumerate(rows):
            yy = y0 - r * rh
            if r == 0:
                ax.add_patch(Rectangle((0.05, yy - 0.012), 0.90, rh, color=NAVY, zorder=2))
            for c in range(3):
                col = "white" if r == 0 else (NAVY if c == 0 else (TEALD if c == 1 else INK))
                wt = "bold" if (r == 0 or c <= 1) else "normal"
                sz = 8.0 if r == 0 else 7.5
                _para(ax, cx[c], yy + 0.009, row[c], tx_right[c], LS, size=sz, color=col, weight=wt, lh=0.012)
        # deployment targets
        y = _section(ax, 0.055, 0.505, "Deployment targets", landscape=LS) - 0.013
        y = _bullets(ax, 0.06, y, [
            ("NVIDIA Jetson Orin — ", "the full server with a GPU (CuPy multisite / Monte-Carlo); the 'Ares-in-a-box' vehicle node."),
            ("Rugged x86 laptop — ", "the full server, CPU or eGPU; pair it with a 500 GB+ SSD for the data packs."),
            ("Raspberry Pi 5 — ", "a 'links-only' node: P2P link mode, RF-link / Co-Opt, DF, HF, space weather."),
            ("Cloud VM — ", "a shared server; leave ARES_AUTH on (the default whenever it isn't bound to loopback)."),
        ], LS, x_right=0.945, size=8.8, lh=0.0165, gap=0.006)
        # reference-grade + honest box
        _round(ax, 0.05, 0.045, 0.90, 0.235, NAVY)
        _t(ax, 0.075, 0.270, "Ares — and honest about the rest", size=12.5, color="white", weight="bold")
        ax.add_patch(Rectangle((0.075, 0.243), 0.30, 0.0025, color=TEAL, zorder=3))
        yy = _para(ax, 0.075, 0.230,
                   "Reference-grade:  the ITS Longley-Rice ITM port (lrprop / avar / adiff / ascat / alos, 7 climate zones, "
                   "NTIA Report 82-100); the ML / Stansfield triangulation + covariance error ellipse; TDOA/FDOA (Chan); "
                   "multi-baseline phase interferometry + CRLB; MUSIC / Capon / Bartlett; SGP4; the ITU-R-style HF circuit; "
                   "CoT / GeoChat I/O including mutual-TLS; the HMAC-signed MANET fusion.",
                   0.945, LS, size=8.4, color="#cdd7e0", lh=0.0185) - 0.014
        _para(ax, 0.075, yy,
              "Still approximate / pending hardware:  the HF foF2 is a parameterised model, not the CCIR/URSI coefficient maps; "
              "ITM isn't yet bit-validated against NTIA's C reference; hardware-in-the-loop (KrakenSDR → fix → CoT) and a live "
              "multi-node mesh are code-exercised but untested on real RF; the ATAK plugin is SDK-blocked for build/signing. "
              "All of it is spelled out in  docs/Ares.md.",
              0.945, LS, size=8.4, color="#9fb6cc", lh=0.0185)
        _footer(ax, "Flyer", 4, 4); pdf.savefig(fig); plt.close(fig)
    print("wrote", path)


# ════════════════════════════════════════════════════════════════════════════
# TUTORIAL  (landscape Letter, slides — exact count is len(SL)+1)
# ════════════════════════════════════════════════════════════════════════════
LS_T = True


def _slide(pdf, n, total, title, kicker, body_fn):
    fig, ax = _fig(LS_T)
    top = _header_bar(ax, title, kicker=kicker, landscape=LS_T)
    body_fn(ax, top)
    _footer(ax, "Tutorial", n, total)
    pdf.savefig(fig); plt.close(fig)


def build_tutorial(path):
    SL = []   # (title, kicker, body_fn)

    # 2 — architecture
    def s_arch(ax, top):
        _t(ax, 0.055, top - 0.028, "Three pieces talking over plain web protocols", size=14, color=NAVY, weight="bold")

        def box(x, y, w, h, head, body, foot):
            _round(ax, x, y, w, h, PANEL, ec=LINE, lw=1.3)
            ax.add_patch(Rectangle((x + 0.014, y + h - 0.012), w - 0.028, 0.005, color=TEAL, zorder=3))
            _t(ax, x + 0.020, y + h - 0.020, head, size=11.5, weight="bold", color=NAVY)
            yb = _para(ax, x + 0.020, y + h - 0.046, body, x + w - 0.014, LS_T, size=8.6, color=MUTE, lh=0.0205)
            _t(ax, x + 0.020, y + 0.018, foot, size=8.2, color=TEALD, weight="bold")
        box(0.055, 0.42, 0.40, top - 0.07 - 0.42, "Backend — Python (FastAPI · NumPy)",
            "The physics engine — ITS Longley-Rice (ITM) + ~12 models, diffraction, atmosphere, HF (ITU-R-style), "
            "SGP4 satellites. The DF solver — ML triangulation + covariance ellipse, TDOA/FDOA, phase interferometry / "
            "MUSIC, EKF tracks. The SDR manager, the CoT-to-ATAK bridge, the MANET peer mesh + group chat. "
            "~90 REST endpoints + a WebSocket.", "→  backend/app/")
        box(0.545, 0.42, 0.40, top - 0.07 - 0.42, "Frontend — JavaScript / React (Vite · Cesium)",
            "The screen — the 2-D Leaflet map + the 3-D CesiumJS globe, the bottom-panel tabs (Results / Terrain / "
            "3-D / DF / Chat / Layers / …), the SDR and ATAK-Server consoles, the drawing tools and NATO symbology. "
            "Talks to the backend over REST + a live WebSocket. Packaged with Electron as a desktop app.", "→  frontend/src/")
        box(0.055, 0.075, 0.40, 0.24, "ATAK plugin — Kotlin (Android)",
            "SOOTHSAYER-style parity against an Ares server, plus DF. SDK-blocked for build/signing — it needs the "
            "tak.gov SDK and a publisher account.", "→  atak-plugin/")
        box(0.545, 0.075, 0.40, 0.24, "Glue — git · Docker · CI · the shell",
            "start-backend.sh / start-web.sh / start-desktop.sh · install.sh · docker-compose.yml · "
            ".github/workflows/ci.yml (the 53-check backend harness + the 8-check frontend tests + a bundle/build).", "")
        ax.add_patch(FancyArrowPatch((0.455, 0.62), (0.545, 0.62), arrowstyle="<|-|>", mutation_scale=15, color=AMBER, lw=2.0, zorder=5))
        _t(ax, 0.50, 0.645, "REST + WebSocket", size=8.5, color=AMBER, ha="center", weight="bold")
        _t(ax, 0.50, 0.604, "(JSON over HTTP)", size=7.5, color=MUTE, ha="center")
    SL.append(("Architecture — what's under the hood", "Tutorial · 2", s_arch))

    # 3 — run it
    def s_run(ax, top):
        y = _bullets(ax, 0.055, top - 0.022, [
            ("Backend (the engine).  ", "cd ares && ./start-backend.sh — runs FastAPI on :8000. The first run with auth on logs an admin password once (auth is ON by default unless the server is bound to a loopback address)."),
            ("Web UI.  ", "./start-web.sh — backend + a built UI on :3000, opens a browser. For development: cd frontend && npm install && npm run dev (:5173)."),
            ("Desktop app.  ", "./start-desktop.sh — the same UI wrapped in Electron."),
            ("Docker.  ", "docker compose up -d — backend + frontend in containers; point ARES_PACKS_HOST_DIR at a pre-staged packs disk, set ARES_AUTH / ARES_NETWORK_POLICY / ARES_MESH_SECRET as needed."),
            ("Air-gapped.  ", "./install.sh --offline-bundle <dir> stages a pre-built data bundle and skips the online terrain download; then run with ARES_NETWORK_POLICY=offline_only."),
            ("Poke the API directly.  ", "http://localhost:8000/docs — every endpoint, with a 'Try it out' button. The fastest way to learn what the engine actually does."),
        ], LS_T, size=10.5, lh=0.025, gap=0.012)
        ptop, ph = y - 0.012, 0.156        # panel: hugs its content
        _round(ax, 0.055, ptop - ph, 0.89, ph, PANEL, ec=LINE, lw=1.2)
        _t(ax, 0.072, ptop - 0.026, "The window, once it's up", size=11, color=NAVY, weight="bold")
        yp = _para(ax, 0.072, ptop - 0.052,
              "Header: the mode (Propagation / Geolocation), the ATAK / Server console, the SDR console, a GPU badge, Run.   "
              "Left column: the transmitter / antenna / atmosphere / propagation controls.   Centre: the map (2-D ⇄ 3-D toggle in the ⚙ menu).",
              0.935, LS_T, size=9.2, color=INK, lh=0.022) - 0.014
        _para(ax, 0.072, yp,
              "Bottom panel (collapsible): Results · Terrain Profile · Link Budget · 3-D View · DF · Chat · dB Calc · Layers · Emitter Summary · "
              "Saved Locations · Space Wx.   Right-click the map to drop an emitter.",
              0.935, LS_T, size=9.2, color=INK, lh=0.022)
    SL.append(("Running it — server, UI, desktop, Docker", "Tutorial · 3", s_run))

    # 4 — the map
    def s_map(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("2-D ⇄ 3-D.  ", "Leaflet 2-D map by default; the ⚙ menu (top-right of the map) or the bottom-panel '3-D View' tab switches to the CesiumJS globe. Camera, layers, drawn features, KMZ and your GPS marker carry across — KMZ added in 2-D persists in 3-D and back."),
            ("Layers.  ", "Drag-and-drop KML / KMZ / GeoJSON / GPX / GeoTIFF / DTED / images onto the map → they appear in the Layers tab (toggle, recolour, remove) and on both views. The import button on the globe imports the same way."),
            ("Drawing tools.  ", "The ✎ palette — points, lines, polygons, rectangles, circles, ellipses, freehand, range rings, fans, range-bearing, geofences, MIL / NATO symbology."),
            ("Ruler & search.  ", "The ruler tool gives two-click distance/bearing; the search box does place search (Nominatim); ⊕ re-centres on the transmitter."),
            ("Origin badges on emitter markers.  ", "Algorithm-tab fixes appear as a rotated-square Σ marker; DF-head fixes get a 'DF' badge; manual TXs stay as the labelled circle — so you can tell at a glance where a fix came from."),
            ("On the map you'll see.  ", "the TX (cyan) and RX (yellow) markers, coverage heatmaps, LoB fans, error ellipses, antenna lobes, KMZ overlays, and a cyan '▲ you' GPS marker."),
        ], LS_T, x_right=0.51, size=9.5, lh=0.022, gap=0.011)
        _screenshot(ax, "map_overview", 0.53, 0.13, 0.42, top - 0.16,
                     caption="2-D Leaflet view with the bottom-panel tabs",
                     fallback_text="Main map")
    SL.append(("The map — 2-D Leaflet ⇄ 3-D CesiumJS globe", "Tutorial · 4", s_map))

    # 5 — coverage
    def s_cov(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("1.  Place the transmitter.  ", "Right-click the map (or set lat/lon in the left panel). Set height AGL, power, frequency, and the antenna — type / gain / azimuth / tilt, a polar pattern (omni · sector · Yagi · dish · …) or an imported measured pattern."),
            ("2.  Pick a propagation model.  ", "ITM (Longley-Rice — the default, terrain-aware) · Okumura-Hata urban/suburban/rural · COST-231 · two-ray · ITU-R P.1546 / P.528 · Egli / Ericsson / SUI · radar. Or 'auto-select' suggests one from frequency / range / environment. Add a diffraction method (Deygout / Bullington / …) and the environment / clutter."),
            ("3.  (Optional) raster mode.  ", "Tick 'raster' next to Run — one ITM path per grid cell instead of a radial sweep: even coverage everywhere, no thinning at long range (heavier — pick it when you want a uniform raster)."),
            ("4.  Run.  ", "The header's Run button. Progress streams over a WebSocket; the result is a colour-graded coverage layer (signal strength) on the 2-D map and the 3-D globe. The Results / Link-Budget tabs show the numbers; Space-Wx shows any HF corrections applied."),
            ("Also under 'simulate'.  ", "multisite (all radios fused) · best-server · best-site / best-site-polygon (Monte-Carlo candidate ranking) · interference / EMCON · ray-trace · route · MANET coverage · satellite visibility."),
        ], LS_T, size=10.5, lh=0.025, gap=0.013)
    SL.append(("Coverage simulation — place, model, run", "Tutorial · 5", s_cov))

    # 6 — P2P
    def s_p2p(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("Set the receiver.  ", "Switch the Coverage tab to 'P2P' (point-to-point); click the map to drop the RX, set its height (and altitude for an airborne RX). The TX/RX pair shows a dashed link line, a translucent first-Fresnel ellipsoid along it on the globe, and an antenna lobe at the TX."),
            ("Run the link.  ", "Run computes the full link budget along the real terrain profile — path loss, received signal vs. sensitivity, fade margin, propagation mode (LOS / diffraction / troposcatter) and the radio horizons."),
            ("Terrain profile & obstruction.  ", "The 'Terrain Profile' bottom tab shows the path's elevation cross-section with the line-of-sight, the Fresnel zone, and where (if anywhere) terrain blocks it."),
            ("Same engine as coverage.  ", "P2P uses the same ITS Longley-Rice port — the model SPLAT! / Radio Mobile / the FCC use — not an approximation. LOS / diffraction / troposcatter labels key off the actual take-off angles."),
        ], LS_T, x_right=0.51, size=9.6, lh=0.0225, gap=0.012)
        _screenshot(ax, "tab_terrain", 0.53, 0.13, 0.42, top - 0.16,
                     caption="Terrain Profile tab — cross-section + Fresnel zone",
                     fallback_text="Terrain Profile")
    SL.append(("Point-to-point links & terrain profiles", "Tutorial · 6", s_p2p))

    # 7 — DF / geolocation
    def s_df(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("Switch to Geolocation mode.  ", "Header → mode → Geolocation. The map gets a DF workflow; the 'Emitter Summary' bottom tab tracks the picture."),
            ("Add lines of bearing.  ", "Radial-menu 'Add LoB from here' on a self/sensor marker (or the LoB list panel): azimuth, RSSI, frequency, antenna pattern, observer height, confidence, emitter id (DMR / IMSI / MAC / callsign). Each LoB draws as a bearing wedge."),
            ("The fix appears automatically.  ", "Two LoBs at the same frequency → a 'Cut'; three or more → a 'Fix' — computed by a maximum-likelihood (iteratively-reweighted Gauss-Newton) triangulator. You get the emitter position, a covariance-derived error ellipse, GDOP, residual RMS, and CEP / 95 % radii."),
            ("Advanced fusion (new).  ", "On the DF tab the 'Advanced fusion' subsection runs ML-grid or EKF fusion across the current LoB list — more robust than pair-intersection on oblique baselines / non-Gaussian σ. 'Send to map' drops the result as an algorithm-origin emitter marker."),
            ("Beyond bearings.  ", "POST /api/v1/geolocate/multilaterate does TDOA (± FDOA) hyperbolic location from ≥3 receivers. POST /api/v1/df/aoa turns an antenna-array snapshot (inter-channel phases, or IQ) into a bearing via phase interferometry / MUSIC / Capon + CRLB σ, then feeds it into the ML fix."),
        ], LS_T, x_right=0.51, size=9.5, lh=0.022, gap=0.011)
        _screenshot(ax, "tab_emitters", 0.53, 0.13, 0.42, top - 0.16,
                     caption="Emitter Summary tab — fixes with origin badges",
                     fallback_text="Emitter Summary")
    SL.append(("Geolocation — line-of-bearing → ML fix", "Tutorial · 7", s_df))

    # 8 — SDR console (1)
    def s_sdr1(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("Open it.  ", "The SDR console button in the header. It lists your SDR sources, the live DF picture, the GPS section, and a pointer to where the CoT push targets live (the ATAK / Server console)."),
            ("Add a source — pick a class.  ", "Single-channel: monitor a spectrum / decode audio, but it cannot produce a line of bearing (DF needs ≥2 coherent channels — the manager rejects its LoBs and tells you why).  Multi-channel: declare the channel count (e.g. 5 for a KrakenSDR) and the array type (UCA / ULA / custom) + element spacing → it does DF."),
            ("Type & host.  ", "KrakenSDR (polls krakensdr_doa's DOA_value over HTTP), Epiq Matchstiq X40 (an external DF process pushes JSON-lines), or generic JSON-lines TCP. Enter host:port, the device's lat/lon (or tick 'use GPS'), the frequency, and the active-signal threshold."),
            ("LoB accuracy estimate.  ", "As you set the channel count / array, Ares shows the expected 1-σ bearing accuracy and the CEP-at-1-km (the interferometry CRLB plus a ~2.5° practical calibration floor that tightens with more channels)."),
            ("GPS.  ", "Set your position in the SDR console (or POST /api/v1/sdr/gps from gpsd / a phone) — it shows as the '▲ you' marker on both maps and becomes the observer position for LoBs that arrive without one."),
            ("Compass mode + calibration.  ", "Absolute LOB (true north — map-plottable) · Relative LOB (degrees off the antenna front, 0° = front) · Clock position. Absolute = (0° + heading) + Relative. The DF panel has a guided calibrate form: aim the antenna at a target whose true bearing you know, shoot a LoB, enter both → heading = (true − relative)."),
        ], LS_T, size=10.0, lh=0.0235, gap=0.010)
    SL.append(("SDR console (1/2) — devices, GPS & compass", "Tutorial · 8", s_sdr1))

    # 9 — SDR console (2): the DF panel
    def s_sdr2(ax, top):
        _t(ax, 0.055, top - 0.028, "The 'DF' bottom-panel tab — three columns", size=14, color=NAVY, weight="bold")
        # thin diagram row
        cols = [("LEFT  ≈  ½", "stacked spectrum viewer(s)", 0.055, 0.42),
                ("MIDDLE", "LoB compass", 0.49, 0.21),
                ("RIGHT", "DF options", 0.71, 0.235)]
        dy, dh = top - 0.07, 0.045
        for lbl, sub, x, w in cols:
            _round(ax, x, dy - dh, w, dh, NAVY)
            _t(ax, x + w / 2, dy - 0.014, lbl, size=8.5, color=TEAL, weight="bold", ha="center")
            _t(ax, x + w / 2, dy - 0.030, sub, size=8.5, color="white", ha="center")
        y = _bullets(ax, 0.055, dy - dh - 0.022, [
            ("Left — spectrum viewer(s).  ", "One viewer for a single-channel source; one per channel, vertically stacked, for a multi-channel array. Scroll-wheel zooms the frequency axis about the cursor; drag pans; click drops the DF tuner. The y-axis never moves — it's pinned to [noise floor − pad, peak + pad], so the noise floor and the strongest signal stay visible. Threshold / noise-floor / peak lines are drawn. A ▦ 'waterfall' toggle opens a scrolling spectrogram underneath each viewer — historical activity, bursts, hopping. (Synthetic until a SoapySDR / rtl-sdr / krakensdr-DAQ capture layer is wired — install SoapySDR and it goes live.)"),
            ("Middle — LoB compass.  ", "A true-north dial with a needle per current line of bearing; the freshest one labelled. In Relative / Clock mode it also draws the antenna-front reference and prints the latest LoB in all three reps — 'abs 117° · rel 12° · 4 o'clock'. Shows the active-LoB count, or 'single-channel — no LoBs'."),
            ("Right — DF options.  ", "DF tune frequency (type, or drop the tuner on a signal) · threshold (min power for a bin to count as active → shoot a LoB) · gain · AGC · demodulate & listen (NFM/AM/SSB built-in; DMR / dPMR / P25 P1+P2 / TETRA / NXDN / D-STAR / YSF / M17 / POCSAG / … dispatched to an installed decoder — op25 / dsd-fme / sdrtrunk / tetra-rx / multimon-ng — it tells you if one isn't installed) · per-spectrum centre / span · the LoB-accuracy estimate + GPS status."),
        ], LS_T, size=9.6, lh=0.022, gap=0.010)
        _para(ax, 0.055, y - 0.004,
              "More channels ⇒ tighter LoBs. LoBs that arrive are plotted on the map automatically from your GPS location, fused with any others at the same frequency, and pushed to ATAK as CoT.",
              0.945, LS_T, size=9, color=TEALD, lh=0.020)
    SL.append(("SDR console (2/2) — the DF bottom-panel tab", "Tutorial · 9", s_sdr2))

    # 9a — Algorithms tab (single-channel DF + multi-method fusion)
    def s_algos(ax, top):
        _t(ax, 0.055, top - 0.028,
           "When you only have one SDR — let motion do the array's job",
           size=14, color=NAVY, weight="bold")
        # Left column: prose + bullets
        y = _bullets(ax, 0.055, top - 0.060, [
            ("RSS log-distance ML.  ",
             "Spatially-sampled RSSI from a moving single antenna → joint maximum-likelihood emitter position, "
             "transmit power and path-loss exponent. Returns a covariance-derived error ellipse."),
            ("RSS-gradient bearing.  ",
             "Linear LS on closely-spaced RSS samples → spatial gradient direction = bearing to the emitter."),
            ("Doppler closest-point-of-approach.  ",
             "Hyperbolic S-curve fit on Doppler vs time as you pass a stationary emitter → CPA distance, CPA time, "
             "and along-track offset (left/right ambiguous; resolve with a second pass)."),
            ("FDOA multi-pose grid.  ",
             "Each (vx, vy, Δf) at a different heading projects the LOS onto the velocity; stack three or more → "
             "2-D position fix."),
            ("Kinematic synthetic-aperture DoA.  ",
             "Coherent IQ snapshots at known positions form a virtual array — beam-form (Bartlett / Capon / MUSIC) "
             "exactly like a physical one. Aperture span sets the resolution."),
            ("Phase-interferometry along track.  ",
             "Carrier-phase Δφ between snapshots over a known baseline → direct DoA readout (ambiguity resolved by "
             "a SAR prior)."),
            ("ML grid fusion + EKF.  ",
             "Universal back-stop: combine AoA from a DF head + RSS + Doppler + TDOA into one likelihood; the EKF "
             "version refines sequentially as more observations land. The Algorithms panel auto-selects the most "
             "specific feasible method for whatever observations you paste / load."),
        ], LS_T, x_right=0.48, size=9.4, lh=0.022, gap=0.008)
        # Right column: screenshot
        _screenshot(ax, "tab_algorithms", 0.50, 0.13, 0.445, top - 0.16,
                     caption="Algorithms tab — feasibility lights + heatmap + 'Send fix to map'",
                     fallback_text="Algorithms tab")
    SL.append(("Algorithms tab — single-channel DF + multi-method fusion",
                "Tutorial · 10", s_algos))

    # 9b — PTT auto-identify
    def s_ptt(ax, top):
        _t(ax, 0.055, top - 0.028,
           "Capture · classify · route to the right open-source decoder",
           size=14, color=NAVY, weight="bold")
        y = _bullets(ax, 0.055, top - 0.060, [
            ("How it works.  ",
             "Auto-detect captures ~500 ms of IQ at the tune frequency and runs three in-process tests: occupied "
             "bandwidth (FFT, 99 %), modulation family (envelope-constancy + k-means quantisation of the FM-disc + "
             "FM-disc bandwidth — digital ≥ 3 kHz vs analog ≤ 3 kHz), and symbol rate (autocorrelation of the "
             "rectified FM-discriminator derivative)."),
            ("What it identifies.  ",
             "DMR (Tier I/II/III) · dPMR · APCO P25 Phase 1 / Phase 2 · TETRA · NXDN 4800 / 9600 · D-STAR · YSF · "
             "M17 · EDACS ProVoice · POCSAG / FLEX paging · narrowband / wideband FM voice · AM. Returns ranked "
             "candidates with per-decoder availability so the UI can pick a fallback if your first-choice decoder "
             "isn't installed."),
            ("Decoder routing.  ",
             "DMR / dPMR / NXDN / D-STAR / YSF / ProVoice → dsd-fme.  P25 → op25.  TETRA → tetra-rx.  M17 → "
             "m17-demod.  POCSAG / FLEX → multimon-ng.  ACARS → acarsdec.  ADS-B → in-process Mode-S decoder. The "
             "decoder catalogue uses an alias table so dump1090-fa / dump1090-mutability both register correctly."),
            ("Installer covers the open ones.  ",
             "./install.sh source-builds dsd-fme + m17-cxx-demod + acarsdec into /usr/local by default; "
             "--with-op25 / --with-sdrtrunk / --with-tetra opts into the heavy ones (op25 pulls all of GNU Radio "
             "for 30–60 min)."),
        ], LS_T, x_right=0.48, size=9.6, lh=0.023, gap=0.010)
        _screenshot(ax, "tab_df", 0.50, 0.13, 0.445, top - 0.16,
                     caption="DF tab — Auto-detect verdict + alternative candidates",
                     fallback_text="DF tab — Auto-detect")
    SL.append(("Auto-detect PTT standards & route to the right decoder",
                "Tutorial · 11", s_ptt))

    # 9c — UAS / FPV video
    def s_video(ax, top):
        _t(ax, 0.055, top - 0.028,
           "FPV / NTSC / PAL — IQ in, viewable raster out — no external software",
           size=14, color=NAVY, weight="bold")
        y = _bullets(ax, 0.055, top - 0.060, [
            ("Auto-tune pipeline.  ",
             "Multi-detector search (FM polar / IQ-balanced FM / AM envelope, scored by sync cadence) → H-sync PLL "
             "with sub-sample line alignment → V-sync via equalising-pulse cadence → active samples-per-line "
             "recovery → field-pair deinterlace → per-line peak-hold IIR clamp with tunable τ → NTSC YIQ / PAL YUV "
             "chroma decode → RGB → frame averaging (EMA across N frames)."),
            ("Operator overrides.  ",
             "Force line rate, frame rate, pixel rate, active scanline duration, horizontal / vertical offsets, "
             "width, peak-hold τ, frame-avg N, deinterlace on/off, colour decode on/off, multi-detector on/off. "
             "Re-demodulate from the current capture without restarting the session."),
            ("Display controls (client-side, instant).  ",
             "Eleven colormaps: native colour · grayscale · amber CRT · green phosphor · blue · red · viridis · "
             "plasma · inferno · ironbow (thermal) · night-vision · ice-blue. Brightness, contrast, gamma sliders. "
             "Scanline FPS (poll cadence). Snapshot to PNG, record to WebM via MediaRecorder over the canvas."),
            ("Spectrum max-hold for hunting.  ",
             "Band scan with max-hold across sweeps so intermittent / hopping FPV downlinks 'draw themselves in' "
             "over time. Resettable + keyed so concurrent scans don't collide."),
        ], LS_T, x_right=0.48, size=9.6, lh=0.023, gap=0.010)
        _screenshot(ax, "tab_video", 0.50, 0.13, 0.445, top - 0.16,
                     caption="UAS Video panel — colormap + B/C/γ + snapshot/record",
                     fallback_text="UAS Video panel")
    SL.append(("UAS / FPV video decode — IQ to viewable raster",
                "Tutorial · 12", s_video))

    # 10 — distributed sensing
    def s_mesh(ax, top):
        _t(ax, 0.055, top - 0.028, "Two ways to fuse more than one sensor", size=14, color=NAVY, weight="bold")
        _bullets(ax, 0.055, top - 0.060, [
            ("Same box.  ", "Just register several SDRs under Devices — the solver groups LoBs by frequency across devices, so two/three antennas on one Ares produce a multi-sensor Cut/Fix automatically."),
            ("Over a MANET.  ", "In the SDR console's 'Distributed sensing — mesh peers' section, add peer Ares nodes' base URLs (e.g. http://node2.lan:8000). Each node opens a WebSocket to every peer's stream and ingests their LoBs / fixes / chat; because it's symmetric, the union of every node's bearings is fused on every node — losing a node loses a sensor, not the picture."),
            ("Trust & integrity.  ", "Set ARES_MESH_SECRET (or let Ares generate one in data/.mesh_secret the first time you add a peer — then copy it to the other nodes). Every inter-node LoB and chat message carries an HMAC-SHA256 over its content, so a node holding the secret rejects unsigned / tampered / origin-replayed peer data — a rogue node can't bias everyone's fixes. Loop-safe: dedup by (origin, id), hop-count TTL; messages propagate transitively, so even a partial mesh converges."),
            ("Where it shows.  ", "fused fixes (with the n-LoBs and the contributing nodes) on the maps; the mesh status in the SDR console and at GET /api/v1/sdr/mesh; peer add/remove in the audit log."),
        ], LS_T, x_right=0.66, size=10.0, lh=0.024, gap=0.012)
        # mesh sketch, right side
        import numpy as np
        cx, cy, R = 0.80, 0.45, 0.13
        ang0 = np.pi / 2
        pts = [(cx + R * np.cos(ang0 + k * 2 * np.pi / 3), cy + R * np.sin(ang0 + k * 2 * np.pi / 3)) for k in range(3)]
        for i in range(3):
            for j in range(i + 1, 3):
                ax.plot([pts[i][0], pts[j][0]], [pts[i][1], pts[j][1]], color=TEAL, lw=1.6, zorder=3, transform=ax.transAxes)
        for i, (x, y) in enumerate(pts):
            _round(ax, x - 0.05, y - 0.018, 0.10, 0.040, NAVY)
            _logo_mark(ax, x - 0.030, y, 0.011, ring="white")
            _t(ax, x + 0.012, y - 0.002, f"Node {chr(65+i)}", size=8, color="white", weight="bold", ha="center")
        _t(ax, cx, cy + R + 0.062, "every node fuses everyone's bearings", size=8, color=NAVY, weight="bold", ha="center")
        _t(ax, cx, cy + R + 0.045, "(signed · deduplicated · hop-bounded)", size=7.5, color=MUTE, ha="center")
        _t(ax, cx, cy - R - 0.045, "lose a node → lose a sensor,", size=7.8, color=MUTE, ha="center")
        _t(ax, cx, cy - R - 0.062, "not the picture", size=7.8, color=MUTE, ha="center")
    SL.append(("Distributed sensing over a MANET", "Tutorial · 10", s_mesh))

    # 11 — chat
    def s_chat(ax, top):
        y = _bullets(ax, 0.055, top - 0.022, [
            ("Open it.  ", "The 'Chat' bottom-panel tab. Pick a room (channel — 'All' is the default; type a name to make a new one), set your callsign (remembered in the browser)."),
            ("One conversation, three carriers.  ", "A message broadcasts on this node's WebSocket, the peer mesh re-ingests it on every Ares node (HMAC-signed, deduplicated, hop-count-bounded), and it goes out as a CoT GeoChat — so ATAK / WinTAK clients on the bus see and can answer it. Inbound GeoChat from ATAK is routed back in by the CoT listener. It's the same chat across Ares nodes and ATAK."),
            ("Geo-tagged messages.  ", "Tick the location toggle to attach your browser position; a received message with coordinates shows a clickable position (drops the RX marker there)."),
            ("In the message list.  ", "sender callsign · origin node (or '(ATAK)' / 'hop N') · time · text. Your own messages are right-aligned; ATAK ones are tinted."),
        ], LS_T, size=10.5, lh=0.025, gap=0.014)
        _round(ax, 0.055, y - 0.10, 0.89, 0.085, PANEL, ec=LINE, lw=1.2)
        _para(ax, 0.072, y - 0.034,
              "Under the hood:  app/core/chat.py  ·  REST:  GET /api/v1/chat/messages | /chat/rooms,  POST /api/v1/chat/send  ·  "
              "mesh:  the same WebSocket the LoBs travel on  ·  CoT type b-t-f (GeoChat), in and out.",
              0.935, LS_T, size=9, color=INK, lh=0.020)
    SL.append(("Group chat — meshed, bridged to ATAK GeoChat", "Tutorial · 11", s_chat))

    # 12 — ATAK / Server console
    def s_atak(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("Open it.  ", "The ATAK / Server button in the header. It's the operator's offline-ops + TAK-integration panel."),
            ("Server status.  ", "Version, online/offline, GPU, disk free, auth state. An 'ATAK integration: ON/OFF' toggle (the master switch for data packs / templates / KMZ export / CoT push). A loud security warning here if auth is off and the server isn't bound to loopback."),
            ("Cursor-on-Target push targets.  ", "Where LoBs / fixes / GeoChat are sent — one per line: udp://239.2.3.1:6969 (the conventional ATAK multicast group), tcp://taksrv.lan:8087, or tls://taksrv.lan:8089 for mutual-TLS to a TAK Server (set ARES_COT_TLS_CA / CERT / KEY)."),
            ("Offline data packs.  ", "Per layer — terrain (SRTM30) · osm · imagery (ESRI World Imagery) · buildings (OSM/Overpass) · clutter (ESA WorldCover). A 'download region pack' form (bbox + max zoom), a job poller, a verify button (integrity / version), and delete. The 3-D globe renders installed packs — extruded buildings, offline imagery, real relief."),
            ("Radio templates & KMZ.  ", "The radio templates the ATAK plugin would see (CRUD). On the map: import a KMZ/KML; export the current coverage as a KMZ GroundOverlay (an ATAK image-overlay / WinTAK / Google Earth)."),
        ], LS_T, size=10.5, lh=0.025, gap=0.013)
    SL.append(("The ATAK / Server console — packs, CoT, KMZ", "Tutorial · 12", s_atak))

    # 13 — HF & satellites
    def s_hfsat(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("HF circuit prediction.  ", "GET /api/v1/hf/muf?lat1=&lon1=&lat2=&lon2=&freq_mhz= — an ITU-R-P.533-style model: multi-hop F2 geometry (hops, take-off angle, incidence at the F2 layer and the 110 km D-region), a parameterised foF2 (solar activity / zenith angle / geomagnetic latitude), the path MUF / FOT (= 0.85·MUF) / HPF / LUF via the secant law, ITU-R P.533 D-region absorption summed over hops, basic loss, received SNR vs an ITU-R P.372 noise floor, and a circuit-reliability percentage. If a voacapl / ITURHFPROP binary is on the PATH it's used instead. (foF2 is parameterised, not the CCIR/URSI coefficient maps — labelled honestly.)"),
            ("Satellite visibility.  ", "POST /api/v1/simulate/satellite_visibility {constellation, ground_lat, ground_lon, min_elevation_deg} — pulls TLEs from CelesTrak and propagates each with SGP4 (the canonical `sgp4` package if installed, else a vendored faithful near-earth SGP4, WGS-72): sub-points, footprint radii, and true topocentric az / el / slant-range to your ground station. Deep-space objects (period ≥ 225 min) are flagged with a 'pip install sgp4' hint for SDP4-grade accuracy."),
            ("Space weather.  ", "GET /api/v1/space_weather — current NOAA SWPC indices (Kp / SFI, R/G storm classes), folded into the HF model and shown in the 'Space Wx' tab; degrades gracefully offline to last-known values."),
        ], LS_T, size=10.0, lh=0.024, gap=0.016)
    SL.append(("HF circuits & satellites", "Tutorial · 13", s_hfsat))

    # 14 — security & learn more
    def s_more(ax, top):
        _bullets(ax, 0.055, top - 0.022, [
            ("Auth.  ", "ARES_AUTH = true | false | auto (the default). 'auto' ⇒ auth ON unless bound to a loopback address — so a networked deployment is authenticated out of the box. POST /api/v1/auth/login → a bearer token (12 h). Optional LDAP/AD backend (ARES_AUTH_BACKEND=ldap, needs the ldap3 package)."),
            ("Mesh integrity & rate limiting.  ", "ARES_MESH_SECRET signs every inter-node LoB / chat (HMAC-SHA256 over the content) and gates the WebSocket. A per-IP token-bucket rate limiter on /api/v1/* (tighter for /simulate & /packs/download), 429 on excess. An append-only audit log at data/audit.log (logins, device & peer changes, calibration, CoT-target & ATAK-toggle changes)."),
            ("CoT over mutual-TLS.  ", "tls:// targets with ARES_COT_TLS_CA / CERT / KEY — what a real TAK Server input expects. A CoT receive listener brings inbound GeoChat back into the chat hub."),
            ("Verify it works.  ", "cd backend && python -m tests.test_validation — a 53-check harness over ITM (incl. reference pins), the ML DF, TDOA, SGP4, HF, the array interferometry, and the security pass. cd frontend && node --test tests/ — 8 pure-maths checks. CI runs both on every push."),
            ("Learn more.  ", "http://localhost:8000/docs (interactive API) · docs/Ares.md (module-by-module: what's rigorous vs. still approximate) · docs/DEPLOYMENT.md (Jetson / laptop / Pi / cloud, air-gapped, CoT, GPS, the smoke test) · docs/BUILD_PLAN.md (the workstreams) · the source: backend/app/ (Python), frontend/src/ (React)."),
            ("New to programming?  ", "Ask the project chat for the learning roadmap — which languages and concepts (Python · JavaScript/React · web/HTTP/JSON · the RF / DF / SDR / GIS / TAK domain · the maths) to study, in what order, and which track to pick."),
        ], LS_T, size=9.8, lh=0.0235, gap=0.010)
    SL.append(("Security & where to learn more", "Tutorial · 14", s_more))

    total = 1 + len(SL)
    with PdfPages(path) as pdf:
        # slide 1 — title (custom layout)
        fig, ax = _fig(LS_T)
        ax.add_patch(Rectangle((0, 0), 1, 1, color=NAVY, zorder=1))
        ax.add_patch(Rectangle((0, 0.615), 1, 0.004, color=TEAL, zorder=2))
        _logo_mark(ax, 0.83, 0.74, 0.105)
        _t(ax, 0.07, 0.82, "ARES ATAK", size=44, color="white", weight="bold")
        _t(ax, 0.072, 0.745, "A hands-on tour of the most important features", size=16, color="#bcd0e4")
        _bullets(ax, 0.08, 0.55, [
            "Running it · the map · coverage & point-to-point links",
            "Geolocation — line-of-bearing, ML fix, error ellipse, advanced fusion",
            "The SDR console + DF panel — spectrum, waterfall, live AoA, calibration",
            "Algorithms tab — single-channel DF (RSS / Doppler-CPA / synthetic aperture / EKF)",
            "Auto-detect PTT (DMR/P25/TETRA/NXDN/D-STAR/YSF/M17) → right decoder",
            "UAS / FPV video decode · MANET sensing · chat · ATAK / TAK · HF / SAT",
        ], LS_T, x_right=0.92, size=11.0, lh=0.038, gap=0.006, color="#dbe6f0", bcolor=TEAL)
        _t(ax, 0.07, 0.18, "Open  http://localhost:8000  (or :3000) alongside this deck — the interactive API is at  /docs.", size=10.5, color="#9fb6cc")
        _t(ax, 0.07, 0.115, VERSION + "   ·   regenerate with  docs/build_pdfs.py", size=9, color="#7f96ac")
        _footer(ax, "Tutorial", 1, total); pdf.savefig(fig); plt.close(fig)
        for i, (title, kicker, body_fn) in enumerate(SL, start=2):
            _slide(pdf, i, total, title, kicker, body_fn)
    print("wrote", path)


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    build_flyer(os.path.join(here, "Ares_Flyer.pdf"))
    build_tutorial(os.path.join(here, "Ares_Tutorial.pdf"))
