"""Reflex configuration for the Saisei frontend.

``api_url`` is the backend/event endpoint baked into the frontend bundle at
build time, so it must be the address the *browser* can reach. The value is
derived by the single-source platform module (``app/shared/platform.py``),
which auto-detects local / Lightning.ai / Hugging Face and honours an explicit
``API_URL`` override:

- Local:        http://localhost:3000
- Lightning.ai: https://3000-<cloudspace-id>.cloudspaces.litng.ai
- Hugging Face: https://<SPACE_HOST>

The Radix Themes plugin is configured explicitly (it backs the component set
used across the meeting-room UI); this also silences the implicit-enablement
deprecation warning emitted from Reflex 0.9+.
"""

import sys
from pathlib import Path

import reflex as rx

# rxconfig.py runs from the repo root at build time, before the `app` package is
# guaranteed to be importable as an installed dist. Ensure the repo root is on
# sys.path so the stdlib-only platform module can be imported directly.
sys.path.insert(0, str(Path(__file__).parent))

from app.shared.platform import resolve_api_url  # noqa: E402

config = rx.Config(
    app_name="app",
    api_url=resolve_api_url(),
    vite_allowed_hosts=True,
    cors_allowed_origins=["*"],
    plugins=[rx.plugins.RadixThemesPlugin()],
)
