# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
store.py — durable, cross-process persistence for Ares.

A single SQLite database under ``$ARES_DATA_DIR/ares.db`` in WAL mode, so several
uvicorn workers (and the CLI) share one consistent store: WAL gives concurrent
readers + a single writer across processes, which is all Ares' low write rate
needs. Connections are opened per call (SQLite is happiest that way under
threads) with a busy timeout so concurrent writers retry instead of erroring.

Tables:
  * ``saved_results`` — simulation-result snapshots (was browser localStorage).
  * ``kv``            — generic JSON key/value for small durable settings/state.

Live hardware sessions (SDR / DF / RID / cellular captures) are deliberately
*not* stored here: a capture is bound to the process that owns the radio, so it
can't meaningfully outlive a restart or be shared across workers. Their derived
*products* (tracks, results) are what persist — track archive as JSON files,
results here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import logging

log = logging.getLogger(__name__)

_INIT_LOCK = threading.Lock()
_INITED = False


def db_path() -> Path:
    return Path(os.environ.get("ARES_DATA_DIR", "data")) / "ares.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=15.0, isolation_level=None)  # autocommit
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables once. Safe to call repeatedly / from multiple workers."""
    global _INITED
    with _INIT_LOCK:
        if _INITED:
            return
        with _connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS saved_results (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    project     TEXT NOT NULL DEFAULT 'Default',
                    type        TEXT NOT NULL DEFAULT 'coverage',
                    created     REAL NOT NULL,
                    point_count INTEGER NOT NULL DEFAULT 0,
                    params      TEXT,   -- JSON
                    results     TEXT,   -- JSON (metadata/warnings/p2pResult)
                    geojson     TEXT    -- JSON FeatureCollection
                );
                CREATE INDEX IF NOT EXISTS ix_saved_results_project ON saved_results(project);
                CREATE INDEX IF NOT EXISTS ix_saved_results_created ON saved_results(created);

                CREATE TABLE IF NOT EXISTS kv (
                    k       TEXT PRIMARY KEY,
                    v       TEXT,       -- JSON
                    updated REAL NOT NULL
                );
                """
            )
        _INITED = True
        log.info("store: SQLite ready at %s", db_path())


def _ensure() -> None:
    if not _INITED:
        init_db()


# ── saved results ────────────────────────────────────────────────────────────
def _row_summary(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "name": r["name"], "project": r["project"], "type": r["type"],
        "created": r["created"], "point_count": r["point_count"],
    }


def _loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def list_saved_results() -> list[dict]:
    """Lightweight list (no heavy geojson) for the catalog UI, newest first."""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT id,name,project,type,created,point_count FROM saved_results ORDER BY created DESC"
        ).fetchall()
    return [_row_summary(r) for r in rows]


def get_saved_result(rid: str) -> Optional[dict]:
    """Full entry incl. params / results / geojson — used on load."""
    _ensure()
    with _connect() as c:
        r = c.execute("SELECT * FROM saved_results WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    out = _row_summary(r)
    out["params"] = _loads(r["params"]) or {}
    out["results"] = _loads(r["results"]) or {}
    out["geojson"] = _loads(r["geojson"])
    return out


def save_result(*, name: str, project: str = "Default", type: str = "coverage",
                params: Optional[dict] = None, results: Optional[dict] = None,
                geojson: Optional[dict] = None, rid: Optional[str] = None,
                created: Optional[float] = None) -> dict:
    """Insert (or replace, if ``rid`` already exists) a result snapshot."""
    _ensure()
    rid = rid or uuid.uuid4().hex[:12]
    created = float(created if created is not None else time.time())
    pc = int(len((geojson or {}).get("features") or [])) if isinstance(geojson, dict) else 0
    with _connect() as c:
        c.execute(
            """INSERT INTO saved_results (id,name,project,type,created,point_count,params,results,geojson)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, project=excluded.project, type=excluded.type,
                 point_count=excluded.point_count, params=excluded.params,
                 results=excluded.results, geojson=excluded.geojson""",
            (rid, name.strip() or "Untitled", (project or "Default").strip() or "Default",
             type or "coverage", created, pc,
             json.dumps(params or {}), json.dumps(results or {}),
             json.dumps(geojson) if geojson is not None else None),
        )
    return {"id": rid, "name": name, "project": project or "Default", "type": type,
            "created": created, "point_count": pc}


def delete_saved_result(rid: str) -> bool:
    _ensure()
    with _connect() as c:
        cur = c.execute("DELETE FROM saved_results WHERE id=?", (rid,))
        return cur.rowcount > 0


# ── generic KV ───────────────────────────────────────────────────────────────
def kv_get(key: str, default: Any = None) -> Any:
    _ensure()
    with _connect() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(r["v"]) if r else default


def kv_set(key: str, value: Any) -> None:
    _ensure()
    with _connect() as c:
        c.execute(
            "INSERT INTO kv (k,v,updated) VALUES (?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
            (key, json.dumps(value), time.time()),
        )
