# Saisei Financial Extraction Rules

## TDB Identity Resolution

The 7-digit TDB Kigyo code (企業コード) is the primary key for corporate identity lookup.
Resolution steps:
1. Load TDB credit report → obtain 13-digit Hojin Bango (法人番号) and CompanyProfile.
2. Run anti-social-forces check (反社会的勢力チェック). If FLAGGED → hard error, no turnaround.
3. Populate `hojin_bango`, `company_profile`, `tdb_score` on state.

## Shisanhyo (試算表) Loading

Monthly J-GAAP trial balances are loaded by Hojin Bango from Core Banking.
Required accounts per row:
- `uriage` (売上) — Sales
- `uriage_genka` (売上原価) — COGS
- `hanbaihi` (販売費) — SG&A
- `eigai_shueki` (営業外収益) — Non-operating income (default 0)
- `eigai_hiyo` (営業外費用) — Non-operating expenses (default 0)

Derived fields (computed, never stored):
- `uriage_sourieki` = uriage − uriage_genka (Gross profit / 売上総利益)
- `eigyo_rieki` = uriage_sourieki − hanbaihi (Operating profit / 営業利益)
- `keijo_rieki` = eigyo_rieki + eigai_shueki − eigai_hiyo (Ordinary profit / 経常利益)

## Working-Capital Gap Estimation (Shikin Kuri / 資金繰り)

Formula:
```
cash_cycle_days = receivable_days − payable_days
daily_burn = monthly_cogs / 30
base_gap = cash_cycle_days × daily_burn
rate_stress = 1 + (latest_boj_rate_bps / 10_000)
monthly_margin = monthly_sales − monthly_cogs
gap = monthly_margin − (base_gap × rate_stress)
```
Negative gap = funding deficit.

## Keieisha Hosho (経営者保証) Data Requirements

For the guarantee-release assessment, the following data points are used:

### Condition 1: 法人個人分離 (Houjin-Kojin Bunri)
Proxy derived from trial balance non-operating items:
- `eigai_hiyo` (営業外費用) includes owner-loan interest (役員貸付利息)
- High eigai_hiyo relative to eigai_shueki signals poor separation
- Separation ratio = eigai_shueki / max(eigai_hiyo, 1)
- Threshold: ratio >= 1.0 → adequate separation

### Condition 2: 財務基盤の強化 (Zaimu Kiban no Kyouka)
Reuses existing EWS score and working-capital gap:
- EWS score < 40 AND working_capital_gap >= 0 → strong financial base

### Condition 3: 適時適切な情報開示 (Tekiji Tekisetsu na Jouhou Kaiji)
Data-completeness check:
- 12 months of Shisanhyo available → full disclosure
- TDB score present → external verification
- No errors in state → clean record
