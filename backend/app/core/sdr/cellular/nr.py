"""
5G NR SSB / MIB / SIB1 passive sniffer wrapper around 5GSniffer.

5GSniffer (spritelab, IEEE S&P 2023; ``github.com/spritelab/5GSniffer``)
decodes the broadcast SSB → MIB → SIB1 chain to recover the Physical Cell
ID (PCI), SSB RSRP, MCC / MNC / TAC / Cell-ID, and the PDCCH DCIs /
RNTIs — without attaching to the network. The upstream binary is
``5g_sniffer`` (lowercase, underscore). We treat the cell itself as an
identifier (``kind='nr_cell'``); subscriber pseudonyms are not recoverable
passively in NR (subject to encryption).

For PDCCH + downlink-injection + Wireshark integration, see the USENIX
2024 follow-on at ``github.com/asset-group/Sni5Gect-5GNR-sniffing-and-
exploitation``; set ``ARES_5GSNIFFER_GIT_URL`` in the installer to switch
to that fork.
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

_RE_MIB = re.compile(r"MIB.*?PCI[=:\s]+(?P<pci>\d+)", re.IGNORECASE)
_RE_SIB1 = re.compile(
    r"SIB1.*?MCC[=:\s]+(?P<mcc>\d+).*?MNC[=:\s]+(?P<mnc>\d+).*?TAC[=:\s]+(?P<tac>0?x?[0-9a-fA-F]+).*?Cell\s*ID[=:\s]+(?P<ci>0?x?[0-9a-fA-F]+)",
    re.IGNORECASE,
)
_RE_RSRP = re.compile(r"SSB\s*RSRP[=:\s]+(?P<rsrp>-?\d+(?:\.\d+)?)", re.IGNORECASE)


class NrSnifferSession(CellularSession):
    KIND = "nr"

    def __init__(self, sid: str, device, center_hz: float, scs_khz: int = 30):
        super().__init__(sid=sid, device=device, center_hz=center_hz)
        self.scs_khz = scs_khz
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        meta = (device or {}).get("metadata") or {}
        self._observer = {"lat": meta.get("lat"), "lon": meta.get("lon")}
        self._pci: Optional[int] = None
        self._cell_md: dict = {}
        self._binary = shutil.which("5g_sniffer") or shutil.which("5GSniffer") or shutil.which("5g-sniffer")

    def _start_impl(self) -> None:
        if self._binary is None:
            raise RuntimeError(
                "5G NR sniffer not installed. Install via ./install.sh (default --with-5g-sniffer "
                "is ON; clones github.com/spritelab/5GSniffer with --recurse-submodules and builds "
                "with clang). Override with ARES_5GSNIFFER_GIT_URL=<url> to use a fork.")
        argv = [self._binary,
                "--freq", f"{int(self.center_hz)}",
                "--scs", f"{self.scs_khz}"]
        self.extra["argv"] = argv
        log.info("NR sniffer %s argv: %s", self.sid, " ".join(argv))
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, bufsize=1, text=True)
        self._reader = threading.Thread(target=self._read_loop, name=f"nr-{self.sid}", daemon=True)
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
            if line.startswith("{") and line.endswith("}"):
                try:
                    self._emit_json(json.loads(line)); continue
                except Exception:
                    pass
            m = _RE_MIB.search(line)
            if m:
                self._pci = int(m.group("pci"))
                self._emit_id("nr_cell", f"PCI-{self._pci}", pci=self._pci, source_line=line)
            m = _RE_SIB1.search(line)
            if m:
                mcc, mnc = m.group("mcc"), m.group("mnc")
                tac = str(int(m.group("tac"), 0))
                ci = str(int(m.group("ci"), 0))
                self._cell_md = {"mcc": mcc, "mnc": mnc, "tac": tac, "cell_id": ci, "pci": self._pci}
                cell_value = f"{mcc}-{mnc}-{tac}-{ci}"
                self._emit_id("nr_cell", cell_value, **self._cell_md)
            m = _RE_RSRP.search(line)
            if m and self._pci is not None:
                self._emit_id("nr_cell",
                                f"PCI-{self._pci}" if not self._cell_md
                                else f"{self._cell_md.get('mcc','?')}-{self._cell_md.get('mnc','?')}-{self._cell_md.get('tac','?')}-{self._cell_md.get('cell_id','?')}",
                                rssi_dbm=float(m.group("rsrp")), **self._cell_md)
        self._proc = None

    def _emit_json(self, j: dict) -> None:
        if "pci" in j and self._pci is None:
            self._pci = int(j["pci"])
        if "mcc" in j and "mnc" in j:
            self._cell_md = {k: j[k] for k in ("mcc", "mnc", "tac", "cell_id") if k in j}
        cell_value = (f"{j.get('mcc','?')}-{j.get('mnc','?')}-{j.get('tac','?')}-{j.get('cell_id','?')}"
                       if "mcc" in j else f"PCI-{self._pci}")
        self._emit_id("nr_cell", cell_value,
                        rssi_dbm=j.get("ssb_rsrp_dbm") or j.get("rsrp"),
                        pci=self._pci, **self._cell_md)

    def _emit_id(self, identifier_kind: str, identifier_value: str, **payload) -> None:
        self.emit({
            "identifier_kind": identifier_kind,
            "identifier_value": identifier_value,
            "rssi_dbm": payload.pop("rssi_dbm", None),
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
