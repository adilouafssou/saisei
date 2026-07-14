# LOAN_LIFECYCLE.md — The loan-facility lifecycle spine

This is the **spec** for the loan facility (融資案件) as a first-class domain
entity in Saisei. It is the additive *spine* that lets Saisei own the full loan
lifecycle — from application (申込) through repayment (完済), restructuring
(条件変更), or write-off (償却) — without restructuring any existing work.

The canonical implementation is [`app/shared/models/loan.py`](../../app/shared/models/loan.py);
this document is the human-readable contract it satisfies.

## Why a spine, and why now

Saisei today is deep on the **post-origination distress half** of a loan's life
(EWS → FSA classification → turnaround → workout) but has no first-class loan
entity: `app/shared/models/` carries `money`, `accounting`, and `classification`
— never the facility itself. Without a loan-status spine, the borrower's history
is fragmented and neither origination nor turnaround can be expressed as a state
of one continuous, auditable record.

The spine fixes that with the smallest possible, fully additive change: a new
domain model that **nothing existing imports yet**. It changes no graph, node,
critic, HITL flow, Keikakusho engine, or UI, so it cannot regress current
behaviour. Breadth (origination) and depth (turnaround mapping) then attach to
it as separate, deliberate MRs.

## The lifecycle

Statuses are a **closed set** (`LoanStatus`), in the same spirit as the closed
five-value `FsaClass`. The arc spans origination → servicing → distress →
terminal:

| Status | 日本語 | Meaning |
|---|---|---|
| `APPLIED` | 申込 | A facility request exists, not yet reviewed. |
| `UNDER_REVIEW` | 審査中 | Underwriting / 稟議 in progress. |
| `APPROVED` | 承認 | Approved, awaiting disbursement. |
| `DECLINED` | 謝絶 | Declined. **Terminal.** |
| `DISBURSED` | 実行 | Disbursed / drawn down. |
| `PERFORMING` | 正常 | On the agreed repayment schedule. |
| `RESTRUCTURED` | 条件変更 | Restructured (リスケ); a 貸出条件緩和債権. |
| `WORKOUT` | 管理回収 | Managed recovery. |
| `CLOSED` | 完済 | Fully repaid. **Terminal.** |
| `WRITTEN_OFF` | 償却 | Written off. **Terminal.** |

## Legal state-transition table

The state machine is the single source of truth in code
(`_ALLOWED_TRANSITIONS`). An event log that violates it fails validation.

| From | Allowed to |
|---|---|
| `APPLIED` | `UNDER_REVIEW`, `DECLINED` |
| `UNDER_REVIEW` | `APPROVED`, `DECLINED` |
| `APPROVED` | `DISBURSED`, `DECLINED` |
| `DISBURSED` | `PERFORMING` |
| `PERFORMING` | `RESTRUCTURED`, `WORKOUT`, `CLOSED` |
| `RESTRUCTURED` | `PERFORMING`, `WORKOUT`, `CLOSED` |
| `WORKOUT` | `CLOSED`, `WRITTEN_OFF` |
| `DECLINED` / `CLOSED` / `WRITTEN_OFF` | — (terminal) |

## Human-in-the-loop gated transitions

Human authority is non-negotiable: **the system proposes, the banker decides.**
The following transitions require an explicit banker sign-off before they may be
recorded (`HITL_GATED_TRANSITIONS`). They are the credit-authority and
distress-recognition moments — never automatable:

- `UNDER_REVIEW → APPROVED` and `UNDER_REVIEW → DECLINED` (the credit decision)
- `APPROVED → DECLINED` (revoking an approval)
- `PERFORMING → RESTRUCTURED` (granting a 条件変更)
- `PERFORMING → WORKOUT` and `RESTRUCTURED → WORKOUT` (recognising distress)
- `WORKOUT → WRITTEN_OFF` (償却)

Gating is enforced upstream (graph / API); this module is the canonical list.

## Event-sourced, deterministic, replayable

A loan's current status is **never a mutable field** — it is derived by replaying
an ordered, append-only log of `LoanEvent` records (`current_status`). This
mirrors the project's append-only, hash-chained audit ledger and its
replayability principle: any loan's state at any point is reconstructable.

Invariants enforced at validation time:

- the log is non-empty and its first event is `APPLIED`;
- every subsequent event is a legal transition from the prior status;
- `principal` is strict integer yen (`JPY`) — fractional / whole-float / bool
  values are rejected.

## The does / never-does boundary

The loan ledger restates the product's core invariant at the domain layer:

- **Does:** compute balances and statuses deterministically; serve as the
  source of truth for where a facility is in its life; record who/when for every
  transition (audit-bearing events).
- **Never:** the LLM never produces or alters a figure here, and never performs
  a transition in a human's place. Every banker-authority transition is
  HITL-gated; the AI may reason and recommend, but it does not decide.

## How depth and breadth attach (sequenced)

Both halves of the spine are now shipped: a facility can be driven from
application through disbursement, and from servicing through distress, as one
continuous, auditable, HITL-gated record.

- **Depth (turnaround mapping). — shipped.** `RESTRUCTURED` and `WORKOUT` map
  onto the existing `FsaClass.requires_turnaround` / `FsaClass.requires_workout`
  routing in
  [`classification.py`](../../app/shared/models/classification.py) via
  `proposed_transition_for`, the turnaround HITL approve path records the
  `条件変更` / `管理回収` transition, the workout node records the deterministic
  `管理回収` handoff, and both reserve the `貸倒引当金` (`provision_amount`).
  Transitions are persisted to the append-only loan ledger.
- **Breadth (origination). — shipped.** The deterministic origination mirror of
  the depth helpers lives in [`loan.py`](../../app/shared/models/loan.py):
  `recommend_origination` (`proposed_origination_decision` +
  `max_facility_amount`) turns an applicant's TDB assessment into an advisory
  APPROVE / DECLINE recommendation and a provisional `融資上限` at the `稟議`
  gate. The graph-side
  [`loan_origination_node`](../../app/backend/nodes/loan_origination.py)
  realises it under the full Saisei boundary: it grounds the advisory reason
  through the claim-grounding gate (no uncited figure reaches the banker),
  records a version-pinned `ORIGINATION_DECISION` audit event, and records the
  administrative `APPLIED → UNDER_REVIEW` transition. The **graph edge** — the
  origination StateGraph in
  [`graph_origination.py`](../../app/backend/graph_origination.py) — closes the
  front of the lifecycle:

      START → origination_intake (seed APPLIED)
            → loan_origination   (grounded/audited recommendation; APPLIED → UNDER_REVIEW)
            → origination_hitl    (interrupt; banker 承認 / 謝絖; UNDER_REVIEW → APPROVED / DECLINED)
            → approve → disbursement (APPROVED → DISBURSED) → END
              decline → END

  The credit decision is HITL-gated (the
  [`origination_orchestrator`](../../app/backend/agents/origination_orchestrator.py)
  records `承認` / `謝絖` and the HUMAN_DECISION audit event); disbursement is the
  deterministic operational step on the approve path. This grounded + audited +
  full-lifecycle combination distinguishes Saisei from shallow origination-only
  tools. The origination graph is additive — it shares only `SaiseiState`
  and the loan spine with the turnaround graph, so a facility originated here can
  later be assessed on the same record.

A facility originated through this graph and later assessed by the turnaround
graph forms one append-only ledger from `申込` to `完済` / `償却`. The origination
HTTP surface (`POST /api/v1/origination` start / get / decision, mirroring the
assessment run/resume API) is shipped in
[`api/origination_runs.py`](../../app/backend/api/origination_runs.py), so the
full lifecycle is drivable both in-process and as a service.
