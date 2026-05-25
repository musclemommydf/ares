# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Detection + command bridge for USB multi-protocol field tools (sub-GHz / RFID /
NFC / IR / iButton / GPIO / HID) and RFID-research tools.

We deliberately expose **capabilities**, not brand names — a connected device is
reported by what it can *do* (its capability set), not by its make/model. Detection
matches the USB CDC-ACM serial descriptor (VID:PID, then manufacturer/product
strings) against a capability profile; operation speaks the device's documented
serial CLI over pyserial. With no compatible tool plugged in, detection returns an
empty list and any action raises ``ToolUnavailable`` (honest — never fabricated).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


class ToolUnavailable(RuntimeError):
    """No compatible tool is connected for the requested capability."""


# Capability bundles by hardware class. Brand stays internal; the UI only ever
# sees the generic ``kind`` + ``capabilities``.
_MULTITOOL = ["subghz", "rfid_lf", "nfc_hf", "infrared", "ibutton", "gpio", "badusb"]
_RFID_RESEARCH = ["rfid_lf", "nfc_hf"]

# (vid, pid) → (kind, capabilities, baud). Matched first; string match is the fallback.
_USB_PROFILES: dict[tuple[int, int], tuple[str, list[str], int]] = {
    (0x0483, 0x5740): ("multitool", _MULTITOOL, 115200),       # STM32 CDC field multitool
    (0x9AC4, 0x4B8F): ("rfid_research", _RFID_RESEARCH, 115200),
    (0x2D2D, 0x504D): ("rfid_research", _RFID_RESEARCH, 115200),
}

# Fallback: lowercase substrings in manufacturer/product → capability class.
_STRING_PROFILES: list[tuple[tuple[str, ...], str, list[str], int]] = [
    (("multi", "field tool", "sub-ghz", "subghz"), "multitool", _MULTITOOL, 115200),
    (("rfid", "nfc", "rdv4", "iceman"), "rfid_research", _RFID_RESEARCH, 115200),
]

_KIND_LABEL = {
    "multitool": "Multi-protocol field tool (sub-GHz · RFID · NFC · IR · iButton · GPIO · HID)",
    "rfid_research": "RFID / NFC research tool (LF + HF)",
}


@dataclass
class DetectedTool:
    id: str
    kind: str
    label: str
    capabilities: list[str]
    transport: str
    port: str
    baud: int
    serial_no: Optional[str] = None

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "capabilities": self.capabilities, "transport": self.transport,
                "port": self.port, "serial": self.serial_no}


def _match_profile(vid, pid, manuf, product):
    if vid is not None and pid is not None and (vid, pid) in _USB_PROFILES:
        return _USB_PROFILES[(vid, pid)]
    hay = f"{manuf or ''} {product or ''}".lower()
    for needles, kind, caps, baud in _STRING_PROFILES:
        if any(n in hay for n in needles):
            return kind, caps, baud
    return None


def detect_serial_tools() -> list[DetectedTool]:
    """Enumerate USB serial ports and return the compatible field tools, by capability."""
    try:
        from serial.tools import list_ports
    except Exception as e:                       # pyserial not installed
        log.debug("pyserial unavailable: %s", e)
        return []
    out: list[DetectedTool] = []
    seen = 0
    for p in list_ports.comports():
        # Only USB CDC devices are candidates — skip legacy /dev/ttyS* UARTs.
        if not (getattr(p, "vid", None) or "ACM" in (p.device or "") or "USB" in (p.device or "")):
            continue
        prof = _match_profile(getattr(p, "vid", None), getattr(p, "pid", None),
                              getattr(p, "manufacturer", None), getattr(p, "product", None))
        if prof is None:
            continue
        kind, caps, baud = prof
        out.append(DetectedTool(
            id=f"{kind}-{seen}", kind=kind, label=_KIND_LABEL.get(kind, kind),
            capabilities=list(caps), transport="usb-serial", port=p.device, baud=baud,
            serial_no=getattr(p, "serial_number", None)))
        seen += 1
    return out


def find_tool(capability: str) -> DetectedTool:
    """First connected tool that offers ``capability``, else raise ToolUnavailable."""
    for t in detect_serial_tools():
        if capability in t.capabilities:
            return t
    raise ToolUnavailable(f"no connected tool provides '{capability}' "
                          f"(plug in a compatible device over USB)")


def find_tool_by_id(tool_id: str) -> DetectedTool:
    """The connected tool with this detect id, else raise ToolUnavailable."""
    for t in detect_serial_tools():
        if t.id == tool_id:
            return t
    raise ToolUnavailable(f"tool {tool_id!r} is not connected")


# ── serial CLI bridge ─────────────────────────────────────────────────────────
_locks: dict[str, threading.Lock] = {}


def _port_lock(port: str) -> threading.Lock:
    return _locks.setdefault(port, threading.Lock())


def cli(port: str, baud: int, command: str, *, settle_s: float = 0.4,
        read_s: float = 2.0) -> str:
    """Send one CLI line to a text-protocol tool and return its response text.

    BLOCKING — call from an executor. Raises ToolUnavailable if the port can't be
    opened (tool unplugged mid-session). Never invents output: returns exactly what
    the device sent (decoded, prompt/echo trimmed)."""
    try:
        import serial
    except Exception as e:
        raise ToolUnavailable(f"pyserial not installed: {e}")
    with _port_lock(port):
        try:
            ser = serial.Serial(port, baudrate=baud, timeout=read_s, write_timeout=read_s)
        except Exception as e:
            raise ToolUnavailable(f"cannot open {port}: {e}")
        try:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write((command.strip() + "\r\n").encode())
            ser.flush()
            time.sleep(settle_s)
            chunks = []
            deadline = time.time() + read_s
            while time.time() < deadline:
                n = ser.in_waiting
                if n:
                    chunks.append(ser.read(n))
                    deadline = time.time() + 0.25     # extend while data still flows
                else:
                    time.sleep(0.03)
            raw = b"".join(chunks).decode(errors="replace")
        finally:
            try:
                ser.close()
            except Exception:
                pass
    # strip the echoed command and the trailing prompt
    lines = [ln for ln in raw.replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln.strip() and ln.strip() != command.strip()]
    return "\n".join(lines).strip()
