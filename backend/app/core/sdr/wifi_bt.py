"""
sdr/wifi_bt.py — passive WiFi + Bluetooth Low Energy monitors.

Both implementations spawn the standard Linux tooling (hcxdumptool /
airodump-ng / btmon) and parse their output into structured events that
hand straight into the target tracker. MAC randomisation is documented in
each monitor's status so the UI can warn the operator that durable tracking
is constrained to associated / advertising-with-fixed-address devices.

These are CellularSession subclasses so they live alongside cellular
sessions in the same `/api/v1/cellular/sessions` listing — the operator
sees one place to manage every passive observation source.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

from .cellular.session import CellularSession

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# WiFi
# ─────────────────────────────────────────────────────────────────────────────
class WifiMonitor(CellularSession):
    KIND = "wifi"

    # Lines we recognise in hcxdumptool / airodump-ng csv stdout:
    #   - hcxpcapngtool style: AP <ssid> <bssid> <channel> <rssi>
    #   - airodump-ng csv:    BSSID,Time,channel,RSSI,SSID,...
    #   - tcpdump probe-req:   STA <sta_mac> probed for <ssid> rssi <rssi>
    _RE_AP   = re.compile(r"(?:^|\s)AP\s+(?P<ssid>\S+)\s+(?P<bssid>[0-9a-f:]{17})\s+ch\s*(?P<ch>\d+)\s+rssi\s*(?P<rssi>-?\d+)", re.IGNORECASE)
    _RE_STA  = re.compile(r"(?:^|\s)STA\s+(?P<mac>[0-9a-f:]{17})\s+(?:probed|assoc)\s+(?P<ssid>\S*).*?rssi\s*(?P<rssi>-?\d+)", re.IGNORECASE)

    def __init__(self, sid: str, interface: str = "wlan0mon", channel: Optional[int] = None,
                  observer: Optional[dict] = None):
        super().__init__(sid=sid, device={"id": interface, "metadata": observer or {}})
        self.interface = interface
        self.channel = channel
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._observer = (observer or {})
        self._binary = self._pick_binary()
        self._mode = self._binary["name"] if self._binary else None

    @staticmethod
    def _pick_binary():
        for name, args in (
            ("hcxdumptool", ["--ieee80211w=1", "--rds=1"]),
            ("airodump-ng", ["-w", "/tmp/ares-wifi-mon", "--output-format", "csv"]),
        ):
            p = shutil.which(name)
            if p:
                return {"name": name, "path": p, "args": args}
        return None

    def _start_impl(self) -> None:
        if not self._binary:
            raise RuntimeError(
                "no WiFi capture tool installed. Install one of: hcxdumptool (preferred), "
                "aircrack-ng (provides airodump-ng). See ./install.sh --with-wifi-bt.")
        if not _interface_in_monitor_mode(self.interface):
            raise RuntimeError(
                f"interface {self.interface} is not in monitor mode. "
                f"Enable it out-of-band: 'sudo airmon-ng start <iface>' or "
                f"'sudo ip link set {self.interface} down && sudo iw dev {self.interface} set monitor control'.")
        argv = [self._binary["path"]] + list(self._binary["args"])
        if self._binary["name"] == "hcxdumptool":
            argv += ["-i", self.interface]
            if self.channel:
                argv += ["--channel", str(self.channel)]
        else:                           # airodump-ng
            argv += [self.interface]
            if self.channel:
                argv += ["--channel", str(self.channel)]
        self.extra["argv"] = argv
        log.info("wifi monitor %s: %s", self.sid, " ".join(argv))
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, bufsize=1, text=True)
        self._reader = threading.Thread(target=self._read_loop, name=f"wifi-{self.sid}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for line in iter(self._proc.stdout.readline, ""):
            if self._stop_event.is_set():
                break
            line = line.rstrip("\r\n")
            if not line:
                continue
            m = self._RE_AP.search(line)
            if m:
                bssid = m.group("bssid").lower()
                ssid = m.group("ssid") or ""
                rssi = float(m.group("rssi"))
                self.emit({
                    "event_kind": "wifi_ap",
                    "identifier_kind": "bssid", "identifier_value": bssid,
                    "rssi_dbm": rssi,
                    "observer_lat": self._observer.get("lat"),
                    "observer_lon": self._observer.get("lon"),
                    "ssid": ssid, "channel": int(m.group("ch")),
                })
                continue
            m = self._RE_STA.search(line)
            if m:
                mac = m.group("mac").lower()
                rssi = float(m.group("rssi"))
                self.emit({
                    "event_kind": "wifi_sta",
                    "identifier_kind": "mac", "identifier_value": mac,
                    "rssi_dbm": rssi,
                    "observer_lat": self._observer.get("lat"),
                    "observer_lon": self._observer.get("lon"),
                    "ssid_probed": m.group("ssid"),
                    "mac_randomisation_caveat": _is_likely_randomised_mac(mac),
                })
                continue
            # CSV / JSON fallbacks
            if line.startswith("{") and line.endswith("}"):
                try:
                    self._emit_json(json.loads(line))
                except Exception:
                    pass
        self._proc = None

    def _emit_json(self, j: dict) -> None:
        bssid = j.get("bssid") or j.get("ap_mac")
        sta = j.get("sta") or j.get("client") or j.get("station")
        if bssid:
            self.emit({
                "event_kind": "wifi_ap",
                "identifier_kind": "bssid",
                "identifier_value": str(bssid).lower(),
                "rssi_dbm": j.get("rssi") or j.get("rssi_dbm"),
                "ssid": j.get("ssid"),
                "channel": j.get("channel"),
                "observer_lat": self._observer.get("lat"),
                "observer_lon": self._observer.get("lon"),
            })
        if sta:
            mac = str(sta).lower()
            self.emit({
                "event_kind": "wifi_sta",
                "identifier_kind": "mac", "identifier_value": mac,
                "rssi_dbm": j.get("rssi") or j.get("rssi_dbm"),
                "observer_lat": self._observer.get("lat"),
                "observer_lon": self._observer.get("lon"),
                "mac_randomisation_caveat": _is_likely_randomised_mac(mac),
            })

    def _stop_impl(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# BLE
# ─────────────────────────────────────────────────────────────────────────────
class BleMonitor(CellularSession):
    KIND = "ble"

    # btmon line: "> HCI Event: LE Meta Event ... LE Advertising Report ... Address: AA:BB:CC:DD:EE:FF ... RSSI: -67 dBm"
    _RE_ADDR = re.compile(r"Address:\s+([0-9A-F:]{17})(?:\s+\(([^)]+)\))?", re.IGNORECASE)
    _RE_RSSI = re.compile(r"RSSI:\s+(-?\d+)\s*dBm", re.IGNORECASE)
    _RE_NAME = re.compile(r"(?:Name|Complete Local Name):\s+'?([^'\n]+?)'?$", re.IGNORECASE | re.MULTILINE)

    def __init__(self, sid: str, interface: str = "hci0", observer: Optional[dict] = None):
        super().__init__(sid=sid, device={"id": interface, "metadata": observer or {}})
        self.interface = interface
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._observer = (observer or {})
        self._binary = shutil.which("btmon")

    def _start_impl(self) -> None:
        if not self._binary:
            raise RuntimeError(
                "btmon not installed. Install BlueZ utilities: "
                "sudo apt install bluez bluez-tools.")
        # Make sure scanning is on; ignore failure (user may have it on already)
        bluetoothctl = shutil.which("bluetoothctl")
        if bluetoothctl:
            try:
                subprocess.run([bluetoothctl, "--", "scan", "on"], timeout=3,
                                capture_output=True, check=False)
            except Exception:
                pass
        argv = [self._binary, "-i", self.interface]
        self.extra["argv"] = argv
        log.info("ble monitor %s: %s", self.sid, " ".join(argv))
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, bufsize=1, text=True)
        self._reader = threading.Thread(target=self._read_loop, name=f"ble-{self.sid}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        # btmon emits multi-line packet blocks; we accumulate per-block then
        # emit on a blank-line / new-event boundary.
        block: list[str] = []
        for line in iter(self._proc.stdout.readline, ""):
            if self._stop_event.is_set():
                break
            line = line.rstrip("\r\n")
            if line.startswith("> ") or line.startswith("@ "):
                if block:
                    self._consume(block)
                    block = []
            block.append(line)
        if block:
            self._consume(block)
        self._proc = None

    def _consume(self, lines: list[str]) -> None:
        text = "\n".join(lines)
        if "Advertising Report" not in text and "Advertising Indication" not in text:
            return
        m_addr = self._RE_ADDR.search(text)
        if not m_addr:
            return
        addr = m_addr.group(1).upper()
        kind = m_addr.group(2) or ""
        m_rssi = self._RE_RSSI.search(text)
        rssi = float(m_rssi.group(1)) if m_rssi else None
        m_name = self._RE_NAME.search(text)
        name = m_name.group(1).strip() if m_name else None
        self.emit({
            "event_kind": "ble_advertisement",
            "identifier_kind": "ble", "identifier_value": addr,
            "rssi_dbm": rssi,
            "observer_lat": self._observer.get("lat"),
            "observer_lon": self._observer.get("lon"),
            "address_type": kind,
            "name": name,
        })

    def _stop_impl(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _is_likely_randomised_mac(mac: str) -> bool:
    """The locally-administered (LA) bit of the first octet's lowest 2 bits
    being 1 marks a randomised MAC per IEEE 802. Real OUIs are universally
    administered (bit = 0)."""
    try:
        first = int(mac.split(":")[0], 16)
        return (first & 0x02) != 0
    except Exception:
        return False


def _interface_in_monitor_mode(iface: str) -> bool:
    """Best-effort check for monitor mode. Reads /sys/class/net/<iface>/type.
    Type 803 (ARPHRD_IEEE80211_RADIOTAP) means the iface is a monitor."""
    p = f"/sys/class/net/{iface}/type"
    if not os.path.isfile(p):
        return False
    try:
        with open(p) as f:
            return f.read().strip() in ("803", "802")
    except Exception:
        return False
