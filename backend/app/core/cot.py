# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
CoT (Cursor-on-Target) publisher (Workstream D).

Translates Ares LoB / fix events into CoT XML and pushes them to whatever
ATAK / WinTAK / TAK-Server targets the operator has configured. Targets come
from the ``ARES_COT_TARGETS`` env var (or :func:`set_targets` at runtime),
comma-separated, each:

  - ``udp://<host>:<port>``    UDP unicast
  - ``mcast://239.2.3.1:6969``  UDP multicast (the conventional ATAK group)
  - ``tcp://<host>:<port>``     TCP unicast (e.g. a TAK Server :8087 plain port)

When no targets are set, ``publish_*`` is a no-op (so dev / unit tests don't
spray packets). Errors are logged at DEBUG and dropped — CoT delivery is
fire-and-forget by design.

Encodes two CoT event types:
  - Each LoB → a ``u-d-r`` drawing route (TX device → bearing endpoint),
    colour-coded by frequency, with bearing/RSSI/freq in the callsign + remarks.
  - Each fix → an ``a-u-G-U-C-I`` (intelligence / unknown / ground) point with
    ``ce=<CEP_m>`` so ATAK draws an uncertainty circle, callsign
    ``Ares Emitter <freq>MHz``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Optional
from xml.etree import ElementTree as ET

from app.core import geolocation

log = logging.getLogger(__name__)

_TARGETS: list[tuple[str, str, int]] = []   # (kind, host, port) — kind = udp|mcast|tcp|tls
_TCP_CONNS: dict[tuple[str, int], tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
_TCP_LOCK = asyncio.Lock()
_TLS_CTX = None   # cached ssl.SSLContext for `tls://` targets (mutual-TLS to a TAK Server)


def _tls_context():
    """Build (and cache) the SSL context for `tls://` CoT targets. Honours:
      ARES_COT_TLS_CA       — CA bundle / TAK Server truststore (PEM)
      ARES_COT_TLS_CERT     — client certificate (PEM) for mutual-TLS
      ARES_COT_TLS_KEY      — client private key (PEM); defaults to ARES_COT_TLS_CERT
      ARES_COT_TLS_INSECURE — "true" ⇒ don't verify the server cert (lab only)
    """
    global _TLS_CTX
    if _TLS_CTX is not None:
        return _TLS_CTX
    import ssl
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ca = os.getenv("ARES_COT_TLS_CA", "").strip()
    if ca:
        try:
            ctx.load_verify_locations(ca)
        except Exception as e:
            log.warning("ARES_COT_TLS_CA load failed: %s", e)
    if os.getenv("ARES_COT_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning("CoT TLS: certificate verification DISABLED (ARES_COT_TLS_INSECURE) — lab use only")
    cert = os.getenv("ARES_COT_TLS_CERT", "").strip()
    if cert:
        try:
            ctx.load_cert_chain(cert, os.getenv("ARES_COT_TLS_KEY", "").strip() or None)
        except Exception as e:
            log.warning("ARES_COT_TLS_CERT load failed: %s", e)
    _TLS_CTX = ctx
    return ctx


def _parse_target(spec: str) -> Optional[tuple[str, str, int]]:
    spec = spec.strip()
    if not spec:
        return None
    for prefix in ("udp://", "mcast://", "tcp://", "tls://", "ssl://"):
        if spec.startswith(prefix):
            kind = prefix[:-3]  # "udp" | "mcast" | "tcp" | "tls" | "ssl"
            if kind == "ssl":
                kind = "tls"
            rest = spec[len(prefix):]
            if ":" not in rest:
                return None
            host, _, port_s = rest.partition(":")
            try:
                return (kind, host, int(port_s))
            except ValueError:
                return None
    return None


def set_targets(specs: list[str]) -> list[str]:
    global _TARGETS
    parsed = [t for t in (_parse_target(s) for s in specs) if t]
    _TARGETS = parsed
    # drop pooled TCP/TLS conns; they'll be recreated on next send
    for _key, (_, w) in list(_TCP_CONNS.items()):
        try: w.close()
        except Exception: pass
    _TCP_CONNS.clear()
    out = [f"{k}://{h}:{p}" for k, h, p in parsed]
    try:
        from app.core.security import audit
        audit("cot.targets", targets=out)
    except Exception:
        pass
    return out


def list_targets() -> list[str]:
    return [f"{k}://{h}:{p}" for k, h, p in _TARGETS]


def _bootstrap_from_env() -> None:
    env = os.getenv("ARES_COT_TARGETS", "").strip()
    if env:
        set_targets([s for s in env.split(",") if s.strip()])


_bootstrap_from_env()


# ─────────────────────────────────────────────────────────────────────────────
# XML builders
# ─────────────────────────────────────────────────────────────────────────────
def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _event(uid: str, type_: str, lat: float, lon: float, *, ce_m: float = 9999999.0,
           stale_s: float = 300.0, how: str = "m-g") -> ET.Element:
    now = time.time()
    ev = ET.Element("event", {
        "version": "2.0", "uid": uid, "type": type_, "how": how,
        "time": _iso(now), "start": _iso(now), "stale": _iso(now + stale_s),
    })
    ET.SubElement(ev, "point", {
        "lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "hae": "9999999.0",
        "ce": f"{ce_m:.1f}", "le": "9999999.0",
    })
    return ev


def lob_cot(ev) -> bytes:
    """Build a ``u-d-r`` (drawn-route) CoT for one LoB: device → bearing endpoint."""
    length_m = max(500.0, ev.estimated_distance_m or geolocation.estimate_distance_m(
        ev.rssi_dbm, ev.frequency_hz, 30.0, ev.environment, 0.0))
    end_lat, end_lon = geolocation.destination_point(ev.lat, ev.lon, ev.azimuth_deg, length_m)
    uid = f"ares-lob-{ev.id}"
    root = _event(uid, "u-d-r", ev.lat, ev.lon, how="h-g-i-g-o", stale_s=120.0)
    detail = ET.SubElement(root, "detail")
    # link chain (closed=false → polyline)
    ET.SubElement(detail, "link", {
        "uid": f"{uid}-a", "point": f"{ev.lat:.7f},{ev.lon:.7f},0", "type": "b-m-p-w-GOTO", "relation": "c",
    })
    ET.SubElement(detail, "link", {
        "uid": f"{uid}-b", "point": f"{end_lat:.7f},{end_lon:.7f},0", "type": "b-m-p-w-GOTO", "relation": "c",
    })
    shape = ET.SubElement(detail, "shape")
    ET.SubElement(shape, "polyline", {"closed": "false", "fillColor": "0", "color": "-256"})  # yellow
    ET.SubElement(detail, "contact", {
        "callsign": f"LoB {ev.frequency_hz/1e6:.3f}MHz {ev.azimuth_deg:.1f}°",
    })
    ET.SubElement(detail, "remarks").text = (
        f"Ares DF · device={ev.device_id} type={ev.device_type} "
        f"RSSI={ev.rssi_dbm:.1f}dBm conf={ev.confidence_pct:.0f}% "
        f"range≈{length_m/1000:.1f}km"
    )
    ET.SubElement(detail, "takv", {"platform": "Ares", "device": "ares-server", "version": "1.1"})
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root)


def fix_cot(group: dict) -> Optional[bytes]:
    centroid = group.get("centroid") or {}
    if "lat" not in centroid or "lon" not in centroid:
        return None
    cep = group.get("cep") or {}
    cep_m = float(cep.get("semiMajorM") or cep.get("semi_major_m") or 250.0)
    freq = float(group.get("frequency_hz") or 0)
    dev_id = group.get("device_id") or ""
    uid = f"ares-fix-{int(freq)}-{dev_id or 'x'}"
    root = _event(uid, "a-u-G-U-C-I", float(centroid["lat"]), float(centroid["lon"]),
                  ce_m=cep_m, stale_s=300.0)
    detail = ET.SubElement(root, "detail")
    ET.SubElement(detail, "contact", {
        "callsign": f"Ares Emitter {freq/1e6:.3f}MHz" + (f" · {dev_id}" if dev_id else ""),
    })
    ET.SubElement(detail, "remarks").text = (
        f"Ares DF {group.get('kind', 'fix').upper()} from "
        f"{group.get('n_lobs', 0)} LoB(s); CEP {cep_m:.0f} m"
    )
    ET.SubElement(detail, "takv", {"platform": "Ares", "device": "ares-server", "version": "1.1"})
    # group / colour — render emitters as red ground-unknown markers
    ET.SubElement(detail, "color", {"argb": "-65536"})
    ET.SubElement(detail, "__group", {"name": "Red", "role": "Team Member"})
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root)


# ─────────────────────────────────────────────────────────────────────────────
# Transports
# ─────────────────────────────────────────────────────────────────────────────
async def _send_udp(kind: str, host: str, port: int, payload: bytes) -> None:
    loop = asyncio.get_event_loop()
    def _do():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            if kind == "mcast":
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.sendto(payload, (host, port))
        finally:
            s.close()
    await loop.run_in_executor(None, _do)


async def _send_tcp(host: str, port: int, payload: bytes, tls: bool = False) -> None:
    key = ("tls" if tls else "tcp", host, port)
    async with _TCP_LOCK:
        conn = _TCP_CONNS.get(key)
        if conn is not None:
            _r, w = conn
            if w.is_closing():
                conn = None
        if conn is None:
            ssl_ctx = _tls_context() if tls else None
            r, w = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx,
                                        server_hostname=(host if tls else None)), timeout=5.0)
            _TCP_CONNS[key] = (r, w)
        else:
            r, w = conn
    try:
        w.write(payload + b"\n")
        await w.drain()
    except Exception:
        async with _TCP_LOCK:
            _TCP_CONNS.pop(key, None)
        try: w.close()
        except Exception: pass
        raise


async def _send_all(payload: bytes) -> None:
    if not _TARGETS:
        return
    async def _one(kind, host, port):
        try:
            if kind in ("udp", "mcast"):
                await _send_udp(kind, host, port, payload)
            else:
                await _send_tcp(host, port, payload, tls=(kind == "tls"))
        except Exception as e:
            log.debug("CoT send failed (%s://%s:%s): %s", kind, host, port, e)
    await asyncio.gather(*[_one(*t) for t in _TARGETS])


def geochat_cot(msg: dict) -> bytes:
    """ATAK **GeoChat** (``b-t-f``) for a chat message — so ATAK/WinTAK clients see it."""
    room = msg.get("room", "All")
    sender = msg.get("from_label") or msg.get("callsign") or msg.get("from_node") or "Ares"
    sender_uid = f"ares-{msg.get('from_node', 'node')}"
    mid = msg.get("id") or "m"
    lat = msg.get("lat") if isinstance(msg.get("lat"), (int, float)) else 0.0
    lon = msg.get("lon") if isinstance(msg.get("lon"), (int, float)) else 0.0
    has_geo = isinstance(msg.get("lat"), (int, float)) and isinstance(msg.get("lon"), (int, float))
    uid = f"GeoChat.{sender_uid}.{room}.{mid}"
    root = _event(uid, "b-t-f", float(lat), float(lon), how="h-g-i-g-o", stale_s=180.0,
                  ce_m=(99.0 if has_geo else 9999999.0))
    d = ET.SubElement(root, "detail")
    chat = ET.SubElement(d, "__chat", {"parent": "RootContactGroup", "groupOwner": "false",
                                       "chatroom": room, "id": room, "senderCallsign": str(sender)})
    ET.SubElement(chat, "chatgrp", {"uid0": sender_uid, "uid1": room, "id": room})
    ET.SubElement(d, "link", {"uid": sender_uid, "type": "a-f-G-U-C", "relation": "p-p"})
    rmk = ET.SubElement(d, "remarks", {"source": f"BAO.F.Ares.{sender_uid}", "to": room, "time": _iso(time.time())})
    rmk.text = str(msg.get("text", ""))
    marti = ET.SubElement(d, "marti")
    ET.SubElement(marti, "dest", {"callsign": room})
    ET.SubElement(d, "takv", {"platform": "Ares", "device": "ares-server", "version": "2.0"})
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root)


async def publish_chat(msg: dict) -> None:
    if not _TARGETS:
        return
    try:
        await _send_all(geochat_cot(msg))
    except Exception:
        log.debug("publish_chat failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# CoT receive — listen on the multicast / UDP targets and route incoming
# **GeoChat** (b-t-f) back into the chat hub, so it's one conversation across
# Ares nodes *and* ATAK clients on the same bus. (Parsing of inbound LoB/fix CoT
# from non-Ares EW kit is a follow-up; we only re-ingest GeoChat here.)
# ─────────────────────────────────────────────────────────────────────────────
_LISTENERS: list = []


def _parse_geochat(xml_bytes: bytes) -> Optional[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return None
    if not str(root.get("type", "")).startswith("b-t-f"):
        return None
    d = root.find("detail")
    if d is None:
        return None
    chat = d.find("__chat")
    rmk = d.find("remarks")
    text = (rmk.text if rmk is not None else "") or ""
    if not text.strip():
        return None
    room = (chat.get("chatroom") or chat.get("id") or "All") if chat is not None else "All"
    sender = (chat.get("senderCallsign") if chat is not None else None) or ""
    link = d.find("link")
    sender_uid = (link.get("uid") if link is not None else None) or sender or "unknown"
    pt = root.find("point")
    lat = lon = None
    try:
        if pt is not None and float(pt.get("ce", "9e9")) < 1e6:
            lat = float(pt.get("lat")); lon = float(pt.get("lon"))
    except Exception:
        lat = lon = None
    uid = str(root.get("uid", ""))
    # our own GeoChat carries uid "GeoChat.ares-<node>.<room>.<msgid>"
    is_ares = "ares-" in sender_uid or sender_uid.startswith("ares")
    return {"text": text, "callsign": sender, "room": room, "lat": lat, "lon": lon,
            "msg_id": uid.split(".")[-1] if uid else None,
            "sender_node": sender_uid if is_ares else f"atak:{sender or sender_uid}"}


class _CotRxProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        m = _parse_geochat(data)
        if not m:
            return
        try:
            from app.core.chat import chat_hub
            chat_hub.ingest_cot(**m)
        except Exception:
            log.debug("ingest_cot failed", exc_info=True)


async def start_cot_listener() -> None:
    """Bind a UDP receiver on each distinct port among the mcast:// / udp:// CoT
    targets (joining the multicast group for mcast ones) and route inbound GeoChat
    back into the chat hub. Idempotent; bind failures are logged and skipped."""
    await stop_cot_listener()
    ports: dict[int, list[str]] = {}      # port → multicast groups to join (empty ⇒ plain udp)
    for kind, host, port in _TARGETS:
        if kind in ("mcast", "udp"):
            ports.setdefault(port, [])
            if kind == "mcast":
                ports[port].append(host)
    if not ports:
        return
    loop = asyncio.get_event_loop()
    for port, groups in ports.items():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
            sock.bind(("", port))
            for g in groups:
                try:
                    mreq = socket.inet_aton(g) + socket.inet_aton("0.0.0.0")
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError as e:
                    log.warning("CoT listener: could not join multicast %s: %s", g, e)
            sock.setblocking(False)
            transport, _ = await loop.create_datagram_endpoint(_CotRxProtocol, sock=sock)
            _LISTENERS.append(transport)
            log.info("CoT listener bound on UDP :%d%s", port, (f" (mcast {','.join(groups)})" if groups else ""))
        except OSError as e:
            log.warning("CoT listener: bind on :%d failed: %s", port, e)


async def stop_cot_listener() -> None:
    for t in list(_LISTENERS):
        try:
            t.close()
        except Exception:
            pass
    _LISTENERS.clear()


async def publish_lob(ev) -> None:
    if not _TARGETS:
        return
    try:
        await _send_all(lob_cot(ev))
    except Exception:
        log.debug("publish_lob failed", exc_info=True)


async def publish_fix(group: dict) -> None:
    if not _TARGETS:
        return
    payload = fix_cot(group)
    if payload is None:
        return
    try:
        await _send_all(payload)
    except Exception:
        log.debug("publish_fix failed", exc_info=True)
