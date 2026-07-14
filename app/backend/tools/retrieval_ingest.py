"""Ingestion for feasibility-critic RAG precedents — long-term agent memory.

Upserts precedent documents — past Keikakusho excerpts, industry benchmarks, and
FSA-manual passages — into the pgvector **long-term memory** table that
:class:`app.backend.tools.retrieval.PgVectorLongTermMemory` recalls from.

Why long-term memory? Ingestion seeds the agents' *durable* knowledge base — the
corpus that must survive restarts and accumulate over time. The RediSearch
short-term tier is a transient recall cache populated automatically at query
time (see :class:`app.backend.tools.retrieval.TwoTierRetrievalProvider`), so it
is never seeded directly here.

The conventional pgvector path: ``psycopg`` (already a project dependency) +
plain SQL. Ingestion creates the table if needed, embeds each document with
:mod:`app.backend.tools.embeddings`, and idempotently upserts on ``doc_id``.
Safe to import offline; it only touches the database when explicitly called with
a configured ``SAISEI_PGVECTOR_DSN``.

Run as a module to seed the bundled starter corpus into long-term memory::

    uv run python -m app.backend.tools.retrieval_ingest

This module is the canonical location under ``app.backend.tools.retrieval_ingest``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.backend.tools.embeddings import embed_texts
from app.backend.tools.retrieval import _to_pgvector_literal
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "PrecedentDoc",
    "load_seed_corpus",
    "ingest_documents",
    "ingest_seed_corpus",
    "build_table_sql",
    "build_hnsw_index_sql",
    "SEED_CORPUS_PATH",
]

_log = get_logger(__name__)

#: Bundled starter corpus shipped inside the package.
SEED_CORPUS_PATH: Path = Path(__file__).resolve().parent / "fixtures" / "rag_seed_corpus.json"


class PrecedentDoc(BaseModel):
    """One precedent document to index into long-term memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str = Field(description="Stable unique id (used for idempotent upsert).")
    source: str = Field(description="Origin label: 'past_keikakusho' | 'benchmark' | 'fsa_manual'.")
    text: str = Field(description="The passage text to embed and index.")


def load_seed_corpus(path: Path | None = None) -> list[PrecedentDoc]:
    """Load and validate the bundled (or given) seed corpus JSON.

    Args:
        path: Optional override for the corpus file.

    Returns:
        The parsed precedent documents.
    """
    corpus_path = path or SEED_CORPUS_PATH
    raw = json.loads(corpus_path.read_text(encoding="utf-8"))
    return [PrecedentDoc.model_validate(item) for item in raw]


def _ensure_table_sql(table: str, dim: int) -> str:
    """DDL that creates the long-term-memory table if it does not exist.

    The ``vector(dim)`` column requires the pgvector extension (enabled by
    ``scripts/init_pgvector.sql`` on first container boot). Table name and dim
    come from trusted settings, not user input.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {table} ("  # noqa: S608
        "  doc_id text PRIMARY KEY,"
        "  source text NOT NULL,"
        "  text text NOT NULL,"
        f"  embedding vector({dim}) NOT NULL"
        ")"
    )


#: Public alias for the table DDL builder (kept testable / importable).
build_table_sql = _ensure_table_sql


def build_hnsw_index_sql(table: str, *, m: int, ef_construction: int) -> str:
    """DDL that creates the pgvector HNSW ANN index if it does not exist.

    Feature 4 (real ANN index): without an index, similarity search is an exact
    sequential scan (``ORDER BY embedding <=> q``) — fine for the seed corpus but
    O(n) as the precedent corpus grows. An HNSW index gives approximate-nearest-
    neighbour recall at production scale.

    Uses ``vector_cosine_ops`` so the index matches the cosine-distance operator
    (``<=>``) the long-term-memory query uses in
    :meth:`~app.backend.tools.retrieval.PgVectorLongTermMemory._query_pgvector`
    (the two MUST agree or the index is ignored by the planner). HNSW is chosen
    over IVFFlat because it needs no training step and works on an empty /
    growing table — the corpus accumulates over time.

    Idempotent (``IF NOT EXISTS``) so re-running ingest never errors. The index
    name is derived from the (trusted) table name; ``m`` / ``ef_construction``
    come from trusted settings, not user input.

    Args:
        table: The long-term-memory table name (trusted settings value).
        m: HNSW ``m`` parameter (max edges per node per layer).
        ef_construction: HNSW ``ef_construction`` parameter (build candidate list).

    Returns:
        The ``CREATE INDEX IF NOT EXISTS ... USING hnsw`` DDL string.
    """
    index_name = f"{table}_embedding_hnsw"
    return (
        f"CREATE INDEX IF NOT EXISTS {index_name} "  # noqa: S608
        f"ON {table} USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)})"
    )


def ingest_documents(docs: list[PrecedentDoc], settings: Settings | None = None) -> int:
    """Embed and upsert precedent documents into pgvector long-term memory.

    Best-effort and explicit: a no-op (returns 0) when pgvector is not
    configured, so importing/calling this offline never touches the database.
    Creates the table if needed, then idempotently upserts each document on
    ``doc_id``. Raises only on an actual database error during a configured run,
    so callers/CLI see real failures.

    Args:
        docs: Documents to upsert into long-term memory.
        settings: Optional settings override (defaults to cached settings).

    Returns:
        The number of documents written (0 when pgvector is unconfigured / empty).
    """
    settings = settings or get_settings()
    if not settings.pgvector_dsn:
        _log.warning("ingest.pgvector_unconfigured", docs=len(docs))
        return 0
    if not docs:
        return 0

    import psycopg

    table = settings.pgvector_table
    embeddings = embed_texts([d.text for d in docs], settings)
    upsert_sql = (
        f"INSERT INTO {table} (doc_id, source, text, embedding) "  # noqa: S608
        "VALUES (%s, %s, %s, %s::vector) "
        "ON CONFLICT (doc_id) DO UPDATE SET "
        "  source = EXCLUDED.source, text = EXCLUDED.text, "
        "  embedding = EXCLUDED.embedding"
    )
    rows = [
        (d.doc_id, d.source, d.text, _to_pgvector_literal(vec))
        for d, vec in zip(docs, embeddings, strict=True)
    ]
    with (
        psycopg.connect(
            settings.pgvector_dsn, connect_timeout=int(settings.retrieval_timeout_seconds)
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(_ensure_table_sql(table, settings.pgvector_embedding_dim))
        # Feature 4: ensure the HNSW ANN index exists so similarity search uses
        # approximate-nearest-neighbour recall instead of an O(n) sequential
        # scan as the corpus grows. Idempotent; matches the cosine operator the
        # query uses. Created before the upsert so the very first ingest leaves a
        # fully production-ready table.
        cur.execute(
            build_hnsw_index_sql(
                table,
                m=settings.pgvector_hnsw_m,
                ef_construction=settings.pgvector_hnsw_ef_construction,
            )
        )
        cur.executemany(upsert_sql, rows)
        conn.commit()
    _log.info("ingest.upserted", docs=len(docs), table=table)
    return len(docs)


def ingest_seed_corpus(settings: Settings | None = None) -> int:
    """Load the bundled seed corpus and upsert it into pgvector long-term memory.

    Returns:
        The number of documents written (0 when pgvector is unconfigured).
    """
    return ingest_documents(load_seed_corpus(), settings=settings)


if __name__ == "__main__":  # pragma: no cover - manual operational entry point
    # Operator-facing CLI: fail loudly (non-zero exit) when pgvector is not
    # configured, instead of silently no-opping and printing a misleading
    # success. The library functions above keep their no-op/return-0 contract
    # so importing them offline stays safe; only this entry point is made loud.
    _settings = get_settings()
    if not _settings.pgvector_dsn:
        # ASCII-only message: avoid non-ASCII (e.g. em-dash) so this never
        # raises UnicodeEncodeError on a C/POSIX-locale stderr (minimal
        # containers).
        sys.stderr.write(
            "error: SAISEI_PGVECTOR_DSN is not set; nothing was seeded.\n"
            "Configure pgvector long-term memory before running make seed-memory.\n"
        )
        raise SystemExit(1)
    count = ingest_seed_corpus(settings=_settings)
    _log.info("ingest.cli_done", count=count)
    sys.stdout.write(f"seeded {count} document(s) into pgvector long-term memory\n")
