# Keiei Kaizen Keikakusho Templates

## Keikakusho Markdown Template

The following is the canonical Markdown template for the 経営改善計画書 (Keiei Kaizen Keikakusho).
All `{placeholder}` values are filled deterministically from state; no LLM generates figures.

```markdown
# 経営改善計画書（Keiei Kaizen Keikakusho）

- 企業名（Company）: {company_name}
- 法人番号（Hojin Bango）: {hojin_bango}
- 債務者区分（FSA classification）: {fsa_kanji}

## 1. 現状分析（Current position）

- 売上（Uriage）: {uriage}
- 売上原価（Uriage Genka）: {uriage_genka}
- 販売費（Hanbaihi）: {hanbaihi}
- 経常利益（Keijo Rieki）: {keijo_rieki}
- 資金繰りギャップ（Working-capital gap）: {working_capital_gap}

## 2. 改善施策（Turnaround strategy）

### {strategy_title}

{strategy_rationale}

- 期待される経常利益改善（Expected Keijo Rieki uplift）: {expected_uplift} / 年

## 3. 実行計画（Action plan）

1. 施策の実行体制を構築し、担当者と期限を設定する。
2. 月次で進捗をモニタリングし、経常利益と資金繰りを検証する。
3. 銀行と四半期ごとにレビューを実施する。
```

## LLM Polish System Prompt

The following system prompt is used for the optional LLM polish pass.
The LLM MUST NOT change any figures, section headings, or FSA classification.

```
You are a Japanese regional-bank credit officer. Improve the readability
and tone of the following Keiei Kaizen Keikakusho (経営改善計画書) draft.
Preserve ALL monetary figures, section headings, and the FSA classification
exactly. Do not invent numbers. Keep the Markdown structure. Respond with
the improved Markdown only.
```

## Strategy Templates

### 価格転嫁の実行（Price pass-through）
Renegotiate unit prices with key customers to recover input-cost
inflation (genka koutou). A 3% price increase restores margin
eroded by failed kakaku tenka.

Expected uplift formula: `annual_sales × 0.03`

### 原価低減（COGS reduction）
Diversify suppliers and improve yield to cut COGS by ~2%,
directly lifting gross profit (uriage sourieki).

Expected uplift formula: `annual_cogs × 0.02`

### 販売費・一般管理費の見直し（SG&A rationalisation）
Rationalise overhead by ~5% to protect ordinary profit while
price and cost measures take effect.

Expected uplift formula: `annual_sga × 0.05`

### 資金繰り改善（Working-capital / Shikin Kuri）
Shorten receivable days and negotiate extended payable terms
to close the working-capital deficit widened by BOJ rate hikes
and T+1/T+2 settlement pressure.

Expected uplift formula: `abs(working_capital_gap)`

## Negotiation Report Template

For distressed borrowers (要注意先/破綻懸念先), the final output combines:
1. Creditor-meeting burden-share simulation (from lead_arranger)
2. Guarantee-release / succession-readiness assessment (from keieisha_hosho)

```markdown
# 交渉報告書（Negotiation Report）

## 保証解除評価（Guarantee Release Assessment）
- 保証解除スコア（Hosho Kaijo Score）: {hosho_kaijo_score}/100
- 承継準備状況（Succession Readiness）: {succession_ready}

## 債権者会議シミュレーション（Creditor Meeting Simulation）
- 交渉ステータス（Negotiation Status）: {negotiation_status}

### 負担分担表（Burden-Sharing Table）
{burden_sharing_table}

### 修正指示（Revision Directive）
{revision_directive}
```
