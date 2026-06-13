# DOMAIN_ONBOARDING.md — Japanese SME finance, explained for this project

> A self-contained primer on the Japanese regional-banking and accounting concepts behind
> Saisei, written for a reviewer, interviewer, or AI engineer who is **not** a specialist in
> Japanese finance. Every term is defined in plain English, then mapped to where it lives in
> the code. Accuracy notes flag where the project deliberately simplifies reality.

---

## 1. The business context: post-lending SME management

In Japan, **regional banks** (地方銀行, *chihō ginkō*) are the primary lenders to small and
medium-sized enterprises (SMEs / 中小企業, *chūshō kigyō*) outside the big cities. Their
relationship with a borrower does not end when the loan is paid out. Japanese supervisory
practice expects the bank to **keep monitoring the borrower's health** and, if it weakens,
to **actively help the company recover** rather than immediately call the loan.

This "help them recover" philosophy is institutional. A bank that supports a viable but
struggling SME back to health keeps a customer, avoids a write-off, and meets supervisory
expectations. The central artifact of that support is a **turnaround plan** the bank helps
the borrower write (see §5).

> **Why it matters for Saisei:** the whole product exists in the *post-lending* window —
> watching, scoring, classifying, and then co-authoring the recovery plan.

## 2. The regulator and the rulebook

- **FSA** — the **Financial Services Agency** (金融庁, *Kinyū-chō*), Japan's financial
  regulator. It supervises banks and sets the expectations they are examined against.
- **Financial Inspection Manual** (金融検査マニュアル, *Kinyū Kensa Manyuaru*) — historically
  the FSA's examination handbook that told banks how to assess borrowers and provision for
  loan losses. It defined the **debtor classification** system below.

> **Accuracy note:** the FSA formally **abolished** the Inspection Manual in **December 2019**,
> moving to a more principles-based, bank-by-bank supervisory dialogue. However, its
> **debtor-classification framework remains the de-facto industry standard** that banks still
> use internally for self-assessment (*jiko satei* / 自己査定) and provisioning. Saisei models
> that still-standard framework; calling it "the FSA framework" is accurate in spirit, and a
> sharp interviewer will appreciate you knowing the Manual itself was retired.

## 3. Debtor classification (債務者区分) — the heart of credit risk

When a bank self-assesses a borrower, it assigns a **debtor classification** (債務者区分,
*saimusha kubun*). This drives how much the bank must set aside for potential losses.

The **full** framework has five tiers, from healthiest to worst:

| # | Japanese | Romaji | Plain English |
|---|----------|--------|---------------|
| 1 | 正常先 | *seijō-saki* | **Normal** — sound, no concerns |
| 2 | 要注意先 | *yōchūi-saki* | **Needs attention / Substandard** — some weakness |
| — | └ 要管理先 | *yōkanri-saki* | **Needs management** — a *sub-category* of #2: loans already past due ≥3 months or with relaxed terms |
| 3 | 破綻懸念先 | *hatan-kenen-saki* | **In danger of bankruptcy** |
| 4 | 実質破綻先 | *jisshitsu-hatan-saki* | **De-facto bankrupt** |
| 5 | 破綻先 | *hatan-saki* | **Bankrupt** |

**What Saisei models — and why it's a deliberate simplification:**

Saisei implements the **top three states** that matter for the *early-warning + turnaround*
use case:

| Saisei `FsaClass` | Japanese | In the code |
|---|---|---|
| `JOYO` | **正常** (Normal) | monitor-only; ends the workflow |
| `YOI_KANRI` | **要注意** (Substandard) | enters turnaround |
| `YUKYO_GUCHI` | **要管理** (Needs management) | enters turnaround |

This is intentional: a turnaround engine acts in the **"still savable"** band. Once a borrower
is 破綻懸念先 or worse, the workflow is liquidation/recovery, not a 経営改善計画書. Modelling the
full five tiers would add regulatory surface without serving the product's purpose.

> **Romanization caveat:** the code uses `YUKYO_GUCHI` as the identifier for 要管理. The standard
> reading of 要管理 is *yōkanri*. Treat the enum *name* as an internal label; the authoritative
> display value is the kanji `要管理`, which is what reviewers and the UI see.

> 🔎 In the code: `app/shared/models/classification.py` (the closed `FsaClass` enum,
> kanji/English labels, and `requires_turnaround`); the thresholds live in
> `app/backend/nodes/ews_scoring.py`.

## 4. Reading the financials: J-GAAP and the trial balance

**J-GAAP** = Japanese Generally Accepted Accounting Principles, the domestic accounting
standard most SMEs report under (as opposed to IFRS or US-GAAP).

A **trial balance** (試算表, *shisanhyo*) is a periodic snapshot of account totals. Saisei
consumes **monthly** trial balances and reads the standard J-GAAP profit ladder from them:

| Japanese | Romaji | English | Definition |
|----------|--------|---------|------------|
| 売上 | *uriage* | **Sales / revenue** | top line |
| 売上原価 | *uriage genka* | **COGS** (cost of goods sold) | direct cost of what was sold |
| 売上総利益 | *uriage sōrieki* | **Gross profit** | Sales − COGS |
| 販売費及び一般管理費 | *hanbaihi oyobi ippan kanrihi* (SG&A) | **Operating overhead** | selling + admin expense (code: `hanbaihi`) |
| 営業利益 | *eigyō rieki* | **Operating profit** | Gross profit − SG&A |
| 営業外収益／費用 | *eigyō-gai shūeki / hiyō* | **Non-operating income / expense** | e.g. interest |
| 経常利益 | *keijō rieki* | **Ordinary profit** | Operating profit + non-op income − non-op expense |

**経常利益 (ordinary profit) is the number Japanese analysts watch most** — it captures
recurring earning power before one-off items, and it is the headline measure of whether a
business is fundamentally healthy. Saisei's early-warning and uplift logic both center on it.

> 🔎 In the code: `app/shared/models/accounting.py` — `TrialBalance` stores the raw accounts and
> computes `uriage_sourieki` (gross), `eigyo_rieki` (operating), and `keijo_rieki` (ordinary)
> as derived fields, so the profit ladder is always internally consistent.

### Two stress patterns the project models

- **原価高騰 (*genka kōtō*) — cost inflation.** Input costs rise, so 売上原価 climbs and gross
  margin gets squeezed even if sales hold.
- **価格転嫁 (*kakaku tenka*) — price pass-through.** Raising your own prices to pass higher
  costs on to customers. **Failed** *kakaku tenka* (you *couldn't* raise prices) is the
  classic SME margin trap: costs up, prices stuck, profit collapses. This is exactly the
  Aichi-manufacturer fixture's predicament.

## 5. The deliverable: 経営改善計画書 (the turnaround plan)

The **Keiei Kaizen Keikakusho** (経営改善計画書) is a **management-improvement / turnaround
plan**. When a borrower slips into 要注意 / 要管理, the bank helps the SME produce this document:
it states the current position, the causes of deterioration, the concrete improvement measures,
and the projected recovery path. It is the practical instrument of the "help them recover"
philosophy in §1, and it influences how the bank may treat the loan.

> 🔎 In the code: `plan_writer_node` in `app/backend/nodes/kaizen_generation.py` renders the
> 経営改善計画書 as deterministic Markdown from the approved strategy and the latest financials;
> `propose_strategies` (same file) proposes the measures (price pass-through, COGS reduction,
> SG&A rationalisation, working-capital repair), each with an expected 経常利益 uplift grounded
> in the company's real figures.

## 6. Corporate identity numbers

Two different identifiers appear, and they are not interchangeable:

- **TDB 企業コード** (*kigyō code*) — a **7-digit** company code from **Teikoku Databank**
  (帝国データバンク), Japan's leading private credit-research agency. It keys TDB's proprietary
  profile and credit data.
- **法人番号** (*hōjin bangō*) — the **13-digit Corporate Number**, a *public* government ID
  assigned to every registered company (analogous in spirit to a company tax ID).

Saisei resolves the TDB 7-digit code at intake, then uses the 13-digit 法人番号 to pull the
company's trial balances from the core-banking system.

> 🔎 In the code: `app/backend/tools/tdb_api.py` validates the 7-digit code and 13-digit
> 法人番号 in `CompanyProfile`; `intake_node` (in `app/backend/nodes/financial_extraction.py`)
> resolves identity and runs the anti-social-forces check below.

### 反社会的勢力 (anti-social forces) check

**反社会的勢力** (*hanshakaiteki seiryoku*, "anti-social forces") is the standard Japanese term
for organized crime / *yakuza*-linked entities. Japanese banks are **legally required** to
screen for and refuse business with them. A flagged result is a hard stop — no turnaround
support, escalate. Saisei treats a `FLAGGED` check as a blocking error.

> 🔎 In the code: `AntiSocialCheck` in `app/backend/tools/tdb_api.py`; the hard-stop handling
> in `intake_node` (`app/backend/nodes/financial_extraction.py`).

## 7. Money: the yen

- The currency is the **Japanese yen** (¥ / JPY). The yen has **no minor unit in practice** —
  principal amounts are whole integers; there is no "cents." Writing ¥1,000.50 of loan
  principal is a category error.
- Conventional formatting uses thousands separators: **¥150,000,000**.

> 🔎 In the code: `app/shared/models/money.py` defines a `JPY` type that **rejects fractional
> floats at validation time** and a `format_jpy` helper that renders `¥150,000,000`. This is
> the project encoding a real domain rule directly into the type system.

## 8. Macro & settlement context

- **BOJ** = the **Bank of Japan** (日本銀行), the central bank. After years of negative/zero
  interest-rate policy, the BOJ began **raising rates** — a regime change that increases
  borrowing costs and tightens cash for leveraged SMEs.
- **T+1 / T+2 settlement** = a trade/payment settles one or two business days after the
  transaction date. Longer settlement and rising rates widen the gap between when a company
  *pays* its suppliers and when it *collects* from customers.
- **資金繰り** (*shikin kuri*) — **cash-flow / working-capital management**: the day-to-day
  juggling of inflows and outflows. A **working-capital gap / deficit** means cash is tied up
  in the operating cycle faster than the business generates it — a common way an otherwise
  profitable SME runs out of money.

> 🔎 In the code: `macro_node` in `app/backend/nodes/financial_extraction.py` estimates the
> 資金繰り gap from the cash-conversion cycle (receivable days − payable days), the daily cash
> burn, and a stress factor from the latest BOJ policy rate; `app/backend/tools/boj_macro.py`
> supplies the rising rate curve and settlement metrics. (**EDINET** is the FSA's electronic
> disclosure system — here it stands in for the macro/disclosure data source.)

## 9. The worked example: the Aichi manufacturer

The primary fixture is **愛知精密製作所株式会社**, a metal-parts manufacturer in **Aichi
prefecture** (愛知県) — the heart of Japan's auto-supply-chain manufacturing. Its story, which
the numbers in `app/backend/tools/fixtures/aichi_manufacturer.json` tell month by month:

1. Input costs rise (**原価高騰**), lifting 売上原価.
2. The firm **can't raise its own prices** (**failed 価格転嫁**) — sales drift *down*, not up.
3. Gross margin compresses; 経常利益 trends toward and then below zero.
4. Rising BOJ rates + settlement timing widen the **資金繰り** gap into a deficit.
5. The signals push the borrower to **要管理** — squarely in the turnaround band.

This is the canonical "viable business, wrong moment" case the entire system is built to catch
early and help fix.

---

## Glossary (quick reference)

| Term | Romaji | English |
|---|---|---|
| 地方銀行 | chihō ginkō | regional bank |
| 中小企業 | chūshō kigyō | SME |
| 金融庁 | Kinyū-chō | FSA (regulator) |
| 金融検査マニュアル | — | (former) Financial Inspection Manual |
| 債務者区分 | saimusha kubun | debtor classification |
| 正常先 / 要注意先 / 要管理先 | seijō / yōchūi / yōkanri | Normal / Substandard / Needs-management |
| 試算表 | shisanhyo | trial balance |
| 売上 / 売上原価 | uriage / uriage genka | sales / COGS |
| 売上総利益 / 営業利益 / 経常利益 | sōrieki / eigyō rieki / keijō rieki | gross / operating / ordinary profit |
| 販売費 | hanbaihi | SG&A |
| 原価高騰 | genka kōtō | cost inflation |
| 価格転嫁 | kakaku tenka | price pass-through |
| 経営改善計画書 | keiei kaizen keikakusho | management-improvement (turnaround) plan |
| 企業コード | kigyō code | TDB 7-digit company code |
| 法人番号 | hōjin bangō | 13-digit Corporate Number |
| 反社会的勢力 | hanshakaiteki seiryoku | anti-social forces (organized crime) |
| 資金繰り | shikin kuri | cash-flow / working-capital management |
| 日本銀行 (BOJ) | Nippon Ginkō | Bank of Japan |
