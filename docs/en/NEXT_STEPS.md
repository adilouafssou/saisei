# NEXT_STEPS.md — Roadmap

This is the forward-looking backlog: **what should be built next**, not a record of what is
done. For the current state of the system, read [`README.md`](../../README.md) and the
architectural spec in [`ROADMAP.md`](ROADMAP.md). Prior work is referenced here only where it
is needed to frame a remaining item.

The document is organised in three parts:

1. **Active backlog** — work to implement, in rough priority order.
2. **Parked** — built but deliberately paused by product decision until evidence accrues; do
   not resume until the stated trigger is met.
3. **V2 horizon** — the next-after-next direction; intentionally not active backlog and kept
   lighter than spec-grade so the roadmap stays lean.

A fourth short section, **Operational follow-ups**, lists live-endpoint confirmations that are
not code work: each path is already gated in-code and offline-green, and what remains is
pointing it at a real service with real credentials.

## Guiding principles

- **Determinism stays the source of truth.** Every figure is computed and auditable; LLMs
  assist with language and reasoning, never with numbers.
- **Everything observable, everything replayable.** A regulated lender must be able to
  explain any decision after the fact.
- **Human authority is non-negotiable.** The banker approves; the system proposes.
- **More power, never more authority.** New capability may inform the banker; it must never
  gain a vote in a gate, route, or figure.
- **No unverified LLM claim reaches the banker as fact.** Numbers are deterministic and
  verifier-gated; qualitative LLM output must be grounded in attributable evidence (a
  deterministic figure or a retrieved source) or visibly marked unverified. “Advisory-only” is
  not a sufficient safeguard — an ungrounded rationale can still steer a human decision.

---

## Active backlog

**Empty.** The active *code* backlog is cleared: every roadmap item whose code seam was the
deliverable has shipped (secrets seam, Alembic migrations, audit analytics, and the PII-safe
in-VPC trajectory export boundary). What was left of the former Active items has been
reclassified to where it actually belongs:

- **Operational, not code** — growing the RAG corpus (real-corpus ingest + per-source access
  controls), platform productionisation (run migrations, provision the secret manager,
  e-signature vendor wiring, distributed run worker), and audit-ledger hardening (physical
  purge + external notarisation). The retrieval / secret / migration / analytics *code* paths
  are shipped and offline-green; what remains is pointing them at real services. See
  *Operational follow-ups* below.
- **Deferred (code, gated)** — multi-round trajectory granularity. Recorded under *V2 horizon*
  beside the training loop it serves; do not build until a training run wants round-by-round
  signal.
- **V2 horizon** — PDF/photo OCR intake (the one remaining capture channel), already carried
  below; deferred until the confirm-and-correct loop is hardened.

Do not add speculative items here. A new Active-backlog entry should appear only when there is
fresh, codeable, not-yet-built work whose deliverable is code (not an operational connection
and not a product-gated deferral).

---

## Operational follow-ups (live-endpoint confirmations, not code work)

These are not roadmap features. Each path is shipped, offline-green, and guarded in-code
(skip-guards / offline no-op contracts / `# VERIFY` branches), so `make verify` stays offline
until an operator runs them.

- **LangSmith golden-dataset push.** Run `observability.push_golden_dataset` against a real
  LangSmith account and confirm the `saisei-classification-golden` dataset populates.
- **Live LLM-as-judge for the Keikakusho polish.** Run `tests/eval/online/test_polish_judge.py`
  against a real LLM endpoint (the deterministic numeric pre-gate already runs offline).
- **Tune thresholds from real telemetry.** Once traces accrue, tune `DEFAULT_FAITHFULNESS_FLOOR`
  (claim-grounding) and the per-node `NODE_LATENCY_BUDGETS_MS` / cost budgets in
  `analysis/node_budgets.py` from observed values.
- **Live data-client confirmation.** Confirm each `# VERIFY` branch (TDB, Core Banking,
  EDINET, BOJ, NTA Hojin Bango) against the real service, add Redis response caching on the
  live read paths, and add network-marked integration tests.
- **OIDC provider registration.** The token-verification transport is shipped and offline-green;
  what remains is operational: register the API as a client in the bank's identity provider
  (Okta / Entra / Keycloak) and set `SAISEI_AUTH_JWKS_URL` / `SAISEI_AUTH_ISSUER` /
  `SAISEI_AUTH_AUDIENCE` to its values. With those unset the API stays in the placeholder-identity
  posture (gated by `SAISEI_AUTH_REQUIRED`) and makes no network calls.
- **Run database migrations.** Alembic owns the application schema (`alembic.ini` + `migrations/`;
  the audit / trajectory / portfolio / pgvector-memory tables, reusing each store's `SCHEMA_SQL`
  as the single source of truth). The in-code idempotent bootstrap still runs so a fresh clone /
  offline run needs no migration step; for a managed deployment, run `make migrate` (Alembic
  `upgrade head`) against the real `SAISEI_POSTGRES_DSN` as part of the release. The LangGraph
  checkpointer tables remain owned by `PostgresSaver.setup()` and are not Alembic-managed.
- **Secret-manager provisioning + credentials.** Stand up the Vault / cloud secret manager and
  populate it with the real per-bank credentials (DB DSNs, LLM keys, data-source API keys). The
  in-code provider seam consumes them (`app/backend/secrets.py`: secrets are read through
  `resolve_secret`; a `@env:` / `@file:` / `@/path` reference dereferences without touching
  `.env`, and a Vault / cloud backend installs via `set_secret_provider`). All live secret
  reads now flow through the seam (`postgres_dsn` / `audit_dsn`, `llm_api_key` chat + embeddings,
  `tdb_api_key`, `core_banking_api_key`, `hojin_bango_app_id`, plus the audit signing and
  LangSmith keys). Provisioning and credential entry are operator-owned.
- **Run a trajectory training-data export.** The PII-safe export boundary is shipped and
  offline-green (`trajectory/export.py:run_export`; local JSONL only, no network, gated by
  `SAISEI_TRAJECTORY_EXPORT_ENABLED` + `SAISEI_TRAJECTORY_EXPORT_DIR`). What remains is
  operational: after data-governance / PII review, set those (and a real
  `SAISEI_TRAJECTORY_EXPORT_SALT`, optionally `SAISEI_TRAJECTORY_EXPORT_INCLUDE_FREE_TEXT`)
  and run an export for the chosen threads with `make export-trajectories ARGS="<thread-ids>"`
  (or `ARGS="--threads-file <file>"`; add `--dry-run` to preview the count offline without
  writing), then feed the resulting local JSONL to the in-VPC training run.
- **Real RAG corpus ingest.** Ingest the bank's back-catalogue of successful Keikakusho and the
  full FSA manual into pgvector, and wire per-source access controls to the bank's identity
  systems. The ingest tooling and the advisory-only retrieval path already exist; loading real
  governed content is an operational data-governance action.
- **E-signature delivery.** Contract an e-signature provider and connect the Keikakusho export
  to it (vendor account, signing templates, callback credentials). The PDF/DOCX render exists;
  the vendor wiring is a deployment/contract task.
- **Audit physical purge + external notarisation.** Two privileged audit-ledger ops left to the
  deployment: (1) the PHYSICAL purge of retention-eligible threads from `plan_retention` (must
  relax the append-only trigger under a dedicated, separately-logged DB role; the in-code
  planning + legal-hold exclusion already exist); (2) EXTERNAL notarisation — periodically
  anchoring a ledger digest to an independent timestamping authority / append-only external
  log, on top of the in-code Ed25519 signatures.
- **Distributed run worker (horizontal scale).** The off-request-path execution seam ships in
  code (`SAISEI_RUN_ASYNC` + the in-process thread executor; runs return `phase="running"` and
  are polled). Scaling across PROCESSES is the operational step: drop a distributed worker
  (Celery / RQ / Arq over the existing Redis) into the `RunExecutor` seam and run worker
  replicas. The route code does not change; durable cross-process run status moves with the
  worker.

---

## Parked — reconciliation-threshold calibration loop

The LLM-vs-floor reconciliation calibration loop (capture → analyze → `make calibrate` →
display panel) is built and then paused by product decision until the captured
`reconciliation_outcomes` corpus is meaningful (**≥ 10 labelled outcomes**). Keep the simple
hand-set constant, let the capture step accrue data, and do not act on the analysis/CLI/panel
layers until there is enough evidence. Resume by prompting the banker for the who-was-right
verdict at HITL, then **reviewing (never auto-applying)** the recommendation, and finally
generalising the same loop to `MAX_RECONCILIATION_TRIGGERS`.

---

## V2 horizon — frictionless capture & training (beyond the MVP)

*Not active backlog.* The next-after-next, product-defining upgrades — the direction once the
core (assess → rehearse → plan) and the MVP capture surface are proven in the field. Recorded
so the vision is not lost, kept deliberately lighter than the active backlog above. The same
architectural commitment holds for every one: **each channel normalises to the same typed
records and every non-deterministic extraction is banker-confirmed before it enters the
deterministic spine.**

- **Forward-to-Saisei email intake.** A dedicated per-borrower intake address
  (e.g. `borrower-xxx@intake.saisei`) so the 顧問税理士 emails the monthly 試算表 straight from
  their outbox; it is parsed and queued for banker confirmation through the existing
  confirm-and-correct seam. Needs inbound-mail infrastructure (address provisioning,
  attachment parsing, spam/abuse and sender-verification surface, per-tenant routing) that
  earns its keep only once upload + guided entry have proven demand. Auth/login is explicitly
  NOT this (single sign-on / user accounts belong to the platform productionisation work);
  this is purely a zero-effort *data-capture* channel.
- **PDF / photo OCR with confirm-and-correct.** Drag a scanned or photographed 試算表;
  deterministic OCR proposes figures the banker confirms cell-by-cell against the source image.
  The highest-leverage long-tail capture (distressed SMEs' data is often only on paper) and the
  natural completion of the document-drop channel — deferred because fuzzy extraction must
  never reach a credit decision unconfirmed, so it needs the confirm-and-correct loop hardened
  first.
- **Preference-optimisation training loop (GRPO/DPO/ORPO + SFT + reward model).** Turn the
  captured `(chosen, rejected, critique)` preference pairs into a measurably better strategist:
  supervised fine-tuning, preference optimisation (GRPO/DPO/ORPO), and a reward model over the
  banker revision notes, closed with **offline replay** measuring how often a candidate model
  would have proposed the strategy the banker actually chose. The algorithms are writable now,
  but their efficacy is gated on accumulated real trajectory volume and the strict in-VPC
  export/training boundary (financial data never leaves the bank). The capture seam already
  emits the training signal; this is the consumer that earns its keep only once the data
  exists.
  - **Deferred sub-item (code, gated): multi-round trajectory granularity.** Today one record
    is captured per HITL decision; capturing each intermediate revision round as its own record
    is codeable now but deliberately deferred — it is needed only once this training loop wants
    round-by-round signal. Build it together with (or just before) the consumer above, not on
    its own.
- **Scheduled continuous-monitoring daemon.** The Portfolio watchlist is persisted at rest and
  has deterministic read-side planning (`portfolio/monitor.py`: `plan_refresh` for due
  borrowers + `detect_crossings` for threshold events). What remains for V2 is the **scheduled
  ingest + alerting daemon** that drives those planners on a cadence and notifies the banker
  — deployment infrastructure (a job runner + a notification channel), gated behind its real
  data-governance decision (holding the whole book, not one borrower on demand). Saisei's
  invariant is preserved: the daemon surfaces and notifies; it never auto-runs an assessment.
