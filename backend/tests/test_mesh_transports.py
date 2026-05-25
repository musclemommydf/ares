# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for the Meshtastic + Silvus mesh transports (Track D, D2.2/D2.3).

Run from `backend/`:   python -m tests.test_mesh_transports

Tests:
  1. Meshtastic fit — a normal LoB frame fits one LoRa payload; an oversize chat
     does not, and _send drops it (without touching the radio).
  2. Meshtastic deliver — an encoded LoB / chat payload decodes and fans out to
     the async LoB sink / sync chat sink.
  3. Silvus parse — a JSON-RPC routing response (mixed key names) normalises to
     {ip, snr_db, hops}.
  4. Silvus sync — sync_peers adds an Ares peer URL per neighbour, skips our own
     IP, and never re-adds an existing peer.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.core.sdr import mesh_codec
from app.core.sdr.mesh_meshtastic import MAX_PAYLOAD, MeshtasticTransport
from app.core.sdr.mesh_silvus import SilvusAdapter


def _sample_lob() -> dict:
    return {"id": "abc123", "origin_node": "node-a", "origin_device": "kraken-0",
            "device_id": "kraken-0", "lat": 38.9, "lon": -77.0, "azimuth_deg": 142.7,
            "frequency_hz": 462562500.0, "rssi_dbm": -73.0, "t": 1748131200.0,
            "confidence_pct": 88.0, "observer_height_m": 2.0, "estimated_distance_m": 1000.0,
            "environment": "suburban", "device_type": "kraken", "target_device_id": "", "hops": 1}


class _FakeIface:
    def __init__(self):
        self.sent = []
    def sendData(self, data, portNum=None):  # noqa: N803 (match meshtastic API)
        self.sent.append((bytes(data), portNum))


def test_meshtastic_fit():
    t = MeshtasticTransport()
    t._iface = _FakeIface()
    # normal LoB fits and is sent
    asyncio.run(t.send_lob(_sample_lob()))
    fit_ok = len(t._iface.sent) == 1 and t._dropped == 0
    # oversize chat (long text) is dropped, not sent
    big = {"from_node": "n", "id": "m", "room": "All", "text": "x" * (MAX_PAYLOAD + 50),
           "lat": None, "lon": None, "t": 1.0}
    asyncio.run(t.send_chat(big))
    drop_ok = t._dropped == 1 and len(t._iface.sent) == 1
    ok = fit_ok and drop_ok
    return ("meshtastic fit/drop", ok, f"sent={len(t._iface.sent)} dropped={t._dropped}")


def test_meshtastic_deliver():
    got_lob: list[dict] = []
    got_chat: list[dict] = []

    async def on_lob(d):
        got_lob.append(d)

    def on_chat(d):
        got_chat.append(d)

    t = MeshtasticTransport(on_lob=on_lob, on_chat=on_chat)
    lob_frame = mesh_codec.encode_lob(_sample_lob())
    chat_frame = mesh_codec.encode_chat({"from_node": "n", "id": "m", "room": "All",
                                         "text": "hi", "lat": 1.0, "lon": 2.0, "t": 9.0})
    asyncio.run(t._deliver(lob_frame))
    asyncio.run(t._deliver(chat_frame))
    ok = (len(got_lob) == 1 and abs(got_lob[0]["frequency_hz"] - 462562500.0) < 1e-3
          and len(got_chat) == 1 and got_chat[0]["text"] == "hi")
    return ("meshtastic deliver→sinks", ok, f"lob={len(got_lob)} chat={len(got_chat)}")


def test_silvus_parse():
    payload = {"result": [
        {"ip": "10.0.0.2", "snr": "22.5", "hops": 1},
        {"node_ip": "10.0.0.3", "rssi": -61, "hop_count": 2},
        {"foo": "bar"},                       # no ip ⇒ skipped
    ]}
    nodes = SilvusAdapter._parse_nodes(payload)
    ok = (len(nodes) == 2 and nodes[0]["ip"] == "10.0.0.2" and abs(nodes[0]["snr_db"] - 22.5) < 1e-6
          and nodes[1]["ip"] == "10.0.0.3" and nodes[1]["hops"] == 2)
    return ("silvus parse nodes", ok, f"{len(nodes)} nodes parsed")


class _FakePeerMesh:
    def __init__(self, existing=None):
        self.peers = list(existing or [])
    def list_peers(self):
        return list(self.peers)
    def add_peer(self, url):
        self.peers.append(url)
        return url


def test_silvus_sync():
    adapter = SilvusAdapter("192.168.1.10", peer_port=8000)
    adapter.neighbours = lambda: [   # type: ignore[method-assign]
        {"ip": "10.0.0.2", "snr_db": 22.5, "hops": 1},
        {"ip": "10.0.0.3", "snr_db": 18.0, "hops": 2},
        {"ip": "10.0.0.9", "snr_db": 30.0, "hops": 0},   # our own node — must be skipped
    ]
    pm = _FakePeerMesh(existing=["http://10.0.0.2:8000"])   # already known — not re-added
    res = adapter.sync_peers(pm, self_ips={"10.0.0.9"})
    added = set(res["added"])
    ok = (added == {"http://10.0.0.3:8000"}
          and "http://10.0.0.9:8000" not in pm.peers
          and pm.peers.count("http://10.0.0.2:8000") == 1
          and res["links"]["10.0.0.3"] == 18.0)
    return ("silvus sync_peers", ok, f"added={sorted(added)}")


def main() -> int:
    tests = [test_meshtastic_fit, test_meshtastic_deliver, test_silvus_parse, test_silvus_sync]
    passed = 0
    print("=" * 72)
    print("Ares — Meshtastic + Silvus transport harness")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:28s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} transport tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
