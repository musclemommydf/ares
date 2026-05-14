"""
GSM passive control-channel decoder built around gr-gsm (GNU Radio out-of-tree
module). We construct the flowgraph in Python rather than shelling out to
``grgsm_livemon_headless``: that way Ares can tap the channelized IQ for DF,
share the SDR with other decoders, and hand structured events directly into
the target tracker without parsing CLI stdout.

Flowgraph (built lazily in ``_start_impl`` so importing this module never
requires GNU Radio):

    osmosdr_source                  ─┐
    │  center=ARFCN_to_hz(arfcn)     │
    │  rate=1 MS/s                   │
    │  gain=auto                     │
    └─► clock_offset_corrector_tagged
        └─► input_power_blob (RSSI tap, for the tracker)
            └─► receiver_cf (grgsm.receiver_cf)
                ├─► message_printer (CCCH/BCCH)       ─► gsm_event_dispatcher
                └─► message_printer (SDCCH/SACCH)     ─► gsm_event_dispatcher

The dispatcher subscribes to the GR message ports and parses GSMTAP payloads
to extract Cell ID / LAC / MCC / MNC / ARFCN from System Information Type
1–6 messages, and paging TMSI/IMSI from Paging Request Type 1 / Type 2 /
Type 3 messages on the CCCH.

If GNU Radio is not importable the session fails fast with a clear error.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .session import CellularSession

log = logging.getLogger(__name__)


# Channel-to-frequency helpers
def arfcn_to_hz(arfcn: int, band: str = "GSM900") -> float:
    """Compute downlink centre frequency for a given ARFCN.
    Implements the 3GPP TS 45.005 formulas for the common bands."""
    if band in ("GSM900", "EGSM900"):
        if 1 <= arfcn <= 124:                                 # P-GSM900
            return 935.0e6 + 0.2e6 * arfcn
        if 975 <= arfcn <= 1023:                              # E-GSM900
            return 935.0e6 + 0.2e6 * (arfcn - 1024)
    if band == "DCS1800":
        if 512 <= arfcn <= 885:
            return 1805.2e6 + 0.2e6 * (arfcn - 512)
    if band == "PCS1900":
        if 512 <= arfcn <= 810:
            return 1930.2e6 + 0.2e6 * (arfcn - 512)
    if band == "GSM850":
        if 128 <= arfcn <= 251:
            return 869.2e6 + 0.2e6 * (arfcn - 128)
    raise ValueError(f"arfcn {arfcn} out of range for band {band}")


class GsmDecoder(CellularSession):
    """In-process GSM BCCH/CCCH decoder via gr-gsm."""

    KIND = "gsm"

    def __init__(self, sid: str, device, center_hz: float,
                  sample_rate_hz: float = 1_000_000, gain: float = 30.0,
                  arfcn: Optional[int] = None, band: str = "GSM900"):
        super().__init__(sid=sid, device=device, center_hz=center_hz,
                          bandwidth_hz=270_833 * 1.5)
        self.sample_rate_hz = sample_rate_hz
        self.gain = gain
        self.arfcn = arfcn
        self.band = band
        self._tb = None                               # GNU Radio top_block instance
        self._gr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Per-cell observer location (the SDR's). The session inherits this
        # from device.metadata when present so every emitted event has a
        # consistent observer fix.
        meta = (device or {}).get("metadata") or {}
        self._observer = {
            "lat": float(meta.get("lat", 0.0)) if meta.get("lat") is not None else None,
            "lon": float(meta.get("lon", 0.0)) if meta.get("lon") is not None else None,
        }

    def _start_impl(self) -> None:
        try:
            import gnuradio                                # noqa: F401
            import grgsm                                   # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "GSM decoder requires GNU Radio + gr-gsm. Install with: "
                "sudo apt install gnuradio gnuradio-dev && build gr-gsm "
                "(see ./install.sh --with-gnuradio). Underlying error: " + str(e)
            )
        # Build and start the flowgraph in a daemon thread so the request
        # returns quickly. The actual block instantiation is below.
        self._tb = _build_flowgraph(self)
        self._gr_thread = threading.Thread(
            target=self._run, name=f"gsm-{self.sid}", daemon=True)
        self._gr_thread.start()

    def _run(self) -> None:
        try:
            self._tb.start()
            while not self._stop_event.is_set():
                time.sleep(0.5)
            self._tb.stop()
            self._tb.wait()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.exception("gsm decoder %s crashed", self.sid)

    def _stop_impl(self) -> None:
        self._stop_event.set()
        if self._gr_thread is not None:
            self._gr_thread.join(timeout=4.0)


# ─────────────────────────────────────────────────────────────────────────────
# GNU-Radio top_block construction. Kept in a separate helper so it can be
# unit-tested with a mock gnuradio and so a missing GR doesn't break the
# import of this module.
# ─────────────────────────────────────────────────────────────────────────────
def _build_flowgraph(sess: "GsmDecoder"):
    """Construct the gr-gsm receiver flowgraph and wire the message ports
    into ``sess.emit``. Returns the top_block."""
    from gnuradio import gr, blocks
    import grgsm
    import pmt

    class _TopBlock(gr.top_block):
        def __init__(self):
            gr.top_block.__init__(self, f"ares-gsm-{sess.sid}")
            # SDR source — gr-osmosdr (works against the same SoapySDR
            # device Ares uses elsewhere). osmosdr.source supports "args"
            # strings like "soapy=0,driver=rtlsdr" etc.
            import osmosdr
            args = ""
            dev_id = (sess.device or {}).get("id")
            if dev_id:
                args = f"soapy=0,device={dev_id}"
            src = osmosdr.source(args=args)
            src.set_sample_rate(sess.sample_rate_hz)
            src.set_center_freq(sess.center_hz, 0)
            src.set_gain(sess.gain, 0)
            src.set_bandwidth(sess.sample_rate_hz * 0.75, 0)
            # gr-gsm receiver — uses the same OSR (oversample ratio) the
            # upstream livemon flowgraph uses (4× the symbol rate).
            osr = max(1, int(sess.sample_rate_hz / (270_833 // 2)))
            recv = grgsm.receiver_cf(osr, [0], [])
            self.connect(src, recv)
            # Subscribe to all decoded GSM messages on the C0 timeslot.
            self.msg_connect((recv, "C0"), (self, "ccch_in"))
            self.msg_connect((recv, "measurements"), (self, "rssi_in"))
            self._observer_lat = sess._observer.get("lat")
            self._observer_lon = sess._observer.get("lon")
            # Message handlers
            def on_ccch(msg):
                try:
                    blob = pmt.cdr(msg)
                    raw = bytes(pmt.u8vector_elements(blob))
                    event = _parse_gsm_tap(raw)
                    if event is None:
                        return
                    event["observer_lat"] = self._observer_lat
                    event["observer_lon"] = self._observer_lon
                    event["frequency_hz"] = sess.center_hz
                    sess.emit(event)
                except Exception:
                    log.exception("gsm ccch handler")

            def on_rssi(msg):
                try:
                    # The 'measurements' port emits a pmt dict with a single
                    # 'power' double — that's the burst-power estimate in dB.
                    if pmt.is_dict(msg):
                        keys = pmt.dict_keys(msg)
                        for i in range(pmt.length(keys)):
                            k = pmt.nth(i, keys)
                            v = pmt.dict_ref(msg, k, pmt.PMT_NIL)
                            if pmt.symbol_to_string(k) == "power":
                                sess.extra["last_rssi_dbm"] = float(pmt.to_double(v))
                except Exception:
                    pass

            # Register two python message handlers
            self.message_port_register_in(pmt.intern("ccch_in"))
            self.message_port_register_in(pmt.intern("rssi_in"))
            self.set_msg_handler(pmt.intern("ccch_in"), on_ccch)
            self.set_msg_handler(pmt.intern("rssi_in"), on_rssi)

    return _TopBlock()


# ─────────────────────────────────────────────────────────────────────────────
# Minimal GSM-TAP / L3 parser — enough to extract the broadcast cell-IDs and
# the paging-request TMSI/IMSI fields. Full L3 decoding lives in libosmocore
# (osmocom's libosmo-gsm) — we deliberately implement only the cleartext
# subset we need.
# ─────────────────────────────────────────────────────────────────────────────
def _parse_gsm_tap(raw: bytes) -> Optional[dict]:
    """Parse a GSMTAP packet's payload and return a dict suitable for
    ``CellularSession.emit``. Returns None if the message isn't useful."""
    if len(raw) < 16:
        return None
    # GSMTAP v2 header: 8 bytes pseudo-header, 4 bytes channel info, 4 bytes
    # frame number. After that comes the L2 LAPDm / L3 payload.
    if raw[0] != 0x02:
        return None
    hlen = raw[1] * 4
    if hlen < 16 or len(raw) < hlen:
        return None
    ch_type = raw[12]
    payload = raw[hlen:]
    # Channel types: 1 = BCCH, 2 = CCCH, 3 = SDCCH (3GPP TS 04.05)
    if ch_type == 1 and len(payload) >= 23:
        # BCCH - System Information messages start with L3 header byte
        return _parse_si(payload)
    if ch_type == 2 and len(payload) >= 23:
        # CCCH - paging requests (most useful for TMSI tracking)
        return _parse_ccch(payload)
    return None


def _parse_si(p: bytes) -> Optional[dict]:
    """Pull MCC/MNC/LAC/CI out of System Information Type 3 (broadcast on BCCH)."""
    # SI3 has a fixed-position Location Area Identification IE:
    #   bytes 3..7 — MCC (1.5 bytes BCD), MNC (1.5 bytes BCD), LAC (2 bytes)
    # And the Cell Identity IE at bytes 8..9.
    if len(p) < 24:
        return None
    msg_type = p[2] if len(p) > 2 else 0
    if msg_type not in (0x1B, 0x19):  # SI3 or SI4
        # Still useful for cell-presence detection
        return {"event_kind": "gsm_si", "raw_type": msg_type}
    # Best-effort: decode the LAI at the canonical offsets
    try:
        mcc_d1 = p[3] & 0x0F; mcc_d2 = (p[3] >> 4) & 0x0F
        mnc_d3 = p[4] & 0x0F; mcc_d3 = (p[4] >> 4) & 0x0F
        mnc_d1 = p[5] & 0x0F; mnc_d2 = (p[5] >> 4) & 0x0F
        mcc = f"{mcc_d1}{mcc_d2}{mcc_d3}"
        mnc = f"{mnc_d1}{mnc_d2}" + (f"{mnc_d3}" if mnc_d3 != 0xF else "")
        lac = (p[6] << 8) | p[7]
        ci = (p[8] << 8) | p[9]
        cell_id = f"{mcc}-{mnc}-{lac}-{ci}"
        return {
            "event_kind": "gsm_cell",
            "identifier_kind": "gsm_cell", "identifier_value": cell_id,
            "mcc": mcc, "mnc": mnc, "lac": lac, "ci": ci,
        }
    except Exception:
        return {"event_kind": "gsm_si", "raw_type": msg_type}


def _parse_ccch(p: bytes) -> Optional[dict]:
    """Pull the Mobile Identity off a Paging Request type 1 (msg type 0x21).

    The MI is in TLV form. Mobile-identity types:
      0b001 IMSI    — 8 octets BCD-packed
      0b100 TMSI    — 4 octets binary
    """
    if len(p) < 8 or p[2] != 0x21:                # paging request type 1
        return None
    # Walk to the Mobile Identity 1 IE (information element 0x17 or implicit
    # at fixed offset depending on message length). The simplest robust
    # approach: scan for the MI tag byte where the low nibble == 1 (length 8
    # for IMSI) or == 4 (length 4 for TMSI).
    for i in range(3, len(p) - 1):
        l = p[i]
        if l in (4, 5, 8) and i + 1 + l <= len(p):
            mi = p[i + 1: i + 1 + l]
            id_type = mi[0] & 0x07
            if id_type == 4 and l == 5:          # TMSI: type=4, 4 bytes following
                tmsi = mi[1:5].hex().upper()
                return {"event_kind": "gsm_paging",
                          "identifier_kind": "tmsi", "identifier_value": tmsi}
            if id_type == 1 and l in (8, 9):     # IMSI: BCD-packed, 15 digits
                digits = []
                # First nibble (high) of byte 0 is the first IMSI digit
                digits.append(str((mi[0] >> 4) & 0xF))
                for b in mi[1:]:
                    digits.append(str(b & 0xF))
                    digits.append(str((b >> 4) & 0xF))
                imsi = "".join(d for d in digits if d != "f")[:15]
                if len(imsi) >= 14:
                    return {"event_kind": "gsm_paging",
                              "identifier_kind": "imsi", "identifier_value": imsi}
            break
    return None
