"""Regression: the remote embeddings path must enforce the pgvector dimension.

The module promises "dimensionality always matches pgvector_embedding_dim so the
pgvector column and the query vector agree". The offline embedder honoured that;
the remote path returned whatever the API produced. A configured embedding model
whose native dimension differs from the column width would push wrong-width
vectors into the vector(N) column / <=> operator, breaking ingest and retrieval.

This pins that a dimension mismatch on the remote path degrades to the
correct-width offline embedder. Fully offline (the remote call is monkeypatched).
"""

from __future__ import annotations

from typing import Any

import pytest
from app.backend.tools.embeddings import _embed_remote, embed_texts
from app.shared.settings import Settings


def _remote_settings(dim: int) -> Settings:
    return Settings(
        embedding_model="text-embedding-x",
        llm_api_key="sk-test",
        pgvector_embedding_dim=dim,
    )


def test_embed_remote_rejects_wrong_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned vector of the wrong width raises (so the caller falls back)."""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            # Model returns 4-dim vectors; column expects 8.
            return {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}

    monkeypatch.setattr("app.backend.tools.embeddings.httpx.post", lambda *a, **k: _Resp())
    with pytest.raises(ValueError, match="dimension mismatch"):
        _embed_remote(["q"], _remote_settings(dim=8))


def test_embed_texts_falls_back_to_offline_on_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_texts degrades to the correct-width offline embedder on mismatch."""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}

    monkeypatch.setattr("app.backend.tools.embeddings.httpx.post", lambda *a, **k: _Resp())
    vectors = embed_texts(["q"], _remote_settings(dim=8))
    assert len(vectors) == 1
    assert len(vectors[0]) == 8  # offline embedder honours the configured dim


def test_embed_remote_accepts_matching_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.0] * 8}]}

    monkeypatch.setattr("app.backend.tools.embeddings.httpx.post", lambda *a, **k: _Resp())
    vectors = _embed_remote(["q"], _remote_settings(dim=8))
    assert len(vectors[0]) == 8
