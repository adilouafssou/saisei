# Saisei database migrations (Alembic)

Alembic owns Saisei's **application** Postgres schema as an ordered, reviewable
migration history. This replaces relying solely on the idempotent
`CREATE ... IF NOT EXISTS` bootstrap that each store module still runs on init
(that bootstrap is kept so a fresh clone / offline run "just works" with no
migration step; Alembic is the authoritative path for a managed deployment).

## Scope

Managed by Alembic:

- `saisei_audit_log` — immutable audit ledger (+ append-only trigger)
- `saisei_trajectory` — agent-trajectory store (+ append-only trigger)
- `saisei_portfolio_watchlist` — tenant-scoped current-state watchlist
- `saisei_keikakusho_memory` — pgvector long-term memory table (+ HNSW index)

**Not** managed by Alembic (owned by their libraries):

- The LangGraph checkpointer tables — created by `PostgresSaver.setup()`.
  Alembic must not fight LangGraph's own schema.
- The `vector` extension — enabled by `scripts/init_pgvector.sql` on first
  container boot (and guarded with `CREATE EXTENSION IF NOT EXISTS` in the
  initial migration as a safety net for non-container Postgres).

## Single source of truth

The initial revision executes the SAME `SCHEMA_SQL` / DDL builders exported by
the store modules (`audit.sink_postgres.SCHEMA_SQL`,
`trajectory.store_postgres.SCHEMA_SQL`,
`portfolio.store_postgres.SCHEMA_SQL`, and
`tools.retrieval_ingest.build_table_sql` / `build_hnsw_index_sql`). So the
migration and the in-code bootstrap can never drift — `tests/test_migrations.py`
asserts the revision references those exact constants.

## Usage

The DSN is resolved from `Settings.postgres_dsn` through the secret seam (see
`migrations/env.py`); no credential lives in `alembic.ini`.

```sh
make migrate                      # upgrade to head
make migrate-create m="add x"     # create a new revision skeleton
uv run alembic downgrade -1       # roll back one revision
uv run alembic -x dsn=postgresql://... upgrade head   # one-off target DB
```
