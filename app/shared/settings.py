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

from app.shared.platform import (
    Platform,
    detect_platform,
    should_persist_checkpoints,
)

__all__ = ["Settings", "get_settings", "get_platform"]


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
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis connection URL.")

    # --- Checkpointer selection (auto-detected per platform) ---
    # Persistence is auto-derived from the runtime platform + DSN by
    # app/shared/platform.should_persist_checkpoints:
    #   * SAISEI_PERSIST_CHECKPOINTS (true/false) wins if set.
    #   * Hugging Face / no real DB -> in-memory MemorySaver (DB-less hosting).
    #   * A real (non-localhost) Postgres DSN -> durable PostgresSaver.
    # The default_factory runs detection at construction time so a fresh clone
    # "just works" on local / Lightning.ai / Hugging Face with zero config.
    persist_checkpoints: bool = Field(
        default_factory=should_persist_checkpoints,
        description=(
            "Persist LangGraph checkpoints in Postgres (True) or use an "
            "in-process MemorySaver (False). Auto-detected from the platform / "
            "DSN; override with SAISEI_PERSIST_CHECKPOINTS."
        ),
    )

    # --- Demo access gate (optional) ---
    # When set, the UI shows a password screen before the app, so a public demo
    # URL (e.g. a Hugging Face Space) is only usable by people you send the
    # password to. Empty (default) disables the gate entirely — local dev and
    # tests are unaffected. Set SAISEI_DEMO_PASSWORD as a deployment secret.
    demo_password: str = Field(
        default="",
        description=(
            "Optional shared password gating the whole UI for demo hosting. "
            "Empty disables the gate. Set via SAISEI_DEMO_PASSWORD."
        ),
    )

    # --- Backend API ---
    api_host: str = Field(default="0.0.0.0", description="API bind host.")
    api_port: int = Field(default=8000, description="API bind port.")
    log_level: str = Field(default="INFO", description="Root log level.")
    # When True, the run/resume HTTP endpoints dispatch the (blocking) graph work
    # OFF the request path via the run executor and return immediately with
    # phase='running'; the client then polls GET /runs/{thread_id}. False
    # (default) keeps the original synchronous behaviour, so offline runs, the
    # existing API tests, and the demo are unchanged. Set via SAISEI_RUN_ASYNC.
    run_async: bool = Field(
        default=False,
        description=(
            "Run the graph off the request path (async submit + poll) instead "
            "of blocking the HTTP call. False keeps the synchronous behaviour. "
            "Set via SAISEI_RUN_ASYNC."
        ),
    )

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
    pgvector_hnsw_m: int = Field(
        default=16,
        ge=2,
        description=(
            "pgvector HNSW index 'm' parameter (max edges per layer node). The "
            "pgvector default (16) suits the precedent corpus; higher = better "
            "recall + larger index / slower build. Used by the idempotent "
            "CREATE INDEX ... USING hnsw at ingest time. Set via "
            "SAISEI_PGVECTOR_HNSW_M."
        ),
    )
    pgvector_hnsw_ef_construction: int = Field(
        default=64,
        ge=4,
        description=(
            "pgvector HNSW index 'ef_construction' parameter (build-time "
            "candidate list size). The pgvector default (64) balances build "
            "cost and recall; higher = better recall + slower build. Set via "
            "SAISEI_PGVECTOR_HNSW_EF_CONSTRUCTION."
        ),
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
    use_mocks: bool = Field(default=True, description="When true, nodes use the mock data engine.")

    # --- Real data sources (optional; offline mock is the default) ---
    # Both default to empty/unconfigured so `make verify` runs fully offline.
    # A client activates its live HTTP path ONLY when its config is non-empty;
    # on any error it degrades to the deterministic mock (mirrors the
    # polish_keikakusho offline-fallback contract).

    # BOJ policy / call-rate time series (public, no auth). When the base URL is
    # set, BojRateClient fetches the live curve; otherwise it returns the mock.
    boj_api_base_url: str = Field(
        default="",
        description=(
            "Base URL for the public BOJ / e-Stat time-series API used to fetch "
            "the policy-rate curve. Empty uses the deterministic mock curve. "
            "Set via SAISEI_BOJ_API_BASE_URL."
        ),
    )
    boj_api_series_id: str = Field(
        default="",
        description=(
            "Series identifier for the BOJ policy / uncollateralized overnight "
            "call rate. Required when boj_api_base_url is set. "
            "Set via SAISEI_BOJ_API_SERIES_ID."
        ),
    )
    boj_api_timeout_seconds: float = Field(
        default=10.0, description="Timeout for BOJ rate API requests (seconds)."
    )

    # NTA (National Tax Agency) Hojin Bango / Corporate Number Web-API (free,
    # requires a registered application id). When the id is set,
    # HojinBangoClient validates/enriches the 13-digit number against the live
    # registry; otherwise it uses the deterministic offline validator only.
    hojin_bango_app_id: str = Field(
        default="",
        description=(
            "Application ID for Japan's NTA Corporate Number (法人番号) Web-API. "
            "Empty disables live lookup (check-digit validation still runs "
            "offline). Set via SAISEI_HOJIN_BANGO_APP_ID."
        ),
    )
    hojin_bango_base_url: str = Field(
        default="https://api.houjin-bangou.nta.go.jp/4",
        description=(
            "Base URL for the NTA Corporate Number Web-API (version 4). "
            "Set via SAISEI_HOJIN_BANGO_BASE_URL."
        ),
    )
    hojin_bango_timeout_seconds: float = Field(
        default=10.0, description="Timeout for Hojin Bango API requests (seconds)."
    )

    # TDB (Teikoku Databank) credit-report API (paid, contract-only). When an
    # API key is set, TdbClient fetches the live credit report; otherwise, or on
    # any error or boundary-guard rejection, it degrades to the deterministic
    # TDB mock (mirrors the polish_keikakusho offline-fallback contract).
    tdb_api_key: str = Field(
        default="",
        description=(
            "API key for the Teikoku Databank (TDB) credit-report API. "
            "Empty uses the deterministic TDB mock (fully offline). "
            "Never commit a real key. Set via SAISEI_TDB_API_KEY."
        ),
    )
    tdb_api_base_url: str = Field(
        default="https://api.tdb.example",
        description=(
            "Base URL for the TDB credit-report API. Only used when "
            "tdb_api_key is set. Set via SAISEI_TDB_API_BASE_URL."
        ),
    )
    tdb_api_timeout_seconds: float = Field(
        default=10.0, description="Timeout for TDB API requests (seconds)."
    )
    tdb_api_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Max retry attempts for a failed live TDB request (in addition to "
            "the first try). Exponential backoff between attempts. 0 disables "
            "retries. Only affects the live path; the offline mock never retries."
        ),
    )
    tdb_api_backoff_base_seconds: float = Field(
        default=0.5,
        ge=0.0,
        description=(
            "Base delay (seconds) for exponential backoff between TDB retries: "
            "attempt n waits base * 2**n. Set 0 for no delay (used in tests)."
        ),
    )
    tdb_circuit_breaker_threshold: int = Field(
        default=5,
        ge=1,
        description=(
            "Consecutive live-TDB failures that trip the circuit breaker. While "
            "open, the client short-circuits straight to the mock without a "
            "network call, protecting the request path from a sustained outage."
        ),
    )

    # Core Banking Shisanhyo API (bank-internal). When a base URL is set,
    # CoreBankingClient fetches live monthly trial balances; otherwise, or on
    # any error / boundary-guard rejection, it degrades to the deterministic
    # core-banking mock. Resilience knobs reuse the same retry/breaker design.
    core_banking_base_url: str = Field(
        default="",
        description=(
            "Base URL for the bank-internal Core Banking Shisanhyo API. Empty "
            "uses the deterministic core-banking mock (fully offline). "
            "Set via SAISEI_CORE_BANKING_BASE_URL."
        ),
    )
    core_banking_api_key: str = Field(
        default="",
        description=(
            "API key/token for the Core Banking API. Never commit a real key. "
            "Set via SAISEI_CORE_BANKING_API_KEY."
        ),
    )
    core_banking_timeout_seconds: float = Field(
        default=10.0, description="Timeout for Core Banking API requests (seconds)."
    )
    core_banking_max_retries: int = Field(
        default=2, ge=0, description="Max retries for a failed live Core Banking request."
    )
    core_banking_backoff_base_seconds: float = Field(
        default=0.5, ge=0.0, description="Base backoff delay (seconds) for Core Banking retries."
    )
    core_banking_circuit_breaker_threshold: int = Field(
        default=5, ge=1, description="Consecutive failures that trip the Core Banking breaker."
    )

    # EDINET macro API (public). When a base URL is set, EdinetMacroClient may
    # fetch live macro series; otherwise it returns the deterministic mock.
    # Settlement metrics are always mock (bank-internal), as in BojRateClient.
    edinet_base_url: str = Field(
        default="",
        description=(
            "Base URL for the EDINET macro API. Empty uses the deterministic "
            "macro mock (fully offline). Set via SAISEI_EDINET_BASE_URL."
        ),
    )
    edinet_timeout_seconds: float = Field(
        default=10.0, description="Timeout for EDINET API requests (seconds)."
    )
    edinet_max_retries: int = Field(
        default=2, ge=0, description="Max retries for a failed live EDINET request."
    )
    edinet_backoff_base_seconds: float = Field(
        default=0.5, ge=0.0, description="Base backoff delay (seconds) for EDINET retries."
    )
    edinet_circuit_breaker_threshold: int = Field(
        default=5, ge=1, description="Consecutive failures that trip the EDINET breaker."
    )

    # --- UI / presentation pacing ---
    # Deliberate per-bubble delay (seconds) inserted between streamed meeting
    # events so the deterministic spine (which completes in milliseconds offline)
    # reads as a live, turn-by-turn creditor meeting instead of a single
    # all-at-once "waterfall" dump. 0 disables pacing entirely (used by tests /
    # CI so they stay instant). Display-only: pacing never changes any verdict,
    # figure, or route -- it only spaces out when each already-computed bubble
    # appears. Override via SAISEI_UI_MEETING_PACE_SECONDS.
    ui_meeting_pace_seconds: float = Field(
        default=0.6,
        ge=0.0,
        description=(
            "Delay (seconds) between streamed meeting-transcript bubbles so the "
            "creditor meeting feels live and turn-by-turn. 0 disables pacing "
            "(tests/CI). Display-only; never affects a verdict or figure."
        ),
    )

    # --- Immutable audit log (Feature 7; opt-in, offline-safe by default) ---
    # Append-only, hash-chained ledger of classifications, guarantee-release
    # assessments, and human decisions (see
    # docs/en/specs/FEATURE7_AUDIT_LOG_SPEC.md). Empty DSN -> NullAuditSink
    # (no-op), so `make verify` and CI stay fully offline and byte-stable,
    # mirroring the pgvector_/langsmith_ empty -> offline pattern above. May
    # reuse the checkpointer Postgres instance.
    audit_dsn: str = Field(
        default="",
        description=(
            "PostgreSQL DSN for the immutable audit ledger. Empty disables "
            "persistence (a no-op NullAuditSink is used), keeping the system "
            "fully offline. May reuse SAISEI_POSTGRES_DSN. "
            "Set via SAISEI_AUDIT_DSN."
        ),
    )
    audit_actor_default: str = Field(
        default="banker",
        description=(
            "Placeholder actor id recorded for human_decision audit events "
            "until real auth/OIDC (Feature 6) supplies the banker identity. "
            "Set via SAISEI_AUDIT_ACTOR_DEFAULT."
        ),
    )
    # --- Audit ledger cryptographic signing (opt-in; offline-safe) ---
    # When a private key is configured, record_event attaches a detached
    # Ed25519 signature over each event's content_hash, making the ledger
    # tamper-PROOF (not merely tamper-evident): forging an event also requires
    # the private key, which need never live in the DB. Empty (default) leaves
    # events unsigned (NullSigner), so the system stays fully offline and
    # byte-stable. The public key is for examiner-side verification only.
    audit_signing_private_key: str = Field(
        default="",
        description=(
            "Ed25519 private key (PEM text, or '@/path/to/key.pem' to read from "
            "a file) used to sign audit events. Empty leaves events unsigned. "
            "Provide as a deployment secret; never commit a real key. "
            "Set via SAISEI_AUDIT_SIGNING_PRIVATE_KEY."
        ),
    )
    audit_signing_public_key: str = Field(
        default="",
        description=(
            "Ed25519 public key (PEM text, or '@/path') used to VERIFY audit "
            "event signatures (examiner side). Safe to distribute. "
            "Set via SAISEI_AUDIT_SIGNING_PUBLIC_KEY."
        ),
    )

    # --- Keikakusho export delivery (optional bank house template) ---
    # When set to a path to a .docx that contains the {{KEIKAKUSHO_BODY}}
    # placeholder, the Keikakusho export injects the deterministic body into the
    # bank's house template (cover page / letterhead / headers / footers).
    # Empty (default) emits the bare default document, so demo / offline runs are
    # unchanged. The body is always rendered number-safe regardless.
    keikakusho_docx_template: str = Field(
        default="",
        description=(
            "Filesystem path to the bank's Keikakusho .docx house template "
            "containing the {{KEIKAKUSHO_BODY}} placeholder. Empty emits the "
            "default bare document. Set via SAISEI_KEIKAKUSHO_DOCX_TEMPLATE."
        ),
    )

    # --- Agent-trajectory data flywheel (Feature 3; opt-in, offline-safe) ---
    # Append-only store of captured negotiations (inputs digest, proposed
    # strategies, banker decision + note, approved strategy, final plan) used as
    # the offline training corpus (SFT / DPO/ORPO / revision-note reward model).
    # Empty DSN -> NullTrajectoryStore (no-op), so `make verify` and CI stay
    # fully offline and byte-stable, mirroring the opt-in SAISEI_AUDIT_DSN /
    # SAISEI_PORTFOLIO_DSN pattern. The corpus is a training signal, so enabling
    # it is the bank's explicit data-governance decision (financial data never
    # leaves the bank's VPC). May reuse the checkpointer Postgres instance.
    trajectory_dsn: str = Field(
        default="",
        description=(
            "PostgreSQL DSN for the OPT-IN agent-trajectory store (the data "
            "flywheel). Empty (default) disables persistence (a no-op "
            "NullTrajectoryStore is used), so nothing is captured at rest and "
            "the system stays fully offline. May reuse SAISEI_POSTGRES_DSN. "
            "Enabling it is the bank's explicit data-governance decision. "
            "Set via SAISEI_TRAJECTORY_DSN."
        ),
    )

    # --- Trajectory training-data export boundary (Feature 3; opt-in, in-VPC) -
    # Turning captured trajectories into a training corpus is a SEPARATE,
    # higher-bar governance decision from merely capturing them: the export
    # leaves the live system as a dataset. So it is gated by its OWN explicit
    # flag (not just trajectory_dsn) and writes ONLY to a local filesystem
    # destination -- the export path makes no network call, structurally
    # enforcing "financial data never leaves the bank's VPC". Direct identifiers
    # (tdb_code / hojin_bango / actor) are never emitted; borrower grouping uses
    # a salted pseudonymous key. Free-text fields (revision notes / plan drafts)
    # are dropped unless the bank explicitly opts them in after PII review.
    trajectory_export_enabled: bool = Field(
        default=False,
        description=(
            "Master gate for exporting captured trajectories as a training "
            "corpus. False (default) makes the export a hard no-op even when "
            "trajectory_dsn is set -- capturing data and exporting it are "
            "separate governance decisions. Set via "
            "SAISEI_TRAJECTORY_EXPORT_ENABLED only after data-governance review."
        ),
    )
    trajectory_export_dir: str = Field(
        default="",
        description=(
            "Local filesystem directory the trajectory export writes JSONL to. "
            "Must be a LOCAL path inside the bank's VPC; the export never makes "
            "a network call. Empty disables export. Set via "
            "SAISEI_TRAJECTORY_EXPORT_DIR."
        ),
    )
    trajectory_export_salt: str = Field(
        default="",
        description=(
            "Secret salt for the pseudonymous borrower grouping key in exported "
            "training data (HMAC over tdb_code). Keeps the same borrower's "
            "records linkable in the corpus WITHOUT emitting the real code, and "
            "prevents trivial rainbow-table re-identification. Provide as a "
            "deployment secret (supports the @env:/@file: secret-seam refs); "
            "empty falls back to an unsalted hash (linkable but weaker). "
            "Set via SAISEI_TRAJECTORY_EXPORT_SALT."
        ),
    )
    trajectory_export_include_free_text: bool = Field(
        default=False,
        description=(
            "Whether the export includes free-text fields (banker revision "
            "notes / Keikakusho drafts) that may carry borrower-identifying "
            "prose. False (default) drops them, emitting only the deterministic, "
            "non-PII training surface. Enable ONLY after PII review of the notes. "
            "Set via SAISEI_TRAJECTORY_EXPORT_INCLUDE_FREE_TEXT."
        ),
    )

    # --- Auth / OIDC identity mapping (Feature 6) -------------------------
    # The transport/token-validation layer (provider, JWKS, callback) is
    # deployment-owned and lives outside this offline core; once it validates an
    # OIDC token it hands the verified CLAIMS to identity.identity_from_claims,
    # which maps them to the Identity the seam returns. These knobs configure
    # that mapping + the production guard. All default to the safe single-tenant
    # placeholder posture, so an unconfigured deployment behaves exactly as
    # before (no auth required, placeholder identity).
    auth_required: bool = Field(
        default=False,
        description=(
            "When True, persistence/attribution refuse to run under an "
            "UNAUTHENTICATED (placeholder) identity — the production guard for "
            "the opt-in Portfolio store and the audit actor. False (default) "
            "keeps the single-tenant demo posture. Set via SAISEI_AUTH_REQUIRED."
        ),
    )
    auth_tenant_claim: str = Field(
        default="tenant",
        description=(
            "Name of the OIDC claim carrying the bank/branch tenant id (the "
            "storage isolation key). Common choices: 'tenant', 'org', a custom "
            "namespaced claim. Set via SAISEI_AUTH_TENANT_CLAIM."
        ),
    )
    auth_actor_claim: str = Field(
        default="sub",
        description=(
            "Name of the OIDC claim identifying the acting banker (recorded as "
            "the audit actor). Defaults to the standard subject claim 'sub'. "
            "Set via SAISEI_AUTH_ACTOR_CLAIM."
        ),
    )

    # --- Auth / OIDC transport (Feature 6, slice 4: token verification) ----
    # The application-side claim mapping (identity_from_claims) and the
    # production guard (require_persistable) already exist; what these add is the
    # TRANSPORT layer that turns a raw bearer token into VERIFIED claims:
    # JWKS discovery + signature/expiry/issuer/audience checks. All default to
    # empty so the offline / single-tenant demo posture is unchanged -- with no
    # jwks_url configured the API keeps returning the placeholder identity (and,
    # when auth_required is also set, refuses it). A deployment activates real
    # OIDC by setting jwks_url (+ issuer/audience) to its identity provider.
    auth_jwks_url: str = Field(
        default="",
        description=(
            "JWKS endpoint URL of the bank's OIDC identity provider (e.g. "
            "https://idp.example.com/.well-known/jwks.json). Empty disables "
            "token verification (the API uses the placeholder identity, gated "
            "by auth_required). Set via SAISEI_AUTH_JWKS_URL."
        ),
    )
    auth_issuer: str = Field(
        default="",
        description=(
            "Expected OIDC token issuer ('iss' claim). When set, a token whose "
            "issuer does not match is rejected. Empty skips the issuer check "
            "(not recommended in production). Set via SAISEI_AUTH_ISSUER."
        ),
    )
    auth_audience: str = Field(
        default="",
        description=(
            "Expected OIDC token audience ('aud' claim), typically this API's "
            "client id. When set, a token whose audience does not match is "
            "rejected. Empty skips the audience check (not recommended in "
            "production). Set via SAISEI_AUTH_AUDIENCE."
        ),
    )
    auth_jwks_cache_seconds: int = Field(
        default=3600,
        ge=0,
        description=(
            "How long (seconds) to cache the provider's JWKS signing keys before "
            "refetching. Caching avoids a network round-trip per request; the "
            "refresh picks up provider key rotation. Set via "
            "SAISEI_AUTH_JWKS_CACHE_SECONDS."
        ),
    )
    auth_leeway_seconds: int = Field(
        default=60,
        ge=0,
        description=(
            "Clock-skew leeway (seconds) allowed when validating token expiry / "
            "not-before. Set via SAISEI_AUTH_LEEWAY_SECONDS."
        ),
    )

    # --- Portfolio watchlist persistence (Feature 8.1; opt-in, off by default) -
    # The book-level watchlist is EPHEMERAL by default (an in-session view that
    # persists nothing at rest). A bank may opt IN — after its own data-
    # governance / FSA review — to persist the book so the watchlist survives
    # sessions and supports true continuous monitoring. Empty DSN ->
    # NullPortfolioStore (no-op), keeping the system fully offline and the
    # default posture governance-light, mirroring the opt-in SAISEI_AUDIT_DSN.
    # May reuse the checkpointer Postgres instance. THIS IS THE BANK'S DECISION,
    # not a default: leaving it empty stores nothing.
    portfolio_dsn: str = Field(
        default="",
        description=(
            "PostgreSQL DSN for the OPT-IN Portfolio watchlist store. Empty "
            "(default) disables persistence (a no-op NullPortfolioStore is "
            "used), so the watchlist stays ephemeral / in-session and nothing "
            "is stored at rest. May reuse SAISEI_POSTGRES_DSN. Enabling it is "
            "the bank's explicit data-governance decision. "
            "Set via SAISEI_PORTFOLIO_DSN."
        ),
    )
    portfolio_tenant_default: str = Field(
        default="default",
        description=(
            "Tenant id used to scope the persisted watchlist until real "
            "auth/OIDC (Feature 6) supplies the bank/branch identity. The store "
            "is tenant-scoped so one bank can never read another's book. "
            "Set via SAISEI_PORTFOLIO_TENANT_DEFAULT."
        ),
    )

    # --- Loan-lifecycle event store (opt-in, offline-safe by default) ------
    # Durable, append-only, tenant-scoped ledger of a facility's loan-lifecycle
    # events (LoanEvent: 申込 → ... → 条件変更 / 管理回収). The graph already keeps
    # these in the LangGraph checkpointer state; this dedicated store makes a
    # facility's lifecycle durable in its OWN ledger (with a DB-level append-only
    # trigger), the same posture as the audit ledger. Empty DSN ->
    # NullLoanStore (no-op), so `make verify` and CI stay fully offline and
    # byte-stable, mirroring the opt-in SAISEI_AUDIT_DSN / SAISEI_PORTFOLIO_DSN
    # pattern. May reuse the checkpointer Postgres instance.
    loan_dsn: str = Field(
        default="",
        description=(
            "PostgreSQL DSN for the OPT-IN append-only loan-lifecycle event "
            "store. Empty (default) disables persistence (a no-op NullLoanStore "
            "is used), so nothing is stored at rest and the system stays fully "
            "offline. May reuse SAISEI_POSTGRES_DSN. Set via SAISEI_LOAN_DSN."
        ),
    )
    loan_tenant_default: str = Field(
        default="default",
        description=(
            "Tenant id used to scope the persisted loan-event ledger until real "
            "auth/OIDC (Feature 6) supplies the bank/branch identity. The store "
            "is tenant-scoped so one bank can never read another's facility "
            "log. Set via SAISEI_LOAN_TENANT_DEFAULT."
        ),
    )

    # --- LangSmith observability (opt-in; offline-safe by default) ---
    # All four fields default to empty/false so `make verify` runs fully offline
    # with zero network calls. Tracing is activated ONLY when both
    # langsmith_tracing=True AND langsmith_api_key is non-empty, mirroring the
    # empty/false -> offline-mock pattern used by llm_*, pgvector_*, boj_*, and
    # hojin_bango_* above.
    langsmith_tracing: bool = Field(
        default=False,
        description=(
            "Enable LangSmith tracing. When True AND langsmith_api_key is "
            "non-empty, configure_tracing() sets the LANGCHAIN_TRACING_V2 / "
            "LANGCHAIN_API_KEY / LANGCHAIN_PROJECT / LANGCHAIN_ENDPOINT env "
            "vars so LangGraph auto-instruments. False (default) is a strict "
            "no-op: no env vars are set and no network calls are made. "
            "Set via SAISEI_LANGSMITH_TRACING."
        ),
    )
    langsmith_api_key: str = Field(
        default="",
        description=(
            "LangSmith API key. Required for tracing; empty disables it. "
            "Never commit a real key. Set via SAISEI_LANGSMITH_API_KEY."
        ),
    )
    langsmith_project: str = Field(
        default="saisei",
        description=(
            "LangSmith project name (LANGCHAIN_PROJECT). Set via SAISEI_LANGSMITH_PROJECT."
        ),
    )
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com",
        description=(
            "LangSmith ingestion endpoint (LANGCHAIN_ENDPOINT). Set via SAISEI_LANGSMITH_ENDPOINT."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()


@lru_cache(maxsize=1)
def get_platform() -> Platform:
    """Return the cached detected runtime platform (local/lightning/huggingface)."""
    return detect_platform()
