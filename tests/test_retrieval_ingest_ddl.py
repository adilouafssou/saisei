"""Offline tests for the pgvector long-term-memory DDL builders (Feature 4).

The ANN-index slice adds a real pgvector HNSW index so similarity search uses
approximate-nearest-neighbour recall instead of an O(n) sequential scan as the
precedent corpus grows. These tests pin the DDL the ingest path emits WITHOUT a
database (pure string construction), so they run fully offline:

- the table DDL is idempotent and uses the configured dimension;
- the HNSW index DDL is idempotent, uses ``vector_cosine_ops`` (matching the
  ``<=>`` cosine operator the query uses), and honours the m / ef_construction
  settings;
- the index name is derived from the table name (so two tables don't collide).

No network, no psycopg connection — these assert the SQL, which is the
deterministic, verifiable contract.
"""

from __future__ import annotations

from app.backend.tools.retrieval_ingest import (
    build_hnsw_index_sql,
    build_table_sql,
)
from app.shared.settings import Settings


class TestTableSql:
    def test_is_idempotent_and_uses_dim(self) -> None:
        sql = build_table_sql("saisei_mem", 1536)
        assert "CREATE TABLE IF NOT EXISTS saisei_mem" in sql
        assert "embedding vector(1536) NOT NULL" in sql
        assert "doc_id text PRIMARY KEY" in sql

    def test_honours_custom_dim(self) -> None:
        assert "vector(768)" in build_table_sql("t", 768)


class TestHnswIndexSql:
    def test_is_idempotent(self) -> None:
        sql = build_hnsw_index_sql("saisei_mem", m=16, ef_construction=64)
        assert "CREATE INDEX IF NOT EXISTS" in sql

    def test_uses_hnsw_and_cosine_ops(self) -> None:
        # The index opclass MUST match the cosine operator (<=>) the query uses,
        # or the planner ignores the index.
        sql = build_hnsw_index_sql("saisei_mem", m=16, ef_construction=64)
        assert "USING hnsw (embedding vector_cosine_ops)" in sql

    def test_honours_tuning_params(self) -> None:
        sql = build_hnsw_index_sql("saisei_mem", m=32, ef_construction=128)
        assert "m = 32" in sql
        assert "ef_construction = 128" in sql

    def test_index_name_is_derived_from_table(self) -> None:
        a = build_hnsw_index_sql("table_a", m=16, ef_construction=64)
        b = build_hnsw_index_sql("table_b", m=16, ef_construction=64)
        assert "table_a_embedding_hnsw" in a
        assert "table_b_embedding_hnsw" in b
        # Distinct tables yield distinct index names (no collision).
        assert "table_a_embedding_hnsw" not in b

    def test_targets_the_given_table(self) -> None:
        sql = build_hnsw_index_sql("saisei_mem", m=16, ef_construction=64)
        assert "ON saisei_mem USING hnsw" in sql


class TestSettingsDefaults:
    def test_hnsw_defaults_match_pgvector_defaults(self) -> None:
        s = Settings()
        assert s.pgvector_hnsw_m == 16
        assert s.pgvector_hnsw_ef_construction == 64

    def test_hnsw_knobs_are_overridable(self) -> None:
        s = Settings(pgvector_hnsw_m=32, pgvector_hnsw_ef_construction=128)
        sql = build_hnsw_index_sql(
            s.pgvector_table,
            m=s.pgvector_hnsw_m,
            ef_construction=s.pgvector_hnsw_ef_construction,
        )
        assert "m = 32" in sql
        assert "ef_construction = 128" in sql
