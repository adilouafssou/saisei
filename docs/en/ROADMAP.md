# Saisei (再生) — Architectural Spec & Roadmap

> Karpathy Method: this Spec is the design of record. It was approved before
> implementation and is kept in sync with the shipped system. For the live
> engineer onboarding (and the planned Part 4 multi-agent simulator), see
> [`../HANDOFF.md`](../HANDOFF.md). For the production roadmap, see
> [`NEXT_STEPS.md`](NEXT_STEPS.md).

## 1. Directory Layout

Everything lives under the unified **`app/`** package (required by Reflex's
compiler; `rxconfig.py` sets `app_name="app"`).

```
saisei-agentic-enginev/
├── claude.md                     # Engineering rulebook (persistent memory)
├── HANDOFF.md                    # Engineer handoff + Part 4 simulator spec
├── README.md
├── Makefile                      # setup / run-dev / run-prod / verify / clean
├── docker-compose.yml            # web + api + postgres (pgvector) + redis
├── Dockerfile
├── pyproject.toml                # uv-managed deps, ruff/mypy/pytest config
├── uv.lock                       # committed — reproducible builds
├── .env.example
├── .github/workflows/ci.yml      # GitHub Actions — runs make verify gates (ruff + mypy --strict + pytest)
├── rxconfig.py                   # Reflex config (app_name="app")
├── assets/                       # Reflex static assets
├── docs/
│   ├── en/                       # English docs (source of truth)
│   │   ├── ROADMAP.md                # This spec
│   │   ├── NEXT_STEPS.md             # Prototype → production roadmap
│   │   ├── AGENTIC_ONBOARDING.md     # Agentic-AI curriculum via this codebase
│   │   └── DOMAIN_ONBOARDING.md      # Japanese-finance domain primer
│   └── ja/                       # Japanese translations
│
└── app/
    ├── __init__.py
    ├── app.py                    # rx.App() + FastAPI /health /ready + lifespan
    ├── backend/
    │   ├── state.py              # SaiseiState (Pydantic V2) + reducers + sub-models
    │   ├── graph.py              # StateGraph wiring + routers + Postgres checkpointer
    │   ├── agents/
    │   │   └── turnaround_orchestrator.py  # the ONLY agent: interrupt()/resume HITL
    │   ├── nodes/
    │   │   ├── financial_extraction.py     # intake (TDB identity, anti-social) + macro
    │   │   ├── ews_scoring.py              # EWS compute (0-100) + FSA classify
    │   │   ├── kaizen_generation.py        # strategist + plan render + optional LLM polish
    │   │   ├── keieisha_hosho.py           # 経営者保証 guarantee-release + succession
    │   │   ├── lead_arranger.py            # consensus engine (Torimatome fan-in)
    │   │   └── critics/
    │   │       ├── main_bank.py            # P1 accountability gate
    │   │       ├── sub_bank.py             # P2 fairness (pro-rata) gate
    │   │       └── guarantor.py            # P0 compliance (recovery path) gate
    │   ├── tools/
    │   │   ├── provider.py                 # MockDataProvider (swappable seam)
    │   │   ├── core_banking.py             # monthly Shisanhyo (J-GAAP)
    │   │   ├── tdb_api.py                  # credit score, anti-social check, profile
    │   │   ├── boj_macro.py                # BOJ rate curve + settlement liquidity
    │   │   ├── retrieval.py                # two-tier agent memory (pgvector + RediSearch)
    │   │   ├── retrieval_ingest.py         # seed pgvector long-term memory (SQL + embeddings)
    │   │   ├── embeddings.py               # embeddings (OpenAI-compatible + offline fallback)
    │   │   └── fixtures/
    │   │       ├── aichi_manufacturer.json # primary SME (genka koutou / kakaku tenka)
    │   │       └── rag_seed_corpus.json    # starter precedent corpus (long-term memory)
    │   └── prompts/
    │       ├── extraction_rules.md
    │       └── kaizen_templates.md
    ├── frontend/                 # Reflex UI
    │   ├── state.py              # streamed meeting-room state (phase + transcript)
    │   ├── theme.py              # design tokens + Persona identity registry
    │   ├── components/           # avatar, meeting_panel, ews_dashboard, shisanhyo_table
    │   └── pages/                # index.py (two-column meeting room)
    └── shared/
        ├── constants.py          # SINGLE SOURCE OF TRUTH for all thresholds
        ├── settings.py           # pydantic-settings (env prefix SAISEI_)
        ├── logging.py            # structlog setup
        └── models/               # accounting (TrialBalance), money (JPY), classification

tests/                            # pytest — domain, nodes, critics, hosho, graph flow
```

**Node vs Agent** is the core design distinction: a *Node* is a deterministic
single-pass function; an *Agent* loops/routes/uses tools or drives HITL. Today
there is exactly one agent: `turnaround_orchestrator` (the HITL node). The three
critics and `lead_arranger` are deterministic gates — they never let an LLM
decide a verdict or a number.

## 2. LangGraph Architecture

### State Schema (Pydantic V2 — conceptual; see `app/backend/state.py`)

```
SaiseiState:
  # identity
  tdb_code: str            # 7 digits (企業コード)
  hojin_bango: str         # 13 digits (法人番号)
  company_profile: CompanyProfile | None
  tdb_score: int | None

  # financials
  shisanhyo: list[TrialBalance]      # monthly, J-GAAP, JPY int
  working_capital_gap: int | None    # JPY int (Shikin Kuri; negative = deficit)

  # macro
  boj_rate_curve: list[RatePoint]
  settlement_metrics: SettlementMetrics | None

  # assessment
  ews_score: float | None
  fsa_classification: FsaClass | None   # Joyo | Yoi Kanri | Yukyo Guchi

  # Part 2: Keieisha Hosho (経営者保証)
  hosho_kaijo_score: float | None
  hosho_kaijo_conditions: HoshoKaijoConditions | None
  hosho_kaijo_eligible: bool | None
  succession_ready: bool | None

  # turnaround
  proposed_strategies: list[Strategy]
  negotiation_decision: NegotiationDecision | None
  approved_strategy: Strategy | None    # set after HITL
  revision_note: str | None
  keikakusho_draft: str | None

  # Part 3: multi-critic burden-sharing
  critic_feedbacks: list[dict]          # custom reducer; CRITIC_FEEDBACKS_CLEAR resets
  negotiation_status: str               # pending | approved | rejected | needs_human
  revision_directive: str | None
  revision_count: int                   # cycle guard (max MAX_REVISION_CYCLES)
  lender_stakes: dict[str, int]         # optional stake-based pro-rata
  yakuin_hoshu_cut: bool                # banker-only commitment flag
  personal_asset_disposal: bool         # banker-only commitment flag

  # control
  errors: list[str]
```

### Flowchart (Nodes + Conditional Edges)

```
        START
          │
          ▼
     [intake] ── resolves TDB code + Hojin Bango, profile, anti-social check
          │
          ▼
      [ews] ── compute EWS score (0-100) from trend signals
          │
          ▼
     [macro] ── BOJ rates → settlement (T+1/T+2) → Shikin Kuri gap
          │
          ▼
   [classifier] ── FSA class: Joyo / Yoi Kanri / Yukyo Guchi
          │
          ▼
  [keieisha_hosho] ── 経営者保証 release score + succession (ALL borrowers)
          │
   ┌──────┴────── conditional edge (on fsa_classification) ──────┐
   ▼ Joyo (Normal)                              Yoi Kanri / Yukyo Guchi ▼
  [END: monitor only]                                    [strategist] ◀────────┐
                                                              │                 │
                              ┌──────────────fan-out──────────┼─────────┐       │
                              ▼                    ▼                     ▼       │
                   [main_bank_critic]    [sub_bank_critic]    [guarantor_critic] │
                     (P1)                  (P2)                 (P0)             │
                              └──────────────fan-in───────────┬─────────┘       │
                                                              ▼                 │
                                                      [lead_arranger]           │
                                                  (consensus + burden table)    │
                                                              │                 │
                          ┌──── route_after_lead_arranger ────┤                 │
                          ▼ approved / needs_human            ▼ rejected ───────┘
                  ╔═══════════════════════╗          (revision_count < MAX)
                  ║ [hitl_negotiation]    ║          (revision_count ≥ MAX → END escalate)
                  ║  interrupt(): banker  ║ ← the only real decider
                  ║  reviews & decides    ║
                  ╚═══════════════════════╝
                          │
           conditional edge (on resume Command)
      ┌───────────────────┼───────────────────────┐
      ▼ approve           ▼ revise                 ▼ reject
 [plan_writer]      back to [strategist]      [END: escalate]
      │
      ▼
 Keikakusho draft → (optional LLM polish) → END
```

- **interrupt node:** `hitl_negotiation` (`turnaround_orchestrator`) pauses the
  graph; the banker reviews proposed strategies and replies. Execution resumes
  via `Command(resume={...})`.
- **keieisha_hosho** runs for *all* borrowers; **critics + lead_arranger** run
  for distressed (Yoi Kanri / Yukyo Guchi) borrowers only.
- **needs_human:** when the only fatal blockers are banker-only commitment flags,
  `lead_arranger` emits `needs_human` so the graph routes to HITL instead of
  looping the strategist to escalation.
- **Checkpointer:** Postgres (`psycopg` v3) persists state across the interrupt.

## 3. Data Flow

```
  Reflex UI ──HTTP──▶ Backend (FastAPI hosting LangGraph, app/main.py)
                          │
                          ▼
                   LangGraph StateGraph (app/backend/graph.py)
                          │  (data-loading nodes call the provider)
                          ▼
                   MockDataProvider  ◀── deterministic JSON fixtures
                   (core_banking / tdb_api / boj_macro)
                          │
          state persisted ▼  Postgres checkpointer
                          │
   interrupt() ──▶ pause ──▶ Reflex negotiation_panel renders strategies
                          ◀── banker decision (approve/revise/reject)
          Command(resume) ▼
                   plan_writer ──▶ Keikakusho draft ──▶ Reflex dashboard
```

The mock layer is swappable for real Core Banking / TDB / EDINET clients behind
the same `MockDataProvider` interface — no graph changes needed.

## 4. Definition of Done

- [x] `make setup` installs `uv`, builds containers, seeds Postgres.
- [x] `make run-dev` brings up web + api + postgres + redis via docker-compose.
- [x] `make verify` passes: `ruff` clean, `mypy --strict` clean, `pytest` green.
- [x] `.github/workflows/ci.yml` runs `make verify` (ruff + mypy --strict + pytest) on every push to `main` and every pull request, fully offline (no services/secrets).
- [x] MockDataProvider returns deterministic J-GAAP payloads, including the Aichi
      manufacturer fixture (genka koutou + failed kakaku tenka → working-capital deficit).
- [x] LangGraph runs end-to-end: intake → EWS → macro → classify → keieisha_hosho
      → (critics → lead_arranger) → (HITL) → Keikakusho draft.
- [x] HITL interrupt pauses and resumes correctly via `Command(resume=...)` with
      Postgres checkpointing.
- [x] FSA classification strictly limited to Joyo / Yoi Kanri / Yukyo Guchi.
- [x] JPY rendered as `int` with `¥150,000,000`-style formatting throughout.
- [x] Part 2: deterministic 経営者保証 release score + eligibility + succession readiness.
- [x] Part 3: three parallel deterministic critics + lead_arranger consensus and
      burden-sharing table; needs_human routing for banker-only blockers.
- [x] Reflex UI shows the case file (EWS dashboard, Shisanhyo, burden-sharing table)
      and a streamed creditor-meeting transcript with the inline HITL action bar.
- [x] No secrets in repo; `.env.example` documents all required env vars.
- [x] Feasibility-critic advisory RAG over two-tier agent memory: pgvector
      long-term store (SQL + embeddings) and a RediSearch short-term cache, with
      a deterministic offline fallback so retrieval is advisory-only and never
      affects a band, score, gate, or route.

## 5. Planned next

See [`../HANDOFF.md`](../HANDOFF.md) (Part 4 — multi-agent creditor-meeting
simulator) and [`NEXT_STEPS.md`](NEXT_STEPS.md) (LLMOps, data flywheel, live
integrations).
