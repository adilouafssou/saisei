
<div align="center">

# 再生 (Saisei)

<img src="./assets/Saisei_logo.png" alt="Saisei Logo" width="400">

**Autonomous early-warning & turnaround-plan orchestrator for Japanese regional banks.**

[![CI](https://github.com/adilouafssou/saisei/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/adilouafssou/saisei/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2.svg)](https://mypy-lang.org/)
[![Code style: reflex](https://img.shields.io/badge/UI-Reflex-5b4cdb.svg)](https://reflex.dev/)

</div>

Saisei watches an SME borrower's financial health, classifies its credit risk under the FSA
framework, and co-authors a regulatory turnaround plan (経営改善計画書) with a banker in the loop.

Built on **LangGraph** (stateful multi-agent orchestration with native human-in-the-loop),
**Reflex** (Python-native UI), and a strict **Pydantic V2 / mypy --strict** domain core.

---

## The problem

Under the FSA **Financial Inspection Manual** (金融検査マニュアル), a regional bank must continuously
assess each borrower and, when health deteriorates, **help draft a turnaround plan** rather than
call the loan. Today that work is manual and slow:

- Relationship managers eyeball monthly trial balances (試算表) for trouble — sliding sales, margin
  compression, failed price pass-through.
- Credit classification (正常 / 要注意 / 要管理) varies by officer.
- Working-capital stress from BOJ rate normalisation and T+1/T+2 settlement is rarely modelled.
- The plan itself is written from a blank page.

The cost: **early-warning signals are caught late, and support arrives after the SME is already
in distress.**

## The solution

Saisei runs the full assessment a relationship manager would, then **pauses for the banker**
before committing to a strategy:

1. **Intake** — resolve identity (7-digit TDB code → 13-digit 法人番号), pull the credit report,
   run an anti-social-forces check (反社会的勢力).
2. **EWS scoring** — a 0–100 Early Warning Signal from trends in the monthly J-GAAP trial balances.
3. **Macro stress** — fold in the BOJ rate curve and settlement liquidity to estimate the
   working-capital gap (資金繰り).
4. **FSA classification** — map signals to a debtor class. Normal borrowers are monitor-only;
   the rest enter the turnaround workflow.
5. **Strategy proposal** — grounded strategies (price pass-through, COGS reduction, SG&A
   rationalisation, working-capital repair) with uplift derived from the firm's *actual* figures.
6. **Human-in-the-loop** — the graph **interrupts**; the banker approves, requests a revision,
   or escalates.
7. **Plan authoring** — a deterministic Keikakusho draft in Markdown, with an *optional* LLM
   polish that improves prose while preserving every figure.

> **Design stance:** numbers are computed deterministically and are the source of truth; the LLM
> only polishes prose and never invents a figure. The system runs and tests fully **offline**,
> with no LLM configured.

## Architecture

A single LangGraph `StateGraph` over a shared Pydantic V2 state. Data-loading nodes sit behind a
`MockDataProvider` interface, so deterministic mocks swap for live Core Banking / TDB / EDINET
clients **without touching the graph**.

```
            START
              │
              ▼
          [intake] ── TDB code → Hojin Bango, profile, anti-social check
              │
              ▼
           [ews] ── monthly Shisanhyo → 0–100 Early Warning Signal
              │
              ▼
          [macro] ── BOJ rates + T+1/T+2 settlement → Shikin Kuri gap
              │
              ▼
        [classifier] ── FSA class: 正常 / 要注意 / 要管理
              │
              ▼
     [keieisha_hosho] ── 経営者保証 release score + succession readiness
              │            (runs for ALL borrowers — even healthy ones)
   ┌──────┴────── conditional edge (fsa_classification) ──────┐
   ▼ 正常 (Normal)                                  要注意 / 要管理 ▼
  END (monitor only)                                    [strategist] ◀───────────┐
                                                             │                    │
                                  ┌──────────fan-out──────────┼─────────┐        │
                                  ▼                           ▼          ▼        │
                       [main_bank_critic]        [sub_bank_critic]  [guarantor_critic]
                          (P1 accountability)     (P2 fairness)     (P0 compliance)
                                  └──────────fan-in───────────┼─────────┘        │
                                                             ▼                    │
                                                     [lead_arranger]              │
                                              (Torimatome: consensus +            │
                                               burden-sharing table)              │
                                                             │                    │
                                  ┌──── route_after_lead_arranger ────┐           │
                                  ▼ approved / needs_human            ▼ rejected  │
                                  │                          (revision_count<3) ──┘
                                  │                          (count≥3) → END (escalate)
                                  ▼
                       ╭─ interrupt() ─────────────╮
                       │ [hitl_negotiation]        │  ← banker reviews & decides
                       ╰────────────────────────╯  (the only real decider)
                                  │
           ┌──────────────┼────── Command(resume=...) ──┐
           ▼ approve           ▼ revise                 ▼ reject
     [plan_writer]        back to [strategist]      END (escalate)
           │
           ▼
   Keikakusho draft → (optional LLM polish) → END
```

Every step is a deterministic node except `hitl_negotiation`, the one true agent driving the
`interrupt()`/`Command(resume=...)` pause. The three critics and `lead_arranger` are rule-based
gates — verdicts and numbers are never produced by an LLM. `keieisha_hosho` runs for all
borrowers; the critics and `lead_arranger` run only for distressed (要注意 / 要管理) ones. See
[`HANDOFF.md`](HANDOFF.md) for the planned Part 4 multi-agent *simulator* layer.

### Key decisions

| Concern | Decision | Rationale |
|---|---|---|
| Orchestration | LangGraph `StateGraph` | First-class state, conditional edges, native `interrupt()`/`Command(resume=...)` for HITL. |
| State persistence | Postgres checkpointer | The HITL pause can last days; state must survive restarts. |
| Money | Custom `JPY` int type | Yen principal is integer-only; the type rejects fractional floats at validation time. |
| Domain model | Pydantic V2, `frozen`, `extra="forbid"` | Immutable, closed financial records; typos and stray fields fail loudly. |
| FSA classes | Closed `StrEnum` | The regulatory set is exactly three values — the type makes a fourth impossible. |
| LLM | Optional, polish-only | Determinism and auditability first; the model never produces a figure. |
| Data sources | `MockDataProvider` seam | Live clients drop in behind one interface with zero graph changes. |

### Stack

| Layer | Tech |
|---|---|
| Frontend | Reflex >= 0.6 |
| Backend / API | FastAPI + LangGraph >= 0.2 |
| State | PostgreSQL (psycopg v3 checkpointer) |
| Cache / queue | Redis |
| Agent memory | pgvector (long-term) + RediSearch (short-term) |
| Tooling | uv, ruff, mypy (strict), pytest, structlog |

### Agent memory (advisory RAG)

The feasibility critic enriches its **advisory-only** note with retrieved precedents (past plans,
benchmarks, FSA passages). Recall is modelled as two-tier agent memory over the Postgres and
Redis the stack already runs — no new infrastructure:

- **Long-term → pgvector.** The durable precedent corpus, embedded in Postgres. Comprehensive,
  survives restarts. Seeded via `python -m app.backend.tools.retrieval_ingest`.
- **Short-term → RediSearch.** A fast, TTL-bound recall cache in Redis, filled at query time.

Lookups hit short-term memory first, fall back to long-term on a miss, then warm the result back
into short-term memory. Each tier is independently optional (`SAISEI_PGVECTOR_DSN` /
`SAISEI_REDISEARCH_URL`); with neither set, retrieval uses a deterministic mock, keeping the
system testable **offline**. Retrieval is advisory-only and never feeds a band, score, gate, or route.

### Deliberate scope

- **Strict typing covers `backend` and `tests`, not `frontend`.** Reflex models UI as dynamic
  `Var` objects that fight `mypy --strict`; strict-checking the domain core is where the value is.
- **The strategist uses transparent heuristics** (e.g. 3% price increase, 2% COGS cut) over a
  learned model, so every proposed figure stays explainable and grounded in actuals. The learned
  evolution is in [`NEXT_STEPS.md`](docs/en/NEXT_STEPS.md).
- **Classification models 3 of the FSA's 5 tiers** — the "still savable" band the engine acts in
  (see [`DOMAIN_ONBOARDING.md`](docs/en/DOMAIN_ONBOARDING.md)).

## Quick start

```bash
cp .env.example .env      # no LLM key required to run
make setup                # install uv, sync deps, build containers, seed DB
make seed-memory          # (optional) seed pgvector long-term memory
make run-dev              # web + api + postgres + redis
make verify               # ruff + mypy --strict + pytest (the CI gates)
```

The primary fixture is a deteriorating Aichi-prefecture metal-parts manufacturer
(愛知精密製作所株式会社) hit by cost inflation and failed price pass-through, driving it into a
要管理 (Doubtful) classification and a working-capital deficit — the exact case the workflow
exists to handle.

> **Reproducible builds:** `make setup` runs `uv lock`; commit the generated `uv.lock` once and
> CI and the Docker images resolve byte-for-byte identical dependency sets.

## Continuous integration

[GitHub Actions](.github/workflows/ci.yml) runs the same gates as `make verify` — **ruff**
(lint + format), **mypy --strict**, and **pytest** — on every push to `main` and every pull
request. The job installs dependencies with `uv` and runs fully offline (the app falls back
to deterministic mock providers), so CI needs no database, Redis, or API keys. New to GitHub
Actions? See [`docs/GITHUB_CI_SETUP.txt`](docs/GITHUB_CI_SETUP.txt) for a step-by-step guide.

## Repository map

```
app/
  main.py                     FastAPI /health /ready + rx.App() lifespan
  backend/
    state.py                  SaiseiState (Pydantic V2) + reducers + sub-models
    graph.py                  StateGraph wiring, routers, Postgres checkpointer
    agents/                   turnaround_orchestrator (HITL interrupt/resume)
    nodes/                    Deterministic workflow steps
      financial_extraction.py   intake (TDB identity, anti-social) + macro (Shikin Kuri gap)
      ews_scoring.py            EWS compute (0-100) + FSA classify
      kaizen_generation.py      strategist + plan render + optional LLM polish
      keieisha_hosho.py         guarantee-release + succession assessment
      lead_arranger.py          consensus engine (Torimatome fan-in)
      critics/                  3 parallel critic nodes (main_bank / sub_bank / guarantor)
    tools/                    MockDataProvider + fixtures/ (bundled JSON)
      retrieval.py              two-tier agent memory: pgvector (long-term) + RediSearch (short-term)
      retrieval_ingest.py       seed precedent corpus into pgvector long-term memory
    prompts/                  extraction_rules.md, kaizen_templates.md
  frontend/                   Reflex UI (state, components, pages)
  shared/
    constants.py              Single source of truth for all thresholds
    settings.py               pydantic-settings (env prefix SAISEI_)
    models/                   accounting (TrialBalance), money (JPY), classification (FsaClass)
assets/                       Static assets for Reflex (images, fonts, CSS overrides)
tests/                        pytest — domain, nodes, graph flow, end-to-end
```

## Further reading

- [`ROADMAP.md`](docs/en/ROADMAP.md) — the original architectural spec.
- [`NEXT_STEPS.md`](docs/en/NEXT_STEPS.md) — the path from prototype to production.
- [`AGENTIC_ONBOARDING.md`](docs/en/AGENTIC_ONBOARDING.md) — an agentic-AI curriculum taught through this codebase.
- [`DOMAIN_ONBOARDING.md`](docs/en/DOMAIN_ONBOARDING.md) — the Japanese-finance domain for non-specialists.
- [`claude.md`](claude.md) — engineering rules and guardrails.

## 日本語ドキュメント (Japanese documentation)

Japanese translations of all major documents are in `docs/ja/`.

| 日本語訳 | English source |
|---|---|
| [docs/ja/説明.md](docs/ja/説明.md) | [README.md](README.md) |
| [docs/ja/引き継ぎ.md](docs/ja/引き継ぎ.md) | [HANDOFF.md](HANDOFF.md) |
| [docs/ja/ロードマップ.md](docs/ja/ロードマップ.md) | [docs/en/ROADMAP.md](docs/en/ROADMAP.md) |
| [docs/ja/今後のステップ.md](docs/ja/今後のステップ.md) | [docs/en/NEXT_STEPS.md](docs/en/NEXT_STEPS.md) |
| [docs/ja/エージェント入門.md](docs/ja/エージェント入門.md) | [docs/en/AGENTIC_ONBOARDING.md](docs/en/AGENTIC_ONBOARDING.md) |
| [docs/ja/ドメイン入門.md](docs/ja/ドメイン入門.md) | [docs/en/DOMAIN_ONBOARDING.md](docs/en/DOMAIN_ONBOARDING.md) |
| [docs/ja/エンジニアリング規約.md](docs/ja/エンジニアリング規約.md) | [claude.md](claude.md) |
