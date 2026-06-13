"""Bundled fixture data for the Saisei mock data clients.

Fixtures are shipped inside the ``app`` package so the application does not
depend on the legacy top-level ``mocks/fixtures/`` directory at runtime.
The canonical path is resolved via :data:`FIXTURES_DIR`.
"""

from pathlib import Path

#: Absolute path to the bundled fixtures directory.
FIXTURES_DIR: Path = Path(__file__).parent
