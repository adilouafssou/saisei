"""Single source of truth for runtime-platform detection and derived config.

The Saisei app must run unchanged across very different topologies:

=================  =========================  ============================
Platform           Topology                   Public URL / DB
=================  =========================  ============================
local / compose    Nginx + app + PG + Redis   http://localhost:3000 + PG
Lightning.ai       same compose stack,        https://3000-<host> + PG
                   forwarded port
Hugging Face       single container, no        https://<SPACE_HOST> + none
                   proxy, no DB                (in-memory checkpointer)
generic            unknown remote host        explicit API_URL expected
=================  =========================  ============================

Rather than scatter ``if SPACE_HOST`` / ``if LIGHTNING_*`` checks across
``rxconfig.py``, ``settings.py`` and ``scripts/setup_env.sh``, this module
detects the platform ONCE from environment variables and derives everything
else from it:

* :func:`detect_platform` -> the :class:`Platform` enum.
* :func:`resolve_api_url` -> the browser-reachable backend URL (baked into the
  Reflex bundle by ``rxconfig.py``).
* :func:`should_persist_checkpoints` -> whether to use the durable Postgres
  checkpointer or the in-process ``MemorySaver`` (DB-less hosting).

Design rule: **automatic by default, explicit override always wins.** Every
derivation honours an explicit env var first, then falls back to a sane,
platform-aware default, so a fresh clone "just works" on any platform with zero
manual configuration, while power users can still pin any value.

This module has NO third-party imports (only stdlib) so it is safe to import
from both ``rxconfig.py`` (build-time, before deps are guaranteed) and the
application settings.
"""

from __future__ import annotations

import enum
import os

__all__ = [
    "Platform",
    "detect_platform",
    "resolve_api_url",
    "should_persist_checkpoints",
    "DEFAULT_PROXY_PORT",
    "DEFAULT_BACKEND_PORT",
]

#: The Nginx-published port the browser uses on the compose stack (see
#: docker-compose.yml + nginx.conf). Lightning.ai forwards this same port.
DEFAULT_PROXY_PORT = 3000

#: The Reflex backend/event port (FastAPI + WebSocket /_event).
DEFAULT_BACKEND_PORT = 8000

_LITNG_SUFFIX = "cloudspaces.litng.ai"


class Platform(enum.StrEnum):
    """The runtime hosting platform, detected from the environment."""

    LOCAL = "local"
    LIGHTNING = "lightning"
    HUGGINGFACE = "huggingface"
    GENERIC = "generic"  # an unrecognised remote host (explicit config expected)


def _truthy(value: str | None) -> bool | None:
    """Parse a tri-state boolean env var: True / False / None (unset or blank)."""
    if value is None or value == "":
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def detect_platform() -> Platform:
    """Detect the hosting platform from well-known environment variables.

    Precedence:
      1. Explicit ``SAISEI_PLATFORM`` override (local|lightning|huggingface|generic).
      2. Hugging Face Spaces  -> ``SPACE_HOST`` / ``SPACE_ID`` present.
      3. Lightning.ai         -> ``LIGHTNING_CLOUD_SPACE_*`` present.
      4. Local                -> default.
    """
    override = os.environ.get("SAISEI_PLATFORM")
    if override:
        try:
            return Platform(override.strip().lower())
        except ValueError:
            return Platform.GENERIC

    if os.environ.get("SPACE_HOST") or os.environ.get("SPACE_ID"):
        return Platform.HUGGINGFACE
    if os.environ.get("LIGHTNING_CLOUD_SPACE_HOST") or os.environ.get("LIGHTNING_CLOUD_SPACE_ID"):
        return Platform.LIGHTNING
    return Platform.LOCAL


def _strip_scheme(host: str) -> str:
    """Remove a leading scheme and trailing slash from a host string."""
    host = host.removeprefix("https://").removeprefix("http://")
    return host.rstrip("/")


def resolve_api_url(platform: Platform | None = None) -> str:
    """Return the browser-reachable backend URL for the current platform.

    Precedence:
      1. Explicit ``API_URL`` env var (always wins).
      2. Platform-derived default:
         * Hugging Face -> ``https://<SPACE_HOST>``  (HTTPS:443, no port prefix)
         * Lightning.ai -> ``https://<proxy_port>-<space-host>``
         * local        -> ``http://localhost:<proxy_port>``
      3. A persisted ``.env`` ``API_URL=`` line (compose/local convenience).
      4. localhost default.

    The proxy port can be overridden with ``SAISEI_PROXY_PORT`` for the compose
    / Lightning paths (it must match the Nginx ``listen`` port).
    """
    explicit = os.environ.get("API_URL")
    if explicit:
        return explicit

    platform = platform or detect_platform()
    proxy_port = os.environ.get("SAISEI_PROXY_PORT", str(DEFAULT_PROXY_PORT))

    if platform is Platform.HUGGINGFACE:
        space_host = os.environ.get("SPACE_HOST")
        if space_host:
            return f"https://{_strip_scheme(space_host)}"
        space_id = os.environ.get("SPACE_ID")
        if space_id:
            # SPACE_ID is "<owner>/<name>"; public host is <owner>-<name>.hf.space.
            slug = space_id.replace("/", "-")
            return f"https://{slug}.hf.space"

    if platform is Platform.LIGHTNING:
        litng_host = os.environ.get("LIGHTNING_CLOUD_SPACE_HOST")
        if not litng_host:
            space_id = os.environ.get("LIGHTNING_CLOUD_SPACE_ID")
            if space_id:
                litng_host = f"{space_id}.{_LITNG_SUFFIX}"
        if litng_host:
            return f"https://{proxy_port}-{_strip_scheme(litng_host)}"

    # .env convenience fallback (only meaningful on local / compose).
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    key, _, val = line.partition("=")
                    if key.strip() == "API_URL" and val.strip():
                        return val.strip()
        except OSError:
            pass

    return f"http://localhost:{proxy_port}"


def should_persist_checkpoints(
    postgres_dsn: str | None = None, platform: Platform | None = None
) -> bool:
    """Decide whether to use the durable Postgres checkpointer.

    Automatic-by-default with an explicit override:
      1. ``SAISEI_PERSIST_CHECKPOINTS`` (true/false) wins if set.
      2. Hugging Face (single container, no DB) -> False (in-memory).
      3. A Postgres DSN present AND not the bare localhost default -> True
         (a real DB was configured, so persist).
      4. Otherwise -> False (safe DB-less default; the app still runs).

    The localhost default DSN is treated as "no real DB configured" so a fresh
    clone or a remote host without a DB falls back to in-memory rather than
    crashing on a refused Postgres connection. The compose stack sets a real
    DSN (postgres:5432) via .env, which trips rule 3 and enables persistence.
    """
    override = _truthy(os.environ.get("SAISEI_PERSIST_CHECKPOINTS"))
    if override is not None:
        return override

    platform = platform or detect_platform()
    if platform is Platform.HUGGINGFACE:
        return False

    dsn = postgres_dsn if postgres_dsn is not None else os.environ.get("SAISEI_POSTGRES_DSN", "")
    return bool(dsn and "localhost" not in dsn)
