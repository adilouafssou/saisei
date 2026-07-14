# Saisei (再生) — Data & Memory Architecture

> **Audience.** This document is written to be read by engineers and by non-engineers:
> executive leadership, investors, regulators, auditors, and partner financial
> institutions. It explains *where every category of data lives, why it lives there,
> who is allowed to read it, and how the design satisfies the requirements of a
> regulated Japanese financial-markets product.*
>
> **Status.** Describes the system as implemented in this repository. Every component
> named below maps to a real module under `app/backend/`. Knowledge current as of
> **June 2026**.

---

## 1. Executive summary

Saisei is an early-warning and turnaround-plan orchestrator for Japanese regional
banks. It assesses an SME borrower's financial health, classifies its credit risk
under the FSA framework, and co-authors a regulatory turnaround plan
(経営改善計画書) with a banker in the loop.

The system handles data of fundamentally different character: live precedent
knowledge consulted during a decision, the borrower's financial figures, the
banker's recorded decisions, and the historical corpus used to improve the system
over time. **These are deliberately kept in separate stores, on separate paths,
with separate access patterns and lifecycles.** This separation is not incidental;
it is a governance and reliability requirement.

Three principles govern the entire design:

1. **Numbers are computed deterministically and are the source of truth.** The large
   language model is used only for language and synthesis, never to produce or alter
   a figure, a credit classification, a gate, or a route. Every financial output is
   reproducible and auditable to the yen.
2. **The decision path and the learning path are physically separate.** Data the live
   agent reads to make a recommendation never competes for resources with the
   high-volume stream of data the system writes to learn from later.
3. **The system is safe by default.** With no external services configured, Saisei
   runs fully offline against deterministic mocks. Every integration activates only
   when explicitly configured, and degrades safely to the offline behaviour on any
   error.

---

## 2. The two planes: decision path vs. learning path

Every piece of data in Saisei belongs to exactly one of two planes.

| | **Runtime (decision path)** | **Offline (learning path)** |
|---|---|---|
| **Purpose** | Inform a single live recommendation to a banker | Improve the system over time |
| **Timing** | Synchronous, on the critical path of a run | Asynchronous, never on the critical path |
| **Read pattern** | Read-heavy, online, latency-sensitive | Batch, full-scan, offline |
| **Write pattern** | Rare (cache warming only) | Append-only, higher volume |
| **Who reads it** | The live agent | Analysts, evaluation jobs, calibration jobs |
| **Failure impact** | Degrades recommendation quality, never correctness | None on the live product |

The decisive architectural rule follows from this table: **the learning data is
write-mostly, read-offline, and is never queried by the live agent while it is making
a decision.** Keeping the two planes separate means a firehose of learning writes can
never slow down or destabilise a live borrower assessment.

---

## 3. Agent memory on the decision path

When the feasibility critic evaluates a proposed turnaround strategy, it recalls
relevant precedent context — excerpts of past turnaround plans, industry benchmarks,
and passages from the FSA inspection manual. This recall is **advisory only**: it
enriches a human-readable note and never feeds a deterministic score, band, gate, or
route.

Precedent recall is modelled on human memory, with two tiers mapped onto
infrastructure the platform already operates. **These two tiers are not two databases
doing the same job. They are one memory system with a fast cache in front of a durable
store.**

### 3.1 Long-term memory — the durable knowledge base (pgvector)

Long-term memory is the system of record for precedent knowledge: every precedent the
system has learned, embedded as a vector and persisted durably in PostgreSQL using the
pgvector extension. It is comprehensive, it survives restarts, and it is the
authoritative source. Recall is an honest cosine-similarity search over the precedent
table.

- **Module:** `app/backend/tools/retrieval.py` (`PgVectorLongTermMemory`)
- **Ingestion:** `app/backend/tools/retrieval_ingest.py` (idempotent, batch, offline)
- **Table:** `saisei_keikakusho_memory`
- **Character:** durable, comprehensive, slower per query, the source of truth.

### 3.2 Short-term memory — the hot recall cache (RediSearch)

Short-term memory is a fast, in-memory recall cache in Redis, holding the precedents
touched most recently. It is consulted first because it is hot and cheap. Its entries
carry a time-to-live (default one hour) and are *expected* to expire — that expiry is
the feature that makes the tier “short-term.” Losing a cache entry costs only latency,
never correctness.

- **Module:** `app/backend/tools/retrieval.py` (`RediSearchShortTermMemory`)
- **Index:** `saisei_keikakusho_stm`
- **Character:** ephemeral, fast, disposable, a strict subset of long-term memory.

### 3.3 How the two tiers work together (cache-over-store)

The orchestrator (`TwoTierRetrievalProvider`) ties them together exactly as human
recall works:

1. **Check short-term memory first.** If a relevant precedent was seen recently, return
   it immediately.
2. **Fall back to long-term memory** on a cache miss — the durable corpus of everything
   ever learned.
3. **Consolidate.** Write the long-term result back into short-term memory so the next
   similar query is served hot.

**This is the standard “working memory over long-term memory” pattern, mapped onto
infrastructure already in operation. There is no duplication and no third vector engine
is required.** RediSearch holds a time-limited subset of what pgvector holds durably;
Redis trades durability for latency, PostgreSQL trades latency for durability and
completeness.

Every tier is best-effort. If neither tier is configured, retrieval falls back to a
deterministic mock that returns no precedents, so the workflow runs fully offline and
is never broken by a missing or failing service.

```text
┌─ RUNTIME (online, on the decision path) ────────────────────────────┐
│                                                                       │
│  banker query ─▶ feasibility_critic.search(query, top_k)              │
│                       │                                               │
│                       ▼  TwoTierRetrievalProvider                     │
│     1. SHORT-TERM  RediSearch (TTL ~1h) ── hit ──▶ return (hot, ~ms)   │
│                       │ miss                                          │
│     2. LONG-TERM   pgvector (durable)   ── hit ──▶ return              │
│                       │                  └─ 3. write-back ▶ RediSearch  │
│  (neither configured) ─▶ Mock provider ─▶ [] (offline-safe)            │
└──────────────────────────────────────────────────────┘

   Ingestion (offline, batch): seed corpus / back-catalogue / FSA manual
       └─▶ retrieval_ingest.py ─ embed ─▶ pgvector (saisei_keikakusho_memory)
```

---

## 4. Borrower run state and durable execution

A single borrower assessment can pause for hours while it waits for a banker decision.
The state of that one run — the figures, the proposed strategies, the errors recorded
along the way — is persisted by the LangGraph checkpointer in PostgreSQL, keyed by a
per-session thread identifier. This is what makes the human-in-the-loop interrupt
durable: the process can restart and the run continues exactly where it paused.

- **Configuration:** `SAISEI_POSTGRES_DSN` (`app/shared/settings.py`)
- **Character:** durable per-run state; the substrate for interrupt, resume, and audit.

---

## 5. The learning path (offline, never on the decision path)

The learning path is how Saisei improves. It captures what happened and what the human
decided, then turns that record into evidence for tuning the system. None of it runs on
the live decision path.

### 5.1 Execution trajectories — LangSmith

Every graph run can emit a full trace to LangSmith for offline analysis and evaluation.
Trajectories are append-only, time-series, and read offline by analysts and evaluation
jobs — never re-read by the live agent. Tracing is strictly opt-in: with no LangSmith
key configured, no traces are emitted and no network calls are made.

- **Module:** `app/backend/observability.py` (`configure_tracing`)
- **Character:** write-mostly, offline, opt-in.

### 5.2 Human decisions — the labelled outcomes corpus

Two categories of human judgement are captured as the raw material for future
evaluation and tuning:

- **HITL banker decisions** (approve / revise / reject) are captured as labelled
  examples in a LangSmith dataset (`saisei-hitl-decisions`) by
  `capture_hitl_feedback`. Strictly opt-in and a no-op when tracing is unconfigured.
- **Reconciliation outcomes** — the “who-was-right” record of whether a surfaced
  disagreement was genuine — are appended to the run state
  (`reconciliation_outcomes`) and persisted with the checkpointer in PostgreSQL.

- **Character:** append-only, low-volume, human-labelled, read in batch by offline jobs.

### 5.2a Agent-trajectory store — the training corpus

The richest learning signal is the **agent-trajectory store**
(`app/backend/trajectory/`): one append-only `TrajectoryRecord` per HITL decision
capturing the inputs digest (`data_version`), the proposed strategy slate, the
banker's decision + note, the approved strategy, the final plan, **the full
per-node trajectory** (`node_trajectory`: each graph node's output digest) and
**the raw interrupt payload** the banker saw. Each record carries a deterministic
SHA-256 `content_hash` (same canonicalisation as the audit ledger), so it is
tamper-evident and de-duplicable.

It is the offline training corpus: `preference_pair()` derives the
`(chosen, rejected, critique)` triple every preference-optimisation pipeline
(SFT / DPO / ORPO + a revision-note reward model) needs, and the per-node path
makes those pairs trainable by offline replay.

The capture is a strict side-record: `record_trajectory` runs *after* the node
return dict is assembled, is best-effort and never fatal, and never touches a
gate, route, score, figure, or verdict. Persistence is **opt-in** behind a
`TrajectoryStore` seam (`NullTrajectoryStore` offline default → no-op /
`InMemoryTrajectoryStore` for tests / `PostgresTrajectoryStore` for production,
an append-only `saisei_trajectory` table with a `BEFORE UPDATE OR DELETE`
trigger, same defence-in-depth as the audit ledger). Enabling it via
`SAISEI_TRAJECTORY_DSN` is the bank's explicit data-governance decision;
financial data never leaves the bank's VPC.

- **Modules:** `app/backend/trajectory/{record,recorder,store,store_postgres}.py`
- **Character:** append-only, opt-in, write-only on the decision path, offline-read.

### 5.3 Evidence-based calibration

The outcomes corpus has a concrete payoff: it converts recorded human judgement into an
**advisory** recommendation for a sensitive threshold (the reconciliation band
distance), replacing a hand-chosen constant with an evidence-based one. The calibration
is pure, deterministic, offline, and advisory only — it produces a report and never
edits a constant, a gate, or a route, and never calls an LLM.

- **Modules:** `app/backend/analysis/threshold_calibration.py` (pure analysis),
  `app/backend/analysis/calibrate_cli.py` (`make calibrate`)
- **Character:** batch, full-scan, offline, reproducible.

```text
┌─ OFFLINE (data flywheel, never on the decision path) ──────────────┐
│  every run        ─▶ LangGraph ─emit─▶ LangSmith traces (opt-in)        │
│  HITL decision    ─▶ capture_hitl_feedback ─▶ LangSmith dataset        │
│  HITL decision    ─▶ record_trajectory ─▶ Postgres saisei_trajectory    │
│                          (full per-node path + interrupt payload, opt-in)│
│  reconciliation   ─▶ reconciliation_outcomes ─▶ Postgres (run state)    │
│                          └─▶ calibrate_cli ─▶ advisory threshold report │
│                          └─▶ [future] training / fine-tuning corpus     │
└────────────────────────────────────────────────────┘
```

---

## 6. Roles at a glance

Each store has exactly one role. They do not overlap.

| Store | Plane | Role | Lifecycle |
|---|---|---|---|
| **pgvector** (`saisei_keikakusho_memory`) | Runtime | Durable RAG source of truth (long-term memory) | Persistent, accumulates |
| **RediSearch** (`saisei_keikakusho_stm`) | Runtime | Hot recall cache over pgvector (short-term memory) | Ephemeral, TTL'd |
| **PostgreSQL checkpointer** | Both | Durable per-run state; reconciliation outcomes corpus | Persistent, append-only |
| **Trajectory store** (`saisei_trajectory`) | Offline | Append-only training corpus (per-node path + interrupt payload + preference pairs) | Persistent, append-only, opt-in |
| **LangSmith** | Offline | Trajectory and decision capture | Append-only, opt-in |
| **Calibration analysis** | Offline | Advisory threshold recommendation | Batch, derived |

---

## 7. Design decision: no dedicated vector store for the flywheel

A recurring question is whether the flywheel data (trajectories and training data)
should be placed in its own embedded or file-backed vector store. **For the production
system, the answer is no**, for three reasons:

1. **The query does not need vector search.** Flywheel data is append-only and is read
   by full scan (“all labelled outcomes since date X”), not by nearest-neighbour search.
   A vector store would optimise for a query the system does not run on this data.
2. **It would add governance surface for no benefit.** In regulated finance, every
   additional store is one more thing to secure, back up, and explain in an audit. The
   benefit here is hypothetical; the cost is real.
3. **The durable substrate already exists.** PostgreSQL for the corpus and run state,
   LangSmith for traces, Redis for hot recall. A dedicated vector store would overlap
   pgvector rather than fill a gap.

**The one acceptable exception is developer tooling**: an engineer running ad-hoc
semantic search over an exported trajectory on a laptop, entirely outside the production
system. That stays strictly out of the runtime spine.

---

## 8. Data governance posture

- **Deterministic source of truth.** All financial figures, credit classifications,
  gates, and routes are produced by auditable Python, not by the LLM. The model is
  confined to language and is forbidden from altering figures.
- **Least privilege by plane.** The live agent reads only what it needs to make a
  recommendation. Learning data is isolated on the offline plane and is never read on
  the decision path.
- **Advisory boundaries are explicit.** Precedent recall and threshold calibration are
  advisory by construction and cannot change a deterministic outcome.
- **Safe by default, explicit by configuration.** Every external integration (LLM,
  embeddings, pgvector, RediSearch, BOJ, NTA Corporate Number, LangSmith) is disabled
  until configured and degrades safely to deterministic offline behaviour on error. The
  system is fully runnable and testable with zero external dependencies.
- **No secrets in source.** All configuration is environment-sourced via typed settings;
  credentials are never committed.
- **Auditability end to end.** Per-run state is durably checkpointed; structured logging
  records every node event; opt-in tracing provides a complete trajectory record.

---

## 8a. Immutable audit ledger (Feature 7)

Beyond per-run checkpointing and tracing, Saisei keeps a dedicated **append-only,
tamper-evident audit ledger** of the events a regulator cares about: every
classification, every guarantee-release assessment, and every human decision
— *who* decided *what*, *when*, and *on which version of the data and
thresholds*. The checkpointer persists the *current* state for resume; the
ledger is the *historical compliance record* that can be queried after the fact.
It is implemented under `app/backend/audit/`.

**What is recorded.** Four event kinds (`classification`,
`guarantee_release`, `human_decision`, `companion_query`), each emitted as a
side-record at the node edge by `record_event` — *after* the deterministic node
has produced its result, so capturing an event never changes a gate, route,
score, or figure. The write is best-effort and never fatal: a ledger outage can
never break the regulated workflow (it is logged and swallowed, exactly like
tracing).

**Advisory-companion questions (`companion_query`).** The summonable advisory
companion (`app/backend/agents/saisei_chat.py`) is read-only and never decides,
but a free-form conversation about a case can still *shape* a banker's thinking
— so it must leave a trail like any other event a regulator cares about. Each
banker question is recorded as a `companion_query` event pinned to the
`data_version` in force at ask time, with a payload of the question text, the
routed intent, the answer's grounding status (grounded vs. carrying unverified
commentary), and the cited evidence ids. The *answer prose itself is not stored*
— it is reproducible from the pinned data version and the ledger stays lean. The
on-screen chat transcript remains **ephemeral** (it is never persisted as a
transcript store); the durable, compliance-relevant record is this audit event,
which rides the same hash chain as everything else. Like every audit write it is
opt-in (`SAISEI_AUDIT_DSN`) and an offline no-op by default.

**Tamper-evidence (the hash chain).** Each event stores a SHA-256
`content_hash` over its canonical JSON, plus the `prev_hash` of the previous
event for the same `thread_id`. The events therefore form a per-borrower hash
chain: any retro-edit of a stored event, or any removed/reordered event, breaks
the chain and is detected by `verify_chain`, which reports the first offending
event. Canonicalisation (sorted keys, `ensure_ascii=False`, compact separators,
excluding the hash from its own input) is fixed forever so historical hashes
stay reproducible.

**Data-version pinning.** Every event records a `data_version` (a hash over the
borrower inputs it was computed from — the trial balances, TDB score,
working-capital gap, net worth, insolvency flag) and a `thresholds_version` (a
hash over the deterministic constants in force). A classification can thus
always be tied to the exact figures and thresholds at decision time, even after
a constant is later re-tuned.

**Append-only storage, defence in depth.** The production `PostgresAuditSink`
is a single `saisei_audit_log` table (it may reuse the checkpointer instance),
guarded at two layers: the application issues only `INSERT` + `SELECT` (there is
no update/delete method on the sink interface), and the database carries a
`BEFORE UPDATE OR DELETE` trigger that raises, so even a direct SQL mutation
fails. Recommended further hardening: a dedicated DB role for the app user
granted only `INSERT, SELECT` on the table. The schema (table, indexes, trigger)
is created idempotently by the in-code bootstrap so a fresh clone / offline run
needs no migration step; for a managed deployment Alembic owns the schema
migration (`make migrate`; the `saisei_audit_log` table reuses the sink's
`SCHEMA_SQL` as the single source of truth).

**Safe by default.** With no `SAISEI_AUDIT_DSN` configured the sink is a no-op
`NullAuditSink`, so the system stays fully offline and byte-stable — identical
posture to every other integration. A read-only examiner surface,
`GET /audit/{thread_id}`, returns the ordered events plus the chain verdict as
JSON; it is an examiner tool (not the banker UI). The OIDC bearer-token
transport has shipped (`app/backend/auth.py`), so this and the append-only audit
admin actions (redaction / legal hold) resolve the caller through the shared
`require_identity` seam: when a deployment configures its identity provider
(`SAISEI_AUTH_JWKS_URL` / issuer / audience) a verified identity is required and
recorded as the actor; offline they run under the placeholder identity, gated by
`SAISEI_AUTH_REQUIRED`.

---

## 9. Bottom line

Saisei's data architecture is intentionally conservative for a regulated-finance
product. It uses a fast cache over a durable store for agent memory, keeps the live
decision path strictly separate from the offline learning path, computes every figure
deterministically, and activates each external system only on explicit configuration.
The result is a system whose every data flow can be explained to an engineer, an
auditor, a regulator, or an investor with the same diagram.
