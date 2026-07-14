"""Multi-critic nodes for the Saisei creditor-meeting simulation.

Three independent critic nodes evaluate the generated Keikakusho in parallel.
Critics MUST NOT see each other's feedback; they evaluate independently.

All PASS/FAIL gate decisions are DETERMINISTIC rule-based checks.
The LLM may only phrase the prose of the final report, never decide a verdict.

Exported nodes:
- ``feasibility_critic_node``: Turnaround consultant (PART 4 — advisory-only
  upstream operational pre-screen; never gates).
- ``main_bank_critic_node``: Risk-Averse Lead Bank (P1 — accountability).
- ``sub_bank_critic_node``: Regional Syndicate Lender (P2 — fairness).
- ``guarantor_critic_node``: Credit Guarantee Corp / Shinyo Hosho Kyokai (P0 — compliance).
"""

from app.backend.nodes.critics.feasibility import feasibility_critic_node
from app.backend.nodes.critics.guarantor import guarantor_critic_node
from app.backend.nodes.critics.main_bank import main_bank_critic_node
from app.backend.nodes.critics.sub_bank import sub_bank_critic_node

__all__ = [
    "feasibility_critic_node",
    "main_bank_critic_node",
    "sub_bank_critic_node",
    "guarantor_critic_node",
]
