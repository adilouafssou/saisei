"""Tests for the offline embedding backend used by pgvector long-term memory."""

from __future__ import annotations

import math

from app.backend.tools.embeddings import embed_text, embed_texts
from app.backend.tools.retrieval import _to_pgvector_literal
from app.shared.settings import Settings


def _offline_settings(dim: int = 32) -> Settings:
    """Settings with no embedding model -> deterministic offline embedder."""
    return Settings(embedding_model="", llm_api_key="", pgvector_embedding_dim=dim)


def test_offline_embedding_has_configured_dimension() -> None:
    settings = _offline_settings(dim=64)
    vec = embed_text("genka koutou kakaku tenka", settings)
    assert len(vec) == 64


def test_offline_embedding_is_deterministic() -> None:
    settings = _offline_settings()
    assert embed_text("same text", settings) == embed_text("same text", settings)


def test_offline_embedding_is_l2_normalised_when_nonempty() -> None:
    settings = _offline_settings()
    vec = embed_text("price pass-through strategy", settings)
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, abs_tol=1e-9)


def test_offline_embedding_empty_text_is_zero_vector() -> None:
    settings = _offline_settings(dim=16)
    assert embed_text("", settings) == [0.0] * 16


def test_embed_texts_preserves_order_and_count() -> None:
    settings = _offline_settings()
    vectors = embed_texts(["a", "b", "c"], settings)
    assert len(vectors) == 3
    # Distinct inputs should generally produce distinct vectors.
    assert vectors[0] != vectors[1]


def test_to_pgvector_literal_format() -> None:
    assert _to_pgvector_literal([0.0, 1.0, -2.5]) == "[0.0,1.0,-2.5]"
