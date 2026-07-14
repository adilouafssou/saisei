-- Enable the pgvector extension for Saisei's long-term agent memory.
--
-- Runs once on first container boot via docker-entrypoint-initdb.d. The same
-- Postgres instance backs both the LangGraph checkpointer and the pgvector
-- long-term-memory store (see app/backend/tools/retrieval.py); the application
-- creates and manages the memory table itself at ingest time.
CREATE EXTENSION IF NOT EXISTS vector;
