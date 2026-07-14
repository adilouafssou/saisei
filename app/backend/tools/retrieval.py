"""Retrieval seam for feasibility-critic RAG — two-tier agent memory.

Advisory-only retrieval of precedent context — past Keikakusho, industry
benchmarks, and FSA-manual passages — used to enrich the feasibility critic's
*advisory* note. It NEVER feeds a deterministic band, score, gate, or route.

MEMORY METAPHOR
---------------
The agents' precedent recall is modelled as human-like memory with two tiers,
each mapped onto infrastructure the stack already runs:

* **Long-term memory -> pgvector** (:class:`PgVectorLongTermMemory`).
  The durable knowledge base: every precedent the agents have ever learned,
  embedded and persisted in Postgres. Slower, comprehensive, survives restarts.

* **Short-term memory -> RediSearch** (:class:`RediSearchShortTermMemory`).
  A fast, ephemeral recall cache in Redis with a TTL: the precedents the agents
  have touched *recently*. Queried first; entries expire, which is exactly what
  makes it "short-term".

:class:`TwoTierRetrievalProvider` wires them together: consult short-term memory
first (cheap, hot), fall back to long-term memory on a miss, then *write back*
the long-term hits into short-term memory so the next similar query is fast.

Design mirrors :class:`app.backend.tools.provider.MockDataProvider`: a swappable
seam (the :class:`RetrievalProvider` protocol) with a deterministic mock default
so ``make verify`` stays green offline. Each backend is best-effort: on any
failure it degrades to ``[]`` so the advisory layer never breaks the workflow.

This module is the canonical location under ``app.backend.tools.retrieval``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "RetrievalSnippet",
    "RetrievalProvider",
    "MockRetrievalProvider",
    "PgVectorLongTermMemory",
    "RediSearchShortTermMemory",
    "TwoTierRetrievalProvider",
    "get_retrieval_provider",
    "_parse_memory_hits",
]

_log = get_logger(__name__)


class RetrievalSnippet(BaseModel):
    """A single retrieved precedent passage (advisory context only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(
        description="Origin label, e.g. 'past_keikakusho' | 'benchmark' | 'fsa_manual'."
    )
    text: str = Field(description="The retrieved passage text.")
    score: float = Field(default=0.0, description="Similarity score (higher = more relevant).")


@runtime_checkable
class RetrievalProvider(Protocol):
    """Swappable retrieval interface for the feasibility critic.

    Implementations must be best-effort and side-effect-free from the caller's
    perspective: on any failure they return ``[]`` so the advisory layer degrades
    gracefully (mirrors the ``polish_keikakusho`` offline-fallback contract).
    """

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        """Return up to ``top_k`` precedent snippets relevant to ``query``."""
        ...


class MockRetrievalProvider:
    """Deterministic, offline retrieval provider (default).

    Returns an empty list so the feasibility critic's advisory note degrades to
    the deterministic skeleton with no network. This keeps ``make verify`` green
    and is the safe default until agent memory (pgvector / RediSearch) is
    configured.
    """

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:  # noqa: ARG002
        """Return no precedents (deterministic offline fallback)."""
        _log.info("retrieval.mock.search", chars=len(query))
        return []


class PgVectorLongTermMemory:
    """LONG-TERM agent memory backed by pgvector (durable precedent corpus).

    This is the agents' durable knowledge base: the full set of precedents they
    have learned, embedded and persisted in Postgres via the pgvector extension.
    Comprehensive but slower than the short-term cache, and it survives process
    and container restarts.

    Recall is a direct SQL query over the precedent table using pgvector's cosine
    distance operator (``<=>``), against an embedding of the query produced by
    :mod:`app.backend.tools.embeddings`. Using ``psycopg`` (already a project
    dependency) and plain SQL — rather than a bespoke HTTP gateway — keeps this
    on the conventional, production path for pgvector.

    Best-effort: returns ``[]`` when pgvector is not configured or on any
    database / driver error, so the advisory layer never breaks the workflow.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _configured(self) -> bool:
        return bool(self._settings.pgvector_dsn)

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        """Recall precedent snippets from long-term memory; ``[]`` on failure."""
        if not self._configured():
            return []
        try:
            return self._query_pgvector(query, top_k)
        except Exception as exc:  # noqa: BLE001 - retrieval is best-effort
            _log.warning("memory.longterm.pgvector.failed", error=str(exc))
            return []

    def _query_pgvector(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        """Cosine-similarity search over the pgvector precedent table (SQL)."""
        import psycopg

        from app.backend.tools.embeddings import embed_text

        s = self._settings
        embedding = embed_text(query, s)
        vector_literal = _to_pgvector_literal(embedding)
        # 1 - cosine_distance gives a higher-is-better similarity score, matching
        # the RetrievalSnippet.score convention. Table/identifier names come from
        # trusted settings, not user input.
        sql = (
            f"SELECT text, source, 1 - (embedding <=> %s::vector) AS score "  # noqa: S608
            f"FROM {s.pgvector_table} "
            f"ORDER BY embedding <=> %s::vector "
            f"LIMIT %s"
        )
        snippets: list[RetrievalSnippet] = []
        with (
            psycopg.connect(
                s.pgvector_dsn, connect_timeout=int(s.retrieval_timeout_seconds)
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql, (vector_literal, vector_literal, top_k))
            rows: list[tuple[Any, Any, Any]] = cur.fetchall()
        for text, source, score in rows:
            if not isinstance(text, str) or not text.strip():
                continue
            snippets.append(
                RetrievalSnippet(
                    source=str(source) if source else "unknown",
                    text=text.strip(),
                    score=float(score) if score is not None else 0.0,
                )
            )
        _log.info("memory.longterm.pgvector.search", chars=len(query), hits=len(snippets))
        return snippets


class RediSearchShortTermMemory:
    """SHORT-TERM agent memory backed by RediSearch (fast, ephemeral cache).

    This is the agents' working recall: the precedents touched *recently*, held
    in a RediSearch vector index in Redis with a TTL. It is queried before
    long-term memory because it is hot and cheap; its entries expire, which is
    precisely what makes this tier "short-term".

    Best-effort everywhere: a miss or any error returns ``[]`` / is swallowed so
    the surrounding two-tier provider simply falls through to long-term memory.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _configured(self) -> bool:
        return bool(self._settings.redisearch_url)

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        """Recall recently-seen precedents from short-term memory; ``[]`` on miss."""
        if not self._configured():
            return []
        try:
            return self._query_redisearch(query, top_k)
        except Exception as exc:  # noqa: BLE001 - recall is best-effort
            _log.warning("memory.shortterm.redisearch.failed", error=str(exc))
            return []

    def remember(self, query: str, snippets: list[RetrievalSnippet]) -> None:
        """Write snippets into short-term memory so the next query is hot.

        Best-effort and silent on failure: caching is an optimisation, never a
        correctness requirement.
        """
        if not self._configured() or not snippets:
            return
        try:
            self._index_redisearch(query, snippets)
        except Exception as exc:  # noqa: BLE001 - write-back is best-effort
            _log.warning("memory.shortterm.redisearch.remember_failed", error=str(exc))

    def _query_redisearch(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        """Query the RediSearch short-term index and map the hits."""
        s = self._settings
        url = f"{s.redisearch_url.rstrip('/')}/indexes/{s.redisearch_index}/search"
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        headers = {"Content-Type": "application/json"}

        response = httpx.post(
            url, json=payload, headers=headers, timeout=s.retrieval_timeout_seconds
        )
        response.raise_for_status()
        hits = _parse_memory_hits(response.json())
        _log.info("memory.shortterm.redisearch.search", chars=len(query), hits=len(hits))
        return hits

    def _index_redisearch(self, query: str, snippets: list[RetrievalSnippet]) -> None:
        """Upsert snippets into the RediSearch index with the configured TTL."""
        s = self._settings
        url = f"{s.redisearch_url.rstrip('/')}/indexes/{s.redisearch_index}/upsert"
        payload: dict[str, Any] = {
            "query": query,
            "ttl_seconds": s.redisearch_ttl_seconds,
            "documents": [
                {"text": snip.text, "score": snip.score, "source": snip.source} for snip in snippets
            ],
        }
        headers = {"Content-Type": "application/json"}
        response = httpx.post(
            url, json=payload, headers=headers, timeout=s.retrieval_timeout_seconds
        )
        response.raise_for_status()
        _log.info(
            "memory.shortterm.redisearch.remember",
            chars=len(query),
            documents=len(snippets),
        )


class TwoTierRetrievalProvider:
    """Agent memory orchestrator: short-term (RediSearch) over long-term (pgvector).

    Implements the :class:`RetrievalProvider` protocol by modelling recall the
    way a person does:

    1. **Check short-term memory first** (RediSearch). If the agents have seen
       something relevant recently, return it immediately — fast and cheap.
    2. **Fall back to long-term memory** (pgvector) on a short-term miss: the
       durable corpus of everything ever learned.
    3. **Consolidate**: write the long-term hits back into short-term memory so
       the next similar query is served hot (memory "warming").

    Every tier is best-effort, so a missing or failing backend just degrades the
    result quality, never the workflow.
    """

    def __init__(
        self,
        short_term: RediSearchShortTermMemory,
        long_term: PgVectorLongTermMemory,
    ) -> None:
        self._short_term = short_term
        self._long_term = long_term

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        """Recall via short-term memory, falling back to long-term memory."""
        recent = self._short_term.search(query, top_k)
        if recent:
            _log.info("memory.recall", tier="short_term", hits=len(recent))
            return recent

        durable = self._long_term.search(query, top_k)
        if durable:
            _log.info("memory.recall", tier="long_term", hits=len(durable))
            # Consolidate long-term recall into short-term memory (warming).
            self._short_term.remember(query, durable)
        return durable


def _to_pgvector_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal, e.g. ``[0.1,0.2,0.3]``.

    pgvector accepts its vectors as a bracketed, comma-separated string cast to
    ``::vector``. Building the literal here keeps the SQL in one place.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _parse_memory_hits(data: Any) -> list[RetrievalSnippet]:
    """Map a memory-store search response into ``RetrievalSnippet`` objects.

    Used by the RediSearch short-term backend (which speaks HTTP/JSON). The
    pgvector long-term backend reads rows directly via SQL and builds snippets
    inline. Tolerant of the two common envelope shapes — a bare list of hits, or
    an object with a ``results`` / ``data`` / ``hits`` list. Each hit is expected
    to expose a text field (``text`` | ``content`` | ``chunk``), an optional
    ``score``, and an optional ``source`` (top-level or under ``metadata``).
    Unparseable hits are skipped rather than raising, so a partial/odd response
    still yields whatever is usable.

    Adjust the field names here if a backend schema differs; this is the single
    mapping boundary for both memory tiers.

    Args:
        data: The decoded JSON body from a memory-store search endpoint.

    Returns:
        The parsed snippets (possibly empty).
    """
    if isinstance(data, list):
        raw_hits = data
    elif isinstance(data, dict):
        raw_hits = data.get("results") or data.get("data") or data.get("hits") or []
    else:
        return []

    snippets: list[RetrievalSnippet] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        text = hit.get("text") or hit.get("content") or hit.get("chunk")
        if not isinstance(text, str) or not text.strip():
            continue
        metadata = hit.get("metadata")
        source = hit.get("source")
        if source is None and isinstance(metadata, dict):
            source = metadata.get("source")
        raw_score = hit.get("score", 0.0)
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 0.0
        snippets.append(
            RetrievalSnippet(
                source=str(source) if source is not None else "unknown",
                text=text.strip(),
                score=score,
            )
        )
    return snippets


def get_retrieval_provider(settings: Settings | None = None) -> RetrievalProvider:
    """Return the configured retrieval provider (agent memory).

    Builds the two-tier memory provider when at least one tier is configured:

    * ``SAISEI_PGVECTOR_DSN``   -> long-term memory (pgvector)
    * ``SAISEI_REDISEARCH_URL`` -> short-term memory (RediSearch)

    Each unconfigured tier degrades to a no-op, so the two-tier provider still
    works with only one tier set. When neither is configured, returns the
    deterministic mock provider (offline fallback / no precedents).

    Args:
        settings: Optional settings override (defaults to cached settings).

    Returns:
        A retrieval provider implementing :class:`RetrievalProvider`.
    """
    settings = settings or get_settings()
    if settings.pgvector_dsn or settings.redisearch_url:
        return TwoTierRetrievalProvider(
            short_term=RediSearchShortTermMemory(settings),
            long_term=PgVectorLongTermMemory(settings),
        )
    return MockRetrievalProvider()
