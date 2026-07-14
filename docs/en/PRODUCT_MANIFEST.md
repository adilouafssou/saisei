<div align="center">

# 再生 (Saisei) — Product Manifest

**The single entry point to everything Saisei. Pick your role below and follow the trail.**

</div>

---

## What Saisei is, in one paragraph

Saisei is an **autonomous early-warning and turnaround-plan orchestrator for Japanese regional banks**. It continuously watches a small-business borrower's monthly financials, scores its credit health, classifies it under Japan's FSA debtor framework, and — when the borrower is in trouble — co-authors a regulatory turnaround plan (経営改善計画書 / *Keiei Kaizen Keikakusho*) **with a human banker in the loop**. AI does the judgement-heavy reasoning — multi-agent orchestration, a simulated creditor meeting, feasibility analysis, precedent retrieval, and prose drafting — but it **reasons and recommends; it never makes a decision in a human's place, and it never produces or alters a figure**. Every number is computed deterministically and is fully auditable. The whole system runs offline, with no external API key required, so anyone can evaluate it end-to-end on a laptop. (For exactly what the AI does and never does, see [`AI_ARCHITECTURE.md`](AI_ARCHITECTURE.md).)

---

## Choose your path

Saisei serves four very different readers. Start where you fit; each path lists the documents in the order that makes sense for that role.

### If you are an **Engineer**

You want to run it, read the code, and extend it safely.

1. [`README.md`](../../README.md) — architecture diagram, stack, repository map, quick start.
2. [`AI_ARCHITECTURE.md`](AI_ARCHITECTURE.md) — how AI is leveraged across the system, and the precise does / never-does boundary.
3. [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) — get the app running and see a full case in ~10 minutes.
4. [`../../claude.md`](../../claude.md) — engineering rules and guardrails.
5. [`AGENTIC_ONBOARDING.md`](AGENTIC_ONBOARDING.md) — an agentic-AI curriculum taught through this codebase.
6. [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) — the data + two-tier agent-memory architecture.
7. [`CONTINUOUS_INTEGRATION.md`](CONTINUOUS_INTEGRATION.md) — the quality gates (ruff, mypy --strict, pytest, eval).
8. [`ROADMAP.md`](ROADMAP.md) and [`NEXT_STEPS.md`](NEXT_STEPS.md) — the original spec and the path to production.

### If you are a **Bank or Government client**

You care about regulatory fit, auditability, and the workflow your relationship managers will actually use.

1. [`BUSINESS_OVERVIEW.md`](BUSINESS_OVERVIEW.md) — the problem, the value, and the compliance posture in plain language.
2. [`DOMAIN_ONBOARDING.md`](DOMAIN_ONBOARDING.md) — how Saisei maps to the FSA debtor-classification framework and the 経営改善計画書 workflow.
3. [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) — watch the banker-in-the-loop flow on the bundled demo case.
4. [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) — where data lives, the live-vs-offline separation, and the data-governance stance.

### If you are an **Investor (VC)**

You want the market, the differentiators, and proof it works.

1. [`BUSINESS_OVERVIEW.md`](BUSINESS_OVERVIEW.md) — market, problem, differentiators, and the captured-decision advantage.
2. [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) — run the live demo yourself in minutes, no AI key needed.
3. [`NEXT_STEPS.md`](NEXT_STEPS.md) — the path from prototype to production and revenue.
4. [`README.md`](../../README.md) — the technical credibility check (architecture + engineering discipline).

### If you are an **Executive (CEO / decision-maker)**

You want the why, the so-what, and the risk posture — fast.

1. [`BUSINESS_OVERVIEW.md`](BUSINESS_OVERVIEW.md) — read this top to bottom; it is written for you.
2. [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) — the "what your team will see" walkthrough.
3. The **Differentiators** table below — the five things that set Saisei apart.

---

## The problem, in three sentences

Under Japan's FSA framework, a regional bank must continuously assess each borrower and, when health deteriorates, **help draft a turnaround plan** rather than call the loan. Today that work is manual and slow: relationship managers eyeball monthly trial balances for trouble, credit classification varies by officer, and the recovery plan is written from a blank page. The result is that **early-warning signals are caught late, and support arrives after the small business is already in distress.**

## The solution, in seven steps

Saisei runs the full assessment a relationship manager would, then **pauses for the banker** before committing to a strategy:

1. **Intake** — resolve identity (7-digit TDB code → 13-digit 法人番号), pull the credit report, run an anti-social-forces check (反社会的勢力).
2. **EWS scoring** — a 0–100 Early Warning Signal from trends in the monthly trial balances.
3. **Macro stress** — fold in the BOJ rate curve and settlement liquidity to estimate the working-capital gap (資金繰り).
4. **FSA classification** — map the signals to a debtor class; healthy borrowers are monitor-only, the rest enter the turnaround workflow.
5. **Strategy proposal** — grounded strategies (price pass-through, COGS reduction, SG&A rationalisation, working-capital repair) with uplift derived from the firm's *actual* figures, pre-screened by an advisory feasibility critic and judged by three independent creditor critics.
6. **Human-in-the-loop** — the workflow **interrupts**; the banker approves, requests a revision, or escalates.
7. **Plan authoring** — a deterministic Keikakusho draft in Markdown plus a deterministic P&L recovery projection (損益計画), exportable to Word (.docx) and Excel (.xlsx), with an *optional* AI polish that improves prose while preserving every figure.

> **Design stance:** numbers are computed deterministically and are the source of truth; the AI only polishes prose and never invents a figure. The system runs and is tested fully **offline**.

---

## The five differentiators

| # | Differentiator | Why it matters |
|---|---|---|
| 1 | **Deterministic, auditable numbers** | Every score, gate, and verdict is a pure rule-based function. A bank examiner can reproduce any figure. The AI never produces a number. |
| 2 | **Human-in-the-loop by design** | The workflow structurally pauses for a banker before any strategy is committed — the regulatory requirement, built into the graph, not bolted on. |
| 3 | **Runs offline, no AI key required** | The full product is evaluable on a laptop with deterministic mock data — no cloud dependency, no borrower data leaving the building. |
| 4 | **Japanese-finance domain depth** | FSA debtor classes, 経営者保証 release, BOJ rate stress, T+1/T+2 settlement, and the 経営改善計画書 artefact are modelled natively. |
| 5 | **Captured decisions** | Every banker decision (approve / revise / reject) is recorded as labelled data and feeds a two-tier agent memory. |

---

## The whole document set at a glance

| Document | Audience | What it covers |
|---|---|---|
| [`PRODUCT_MANIFEST.md`](PRODUCT_MANIFEST.md) | Everyone | This file — the role-routed index. |
| [`BUSINESS_OVERVIEW.md`](BUSINESS_OVERVIEW.md) | Client / VC / CEO | Market, problem, value, status (non-technical). |
| [`AI_ARCHITECTURE.md`](AI_ARCHITECTURE.md) | Everyone | How AI is used and why it helps; the does / never-does boundary. |
| [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) | Everyone | Step-by-step live demo on the bundled case. |
| [`README.md`](../../README.md) | Engineer | Architecture, stack, quick start, repository map. |
| [`DOMAIN_ONBOARDING.md`](DOMAIN_ONBOARDING.md) | Client / Engineer | The Japanese-finance domain, term by term. |
| [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) | Engineer / Client | Data flow and two-tier agent memory. |
| [`AGENTIC_ONBOARDING.md`](AGENTIC_ONBOARDING.md) | Engineer | Agentic-AI concepts taught through the code. |
| [`CONTINUOUS_INTEGRATION.md`](CONTINUOUS_INTEGRATION.md) | Engineer | The CI quality gates. |
| [`ROADMAP.md`](ROADMAP.md) | Engineer / CEO | The original architectural spec. |
| [`NEXT_STEPS.md`](NEXT_STEPS.md) | Engineer / VC | Prototype → production path. |
| [`../../claude.md`](../../claude.md) | Engineer | Engineering rules and guardrails. |

> Japanese translations of the major documents live in [`docs/ja/`](../ja/). See the table in [`README.md`](../../README.md).
