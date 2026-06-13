# NEXT_STEPS.md — From prototype to product

Saisei today is a clean, deterministic, fully-tested **prototype**: the agent graph runs
end to end, the HITL loop works, and the whole thing is offline-capable. This document is
the honest roadmap for turning it into a **production banking product**. It is ordered
roughly by leverage, and each item names *why* it matters and *what* it concretely involves.

## Guiding principles

- **Determinism stays the source of truth.** Every figure is computed and auditable; LLMs
  assist with language and reasoning, never with numbers.
- **Everything observable, everything replayable.** A regulated lender must be able to
  explain any decision after the fact.
- **Human authority is non-negotiable.** The banker approves; the system proposes.

---

## 1. LLMOps & observability (LangSmith)

**Why:** Once any LLM is in the loop (strategy reasoning, plan polish), "it worked on my
machine" is not good enough. We need traces, evals, cost, and latency per node.

**What:**

- Instrument the graph with **LangSmith** tracing (`LANGCHAIN_TRACING_V2`), tagging every
  run with `tdb_code`, `thread_id`, FSA class, and node name.
- Build a **LangSmith eval suite**: a golden dataset of (financial profile → expected FSA
  class, expected strategy themes). Run it in CI and block merges on regression.
- Add **LLM-as-judge** evals for the Keikakusho polish step: did it preserve every figure?
  Did it keep the FSA classification verbatim? (A numeric-preservation check can be a cheap
  deterministic guard *before* the judge.)
- Track **cost & latency budgets** per node; alert when a node exceeds its SLO.
- Promote prompts out of source code into a **versioned prompt registry** (LangSmith Hub or
  an internal table) so prompt changes are reviewable and rollback-able.

## 2. Agent-trajectory data flywheel

**Why:** Every banker decision (approve / revise / reject + the note) is a high-quality
human preference label. Captured well, this becomes the training signal that makes the
strategist measurably better over time — the core of a defensible product.

**What:**

- Persist **full trajectories**: input state, each node's output, the interrupt payload,
  the banker's `Command(resume=...)`, and the final plan — to an append-only store
  (Postgres + object storage for large artifacts).
- Treat each negotiation as a **preference pair**: approved strategy (chosen) vs. the
  alternatives (rejected), plus the free-text revision note as a critique.
- Build offline pipelines for:
  - **Supervised fine-tuning** of a smaller open model on accepted strategies/plans.
  - **Preference optimisation (DPO/ORPO)** from approve-vs-reject pairs.
  - **Reward-model training** on the revision notes to predict banker acceptance.
- Close the loop with **offline replay**: re-run historical trajectories against a candidate
  model and measure how often it would have proposed the strategy the banker actually chose.
- Strict **PII / data-governance** boundary: financial data never leaves the bank's VPC;
  training runs on-prem or in a dedicated tenancy.

## 3. Replace the mocks with live integrations

**Why:** The whole architecture was built around a swappable `MockDataProvider` seam;
realising it is what makes Saisei real.

**What:**

- Implement `CoreBankingClient`, `TdbClient`, `EdinetMacroClient` against real APIs behind
  the existing interface. No graph changes.
- Add **resilience**: retries with backoff, circuit breakers, response caching in Redis,
  and schema-validation at the boundary (the Pydantic models already enforce shape).
- **Reconciliation tests** comparing live vs. expected J-GAAP invariants (e.g. gross profit
  = sales − COGS) to catch upstream data drift.

## 4. Harden the agent reasoning

**Why:** Today the strategist uses fixed heuristics (3% price, 2% COGS, 5% SG&A). That is
the right *starting* point (grounded, explainable) but a real product should reason about
feasibility per industry and customer concentration.

**Delivered:** a **feasibility-critic** node already stress-tests each strategy before it
reaches the banker, enriched by an advisory **two-tier agent-memory RAG** — a pgvector
long-term store (past plans, benchmarks, FSA passages) behind a RediSearch short-term
cache. Retrieval is advisory-only and never moves a deterministic band, score, gate, or route.

**What's next:**

- **Grow the corpus** beyond the seed set: ingest the bank's full back-catalogue of
  successful Keikakusho and the FSA manual, with per-source access controls.
- **Real embeddings + an ANN index** (e.g. pgvector HNSW) once the corpus is large enough
  to need it; today the offline embedder keeps the path testable with no network.
- **Cite precedent in the plan** so each proposal links the cases that informed it.
- **Model multi-period projections** (12–36 month P&L bridge) instead of single-month
  annualisation, and show the recovery path to 正常.

## 5. Productionise the platform

**What:**

- **API surface:** expose the graph over FastAPI with auth (OIDC), per-bank tenancy, and
  idempotent run/resume endpoints keyed by `thread_id`.
- **Async + scale:** move long LLM/RAG calls off the request path; use Redis/Celery (already
  a dependency) or LangGraph's async runtime; horizontal-scale stateless workers.
- **Migrations:** Alembic for the Postgres schema beyond the checkpointer tables.
- **Secrets:** Vault / cloud secret manager; the codebase already bans hardcoded secrets.
- **Export:** render the Keikakusho to PDF/DOCX with the bank's template and e-signature hooks.

## 6. Compliance, security, audit

**Why:** This is regulated lending; trust is the product.

**What:**

- **Immutable audit log** of every classification and every human decision (who, what, when,
  on what data version).
- **Explainability report** attached to each FSA classification (which signal crossed which
  threshold) — the deterministic core makes this straightforward.
- **Model cards & change log** for any deployed model; record the data snapshot it was
  trained on.
- **Bias / fairness review** across industries and regions; the anti-social-forces check
  must be auditable and overridable by a human.

## 7. Testing & quality, levelled up

- **Property-based tests** (Hypothesis) on the money type and accounting invariants.
- **Snapshot tests** for rendered Keikakusho Markdown.
- **Load tests** for concurrent assessments and the checkpointer under contention.
- **Mutation testing** to confirm the suite actually catches regressions.

---

## Suggested sequencing

1. LangSmith tracing + a golden eval set in CI (cheap, immediate safety net).
2. Trajectory capture (start collecting data *before* you need it).
3. One live integration (TDB) behind the existing seam.
4. Grow the precedent corpus and add real embeddings + an ANN index to the delivered RAG.
5. Preference optimisation from the captured trajectories.
6. Compliance/audit hardening in parallel throughout.
