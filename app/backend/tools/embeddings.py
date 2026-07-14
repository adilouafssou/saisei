"""Embeddings for long-term agent memory (pgvector).

Turns text into a fixed-length vector for similarity search. Two backends, same
contract — mirroring the project's determinism-first, offline-capable stance:

* **Configured** (``SAISEI_EMBEDDING_MODEL`` set): call an OpenAI-compatible
  Embeddings API (reusing the ``llm_*`` base URL / key).
* **Offline default** (model unset): a deterministic hashing embedder so ingest
  and retrieval work with no network and ``make verify`` stays green. It is NOT
  semantically meaningful — it exists so the pgvector SQL path is exercisable
  end to end offline; production sets a real model.

The vector dimensionality always matches ``settings.pgvector_embedding_dim`` so
the pgvector column and the query vector agree.

This module is the canonical location under ``app.backend.tools.embeddings``.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

import httpx

from app.backend.secrets import resolve_secret
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["embed_text", "embed_texts"]

_log = get_logger(__name__)


def embed_texts(texts: list[str], settings: Settings | None = None) -> list[list[float]]:
    """Embed a batch of texts into vectors of ``pgvector_embedding_dim`` length.

    Uses the configured Embeddings API when ``embedding_model`` is set; otherwise
    a deterministic offline embedder. Always returns one vector per input.

    Args:
        texts: The texts to embed.
        settings: Optional settings override (defaults to cached settings).

    Returns:
        One embedding vector per input text.
    """
    settings = settings or get_settings()
    dim = settings.pgvector_embedding_dim
    if not texts:
        return []
    if settings.embedding_model and resolve_secret(settings.llm_api_key):
        try:
            return _embed_remote(texts, settings)
        except Exception as exc:  # noqa: BLE001 - degrade to offline embedder
            _log.warning("embeddings.remote_failed", error=str(exc))
    return [_embed_offline(text, dim) for text in texts]


def embed_text(text: str, settings: Settings | None = None) -> list[float]:
    """Embed a single text (convenience wrapper over :func:`embed_texts`)."""
    return embed_texts([text], settings=settings)[0]


def _embed_remote(texts: list[str], settings: Settings) -> list[list[float]]:
    """Call an OpenAI-compatible Embeddings API; raises on any error.

    Validates that every returned vector has exactly
    ``settings.pgvector_embedding_dim`` components. A configured embedding model
    whose native dimension differs from the pgvector column width would otherwise
    yield wrong-width vectors that the ``vector(N)`` column / ``<=>`` operator
    rejects (or, worse, silently corrupt retrieval) — violating this module's
    documented "dimensionality always matches pgvector_embedding_dim" contract.
    Raising here degrades (via the caller's except) to the offline embedder,
    which always builds correct-width vectors, and logs the misconfiguration.
    """
    url = f"{settings.llm_base_url.rstrip('/')}/embeddings"
    payload: dict[str, Any] = {"model": settings.embedding_model, "input": texts}
    headers = {"Authorization": f"Bearer {resolve_secret(settings.llm_api_key)}"}
    response = httpx.post(url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds)
    response.raise_for_status()
    data = response.json()
    try:
        vectors = [item["embedding"] for item in data["data"]]
    except (KeyError, TypeError) as exc:
        raise ValueError("Unexpected embeddings response shape") from exc
    if len(vectors) != len(texts):
        raise ValueError("Embeddings count does not match input count")
    dim = settings.pgvector_embedding_dim
    for vec in vectors:
        if not isinstance(vec, list) or len(vec) != dim:
            raise ValueError(
                f"embedding dimension mismatch: model returned "
                f"{len(vec) if isinstance(vec, list) else 'non-list'}, "
                f"pgvector_embedding_dim is {dim}"
            )
    return [[float(x) for x in vec] for vec in vectors]


def _embed_offline(text: str, dim: int) -> list[float]:
    """Deterministic, network-free embedding (L2-normalised hashed bag-of-tokens).

    Not semantically meaningful; it gives the pgvector SQL path a stable, valid
    vector so the system is exercisable end to end offline. Same text always
    yields the same vector, so idempotent upserts stay idempotent.
    """
    vec = [0.0] * dim
    tokens = text.lower().split()
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        # Use two bytes per bucket for a stable index, and the next byte's sign.
        idx = int.from_bytes(digest[:2], "big") % dim
        weight = (digest[2] / 255.0) * 2.0 - 1.0
        vec[idx] += weight
    norm = math.sqrt(sum(component * component for component in vec))
    if norm == 0.0:
        return vec
    return [component / norm for component in vec]
