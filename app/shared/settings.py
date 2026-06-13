"""Application settings for Saisei.

All configuration is sourced from environment variables (prefix ``SAISEI_``) via
pydantic-settings. Secrets are never hardcoded; see ``.env.example``.

This module is the canonical location under ``app.shared.settings``.
The legacy path ``shared.settings`` re-exports from here.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Strongly-typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="SAISEI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database (LangGraph Postgres checkpointer) ---
    postgres_dsn: str = Field(
        default="postgresql://saisei:saisei@localhost:5432/saisei",
        description=(
            "Plain libpq PostgreSQL DSN for the LangGraph checkpointer "
            "(PostgresSaver.from_conn_string requires the plain postgresql:// "
            "scheme, not postgresql+psycopg://). Override via "
            "SAISEI_POSTGRES_DSN; never commit real credentials."
        ),
    )

    # --- Redis (cache / task queue) ---
    redis_url: str = Field(
        default="redis://localhost:6379/0", description="Redis connection URL."
    )

    # --- Backend API ---
    api_host: str = Field(default="0.0.0.0", description="API bind host.")
    api_port: int = Field(default=8000, description="API bind port.")
    log_level: str = Field(default="INFO", description="Root log level.")

    # --- LLM provider (OpenAI-compatible Chat Completions API) ---
    llm_api_key: str = Field(default="", description="LLM provider API key (set via env).")
    llm_model: str = Field(default="", description="LLM model identifier.")
    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL for the OpenAI-compatible Chat Completions API.",
    )
    llm_timeout_seconds: float = Field(
        default=30.0, description="Timeout for LLM HTTP requests (seconds)."
    )

    # --- Agent memory / RAG ---------------------------------------------
    # The feasibility critic's advisory retrieval is modelled as two-tier
    # agent memory:
    #   * LONG-TERM  memory -> pgvector (durable precedent corpus in Postgres)
    #   * SHORT-TERM memory -> RediSearch (fast, ephemeral recall cache in Redis)
    # Both backends are already provisioned in docker-compose; no new infra.
    # When neither is configured, retrieval falls back to the mock provider
    # (no precedents) so the workflow runs fully offline.

    # --- Long-term memory (pgvector vector store) ---
    pgvector_dsn: str = Field(
        default="",
        description=(
            "PostgreSQL DSN for the pgvector LONG-TERM agent-memory store "
            "(durable precedent corpus: past plans / benchmarks / FSA "
            "passages). Empty disables long-term retrieval. Typically the same "
            "instance as SAISEI_POSTGRES_DSN (the pgvector image backs both). "
            "Set via SAISEI_PGVECTOR_DSN."
        ),
    )
    pgvector_table: str = Field(
        default="saisei_keikakusho_memory",
        description="pgvector table holding the long-term precedent embeddings.",
    )
    pgvector_embedding_dim: int = Field(
        default=1536,
        description="Embedding vector dimensionality for the pgvector column.",
    )

    # --- Embeddings (OpenAI-compatible Embeddings API) ---
    embedding_model: str = Field(
        default="",
        description=(
            "Embedding model identifier (OpenAI-compatible Embeddings API). "
            "Empty uses a deterministic offline hashing embedder so ingest and "
            "retrieval work with no network (mirrors the LLM offline fallback)."
        ),
    )

    # --- Short-term memory (RediSearch vector index) ---
    redisearch_url: str = Field(
        default="",
        description=(
            "Redis connection URL for the RediSearch SHORT-TERM agent-memory "
            "cache (fast, ephemeral recall queried before long-term memory). "
            "Empty disables the short-term tier. Defaults can reuse "
            "SAISEI_REDIS_URL. Set via SAISEI_REDISEARCH_URL."
        ),
    )
    redisearch_index: str = Field(
        default="saisei_keikakusho_stm",
        description="RediSearch index name for the short-term recall cache.",
    )
    redisearch_ttl_seconds: int = Field(
        default=3600,
        description=(
            "TTL (seconds) for short-term memory entries; expiry is what makes "
            "this tier 'short-term'. 0 disables expiry."
        ),
    )

    # --- Shared retrieval knobs ---
    retrieval_top_k: int = Field(
        default=3, description="Number of precedent snippets to retrieve per query."
    )
    retrieval_timeout_seconds: float = Field(
        default=10.0,
        description="Timeout for memory-store requests (seconds).",
    )

    # --- Mock data engine ---
    use_mocks: bool = Field(
        default=True, description="When true, nodes use the mock data engine."
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
