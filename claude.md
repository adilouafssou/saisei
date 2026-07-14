# claude.md — Saisei (再生) Engineering Rulebook

Persistent memory and rulebook for the Saisei Autonomous EWS & Keiei Kaizen Keikakusho (経営改善計画書) Orchestrator.

> Saisei is a post-lending agentic engine for Japanese Regional Banks (地方銀行). It detects deteriorating SME financial health (Early Warning Signal) and orchestrates the drafting of a regulatory turnaround plan with Human-in-the-Loop strategy negotiation.

---

## 1. Tech Stack Constraints (HARD REQUIREMENTS)

| Component | Constraint |
|---|---|
| Python | `>= 3.12` |
| LangGraph | `>= 0.2.x` |
| Reflex | `>= 0.6.x` |
| Pydantic | `V2 syntax ONLY` |
| Package manager | `uv` (Astral) — never `pip`/`poetry` directly |
| State checkpointing | PostgreSQL via `psycopg` (v3) |
| Cache / queue | Redis |
| Logging | `structlog` |
| Lint / type / test | `ruff`, `mypy`, `pytest` |

## 2. Strict Boundaries (NON-NEGOTIABLE)

- **Never** use `print()` for logging. Always use `structlog`.
- **Never** hardcode API keys, secrets, or DB credentials. Use env vars / `pydantic-settings`.
- **Always** use Pydantic V2 syntax (`model_config`, `field_validator`, `ConfigDict`). No V1 `class Config` / `@validator`.
- **Never** guess or hallucinate external APIs. If unknown, mock it in `mocks/`.
- **Always** type-annotate all public functions; `mypy --strict` must pass.
- **Never** commit large multi-file dumps. One component per change, verify, then proceed.
- **Always** keep JPY as integers for principal amounts. No floats for currency principal.
- **Never** bypass the Verifier loop. Every change ships with a verification checklist.

## 3. Japanese Finance Domain Rules (STRICTLY ENFORCE)

### Accounting (J-GAAP)
Trial Balances (Shisanhyo 試算表) use standard Japanese accounts:
- Uriage (売上) — Sales
- Uriage Genka (売上原価) — COGS
- Hanbaihi (販売費) — SG&A
- Keijo Rieki (経常利益) — Ordinary Profit

### Currency & Formatting
- JPY principal = strict `int` (no decimals).
- Display format: Japanese comma separation, e.g. `¥150,000,000`.

### Corporate Identity
- TDB Kigyo Code (企業コード): 7 digits.
- Hojin Bango (法人番号 — Corporate Number): 13 digits.

### Fiscal Calendar
- Default mock data to Japanese fiscal year ending March 31 (Sangatsu Kessan — 3月決算).

### Regulatory Framework (FSA Financial Inspection Manual / 金融検査マニュアル)
Debtor classification MUST use the five categories (``FsaClass`` StrEnum):
- Seijosaki (正常先) — Normal
- Yochuisaki (要注意先) — Needs Attention (Substandard)
  - Sub-tier: Yokanrisaki (要管理先) — Special Attention; modelled as
    ``special_attention=True`` on state, NOT a separate enum member.
- Hatan Kenensaki (破綻懸念先) — In Danger of Bankruptcy (Doubtful)
- Jisshitsu Hatansaki (実質破綻先) — De facto Bankrupt
- Hatansaki (破綻先) — Bankrupt

### Macro / Settlement
- Factor BOJ macro variables: interest-rate hikes affecting T+1/T+2 settlement cycles and working-capital gaps (Shikin Kuri — 資金繰り).

## 4. State Management Rules (LangGraph HITL)

- Use the Postgres checkpointer so graph state survives restarts.
- Human-in-the-Loop strategy negotiation uses `interrupt()` nodes.
- Resume execution exclusively via `Command(resume=...)`. Never mutate checkpoint state out-of-band.
- The graph State is a single Pydantic V2 model; nodes return partial updates only.
- All monetary fields in State are `int` (JPY). Validators enforce non-negative principal where applicable.

## 5. Workflow: Karpathy Method (Spec -> Verifier -> Environment)

1. **Spec** approved before any implementation.
2. **Verifier**: `make verify` (ruff + mypy + pytest) is the automated gate; GitHub Actions runs it on every push to `main` and every pull request.
3. **Environment**: fully containerized; reproducible via `make setup` / `make run`.
4. One component per MR. Each MR description includes a verification checklist.

## 6. Repository Conventions

- Branch naming: `spec/*`, `feat/*`, `fix/*`, `chore/*`.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`).
- No secrets in repo; `.env.example` documents required vars.
