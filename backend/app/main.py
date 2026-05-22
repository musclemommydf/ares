"""
Ares — FastAPI Application Entry Point
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI
from fastapi.encoders import ENCODERS_BY_TYPE
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Teach FastAPI's JSON encoder how to handle numpy scalar types
ENCODERS_BY_TYPE[np.bool_]    = bool
ENCODERS_BY_TYPE[np.integer]  = int
ENCODERS_BY_TYPE[np.floating] = float
ENCODERS_BY_TYPE[np.ndarray]  = list

from app.config import settings
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.api.system_routes import router as system_router
from app.api.atak_routes import router as atak_router
from app.api.geo_routes import router as geo_router
from app.api.sdr_routes import router as sdr_router
from app.api.df_routes import router as df_router
from app.api.algorithms_routes import router as algorithms_router
from app.api.targets_routes import router as targets_router
from app.api.cellular_routes import router as cellular_router
from app.api.chat_routes import router as chat_router
from app.api.uas_routes import router as uas_router
from app.api.osint_routes import router as osint_router
from app.core.auth import ensure_default_user
from app.core.simulation import periodic_cache_cleanup, purge_all_stale_caches
from app.core.sdr import sdr_manager

# Logging
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown."""
    log.info(f"Starting {settings.app_name} v{settings.app_version}")
    if settings.auth_enabled:
        ensure_default_user()
    else:
        log.warning("Auth is DISABLED (ARES_AUTH=false) — fine for localhost/dev, "
                    "set ARES_AUTH=true for networked / field / ATAK deployments")

    # Initial cache cleanup on startup
    purge_all_stale_caches()

    # Start background cache cleanup task (runs every 24h)
    cleanup_task = asyncio.create_task(periodic_cache_cleanup(24.0))

    # SDR / DF manager — start adapters for every enabled device + wire the
    # auto-coverage runner so a new fix triggers a coverage simulation centred
    # on the emitter (Workstream D). If SoapySDR is installed, register it as the
    # live spectrum provider (else the synthetic one stays).
    try:
        from app.core.sdr import soapy
        soapy.register()
    except Exception:
        log.debug("SoapySDR shim unavailable", exc_info=True)
    # Native IQ capture — feeds the UAS-video software demod and the in-process DF/AoA solver
    # straight from the connected SDR(s) (SignalHound / USRP / Epiq Sidekiq / RTL-SDR via SoapySDR);
    # a no-op when SoapySDR isn't installed (the synthetic-IQ fallbacks then stay).
    try:
        from app.core.sdr import iq_capture
        iq_capture.register()
    except Exception:
        log.debug("native IQ capture unavailable", exc_info=True)
    sdr_manager.set_auto_coverage_runner(_auto_coverage_from_fix)
    await sdr_manager.start()
    # Distributed sensing — connect to any peer Ares nodes on the MANET, fuse
    # their LoBs into the local solver, and relay group chat. Listen on the CoT
    # multicast/UDP so inbound ATAK GeoChat joins the same conversation (Workstream D).
    try:
        from app.core.sdr.mesh import peer_mesh
        peer_mesh.set_lob_sink(sdr_manager._on_lob)
        await peer_mesh.start()
    except Exception:
        log.debug("peer mesh unavailable", exc_info=True)
    try:
        from app.core import cot
        await cot.start_cot_listener()
    except Exception:
        log.debug("CoT listener unavailable", exc_info=True)

    # Continuous track→CoT heartbeat: every active emitter track from the
    # bundled trackers (Kalman + GM-PHD) is re-published on a heartbeat so
    # connected ATAK clients see persistent tracks even between fresh fixes.
    track_cot_task = None
    try:
        from app.core import track_cot_bridge
        track_cot_task = asyncio.create_task(track_cot_bridge.run(interval_s=2.0))
        log.info("track→CoT bridge started (2s heartbeat)")
    except Exception:
        log.debug("track→CoT bridge unavailable", exc_info=True)

    yield

    # Shutdown
    if track_cot_task is not None:
        track_cot_task.cancel()
        try: await track_cot_task
        except (asyncio.CancelledError, Exception): pass
    try:
        from app.core.sdr.mesh import peer_mesh
        await peer_mesh.stop()
        from app.core import cot
        await cot.stop_cot_listener()
    except Exception:
        pass
    await sdr_manager.stop()
    try:
        from app.core.sdr.tap_nic import nic_manager
        nic_manager.stop_all()           # tear down any SDR-as-NIC interfaces
    except Exception:
        pass
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    log.info("Shutdown complete")


async def _auto_coverage_from_fix(group: dict) -> None:
    """Run a coverage simulation centred on a newly-computed emitter fix and
    broadcast the resulting GeoJSON over the SDR WS stream as a `coverage`
    event. Conservative defaults — small radius, modest radial count — so it
    stays cheap even when fixes update every few seconds."""
    import time as _time
    centroid = group.get("centroid") or {}
    if "lat" not in centroid or "lon" not in centroid:
        return
    freq = float(group.get("frequency_hz") or 433e6)
    try:
        from app.core.simulation import (
            get_simulator, CoverageRequest, TransmitterConfig, ReceiverConfig,
        )
        req = CoverageRequest(
            transmitter=TransmitterConfig(
                lat=float(centroid["lat"]), lon=float(centroid["lon"]),
                height_m=2.0, power_dbm=33.0, frequency_hz=freq,
            ),
            receiver=ReceiverConfig(height_m=1.5),
            radius_km=10.0, num_radials=144, points_per_radial=200,
            fetch_space_weather=False,
        )
        result = await get_simulator().compute_coverage(req)
        sdr_manager._broadcast({
            "type": "coverage", "from_fix": True, "frequency_hz": freq,
            "centroid": centroid, "geojson": result.geojson, "t": _time.time(),
        })
    except Exception:
        log.debug("auto-coverage failed (group=%s)", group.get("kind"), exc_info=True)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Terrain-based RF propagation simulator. "
        "Supports ITM/Longley-Rice, Hata, COST-231, Two-Ray, ITU-R P.452/528/1546, "
        "and more. Auto-downloads SRTM terrain data. "
        "Real-time space weather corrections from NOAA SWPC."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Per-IP rate limiting (security pass) — generous default, tighter on /simulate & /packs/download.
from app.core.security import rate_limit_middleware  # noqa: E402
app.middleware("http")(rate_limit_middleware)

# Routes
app.include_router(router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(atak_router, prefix="/api/v1")
app.include_router(geo_router, prefix="/api/v1")
app.include_router(sdr_router, prefix="/api/v1")
app.include_router(df_router, prefix="/api/v1")
app.include_router(algorithms_router, prefix="/api/v1")
app.include_router(targets_router, prefix="/api/v1")
app.include_router(cellular_router, prefix="/api/v1")
app.include_router(chat_router, prefix="/api/v1")
app.include_router(uas_router, prefix="/api/v1")
app.include_router(osint_router, prefix="/api/v1")


_API_INFO = {
    "name": settings.app_name,
    "version": settings.app_version,
    "docs": "/docs",
    "api": "/api/v1",
}


@app.get("/api")
async def api_info():
    return _API_INFO


# Serve the built web UI at "/" so any device on the network (laptop / phone /
# tablet) can drive Ares from a browser at http://<host>:<port>/. It loads
# same-origin, so the existing relative /api/v1 + WebSocket calls Just Work and
# ARES_AUTH protects the whole surface. Registered AFTER the API routers, so they
# always win; this catch-all only handles SPA + static assets.
from pathlib import Path as _Path  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_DIST = _Path(settings.frontend_dist)
if _DIST.is_dir() and (_DIST / "index.html").is_file():
    for _sub in ("assets", "cesium"):
        _d = _DIST / _sub
        if _d.is_dir():
            app.mount(f"/{_sub}", StaticFiles(directory=str(_d)), name=_sub)

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith(("api/", "api", "docs", "redoc", "openapi.json")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        f = _DIST / full_path
        if full_path and f.is_file():
            return FileResponse(str(f))
        return FileResponse(str(_DIST / "index.html"))   # SPA entry / client routes

    log.info("serving web UI from %s", _DIST)
else:
    @app.get("/")
    async def root():
        return _API_INFO

    log.warning("no built web UI at %s — API only (run `npm run build` in frontend/)", _DIST)


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=settings.debug,
            workers=1,  # Single worker for async; use gunicorn for production
        )
    except KeyboardInterrupt:
        pass  # clean Ctrl+C exit — no traceback (uvicorn has already shut down gracefully)
