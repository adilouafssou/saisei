# Saisei (再生) — How AI Is Used (and Why It Helps)

> **Audience:** anyone evaluating whether Saisei is genuinely an AI product and where the AI earns its place — financial-institution reviewers, technical due-diligence teams, and partners. This document answers one question precisely: **what does the AI actually do, and why is that valuable, given that it never decides anything in a human's place?**

---

## The one-sentence answer

**Saisei uses AI to do the judgement-heavy *reasoning* a relationship manager does — reading the financials, weighing turnaround options, arguing them from each creditor's perspective, and drafting the plan — while every regulated *number* is computed deterministically and every *decision* is made by a human banker.**

The AI reasons and recommends. It never decides in a person's place, and it never produces or alters a figure.

---

## Why this separation is the product (not a limitation)

A bank cannot deploy an AI that invents the numbers in a credit file: an examiner must be able to reproduce every yen. A common reaction to that constraint is to conclude “then AI can’t really be used here.” Saisei’s thesis is the opposite: **the constraint tells you *where* to apply AI, not whether to.**

So the work is split into three layers, by who is best at each:

| Layer | Who does it | Why |
|---|---|---|
| **The numbers** (scores, gaps, classifications, burden math) | **Deterministic code** | Must be reproducible and auditable to the yen. A regulator re-derives them. |
| **The reasoning** (analysis, option-weighing, multi-party argumentation, retrieval of precedent, drafting) | **AI** | This is genuine cognitive work that scales badly by hand and is where AI adds the most value. |
| **The decision** (approve / revise / escalate) | **The human banker** | Accountability is non-delegable. The law and good sense require a person to own the outcome. |

The AI is not fenced *out* for safety; it is pointed *at* the part of the job where reasoning matters and aimed *away* from the two things it must never do — fabricate a regulated figure, or take a decision a human is accountable for.

---

## Where the AI does real work

These are the load-bearing AI contributions. None of them produces a final number or a binding decision; all of them are reasoning that would otherwise fall on an overstretched relationship manager.

### 1. Multi-agent orchestration (the spine)

Saisei is built on **LangGraph**, a stateful multi-agent framework. The assessment is not a script: it is a graph of cooperating steps with conditional routing, durable state that survives a multi-day pause, and a native **human-in-the-loop interrupt**. The orchestration *is* the agentic system — it sequences which reasoning to run next based on the borrower's state, and it knows when to stop and hand control to the banker. This is what lets one workflow handle a healthy firm (monitor only), a distressed firm (full turnaround), and a failed firm (legal handoff) without a human wiring each path.

### 2. The simulated creditor meeting

The centrepiece. For a distressed borrower, independent AI **critic agents** each reason from a real creditor's perspective — the lead bank (accountability), the syndicate lender (fairness), and the credit guarantor (compliance) — and argue the proposed plan as those parties would in the actual meeting. A **lead-arranger** agent then consolidates the debate into a briefing the banker reads *before* facing the real creditors.

This is judgement-heavy reasoning: anticipating objections, surfacing weaknesses, and rehearsing a multi-party negotiation. It is exactly the preparation a senior banker does mentally and a junior one cannot yet do well. **The agents argue and recommend; they do not approve anything.**

### 3. Feasibility reasoning and disagreement surfacing

An advisory **feasibility critic** assesses whether a proposed strategy is operationally realistic, and runs a deliberate check: it compares the AI's feasibility read against the deterministic floor. **When the AI and the rule engine disagree strongly, the system routes the case to the human** rather than papering over it. This is a powerful, carefully bounded use of AI — the model is allowed to *raise its hand and ask for human attention*, but the routing itself is a pure, auditable predicate. The AI can escalate to a person; it cannot resolve the disagreement on its own.

### 4. Retrieval-augmented reasoning (agent memory)

The feasibility critic enriches its **advisory** note with retrieved precedents — past plans, benchmarks, relevant FSA passages — via a two-tier memory (long-term vector store + short-term cache). This is the AI recalling relevant experience to reason better, the way an experienced banker draws on cases they have seen. Retrieval is advisory only: it never feeds a score, gate, or route.

### 5. Plan drafting (prose, not figures)

The turnaround plan (経営改善計画書) is drafted from a deterministic skeleton, and an **optional** language model then improves the prose — clarity, tone, readability for the credit committee. A numeric-preservation gate verifies that **every figure is byte-for-byte unchanged** before the polished text is accepted; if any number moved, the deterministic draft is kept instead. The AI makes the document read like a person wrote it carefully; it cannot touch a yen value.

### 6. The companion co-pilot (advisory, read-only Q&A)

A summonable **companion** (再生の精) answers a banker's free-form questions about the
current case — explain a figure, explain why a classification landed where it did, or
compare this borrower to a similar past one. It is the most open-ended surface in the
product, and therefore the one most tightly bound: it is strictly **read-only** (it returns
text, never a state change, and always defers the decision to the banker), and every
qualitative sentence it emits passes through the same claim-grounding gate as the critics —
so a figure it states must resolve against the deterministic state, or it is visibly marked
**【未検証 / unverified】**. When an LLM is configured it may *rephrase* the deterministic,
cited answer for readability, and the rewrite is re-grounded against the same evidence; an
LLM that weakens attribution is rejected in favour of the deterministic text. Offline, the
answer is byte-identical and fully deterministic.

### 7. The data flywheel (learning over time)

Every banker decision — approve, revise, reject, with reasons — is captured as labelled data. Over time this builds a proprietary corpus of real Japanese turnaround judgement that sharpens the AI's advisory reasoning (feasibility, precedent retrieval, critic quality). The numbers stay deterministic; the *reasoning* gets better with use.

---

## How the reasoning is kept grounded (and hallucinations contained)

A reasonable worry about any AI in finance is hallucination — the model confidently asserting something untrue. Saisei contains this risk structurally, with three layers that mean the AI's output is **grounded in real data and references, and verified before it can do harm**:

1. **Grounded in the firm's own figures, not the model's imagination.** The AI never originates the numbers it reasons about. Scores, the working-capital gap, the FSA class, and every strategy's profit uplift are computed deterministically from the borrower's *actual* trial balances. The critic agents argue over those computed signals — so the quantitative basis of the reasoning is real by construction, not generated text.

2. **Grounded in retrieved references (RAG).** Where the AI reasons qualitatively (feasibility), it is augmented with retrieved precedents — past plans, benchmarks, relevant FSA passages — from the agent-memory store, rather than relying on the model's parametric memory alone. This is the same discipline a careful analyst uses: cite the case, don't recall it vaguely. Retrieval is advisory only, so even a poor retrieval cannot move a score, gate, or route.

3. **Verified before acceptance — a real verifier, not a vibe check.** Two deterministic
   verifiers gate the model's free text before it reaches the banker. (a) The plan's prose
   polish passes through a **numeric-preservation verifier**: it extracts every monetary
   value from the original deterministic draft and from the polished text and compares them
   as a multiset — if a single figure was added, dropped, or altered, the polished text is
   **rejected** and the deterministic draft is kept. (b) Every banker-facing *qualitative*
   claim (each critic's rehearsal stance, the feasibility note, and the companion's answers)
   passes through a **claim-grounding verifier**: each asserted sentence must carry a citation
   that resolves to a deterministic signal or a retrieved source, or it is stripped / visibly
   marked **【未検証 / unverified】**. The model is structurally unable to smuggle a
   hallucinated number — or an unattributable claim — into what the banker reads.

Taken together: **numbers cannot be hallucinated** (they are deterministic and a verifier guards the one text-generation step), **reasoning is anchored** in real figures and retrieved references, and **anywhere the model could still be wrong it is advisory-only or escalates to a human** rather than acting. The AI is grounded going in and verified coming out.

---

## The boundary, stated plainly

To avoid any ambiguity, here is what the AI **does** and **never** does:

**The AI does:**
- orchestrate the multi-step assessment and sequence which reasoning to run next;
- analyse the financials and reason about turnaround options;
- simulate the creditor meeting and argue each party's position;
- retrieve and apply relevant precedent;
- answer the banker's free-form questions about the case, read-only and grounded;
- draft and polish the plan's prose;
- flag disagreement and **recommend** a course of action;
- improve its reasoning from captured decisions over time.

**The AI never:**
- produces, derives, or alters a number — all figures are deterministic and auditable;
- makes a decision in a human's place — approve / revise / escalate is always the banker's;
- commits a strategy without an explicit human sign-off;
- lets a low-confidence disagreement pass silently — it escalates to a person instead;
- ships a hallucinated figure — or an unattributable qualitative claim — to the banker: a
  numeric-preservation verifier rejects any generated text whose numbers do not match the
  deterministic source, and a claim-grounding verifier strips or visibly marks any sentence
  that cannot cite a deterministic signal or a retrieved source.

**Reasoning and recommending are not deciding.** The banker is the only decider, by design and by construction — the workflow physically pauses and cannot proceed until a person acts.

---

## Why this is the right design for a regulated product

- **It is genuinely AI-native.** The orchestration, the simulated meeting, the feasibility reasoning, the retrieval, and the drafting are all AI doing cognitive work — not a rules engine with a chatbot stapled on.
- **It is auditable.** Because the numbers and the routing are deterministic, an examiner can reproduce the entire numeric and decision trail.
- **It keeps accountability where it belongs.** A human owns every decision; the AI's job is to make that human dramatically faster, better prepared, and more consistent.
- **It improves safely.** The flywheel sharpens the *reasoning* layer while the *auditable* layer stays fixed — so the product gets smarter without ever getting less reproducible.

---

## See also

- [`README.md`](../../README.md) — architecture diagram and the deterministic-vs-AI split in the graph.
- [`BUSINESS_OVERVIEW.md`](BUSINESS_OVERVIEW.md) — the market and moat view.
- [`DEMO_TUTORIAL.md`](DEMO_TUTORIAL.md) — watch the AI reasoning and the human decision point in a live run.
- [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) — the agent-memory and data-flywheel detail.
