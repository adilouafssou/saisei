# Saisei (再生) — Engineer Handoff

A deterministic, LangGraph + Reflex agentic engine for Japanese SME turnaround
(*Saisei Fainansu*). It assesses a borrower's health, judges its turnaround plan
through a simulated creditor meeting, and assists a banker in producing a
*Keiei Kaizen Keikakusho* (経営改善計画書). Read this once and you can ship.

## The one rule that governs everything

**Determinism is the source of truth. The LLM only phrases prose — it never
decides a verdict or produces a number.** Every score, gate, and pass/fail is a
pure rule-based function. If you add a feature, the decision must be reproducible
and auditable; the LLM may only improve wording (see `polish_keikakusho`).

Corollaries you must preserve:
- Never change or remove existing `SaiseiState` fields (only add).
- `render_keikakusho` output stays byte-identical (snapshot-sensitive).
- All money is **strict integer yen** (`app/shared/models/money.py`).

## Architecture in 90 seconds

Everything lives under the unified **`app/`** package (required by Reflex's
compiler; `rxconfig.py` sets `app_name="app"`).

```
app/
  main.py                     # rx.App() + FastAPI /health /ready + lifespan
  backend/
    state.py                  # SaiseiState (Pydantic V2) + reducers + sub-models
    graph.py                  # StateGraph wiring + routers + Postgres checkpointer
    agents/                   # TRUE agents (loops / routing / tool-use / HITL)
      turnaround_orchestrator.py   # the ONLY agent: interrupt()/resume HITL
    nodes/                    # WORKFLOW steps (deterministic, pure-ish functions)
      financial_extraction.py # intake + macro (TDB identity, Shikin Kuri gap)
      ews_scoring.py          # EWS compute + FSA classify
      kaizen_generation.py    # strategist + plan render + LLM polish
      keieisha_hosho.py       # guarantee-release + succession assessment
      lead_arranger.py        # consensus engine (Torimatome)
      critics/                # 3 parallel critic nodes (main_bank/sub_bank/guarantor)
    tools/                    # swappable data clients (mocks today) + fixtures/
    prompts/                  # extraction_rules.md, kaizen_templates.md (static assets)
  frontend/                   # Reflex UI (state, components, pages)
  shared/                     # models/ (accounting, money, classification),
                              # constants.py, settings.py, logging.py
tests/                        # pytest; import canonically from app.*
```

**Node vs Agent** is the core design distinction (don't blur it): a *Node* is a
deterministic single-pass function; an *Agent* loops/routes/uses tools or drives
HITL. Today there is exactly one agent: `turnaround_orchestrator`.

## The graph flow (what actually runs)

```
intake → ews → macro → classifier → keieisha_hosho
                                          │
              route_after_classification  ▼
   Joyo (正常) → END (monitor only)   |   Yoi Kanri / Yukyo Guchi → strategist
                                          │
   strategist ─fan-out─▶ main_bank_critic ┐
              ─fan-out─▶ sub_bank_critic  ├─▶ lead_arranger
              ─fan-out─▶ guarantor_critic ┘        │ route_after_lead_arranger
                                                   ├ approved / needs_human → hitl_negotiation
                                                   ├ rejected & count<3     → strategist (cycle)
                                                   └ rejected & count>=3    → END (escalate)
   hitl_negotiation (interrupt) ─▶ approve → plan_writer → END
                                  revise  → strategist
                                  reject  → END
```

- **keieisha_hosho runs for ALL borrowers** (even healthy ones); critics run for
  **distressed only**.
- State persists across the HITL `interrupt()` via the **Postgres checkpointer**.
  Resume with `Command(resume={"decision": ...})`.

## Feature 1 — Keieisha Hosho (経営者保証) guarantee release

`keieisha_hosho.py` scores release-eligibility (0–100) from three FSA-guideline
pillars, all deterministic:
- **法人個人分離** (separation, 40 pts) — `eigai_shueki / eigai_hiyo ≥ 1.0` proxy.
- **財務基盤の強化** (financial base, 35 pts) — EWS < 40 and gap ≥ 0.
- **適時適切な情報開示** (disclosure, 25 pts) — 12mo Shisanhyo + TDB score + no errors.

Outputs in state: `hosho_kaijo_score`, `hosho_kaijo_conditions`,
`hosho_kaijo_eligible` (= score ≥ `HOSHO_ELIGIBLE_SCORE`), `succession_ready`.
Known weak spot: the *bunri* proxy can read as inverted vs FSA intent — refine it.

## Feature 2 — Multi-critic "Lead Arranger" (creditor meeting)

Three **independent, parallel critic nodes** (they must NOT see each other's
feedback) each emit a deterministic `CriticFeedback` (PASS/FAIL + blockers +
priority):
- **main_bank** (P1, accountability): FAIL unless `yakuin_hoshu_cut` and, on a
  deficit, `personal_asset_disposal` — these are **banker-only** flags.
- **sub_bank** (P2, fairness): FAIL if burden isn't pro-rata to stake within
  `PRO_RATA_TOLERANCE` (stake-based when `lender_stakes` set, else uplift proxy).
- **guarantor** (P0, compliance): FAIL without a credible 3–5yr recovery path.

`lead_arranger` fans them in: **any FAIL → rejected**, **all PASS → approved**,
blockers ordered P0>P1>P2, plus a deterministic burden-sharing table. It emits
`needs_human` when the only blockers are banker-only, so the graph routes to HITL
instead of looping the strategist to escalation.

**Reducer gotcha:** `critic_feedbacks` uses a custom reducer with the
`CRITIC_FEEDBACKS_CLEAR` sentinel. `strategist_node` returns the sentinel to
reset between revision rounds (plain `[]` would no-op and accumulate stale
verdicts — this was a real bug; don't reintroduce it).

## State cheat-sheet (`app/backend/state.py`)

Key fields you'll touch: `shisanhyo`, `working_capital_gap`, `ews_score`,
`fsa_classification`, `proposed_strategies`, `critic_feedbacks`,
`negotiation_status` (`pending|approved|rejected|needs_human`),
`revision_count` (cycle guard, max 3), `lender_stakes`, the banker flags
`yakuin_hoshu_cut` / `personal_asset_disposal`, and the Hosho outputs.

## Conventions & guardrails

- **Single source of truth for thresholds: `app/shared/constants.py`.** Import
  them; never redefine local copies (drift bugs have happened twice here).
- Add new long Japanese prompt text to `app/backend/prompts/`, not Python files.
- Mock data is swappable behind `MockDataProvider` (`app/backend/tools/`). Real
  TDB/core-banking/EDINET clients drop in behind the same interface, no graph
  changes.
- Fixtures ship inside the package at `app/backend/tools/fixtures/`.

## How to work

```bash
make setup     # uv install, .env, containers, seed DB
make run-dev   # run the stack
make verify    # ruff + mypy --strict + pytest  <- MUST pass before merge
make test      # pytest only
```

`make verify` is the contract. CI (`.github/workflows/ci.yml`) runs the same
gates (ruff + mypy --strict + pytest) on every pull request and on pushes to
`main`. The agent sandbox has no network for installs, so the **pipeline is the
authoritative check** — always let it go green before merging.

## What's next (highest leverage first)

See `docs/en/NEXT_STEPS.md` for the full roadmap. Near-term:
1. LangSmith tracing + a golden eval set gated in CI.
2. Capture HITL trajectories (approve/revise/reject) for a data flywheel.
3. First live integration (TDB) behind `MockDataProvider`.
4. Decide the critics fan-out point (see open issue): blueprint says fan out
   *after* the rendered plan; today they judge `proposed_strategies` pre-render.
5. Refine the Hosho *bunri* separation proxy.

## Planned feature (Part 4) — Multi-agent creditor-meeting *simulator*

**Status: SPEC (not yet built). Read this before adding it.**

### Why

Today the creditor meeting is fully deterministic: three pure-function critics
gate PASS/FAIL, and `lead_arranger` consolidates (Torimatome). That protects
auditability — but the *real* meeting is a negotiation, and the human banker has
to walk in prepared for how the 主幹事銀行 / 協調融資銀行 / 信用保証協会 will
actually argue. Part 4 adds an **LLM persona layer that simulates that meeting**
so the engine becomes a *rehearsal tool* for the banker, who remains the only
real decider.

### The one rule still governs (do not break it)

**The deterministic gate remains the source of truth for every verdict and
number.** The new agents reason about *stance, argument, and feasibility* — never
about the PASS/FAIL gate or any figure. Concretely:

- `CriticFeedback.status` (PASS/FAIL) and the structured blocker codes
  (`yakuin_hoshu_not_cut`, `no_asset_disposal`, …) stay 100% rule-based, because
  `lead_arranger`'s `needs_human` routing string-matches them. An LLM must never
  produce or alter these.
- The LLM adds new *advisory* fields only (simulated argument, predicted
  concessions, feasibility opinion). These never feed routing or scoring.
- Every agent must have a **deterministic offline fallback** (mirror
  `polish_keikakusho`: no-op / canned output when no LLM is configured) so
  `make verify` stays green in the no-network CI sandbox.

### Architecture: hybrid agents (gate + persona)

Each critic becomes a **hybrid**: the existing deterministic gate is unchanged;
a new persona layer reasons on top of it.

```
strategist
   │
   ▼  (NEW, upstream pre-screen)
feasibility_critic  ← TRUE agent: loops over each proposed Strategy, optionally
   │                  RAG over past plans / industry benchmarks, asks "is this
   │                  operationally achievable for THIS firm?" Emits advisory
   │                  feasibility notes. Does NOT gate; annotates strategies.
   ├─fan-out─▶ main_bank_critic   (gate PASS/FAIL  +  persona arg — 主幹事銀行 voice)
   ├─fan-out─▶ sub_bank_critic    (gate PASS/FAIL  +  persona arg — 協調融資銀行 voice)
   └─fan-out─▶ guarantor_critic   (gate PASS/FAIL  +  persona arg — 信用保証協会 voice)
              │
              ▼
        lead_arranger  ← becomes ORCHESTRATOR/chair-agent: consolidates the
                          deterministic verdicts (unchanged) AND assembles a
                          *meeting briefing* from the persona arguments — the
                          rehearsal the banker reads before HITL.
              │
              ▼
        hitl_negotiation (human — the only real decider)
```

**Role separation (keep these distinct):**
- `feasibility_critic` = upstream, asks *"can this firm actually do it?"* (operational).
- the three critics = *"is this fair / compliant / accountable across lenders?"* (per-persona gate + simulated stance).
- `lead_arranger` = chair: consolidates verdicts + produces the burden-sharing
  table (unchanged) **and** the new simulated-meeting briefing.

### Implementation notes

- **New module:** `app/backend/nodes/critics/feasibility.py` (`feasibility_critic_node`).
  Wire it `strategist → feasibility_critic → {three critics}` (insert before the
  existing fan-out; the fan-out edges move to originate from `feasibility_critic`).
- **Prompts** go in `app/backend/prompts/` (one per persona; static `.md`), never
  inline in Python — see existing `extraction_rules.md` / `kaizen_templates.md`.
  This also sets up the future prompt-registry migration in `NEXT_STEPS.md`.
- **LLM client:** reuse the `polish_keikakusho` pattern in `kaizen_generation.py`
  (OpenAI-compatible Chat Completions via `httpx`, `Settings.llm_*`, best-effort
  with fallback). Factor a small shared helper if it grows.
- **State (additive only — never remove/rename existing fields):**
  - `feasibility_notes: list[dict]` (per-strategy advisory; reducer like
    `critic_feedbacks` if produced in parallel).
  - extend `CriticFeedback` with an OPTIONAL `simulated_argument: str = ""`
    (default keeps existing tests/serialization byte-stable).
  - `meeting_briefing: str | None` (the rehearsal text from `lead_arranger`).
  - surface `meeting_briefing` + feasibility in the HITL `_interrupt_payload`.
- **Determinism guard:** add a test asserting that with NO LLM configured, the
  gates, blocker codes, routing, and burden table are byte-identical to today
  (the persona/briefing fields are empty/canned). This is the regression that
  proves the thesis still holds.
- **Showcase value:** this is the honest "multi-agent system" story — a
  deterministic regulatory spine wrapped by a multi-agent *simulation* that
  rehearses, never replaces, the human banker.

### Suggested sequencing
1. ~~Extend `CriticFeedback` with optional `simulated_argument` (no behavior change).~~
   **DONE** (MR !3): added the optional advisory field (default `""`) + a
   determinism-parity test (`tests/test_part4_persona.py`) proving `lead_arranger`
   ignores it.
2. ~~Add `feasibility_critic` agent + prompts (advisory-only, offline fallback).~~
   **DONE**: `app/backend/nodes/critics/feasibility.py` + `prompts/feasibility_critic.md`,
   wired `strategist → feasibility_critic → {three critics}`; advisory `feasibility_notes`
   channel on state; parity test `tests/test_part4_feasibility.py`.
3. ~~Add persona layer to the three critics (gate unchanged; argument added).~~
   **DONE**: shared `critics/_persona.py` helper + per-persona prompts; each critic
   populates `simulated_argument` (empty offline); parity test
   `tests/test_part4_critic_personas.py`.
4. ~~Upgrade `lead_arranger` to emit `meeting_briefing`; surface it in HITL.~~
   **DONE**: `_format_meeting_briefing` assembles the rehearsal from verdicts +
   persona arguments + feasibility notes; surfaced in the HITL payload; parity
   test `tests/test_part4_meeting_briefing.py`.
5. ~~Determinism-parity test + offline `make verify` must stay green.~~
   **DONE**: consolidated end-to-end sign-off `tests/test_part4_signoff.py` runs
   the full graph offline and asserts the spine is reproducible while every
   advisory channel takes its empty/skeleton offline fallback.

**Part 4 status: COMPLETE.** The deterministic regulatory spine is now wrapped
by an advisory multi-agent simulation (feasibility pre-screen + per-persona
arguments + chair briefing) that rehearses the creditor meeting for the banker
and never decides a verdict, route, or figure.

## Gotchas that will bite you

- Don't loop the strategist on **banker-only** blockers → use `needs_human`.
- Don't return `[]` to reset `critic_feedbacks` → use `CRITIC_FEEDBACKS_CLEAR`.
- Don't add LLM output into any number or verdict.
- Don't break `render_keikakusho`'s exact output or remove a state field.
- Keep critics mutually blind and deterministic.

## Planned feature (Part 5) — Meeting-Room frontend (UX)

**Status: v1 SHIPPED in MR !1 (steps 1–3 + design system). Steps 4–6 below
remain. Checkpoint: after the dev-stack + UI-state fixes.**

### What shipped in v1

- **Design system** — `app/frontend/theme.py`: color/space/radius/shadow/font
  tokens, a `Persona` dataclass + `PERSONAS` registry giving each agent a
  distinct avatar monogram (主/協/保/幹/再/君), accent color, role label, and
  Lucide icon for at-a-glance identification.
- **Streaming backend event** — `SaiseiUIState` now streams the graph with
  `graph.stream(stream_mode="updates")` inside `@rx.event(background=True)` via
  `asyncio.to_thread`; each node appends a transcript event as it completes.
  `phase` lifecycle + `active_node` drive progress UI.
- **Meeting transcript** — `components/avatar.py` + `components/meeting_panel.py`:
  chat-style bubbles (critics → persona bubbles with PASS/FAIL + P0/P1/P2 +
  blockers; lead_arranger → chair summary; banker → right-aligned), typing
  indicator, empty state, and an inline HITL action bar with commitment-flag
  toggles, per-strategy approve, revise (note), reject.
- **Case file** — redesigned `ews_dashboard.py` (metric grid, classification
  colored by severity) + real `burden_table` + themed `shisanhyo_table`.
- **Shell/theme** — two-column responsive `index.py`, sticky top bar, dark indigo
  Radix theme in `app.py`, `RadixThemesPlugin` enabled in `rxconfig.py`, and the
  duplicate `add_page` removed.

### Remaining (steps 4–6)



### Guardrails

- UI is display-only: never compute a verdict or number in the frontend; read
  everything from snapshot/stream values (the deterministic spine stays the
  source of truth).
- `_apply_snapshot` must keep reading defensively (checkpoint rehydrates models
  as dicts and enums as strings — use the `_attr` / `_fsa_kanji` helpers; this
  was the `'str' object has no attribute 'kanji'` bug fixed in MR !1).
- Start with steps 1–2 together: the panel is pointless without streaming, and
  streaming is unconvincing without something to show.
