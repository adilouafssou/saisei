"""Regression: the Postgres checkpointer path must resolve the DSN via the seam.

The production (persist_checkpoints=True) checkpointer reads the Postgres DSN
through ``resolve_secret`` so an ``@env:`` / ``@file:`` reference dereferences
before connecting. ``graph.py`` once called ``resolve_secret(...)`` WITHOUT
importing it, so opening the Postgres checkpointer raised ``NameError`` on its
first line -- a production-only break invisible to CI, which runs the offline
MemorySaver path (persist_checkpoints=False) and never enters
``postgres_checkpointer``.

These pin both halves of the fix, fully offline (no DB): the import exists, and
``postgres_checkpointer`` actually routes the DSN through ``resolve_secret``
without raising -- with ``resolve_secret`` and ``PostgresSaver`` monkeypatched
so no network/DB is touched.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import app.backend.graph as graph_mod
from app.backend.graph import postgres_checkpointer
from app.shared.settings import Settings


def test_graph_imports_resolve_secret_from_the_seam() -> None:
    """graph.py imports resolve_secret, so the DSN flows through the secret seam.

    Mirrors test_migrations.py::test_env_resolves_dsn_through_secret_seam: a
    source-level pin so the import can't be dropped again while the
    ``resolve_secret(...)`` call remains.
    """
    src = Path(graph_mod.__file__).read_text(encoding="utf-8")
    assert "from app.backend.secrets import resolve_secret" in src
    assert "resolve_secret(get_settings().postgres_dsn)" in src


def test_resolve_secret_is_bound_in_graph_namespace() -> None:
    """The name is actually defined in the module namespace (not just in source)."""
    assert hasattr(graph_mod, "resolve_secret")


def test_postgres_checkpointer_resolves_dsn_without_nameerror(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """postgres_checkpointer() routes the DSN through resolve_secret and opens.

    Fully offline: resolve_secret is replaced with a recorder, PostgresSaver is
    stubbed so no DB connection is attempted, and get_settings returns a
    reference-style DSN. Before the fix this raised NameError on the
    ``resolve_secret(...)`` line; now it must resolve the reference and yield the
    stub saver.
    """
    seen: dict[str, str] = {}

    def _fake_resolve(value: str) -> str:
        seen["in"] = value
        return "postgresql://resolved-host/saisei"

    class _StubSaver:
        def setup(self) -> None:  # no-op; no DB.
            seen["setup"] = "called"

        @classmethod
        @contextmanager
        def from_conn_string(cls, dsn: str):  # type: ignore[no-untyped-def]
            seen["dsn"] = dsn
            yield cls()

    monkeypatch.setattr(graph_mod, "resolve_secret", _fake_resolve)
    monkeypatch.setattr(graph_mod, "PostgresSaver", _StubSaver)
    monkeypatch.setattr(
        graph_mod,
        "get_settings",
        lambda: Settings(postgres_dsn="@env:SAISEI_PG_DSN"),
    )

    with postgres_checkpointer() as cp:
        assert isinstance(cp, _StubSaver)

    # The configured reference was passed to resolve_secret, and the RESOLVED
    # value (not the raw reference) reached PostgresSaver.from_conn_string.
    assert seen["in"] == "@env:SAISEI_PG_DSN"
    assert seen["dsn"] == "postgresql://resolved-host/saisei"
    assert seen["setup"] == "called"
