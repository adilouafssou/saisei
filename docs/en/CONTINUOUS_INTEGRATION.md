# Continuous Integration

This document describes Saisei's automated quality pipeline: what runs on every
change, why each gate exists, and how to reproduce every check locally. It is
written to be useful to engineers, technical leadership, and auditors alike.

---

## At a glance

| Property | Value |
|---|---|
| Platform | GitHub Actions (`.github/workflows/ci.yml`) |
| Triggers | Every push to `main`; every pull request targeting `main` |
| Runtime | Fully offline — no database, cache, or API keys required |
| Gates | Lint & format, strict type-check, tests, regulated-output eval |
| Merge policy | All gates are merge-blocking |
| Local parity | `make verify` runs the identical gates |

The pipeline is intentionally **hermetic**: the application degrades to
deterministic mock providers when no LLM, PostgreSQL, or Redis is configured, so
CI needs zero external services and produces byte-for-byte reproducible results.

---

## Why this matters

Saisei co-authors a regulated credit document (経営改善計画書) under the FSA
framework. Two properties are therefore non-negotiable and are enforced
mechanically, not by convention:

1. **Determinism of figures.** Every monetary value is computed by typed,
   rule-based code and is the source of truth. The pipeline includes a dedicated
   gate that fails the build if the optional language-model polish ever adds,
   drops, or alters a figure.
2. **Auditability.** Each merge to `main` carries a green, reproducible record
   that the domain logic, type contracts, and regulated-output invariants all
   held. The same checks run identically on any machine via `make verify`,
   so a reviewer can independently reproduce the result.

---

## The gates

The gates run in increasing order of cost, so the cheapest failure is reported
first.

| # | Gate | Command | Purpose |
|---|---|---|---|
| 1 | Lint & format | `ruff check` + `ruff format --check` | Style and common-defect checks; consistency across the codebase. |
| 2 | Type-check | `mypy app tests` (strict) | Verifies the typed domain core (money, classification, state contracts). |
| 3 | Tests | `pytest` | Domain, node, graph-flow, and end-to-end coverage. |
| 4 | Regulated-output eval | `pytest tests/eval` | Golden-spine harness + numeric-preservation verifier for the plan output. |

> **Check-only by design.** CI reports problems; it never rewrites code. If gate
> 1 fails, run `make fix` locally to auto-correct, then commit. CI must never
> mutate a throwaway checkout.

### Scope of strict typing

Strict type-checking covers `app/backend` and `tests` — the domain core where the
value lies. The Reflex frontend models the UI as dynamic `Var` objects that are
intentionally excluded from `mypy --strict`; this is a deliberate, documented
scope decision, not an oversight.

### The online evaluation suites

The language-model-as-judge suites under `tests/eval/online/` require API keys
and are therefore **collected and skipped** in CI. They add no network
dependency to the pipeline and are intended for scheduled or manual runs in an
environment where credentials are configured.

---

## Reproduce locally

The pipeline is a thin wrapper over the project's `make` targets. To run the
exact CI gates on your machine:

```bash
make verify                   # ruff + mypy --strict + pytest (gates 1–3)
uv run pytest tests/eval -v   # gate 4: regulated-output eval
```

Individual gates:

```bash
make lint            # gate 1 — check only
make fix             # auto-fix lint + format, then re-run before committing
make type            # gate 2
make test            # gate 3
```

No `.env`, database, or keys are required — the offline mock providers are the
default.

---

## Operational notes

- **Concurrency.** Superseded runs on the same ref are cancelled automatically
  to conserve runner minutes.
- **Dependency resolution.** Dependencies are installed with `uv` from the
  committed lockfile, so CI and local builds resolve identical dependency sets.
- **Badge integrity.** The CI status badge in `README.md` points at this
  workflow. The workflow `name:` and job `name:` must stay in sync with the
  badge URL; changing either without updating the other will break the badge.

---

## First-time setup (forking or self-hosting)

No configuration is required for the pipeline to run: GitHub Actions executes
`.github/workflows/ci.yml` automatically on the triggers above. To enable it on
a fork or a new remote:

1. Ensure GitHub Actions is enabled for the repository
   (**Settings → Actions → General**).
2. Push a commit to `main` or open a pull request — the `CI` workflow starts
   automatically.
3. (Optional) Protect `main` so the `CI` check must pass before merge
   (**Settings → Branches → Branch protection rules**), making the gates
   formally merge-blocking.

Because the suite is offline, no secrets need to be configured for CI to pass.
Secrets are only needed for the optional online evaluation suites.
