# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
LTE passive PDCCH / SIB1 sniffer wrapper around LTESniffer (or, if that is
not installed, srsRAN's ``srsue`` binary used in scan mode).

LTESniffer is a USRP-native C++ tool that decodes the downlink Physical
Downlink Control Channel (PDCCH) — the cleartext scheduling channel — to
recover per-UE RNTIs (Radio Network Temporary Identifiers, the session
pseudonyms) along with their RSRP. It also blind-decodes SIB1 (system
information block 1), which carries the cell's MCC / MNC / TAC / Cell-ID
in plaintext.

The sniffer is run as a subprocess; its line-buffered stdout is parsed
line-by-line into structured events. We never decrypt or modify any data
plane traffic.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from typing import Optional

from .session import CellularSession

log = logging.getLogger(__name__)

# Patterns used to scrape stdout. LTESniffer emits a mix of human-readable
# log lines and JSON; we accept either. srsue's --log_level=info gives one
# human-readable line per decoded RRC message.
_RE_SIB1 = re.compile(
    r"SIB1.*?MCC[=:\s]+(?P<mcc>\d+).*?MNC[=:\s]+(?P<mnc>\d+).*?TAC[=:\s]+(?P<tac>0?x?[0-9a-fA-F]+).*?Cell\s*ID[=:\s]+(?P<ci>0?x?[0-9a-fA-F]+)",
    re.IGNORECASE,
)
_RE_RNTI = re.compile(
    r"(?:PDCCH|DCI).*?RNTI[=:\s]+(?P<rnti>0?x?[0-9a-fA-F]+).*?(?:RSRP[=:\s]+(?P<rsrp>-?\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)


class LteSnifferSession(CellularSession):
    KIND = "lte"

    def __init__(self, sid: str, device, center_hz: float, bandwidth_hz: float = 10_000_000):
        super().__init__(sid=sid, device=device, center_hz=center_hz, bandwidth_hz=bandwidth_hz)
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        meta = (device or {}).get("metadata") or {}
        self._observer = {"lat": meta.get("lat"), "lon": meta.get("lon")}
        self._cell_md: dict = {}                # latest known MCC/MNC/TAC/CellID
        self._binary = self._pick_binary()

    @staticmethod
    def _pick_binary() -> Optional[str]:
        for name in ("LTESniffer", "lte-sniffer", "srsue"):
            p = shutil.which(name)
            if p:
                return p
        return None

    def _command(self) -> list[str]:
        """Build the argv for the chosen binary. Falls back gracefully when
        a particular binary doesn't accept a flag we expect; the user can
        also pass ``extra.argv`` through ``self.extra``."""
        if self._binary is None:
            raise RuntimeError(
                "no LTE sniffer found on PATH. Install LTESniffer (preferred) or "
                "srsRAN's srsue. See ./install.sh --with-lte-sniffer.")
        if "LTESniffer" in self._binary or "lte-sniffer" in self._binary:
            # LTESniffer CLI: --freq <hz> --rf_args 'soapy=0' --mode SnifferAll
            argv = [self._binary,
                    "--freq", f"{int(self.center_hz)}",
                    "--rf_args", _soapy_args_for(self.device),
                    "--mode", "ImsiCatcher"]   # decodes PDCCH + SIB1
        else:
            # srsue scan mode
            argv = [self._binary,
                    "--rat.eutra.dl_earfcn", _hz_to_earfcn(self.center_hz),
                    "--rf.srate_hz", str(int(self.bandwidth_hz)),
                    "--log_level=info"]
        return argv

    def _start_impl(self) -> None:
        argv = self._command()
        self.extra["argv"] = argv
        log.info("LTE sniffer %s argv: %s", self.sid, " ".join(argv))
        self._proc = subprocess.Popen(argv,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         bufsize=1, text=True)
        self._reader = threading.Thread(target=self._read_loop, name=f"lte-{self.sid}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for line in iter(self._proc.stdout.readline, ""):
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            # JSON line?
            if line.startswith("{") and line.endswith("}"):
                try:
                    self._emit_json(json.loads(line)); continue
                except Exception:
                    pass
            # SIB1 line?
            m = _RE_SIB1.search(line)
            if m:
                self._cell_md = {
                    "mcc": m.group("mcc"), "mnc": m.group("mnc"),
                    "tac": str(int(m.group("tac"), 0)),
                    "cell_id": str(int(m.group("ci"), 0)),
                }
                cell_value = f"{self._cell_md['mcc']}-{self._cell_md['mnc']}-{self._cell_md['tac']}-{self._cell_md['cell_id']}"
                self._emit_id("lte_cell", cell_value, **self._cell_md, source_line=line)
                continue
            # RNTI line?
            m = _RE_RNTI.search(line)
            if m:
                rnti_hex = m.group("rnti")
                rnti = str(int(rnti_hex, 0))
                rsrp = m.group("rsrp")
                self._emit_id("rnti", rnti,
                                rsrp_dbm=(float(rsrp) if rsrp is not None else None),
                                **self._cell_md, source_line=line)
        self._proc = None

    def _emit_json(self, j: dict) -> None:
        # LTESniffer's structured-JSON output: {type, rnti, rsrp, mcc, mnc, tac, cell_id, ...}
        kind = j.get("type") or j.get("event")
        if kind in ("sib1", "cell"):
            self._cell_md = {k: j[k] for k in ("mcc", "mnc", "tac", "cell_id") if k in j}
            cell_value = f"{j.get('mcc','?')}-{j.get('mnc','?')}-{j.get('tac','?')}-{j.get('cell_id','?')}"
            self._emit_id("lte_cell", cell_value, **self._cell_md)
        elif kind in ("rnti", "pdcch"):
            rnti = str(j.get("rnti"))
            self._emit_id("rnti", rnti,
                            rsrp_dbm=j.get("rsrp_dbm") or j.get("rsrp"),
                            **self._cell_md)

    def _emit_id(self, identifier_kind: str, identifier_value: str, **payload) -> None:
        rssi = payload.pop("rsrp_dbm", None) or payload.pop("rssi_dbm", None)
        self.emit({
            "identifier_kind": identifier_kind,
            "identifier_value": identifier_value,
            "rssi_dbm": rssi,
            "frequency_hz": self.center_hz,
            "observer_lat": self._observer.get("lat"),
            "observer_lon": self._observer.get("lon"),
            **payload,
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


def _soapy_args_for(device: Optional[dict]) -> str:
    if not device:
        return "soapy=0"
    dev_id = device.get("id")
    drv = (device.get("metadata") or {}).get("driver")
    parts = ["soapy=0"]
    if drv:
        parts.append(f"driver={drv}")
    if dev_id and dev_id != "synthetic":
        parts.append(f"device={dev_id}")
    return ",".join(parts)


def _hz_to_earfcn(hz: float) -> str:
    """Crude DL E-UTRA absolute radio frequency channel number for the most
    common LTE bands. Used only as a srsue fallback flag — LTESniffer takes
    --freq in Hz directly."""
    # Band 1 (FDD 2100 MHz DL)
    if 2_110_000_000 <= hz <= 2_170_000_000:
        return str(int((hz - 2_110_000_000) / 100_000))
    # Band 3 (FDD 1800 MHz DL)
    if 1_805_000_000 <= hz <= 1_880_000_000:
        return str(1200 + int((hz - 1_805_000_000) / 100_000))
    # Band 7 (FDD 2600 MHz DL)
    if 2_620_000_000 <= hz <= 2_690_000_000:
        return str(2750 + int((hz - 2_620_000_000) / 100_000))
    # Band 20 (FDD 800 MHz DL)
    if 791_000_000 <= hz <= 821_000_000:
        return str(6150 + int((hz - 791_000_000) / 100_000))
    return "0"
