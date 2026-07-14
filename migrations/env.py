"""Alembic migration environment for Saisei's application schema.

This wires Alembic into the project's conventions rather than the generated
default:

* **DSN from Settings + the secret seam.** The database URL is resolved from
  :attr:`Settings.postgres_dsn` through
  :func:`~app.backend.secrets.resolve_secret`, so migrations target the same DSN
  (or a ``@vault:`` / ``@file:`` / ``@env:`` reference) as the running app and no
  credential lives in ``alembic.ini``. The ``sqlalchemy.url`` ini value, or the
  ``-x dsn=...`` command-line override, still win when explicitly set (handy for
  pointing a migration at a one-off DB).
* **No project import at module import time beyond settings/secrets.** Alembic is
  a dev/deploy-only dependency; nothing in the runtime app imports this file, so
  the offline-first contract is preserved.
* **Raw-SQL migrations, no ORM models.** Saisei has no SQLAlchemy models -- the
  schema is plain SQL owned by the store modules. So ``target_metadata`` is None
  and autogenerate is not used; revisions execute the SAME ``SCHEMA_SQL`` the
  in-code bootstrap uses, keeping the two paths from ever drifting.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app.backend.secrets import resolve_secret
from app.shared.settings import get_settings
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Saisei manages its schema as raw SQL in the store modules, not as SQLAlchemy
# models, so there is no metadata to autogenerate against.
target_metadata = None


def _database_url() -> str:
    """Resolve the migration target DSN.

    Precedence: an explicit ``-x dsn=...`` CLI arg, then a ``sqlalchemy.url`` set
    in the ini, then ``Settings.postgres_dsn`` resolved through the secret seam.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("dsn"):
        return resolve_secret(x_args["dsn"])
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return resolve_secret(ini_url)
    return resolve_secret(get_settings().postgres_dsn)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DB connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
