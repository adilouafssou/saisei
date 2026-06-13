"""External data tool adapters for the Saisei backend.

Exposes:
- ``tdb_api``: Teikoku Databank (TDB) corporate identity & credit reports.
- ``boj_macro``: BOJ policy-rate curve and settlement liquidity metrics.
- ``MockDataProvider``: aggregating provider used by graph nodes.
"""

from app.backend.tools.provider import MockDataProvider

__all__ = ["MockDataProvider"]
