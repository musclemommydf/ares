"""
IQ-level SDR drivers — one common interface ([SdrDriver]) over coherent and
single-channel hardware. Distinct from the parent ``sdr/adapters.py``:

  * adapters.py — *pre-computed bearings* polled / received from external DF
    pipelines (krakensdr_doa over HTTP, Matchstiq-side JSON-lines TCP, …).
  * drivers/    — *raw IQ* pulled from the hardware directly; the in-process
    DSP pipeline ([app.core.df.algorithms]) does MUSIC/Bartlett/Capon/MEM/ESPRIT
    to derive bearings. This is the "everything bundled in Ares" path — no
    krakensdr_doa daemon, no external GNU Radio flowgraph, no SDRAngel DOA.

Driver registry: maps a short ``driver_id`` string to a factory. Used by
``/sdr/drivers`` to advertise availability + by the device manager to wire up
a real driver when a device is added.
"""

from __future__ import annotations

import logging
from typing import Callable

from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver
from .heimdall import HeimdallDriver
from .antsdr_e200 import AntsdrE200Driver
from .matchstiq import MatchstiqX40Driver
from .uhd import UhdUsrpDriver
from .plutosdr import PlutoSdrDriver
from .fmcomms5 import FmComms5Driver

log = logging.getLogger(__name__)

# driver_id → (capabilities, factory)
_REGISTRY: dict[str, tuple[DriverCapabilities, Callable[..., SdrDriver]]] = {
    SyntheticDriver.capabilities.driver_id:    (SyntheticDriver.capabilities,    SyntheticDriver),
    HeimdallDriver.capabilities.driver_id:     (HeimdallDriver.capabilities,     HeimdallDriver),
    AntsdrE200Driver.capabilities.driver_id:   (AntsdrE200Driver.capabilities,   AntsdrE200Driver),
    MatchstiqX40Driver.capabilities.driver_id: (MatchstiqX40Driver.capabilities, MatchstiqX40Driver),
    UhdUsrpDriver.capabilities.driver_id:      (UhdUsrpDriver.capabilities,      UhdUsrpDriver),
    PlutoSdrDriver.capabilities.driver_id:     (PlutoSdrDriver.capabilities,     PlutoSdrDriver),
    FmComms5Driver.capabilities.driver_id:     (FmComms5Driver.capabilities,     FmComms5Driver),
}


def list_drivers() -> list[dict]:
    """Public driver list — what the UI offers in 'add device'."""
    return [
        {
            "id": cap.driver_id, "name": cap.name,
            "coherent": cap.coherent, "max_channels": cap.max_channels,
            "sample_rate_range_hz": list(cap.sample_rate_range_hz),
            "tunable_range_hz": list(cap.tunable_range_hz),
            "gain_range_db": list(cap.gain_range_db),
            "iq_capture": cap.iq_capture,
            "on_device_fft": cap.on_device_fft,
            "on_device_doa": cap.on_device_doa,
            "tx_capable": cap.tx_capable,
            "cal_source": cap.cal_source,
            "notes": cap.notes,
        }
        for cap, _ in _REGISTRY.values()
    ]


def create(driver_id: str, **kwargs) -> SdrDriver:
    """Instantiate (but do not open) a driver by id."""
    entry = _REGISTRY.get(driver_id)
    if entry is None:
        raise KeyError(f"unknown SDR driver: {driver_id} (available: {sorted(_REGISTRY.keys())})")
    _, factory = entry
    return factory(**kwargs)


def register(driver_id: str, capabilities: DriverCapabilities, factory: Callable[..., SdrDriver]) -> None:
    """Public hook for third-party drivers (Luowave LW420, QR210, custom test rigs, ...)."""
    _REGISTRY[driver_id] = (capabilities, factory)
    log.info("registered SDR driver: %s", driver_id)


__all__ = [
    "DriverCapabilities", "IqFrame", "SdrDriver",
    "SyntheticDriver", "HeimdallDriver", "AntsdrE200Driver",
    "MatchstiqX40Driver", "UhdUsrpDriver", "PlutoSdrDriver", "FmComms5Driver",
    "list_drivers", "create", "register",
]
