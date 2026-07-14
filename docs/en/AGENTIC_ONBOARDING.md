# ONBOARDING.md — Agentic AI, taught through Saisei

> A guided curriculum for an AI engineer learning to build **production-grade agentic
> systems**, using this codebase as the worked example. Knowledge current as of **June 2026**.
>
> Read it top to bottom with the source open. Every concept points at a real file you can run.

---

## 0. What "agentic" actually means (and what it doesn't)

An **agent** is an LLM-driven system that takes actions in a loop toward a goal, where the
model's output influences which step runs next. The spectrum, from least to most agentic:

1. **Single prompt** — one call, one answer.
2. **Chain / workflow** — fixed sequence of calls (deterministic control flow).
3. **Router** — the model picks among predefined branches.
4. **Agent** — the model drives a loop with tools, memory, and dynamic control flow.
5. **Multi-agent** — several specialised agents collaborate, often with a supervisor.

**Key 2026 lesson the field has internalised:** *use the least agentic design that solves
the problem.* Free-form autonomy is impressive in demos and fragile in production.
Saisei is deliberately a **structured graph with one human-in-the-loop interrupt**, not a
free-roaming autonomous agent — because it operates in regulated finance where every step
must be explainable. **Match the autonomy to the stakes.**

> 🔎 In this repo: `app/backend/graph.py` is the entire control flow, on one screen.
> You can *read* exactly what the system can and cannot do. That is a feature.

## 1. State is the heart of an agent

Everything an agent "knows" during a run lives in its state. Get the state model right and
the nodes become simple pure-ish functions; get it wrong and you fight it forever.

**Principles:**

- Make state **typed and validated** (here: Pydantic V2, `extra="forbid"`). A typo becomes a
  loud error, not a silent `None`.
- Nodes return **partial updates** (plain dicts), not the whole state — this keeps nodes
  composable and makes reducers explicit.
- Keep **derived values out of state**; compute them (see the `@computed_field` profit lines
  in `accounting.py`). Storing derived data is how state goes stale.

> 🔎 Study: `app/backend/state.py` (the shared `SaiseiState`) and how each node in
> `app/backend/nodes/` returns only the fields it owns.

**Exercise:** add a `confidence: float` field to the EWS output. Notice you only touch the
state model and one node — nothing else breaks. That decoupling is the payoff of good state design.

## 2. The graph: nodes, edges, and conditional routing

A graph makes control flow **data**. Nodes are steps; edges are transitions; conditional
edges let the *result* of a node choose the next step.

- **Linear edges** for the deterministic assessment pipeline
  (`intake → ews → macro → classifier`).
- **Conditional edges** for decisions: `route_after_classification` ends the run for a
  healthy borrower and branches to the strategist otherwise.
- **Cycles** for negotiation: `revise` routes *back* to the strategist — a loop, expressed
  declaratively.

> 🔎 Study: `route_after_classification` and `route_after_negotiation` in `app/backend/graph.py`.
> They are tiny pure functions — trivially unit-testable (`tests/test_graph_flow.py`).

**Why a graph and not just Python `if`s?** Because the graph is also a **checkpoint boundary**
and an **observability boundary**. Each node is a place to persist, trace, retry, and resume.
That is what the next two sections are about.

## 3. Human-in-the-loop (HITL): the most underrated pattern

The single highest-leverage pattern for shipping agents into real workflows is **knowing
when to stop and ask a human.** In 2026, HITL is not a fallback for weak models — it is the
correct design for any high-stakes, irreversible, or regulated action.

Saisei implements the canonical pattern:

- The `hitl_negotiation` node calls **`interrupt(payload)`**, which *suspends* the graph and
  surfaces the proposed strategies to a banker.
- Execution resumes only when the application calls **`Command(resume={...})`** carrying the
  banker's decision.
- Because a person might take hours, the state is **persisted by a checkpointer** across the
  pause — the process can restart and the run continues.

> 🔎 Study: `app/backend/agents/turnaround_orchestrator.py` (the interrupt + decision
> handling) and `app/frontend/state.py` (`run_assessment` → pause → `approve/revise/reject`
> → resume). This is a complete, real HITL loop end to end.

**Mental model:** an interrupt is a function call whose return value comes from a human,
minutes or days later. The checkpointer is what makes that possible.

## 4. Persistence & memory

Two different things people conflate:

- **Short-term / thread memory** = the state of *one run*, persisted by the **checkpointer**
  (here: Postgres), keyed by `thread_id`. This is what enables interrupt/resume and time-travel.
- **Long-term memory** = knowledge that outlives a run (past plans, embeddings, user prefs).
  Saisei doesn't need it yet; `NEXT_STEPS.md` adds it as a RAG layer.

> 🔎 Study: `postgres_checkpointer()` in `app/backend/graph.py` and how the UI passes a
> per-session `thread_id`. Swap in `MemorySaver` (as the tests do) and the same graph runs
> in-memory.

## 5. Determinism, tools, and the role of the LLM

A 2026 production lesson, learned the hard way across the industry: **don't let the LLM do
things deterministic code does better.** Arithmetic, classification with clear thresholds,
and formatting are code's job. The LLM is for language, ambiguity, and synthesis.

Saisei embodies this:

- EWS scoring, the working-capital estimate, and FSA classification are **plain Python**
  (`app/backend/nodes/ews_scoring.py`, `app/backend/nodes/financial_extraction.py`) —
  auditable and testable to the yen.
- The LLM appears **once**, as an *optional* polish pass on the final document
  (`polish_keikakusho` in `app/backend/nodes/kaizen_generation.py`), and is explicitly
  forbidden from changing figures.
- It **degrades to a no-op** when no model is configured, so the system is fully testable
  offline and never breaks on a flaky API.

> 🔎 Study: `polish_keikakusho` — note the best-effort try/except that returns the
> deterministic draft on any failure. This "LLM as enhancement, never as dependency" stance
> is how you ship reliable agents.

**Tool-calling note:** here the "tools" are typed data-provider methods
(`MockDataProvider.credit_report`, `.shisanhyo`, ...). Whether a tool is invoked by the LLM
or wired into the graph, the same rules apply: **typed inputs/outputs, validated at the
boundary, with explicit error handling.** A KeyError for an unknown company becomes a
recorded error in state, not a crash (`intake_node` in `app/backend/nodes/financial_extraction.py`).

## 6. Reliability: errors, idempotency, and the data seam

- **Errors as state, not exceptions that kill the run.** Nodes append to `state.errors` and
  continue where sensible (see `intake_node`, `ews_node`). The graph stays inspectable.
- **A clean integration seam.** Every external call goes through `MockDataProvider`, so the
  whole system is testable offline *and* live clients drop in with zero graph changes
  (`app/backend/tools/provider.py`). Designing this seam early is what makes section 3 of
  `NEXT_STEPS.md` a config change rather than a rewrite.
- **Structured logging everywhere** (`structlog`, `print` is banned). Every node emits a
  typed event (`ews.scored`, `hitl.approved`) — these become your traces and metrics.

## 7. Evaluation & observability (where the real work is in 2026)

Building the agent is ~30% of the job; *knowing it still works after every change* is the
rest. The discipline that separates a demo from a product:

- **Unit-test the deterministic core** exhaustively (this repo: money, accounting,
  classifier, mocks).
- **Test the graph's control flow** with an in-memory checkpointer, including the full
  interrupt/resume cycle (`tests/test_graph_flow.py`).
- **Add trace-based evals** for any LLM step: golden datasets, LLM-as-judge with a cheap
  deterministic pre-check, and regression gates in CI (see `NEXT_STEPS.md` §1).
- **Capture trajectories** — every run and every human decision — as both an audit trail and
  future training data (`NEXT_STEPS.md` §2). The data flywheel is the moat.

## 8. Multi-agent: when, and when not

Multi-agent systems are powerful and **expensive in tokens, latency, and debugging
surface.** The 2026 consensus: reach for multi-agent only when tasks are genuinely
parallel or need distinct specialised skills/contexts. A supervisor coordinating focused
sub-agents beats a swarm of generalists.

Saisei is single-graph today. The natural multi-agent evolution (in `NEXT_STEPS.md`) is a
separate **feasibility-critic agent** that challenges the strategist's proposals before the
banker sees them — a classic *generator/critic* pair, which is one of the few multi-agent
patterns that reliably earns its cost.

---

## A suggested learning path through this repo

1. `app/shared/models/money.py` + `tests/test_money.py` — typed domain primitives done right.
2. `app/shared/models/accounting.py` — immutable records with computed (not stored) values.
3. `app/backend/state.py` — the shared state that ties the agent together.
4. `app/backend/nodes/*.py` — read them in pipeline order; note each returns a partial update.
5. `app/backend/graph.py` — see the whole control flow, edges, and checkpointer in one file.
6. `app/backend/agents/turnaround_orchestrator.py` — the interrupt/resume heart of the system.
7. `tests/test_graph_flow.py` — watch the entire agent run, pause, and resume in a test.
8. `app/frontend/state.py` — how a UI drives an interruptible graph in production.
9. `polish_keikakusho` in `app/backend/nodes/kaizen_generation.py` — the disciplined,
   optional, never-load-bearing use of an LLM.
10. `NEXT_STEPS.md` — how all of the above scales into a real product.

## Core mental models to walk away with

- **Least autonomy that works.** Structure beats free-roaming agents in production.
- **State is the architecture.** Type it, validate it, keep derived data out.
- **The graph makes control flow into data** — and gives you checkpoints, traces, and resume points.
- **Stop and ask a human** for high-stakes, irreversible actions. HITL is a first-class design, not a patch.
- **Deterministic core, LLM at the edges.** The model assists; it is never load-bearing for correctness.
- **Errors are state.** Keep the run inspectable.
- **Design the integration seam early.** It's the difference between a config change and a rewrite.
- **Evals and trajectory capture are the product**, not an afterthought.
