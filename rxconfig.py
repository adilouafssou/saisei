"""Reflex configuration for the Saisei frontend.

``api_url`` is the backend/event endpoint baked into the frontend bundle at
build time, so it must be the address the *browser* can reach:

- Local:        http://localhost:8000
- Lightning.ai: https://8000-<cloudspace-id>.cloudspaces.litng.ai

The value is resolved by ``scripts/setup_env.sh`` (which auto-detects
Lightning.ai) and exported as ``API_URL`` / written to ``.env``. We read the
env var first and fall back to parsing ``.env`` so this works even when the
var was not exported into the current shell.

The Radix Themes plugin is configured explicitly (it backs the component set
used across the meeting-room UI); this also silences the implicit-enablement
deprecation warning emitted from Reflex 0.9+.
"""

import os
from pathlib import Path

import reflex as rx

_DEFAULT_API_URL = "http://localhost:8000"


def _resolve_api_url() -> str:
    """Return API_URL from the environment, falling back to .env, then default."""
    value = os.environ.get("API_URL")
    if value:
        return value

    env_file = Path(__file__).parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, _, val = line.partition("=")
            if key.strip() == "API_URL" and val.strip():
                return val.strip()

    return _DEFAULT_API_URL


config = rx.Config(
    app_name="app",
    api_url=_resolve_api_url(),
    vite_allowed_hosts=True,
    cors_allowed_origins=["*"],
    plugins=[rx.plugins.RadixThemesPlugin()],
)
