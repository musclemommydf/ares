"""
Over-the-air Remote ID capture.

Extracts ASTM F3411 / OpenDroneID broadcasts straight from the air and feeds the
message bytes to ``remote_id.parse_f3411`` / ``parse_dji_droneid``:

  * **BLE** via ``bleak`` (BlueZ on Linux, no monitor mode needed) — OpenDroneID
    rides in the advertisement Service Data for 16-bit UUID 0xFFFA: one app-code
    byte (0x0D), a message counter, then the F3411 message / message-pack.
  * **WiFi** via ``scapy`` on a monitor-mode interface — OpenDroneID rides in a
    Beacon vendor-specific element (OUI FA-0B-BC, type 0x0D); DJI DroneID rides
    in a DJI vendor element (best-effort → ``parse_dji_droneid``).

Both transports run in a background thread and push parsed beacons to a callback
and a rolling store. When neither ``bleak`` nor ``scapy`` is importable the
sniffer reports ``active=[]`` and the caller falls back to the synthetic beacon.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger(__name__)

# OpenDroneID well-known identifiers (ASTM F3411 / ASD-STAN prEN 4709-002).
_ODID_BT_UUID16 = "0000fffa"          # 16-bit Service Data UUID 0xFFFA, as a 128-bit prefix
_ODID_AD_APP_CODE = 0x0D              # first service-data byte for OpenDroneID
_ODID_WIFI_OUI = b"\xfa\x0b\xbc"      # ASTM OUI for the WiFi vendor element
_ODID_WIFI_OUI_TYPE = 0x0D
_DJI_OUI = b"\x60\x60\x1f"            # DJI vendor element seen on DroneID WiFi beacons


def bleak_available() -> bool:
    try:
        import bleak  # noqa: F401
        return True
    except Exception:
        return False


def scapy_available() -> bool:
    try:
        import scapy  # noqa: F401
        return True
    except Exception:
        return False


class RemoteIdSniffer:
    """Threaded BLE + WiFi OpenDroneID/DroneID capture.

    transport: 'auto' (every importable transport) | 'ble' | 'wifi'.
    on_beacon(parsed_dict): called for each decoded beacon (parsed_dict carries a
    ``summary`` plus ``_rid_src`` / ``_rid_transport`` / ``_rid_rssi``).
    """

    def __init__(self, sid: str, *, transport: str = "auto",
                 wifi_iface: str = "wlan0mon", ble_adapter: str = "hci0",
                 on_beacon: Optional[Callable[[dict], None]] = None):
        self.sid = sid
        self.transport = (transport or "auto").lower()
        self.wifi_iface = wifi_iface
        self.ble_adapter = ble_adapter
        self.on_beacon = on_beacon
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active: list[str] = []
        self._beacons: dict[str, dict] = {}      # keyed by serial (or src addr)
        self._events: deque = deque(maxlen=512)
        self._count = 0
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> list[str]:
        want_ble = self.transport in ("auto", "ble") and bleak_available()
        want_wifi = self.transport in ("auto", "wifi") and scapy_available()
        if want_ble:
            t = threading.Thread(target=self._run_ble, name=f"rid-ble-{self.sid}", daemon=True)
            t.start(); self._threads.append(t); self._active.append("ble")
        if want_wifi:
            t = threading.Thread(target=self._run_wifi, name=f"rid-wifi-{self.sid}", daemon=True)
            t.start(); self._threads.append(t); self._active.append("wifi")
        if self._active:
            log.info("rid sniffer %s started: %s", self.sid, "+".join(self._active))
        return list(self._active)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)

    @property
    def active(self) -> list[str]:
        return list(self._active)

    def latest(self, max_age_s: float = 30.0) -> list[dict]:
        now = time.time()
        with self._lock:
            return [b for b in self._beacons.values() if now - b.get("_rid_ts", 0) <= max_age_s]

    def stats(self) -> dict:
        return {"active_transports": self.active, "beacon_count": self._count,
                "unique_tracks": len(self._beacons)}

    # ── decode hand-off ──────────────────────────────────────────────────────
    def _emit(self, msg_bytes: bytes, *, transport: str, src: str,
              rssi: Optional[int], kind: str = "f3411") -> None:
        from . import remote_id
        try:
            if kind == "dji":
                parsed = remote_id.parse_dji_droneid(msg_bytes)
            else:
                parsed = remote_id.parse_f3411(msg_bytes)
        except Exception as e:  # pragma: no cover - malformed frame
            log.debug("rid parse failed (%s/%s): %s", transport, kind, e)
            return
        if not parsed or parsed.get("error"):
            return
        summary = parsed.get("summary") or {}
        key = summary.get("serial") or f"{transport}:{src}"
        parsed["_rid_src"] = src
        parsed["_rid_transport"] = transport
        parsed["_rid_rssi"] = rssi
        parsed["_rid_ts"] = time.time()
        parsed["_packet_hex"] = bytes(msg_bytes).hex()
        with self._lock:
            self._beacons[key] = parsed
            self._events.append(parsed)
            self._count += 1
        if self.on_beacon:
            try:
                self.on_beacon(parsed)
            except Exception:  # pragma: no cover
                log.debug("rid on_beacon callback raised", exc_info=True)

    # ── BLE (bleak) ──────────────────────────────────────────────────────────
    def _run_ble(self) -> None:
        import asyncio
        try:
            from bleak import BleakScanner
        except Exception as e:  # pragma: no cover
            log.warning("rid BLE: bleak import failed: %s", e)
            return

        def _detection(device, adv):
            sd = getattr(adv, "service_data", None) or {}
            for uuid, val in sd.items():
                if not str(uuid).lower().startswith(_ODID_BT_UUID16):
                    continue
                b = bytes(val)
                if len(b) < 3 or b[0] != _ODID_AD_APP_CODE:
                    continue
                # [app_code][msg_counter][F3411 message | message-pack]
                self._emit(b[2:], transport="ble", src=str(getattr(device, "address", "?")),
                           rssi=getattr(adv, "rssi", None))

        async def _scan():
            scanner = BleakScanner(detection_callback=_detection, adapter=self.ble_adapter)
            try:
                await scanner.start()
                while not self._stop.is_set():
                    await asyncio.sleep(0.25)
            finally:
                try:
                    await scanner.stop()
                except Exception:
                    pass

        try:
            asyncio.run(_scan())
        except Exception as e:  # pragma: no cover - adapter dependent
            log.warning("rid BLE scan ended: %s", e)

    # ── WiFi (scapy monitor mode) ────────────────────────────────────────────
    def _run_wifi(self) -> None:
        try:
            from scapy.all import sniff, Dot11Elt
        except Exception as e:  # pragma: no cover
            log.warning("rid WiFi: scapy import failed: %s", e)
            return

        def _handle(pkt):
            if not pkt.haslayer(Dot11Elt):
                return
            rssi = None
            try:
                rssi = int(getattr(pkt, "dBm_AntSignal", None)) if hasattr(pkt, "dBm_AntSignal") else None
            except Exception:
                rssi = None
            src = pkt.addr2 or "?"
            el = pkt.getlayer(Dot11Elt)
            while el is not None:
                if el.ID == 221:                       # vendor-specific element
                    info = bytes(el.info)
                    if info[:3] == _ODID_WIFI_OUI and len(info) > 5 and info[3] == _ODID_WIFI_OUI_TYPE:
                        # [OUI(3)][type=0x0D][msg_counter][F3411 message-pack]
                        self._emit(info[5:], transport="wifi", src=src, rssi=rssi)
                    elif info[:3] == _DJI_OUI and len(info) > 4:
                        self._emit(info[3:], transport="wifi", src=src, rssi=rssi, kind="dji")
                el = el.payload.getlayer(Dot11Elt) if el.payload else None

        try:
            sniff(iface=self.wifi_iface, prn=_handle, store=False,
                  stop_filter=lambda _p: self._stop.is_set(), timeout=None)
        except Exception as e:  # pragma: no cover - iface/permission dependent
            log.warning("rid WiFi sniff on %s ended: %s", self.wifi_iface, e)
