# Saisei (再生) — Demo Tutorial

> **Audience:** evaluators and practitioners who want to run a live demo of Saisei and understand what they are seeing — financial-institution credit and risk teams, technical and security reviewers, and partners conducting due diligence. No prior knowledge of Japanese banking regulation or agentic AI is assumed. Budget about **10 minutes** end to end.

---

## 1. What this product does (plain language)

Japanese regional banks are required, by their regulator, to keep watching each small-business borrower after the loan is made and to **help the business recover** if it starts to struggle — rather than just calling in the loan. Today a banker does this by hand: squinting at monthly financial statements, guessing the risk level, and writing a recovery plan from a blank page. By the time trouble is spotted, the business is often already in serious distress.

**Saisei is an agentic AI system that automates the watching and the first draft of the recovery plan, while keeping the banker in charge of every real decision.** A multi-agent workflow — orchestrated with LangGraph — reads the borrower's monthly numbers, produces an early-warning score, classifies the credit risk the way a regulator would, reasons through concrete turnaround strategies grounded in the company's own figures, and runs a simulated creditor meeting in which independent AI agents argue the plan from each creditor's perspective. Only after a human banker approves does it author a formal turnaround plan document.

The innovation is *where the intelligence is applied*. In a regulated credit decision, an AI that invents numbers is unusable. Saisei separates the two: **the AI agents do the reasoning, orchestration, and drafting, while every figure is computed deterministically** — so a bank examiner can reproduce each number exactly, and the generative model is structurally forbidden from altering one. You get the judgement and speed of an agentic system with the auditability a regulator demands.

---

## 2. Before you start

| Requirement | Notes |
|---|---|
| A computer with **Docker** installed and running | macOS, Linux, or Windows (WSL2). |
| **`make`** available | Pre-installed on macOS/Linux; on Windows use WSL2. |
| Internet access **for the one-time setup only** | To download dependencies and container images. |
| **A generative-LLM API key** | **Optional.** The agentic engine — the orchestration, scoring, classification, strategy reasoning, and simulated creditor meeting — runs without one, against deterministic demo data. A key is only needed to enable the optional natural-language *prose polish* on the final plan; it never changes a figure or a decision. |

> **The headline for evaluators:** the full agentic workflow runs on your laptop with **no external API key, no cloud account, and no borrower data leaving your environment**. The deterministic core makes the demo reproducible, and it directly answers the first question a bank's security and compliance teams will ask: *does our data leave the building, and can we audit the result?* Both answers are favourable out of the box.

---

## 3. Set up and start (copy-paste)

From a clean checkout of the repository:

```bash
# 1. Create your local config from the template (no secrets needed for the demo)
cp .env.example .env

# 2. Install everything, build the containers, and start the database
make setup

# 3. Start the full stack (web UI + API + Postgres + Redis)
make run-dev
```

When the stack is up, open the web UI in your browser:

```
http://localhost:3000
```

That's it. If `make setup` or `make run-dev` give you trouble, jump to [Troubleshooting](#7-troubleshooting--faq).

> **Tip for a public demo link:** Saisei can also run **without a database** (state kept in memory) and behind a **single shared password**, which is ideal for hosting a temporary demo for a few named people. Set `SAISEI_PERSIST_CHECKPOINTS=false` and `SAISEI_DEMO_PASSWORD=<something>` in your `.env`. For local laptop demos you can ignore both.

---

## 4. The demo narrative — the Aichi manufacturer

The product ships with a realistic built-in case: **愛知精密製作所株式会社** (Aichi Precision Manufacturing Co.), a metal-parts maker in Aichi Prefecture, founded 1978, 84 employees. Over the last 12 months its sales have slid and its costs have risen — a textbook "failed price pass-through" squeeze — pushing it toward the distressed band the product exists to handle.

### Step 1 — Enter the borrower

In the UI, enter the demo company's **7-digit TDB code**:

```
1234567
```

Start the assessment. Saisei resolves the company's identity, runs an anti-social-forces compliance check, and loads its 12 months of trial balances.

### Step 2 — Watch the assessment run

The interface streams each step as it completes. You will see, in order:

| Stage | What appears on screen | What it means |
|---|---|---|
| **EWS score** | A 0–100 Early Warning Signal (higher = worse) | The deteriorating trend in sales and margins, distilled into one number. |
| **Working-capital gap** | A yen figure (資金繰り) | Cash-flow stress, including BOJ interest-rate pressure. |
| **FSA classification** | A debtor class such as 要管理 (Needs Management) | The regulator-style risk tier. Healthy firms stop here (monitor only); distressed firms continue. |

### Step 3 — The simulated creditor meeting

For a distressed borrower, Saisei proposes concrete **turnaround strategies** — e.g. price pass-through, cost-of-goods reduction, overhead rationalisation, working-capital repair — each with a profit-uplift figure derived from the company's *real* numbers.

Three independent "critic" voices then judge the plan, each playing a creditor role:

- **Main bank** (主幹事銀行) — accountability: are management commitments in place?
- **Sub bank** (協調融資銀行) — fairness: is the burden shared in proportion to each lender's stake?
- **Credit guarantor** (信用保証協会) — compliance: is there a credible 3–5 year recovery path?

A **lead arranger** consolidates their verdicts into a single recommendation and a burden-sharing table — the briefing a banker would read before the real meeting.

### Step 4 — The human decision (the key moment)

This is the part to emphasise in any demo. **The workflow stops and waits for you, the banker.** You can:

- **Approve** — commit to the chosen strategy and generate the plan.
- **Revise** — send it back with a note (e.g. confirming management commitments), and the strategies are re-proposed and re-judged.
- **Escalate / reject** — hand the case off.

For the demo, toggle the management-commitment flags on and **Approve**.

> **Why this matters:** the AI agents reason, debate, and recommend, but they never decide. The structure of the system guarantees a human signs off before anything is committed — exactly what the regulation requires, and the right division of labour between automation and accountability.

### Step 5 — The deliverables

Once approved, Saisei produces:

- **The turnaround plan** (経営改善計画書) — a structured draft a banker can edit and submit.
- **A P&L recovery projection** (損益計画) — a month-by-month chart showing the recomputed early-warning score crossing back into "normal" territory, with the recovery month highlighted.
- **Exports** — the plan as **Word (.docx)** and the recovery projection as **Excel (.xlsx)**, the formats Japanese banks actually exchange.

Every yen figure in those documents is copied verbatim from the deterministic calculation — nothing is re-derived or invented.

---

## 5. Try the other built-in cases

Saisei ships several fixtures so you can show different outcomes. Enter a different borrower to drive a different path:

| Case | Demonstrates |
|---|---|
| Aichi manufacturer (TDB `1234567`) | The full distressed → turnaround → plan flow (the main demo). |
| A healthy service company | The "monitor only" early exit — no turnaround needed. |
| A needs-attention manufacturer | A milder warning tier. |
| A working-capital-deficit company | Heavy cash-flow stress driving the gap. |
| A severely distressed manufacturer | A case near the legal-handoff (workout) boundary. |

> The exact codes for the other fixtures are in `app/backend/tools/fixtures/`. The Aichi case (`1234567`) is the one to lead with.

---

## 6. What to point out during an evaluation

When you run the demo for a stakeholder, these are the five things to call out:

1. **Agentic intelligence with auditable numbers.** Multiple AI agents handle the orchestration, risk reasoning, and the simulated creditor debate — while every score and yen figure is a reproducible deterministic calculation a regulator can re-derive. The generative model improves wording only and is structurally forbidden from touching a number. This combination — AI judgement plus provable arithmetic — is the core innovation.
2. **A human is structurally in the loop.** The system *cannot* commit a strategy without a banker's decision — the compliance requirement is built into the workflow, not promised in a policy document.
3. **It runs in your environment, with no mandatory external API and no borrower data leaving the building.** That directly addresses the primary objection a financial institution's security team will raise.
4. **It speaks Japanese banking natively.** FSA debtor classes, 経営者保証 (personal-guarantee release), BOJ rate stress, and the 経営改善計画書 artefact are first-class concepts, not generic adaptations.
5. **It improves with use — the data flywheel.** Every approve / revise / reject is captured as labelled data, building a proprietary corpus of real banker judgement that compounds over time.

---

## 7. Troubleshooting / FAQ

| Symptom | Fix |
|---|---|
| `make setup` fails immediately | Make sure **Docker Desktop is running** before you start. |
| Browser shows nothing at `localhost:3000` | Give the stack a minute to finish booting; watch the `make run-dev` logs for "ready". |
| Port 3000 (or 8000) already in use | Stop whatever else is using it, or stop a previous Saisei run with `make stop`. |
| "Do I need an external / LLM API key?" | **No, not for the demo.** The full agentic workflow runs without one. Leave the LLM fields in `.env` blank; a key only enables the optional prose polish on the final plan and never affects a figure. |
| I want to reset everything and start clean | `make clean` tears down the containers and volumes (it keeps your `.env`). Then re-run `make setup`. |
| I want to stop without losing data | `make stop` halts the containers but keeps the database volumes. |
| Where do the demo numbers come from? | Bundled JSON fixtures in `app/backend/tools/fixtures/` — no external service is called. |

### Handy commands

```bash
make run-dev   # start the full stack (web + api + postgres + redis)
make stop      # stop containers, keep data
make clean     # tear down containers + volumes (keeps your .env)
make setup     # (re)install deps, build containers, seed the database
```

---

## 8. Where to go next

- **Understand the product strategy:** [`BUSINESS_OVERVIEW.md`](BUSINESS_OVERVIEW.md)
- **Understand the architecture:** [`README.md`](../../README.md)
- **Understand the Japanese-finance domain:** [`DOMAIN_ONBOARDING.md`](DOMAIN_ONBOARDING.md)
- **Find the right doc for your role:** [`PRODUCT_MANIFEST.md`](PRODUCT_MANIFEST.md)
