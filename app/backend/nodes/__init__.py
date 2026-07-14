"""Backend graph nodes for the Saisei turnaround engine.

Node files (blueprint names):
- ``financial_extraction``: intake (TDB identity + Shisanhyo) + macro (working-capital gap).
- ``ews_scoring``: EWS computation + FSA classification.
- ``kaizen_generation``: strategy proposal + plan writing + LLM polish.
- ``keieisha_hosho``: guarantee-release + succession-readiness assessment (PART 2).
- ``lead_arranger``: multi-critic consensus engine (PART 3).
- ``critics/``: main_bank, sub_bank, guarantor critic nodes (PART 3).
"""
