"""
Ares — Configuration
"""
import os
import secrets
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TERRAIN_CACHE_DIR = DATA_DIR / "terrain"
BUILDINGS_CACHE_DIR = DATA_DIR / "buildings"

# Offline data packs (Workstream A): terrain / osm / buildings / clutter / imagery
PACKS_DIR = DATA_DIR / "packs"
PACK_LAYERS = ("terrain", "osm", "buildings", "clutter", "imagery")

# Ensure cache directories exist
TERRAIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
BUILDINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
for _layer in PACK_LAYERS:
    (PACKS_DIR / _layer).mkdir(parents=True, exist_ok=True)


def _default_auth_secret() -> str:
    """Persist a random signing secret to data/.auth_secret unless ARES_AUTH_SECRET is set."""
    env = os.getenv("ARES_AUTH_SECRET")
    if env:
        return env
    f = DATA_DIR / ".auth_secret"
    if f.exists():
        return f.read_text().strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    s = secrets.token_urlsafe(48)
    f.write_text(s)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return s


def _resolve_auth_enabled() -> bool:
    """ARES_AUTH: 'true' | 'false' | 'auto' (default 'auto'). 'auto' ⇒ auth ON unless
    the server is bound to a loopback address (so localhost dev is frictionless but a
    networked / field deployment is authenticated out of the box)."""
    v = os.getenv("ARES_AUTH", "auto").strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    host = os.getenv("HOST", "0.0.0.0").strip()
    return host not in ("127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1")


class Settings(BaseSettings):
    app_name: str = "Ares"
    app_version: str = "2.0.0"
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    cors_origins: list[str] = ["*"]

    # Authentication (Workstream A.1). ARES_AUTH = true | false | auto (default).
    # 'auto' ⇒ ON unless bound to loopback — networked deployments are authenticated
    # by default; localhost dev is open. Force either way with true/false.
    auth_enabled: bool = _resolve_auth_enabled()
    auth_secret: str = _default_auth_secret()
    # Auth backend: "local" (data/users.json), "ldap" (LDAP/AD bind only), or
    # "ldap+local" (try local users first, then LDAP). LDAP needs the `ldap3` pkg;
    # if it isn't installed Ares logs a warning and behaves as "local".
    auth_backend: str = os.getenv("ARES_AUTH_BACKEND", "local")
    ldap_server: str = os.getenv("ARES_LDAP_SERVER", "")          # e.g. ldaps://dc.corp.example.com
    ldap_user_dn_template: str = os.getenv("ARES_LDAP_USER_DN", "")  # e.g. "uid={username},ou=people,dc=example,dc=com" or AD UPN "{username}@corp.example.com"
    ldap_admin_group: str = os.getenv("ARES_LDAP_ADMIN_GROUP", "")   # optional group DN; members get role=admin
    ldap_base_dn: str = os.getenv("ARES_LDAP_BASE_DN", "")           # search base for the group-membership check

    # Network policy (Workstream A.3): auto = use local packs, fall back to online
    # fetch when reachable and cache the result; online_only / offline_only force it.
    network_policy: str = os.getenv("ARES_NETWORK_POLICY", "auto")  # auto|online_only|offline_only

    # ATAK / TAK-server integration master switch (data packs / templates / KMZ export /
    # CoT push). Persisted to data/.atak_enabled; toggled at runtime via the web console.
    atak_enabled: bool = os.getenv("ARES_ATAK", "true").lower() != "false"

    # Terrain data sources
    srtm_url: str = "https://srtm.csi.cgiar.org/wp-content/uploads/files/srtm_5x5/TIFF/"
    copernicus_url: str = "https://opentopography.s3.sdsc.edu/raster/COP30/COP30_hh/"
    terrain_resolution_m: int = 90  # 90m SRTM or 30m

    # Space weather
    noaa_swpc_url: str = "https://services.swpc.noaa.gov"

    # OpenStreetMap
    overpass_url: str = "https://overpass-api.de/api/interpreter"

    # Coverage calculation defaults
    default_radius_km: float = 50.0
    default_radials: int = 360
    default_points_per_radial: int = 500
    max_radius_km: float = 2000.0
    max_frequency_hz: float = 300e9  # 300 GHz (THz range)

    # Emitter/Transmitter defaults
    default_emitter_agl_m: float = 1.8288  # 6 feet AGL default

    # ITM defaults
    itm_climate: int = 5  # Continental temperate
    itm_polarization: int = 0  # Horizontal
    itm_situation_variability: float = 0.5
    itm_time_variability: float = 0.5
    itm_location_variability: float = 0.5

    class Config:
        env_file = ".env"


settings = Settings()
