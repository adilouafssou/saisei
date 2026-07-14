# Saisei (再生) — Business Overview

> **Audience:** investors, executives, and bank or government decision-makers. This is the non-technical view: the market, the problem, the value, and where the product stands. For the hands-on demo see [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md); for the engineering view see [`README.md`](../../README.md).

---

## The name

**再生 (Saisei)** means *rebirth / regeneration*. The product exists to help viable-but-struggling small businesses recover, instead of being written off.

---

## The market

In Japan, **regional banks** (地方銀行) are the primary lenders to the small and medium-sized enterprises (SMEs, 中小企業) that make up the overwhelming majority of the country's companies and employment. Japanese supervisory practice does not treat a loan as finished once it is paid out: the bank is expected to **keep monitoring the borrower and actively help it recover** if its health weakens.

That expectation creates continuous, labour-intensive work at every regional bank in the country — work that is today done largely by hand, inconsistently, and too late.

---

## The problem

Under Japan's FSA framework, when a borrower deteriorates the bank is expected to **help draft a turnaround plan** (経営改善計画書) rather than call the loan. In practice:

- **Early warning is manual.** Relationship managers read monthly trial balances by eye, so sliding sales and margin compression are noticed late.
- **Risk classification is inconsistent.** Whether a borrower is "normal," "needs attention," or "needs management" varies by officer and by mood.
- **Macro stress is rarely modelled.** Pressure from BOJ interest-rate normalisation and short settlement cycles seldom makes it into the assessment.
- **The plan starts from a blank page.** Writing the turnaround plan is slow, bespoke, and hard to make consistent or auditable.

**The cost:** signals are caught late, and support arrives after the SME is already in distress — when recovery is hardest and write-offs are most likely.

---

## The solution

Saisei runs the assessment a relationship manager would, continuously and consistently, and then **drafts the turnaround plan — but pauses for the banker before committing to anything.** It:

1. Resolves the borrower's identity and runs compliance checks.
2. Produces a single 0–100 early-warning score from the trend in the monthly numbers.
3. Folds in macro and cash-flow stress (interest rates, settlement, working capital).
4. Classifies the borrower the way a regulator would.
5. Proposes concrete turnaround strategies grounded in the firm's *actual* figures, judged by a simulated creditor meeting.
6. **Stops for a human banker** to approve, revise, or escalate.
7. Generates the formal plan and a month-by-month recovery projection, exportable to Word and Excel.

---

## How AI is used (and why it helps)

A fair question from any buyer is: if the numbers are deterministic and a human makes every
decision, *where is the AI?* The answer is that the AI does the **judgement-heavy reasoning** a
relationship manager does — the part that is slow, inconsistent, and hard to scale by hand — while
staying away from the two things it must never do.

Concretely, AI:

- **orchestrates** the multi-step assessment as a stateful multi-agent workflow;
- **simulates the creditor meeting**, with independent agents arguing the plan from each creditor's
  perspective so the banker is rehearsed before the real negotiation;
- **reasons about feasibility** and, when it disagrees strongly with the deterministic rules,
  **escalates the case to a human** instead of hiding the disagreement;
- **recalls relevant precedent** to inform its advisory reasoning;
- **drafts the plan's prose**; and
- **learns from every captured banker decision** to get sharper over time.

The boundary is the point: **the AI reasons and recommends, but it never produces or alters a
number, and it never makes a decision in a human's place.** Reasoning and recommending are not
deciding — the banker is the only decider. This is what makes Saisei genuinely AI-native *and*
deployable inside a regulated bank. The full breakdown is in
[`AI_ARCHITECTURE.md`](AI_ARCHITECTURE.md).

---

## Why it is differentiated
| # | Differentiator | What it means for a buyer |
|---|---|---|
| 1 | **Deterministic, auditable numbers** | Every figure is a reproducible calculation, not an AI guess. This is what makes the output usable in a regulated bank — an examiner can re-derive any number. |
| 2 | **Human-in-the-loop by design** | The system cannot commit a strategy without a banker's sign-off. Compliance is structural, not a policy promise. |
| 3 | **Runs offline, no AI key required** | The whole product works on-premises with no borrower data leaving the bank — removing the single biggest security objection. |
| 4 | **Japanese-finance domain depth** | FSA debtor classes, personal-guarantee release (経営者保証), BOJ rate stress, and the 経営改善計画書 artefact are modelled natively, not bolted onto a generic tool. |
| 5 | **Captured decisions** | Every banker decision is recorded as labelled data, building a corpus of Japanese turnaround judgement specific to the bank's own casework. |

---

## The durable advantages

Two advantages accrue over time.

The first is **data the product captures during normal use**: structured records of distressed-SME financials paired with the actual banker decisions made on them (approve / revise / escalate, with reasons). That corpus is generated as a by-product of normal use, is specific to Japanese regional-banking judgement, and can refine the advisory layer over time while the deterministic, auditable core stays fixed.

The second is **trust**: because the numeric core is deterministic and runs offline, the product clears the regulatory and security bar that blocks most AI tools from entering a bank at all.

---

## Who buys it, and why

| Buyer | Primary value |
|---|---|
| **Regional banks** | Catch deterioration earlier, classify consistently, and cut the time to a compliant turnaround plan from days to minutes — while keeping the banker in control. |
| **Government / public financial institutions** | A consistent, auditable, policy-aligned process for supporting SME recovery at scale. |
| **Credit guarantee corporations (信用保証協会)** | A shared, transparent view of recovery feasibility and burden-sharing across creditors. |

---

## Risk and compliance posture

- **Determinism first.** The AI never produces a verdict or a number; it only improves wording, and even that step is guarded so no figure can change.
- **Auditability.** Every score and gate is a pure function of the inputs — reproducible on demand.
- **Data control.** The product runs fully offline by default; live data sources and any AI are opt-in and configurable per deployment.
- **Human authority.** The banker is the only decider; the system rehearses and recommends, it never commits.

---

## Status and what's next

Saisei is a working, end-to-end prototype: the full assessment → simulated creditor meeting → human-in-the-loop → plan-generation flow runs today, offline, on bundled realistic cases. The path from here to production — live data integrations, the captured-decision corpus, and the evaluation harness — is laid out in [`NEXT_STEPS.md`](NEXT_STEPS.md), with the original architectural spec in [`ROADMAP.md`](ROADMAP.md).

---

## See it yourself

The fastest way to understand Saisei is to run it. The [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) gets you from a clean machine to a full worked case in about ten minutes, with no AI key and no real data required.
