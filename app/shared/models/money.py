"""JPY money handling for Saisei.

Japanese yen principal amounts are always strict integers (no decimals) and are
displayed with standard comma separation, e.g. ``¥150,000,000``.

This module is the canonical location under ``app.shared.models.money``.
The legacy path ``shared.domain.money`` re-exports from here.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["JPY", "Yen", "format_jpy"]


def format_jpy(amount: int) -> str:
    """Format an integer yen amount as ``¥150,000,000``.

    Negative amounts render as ``-¥150,000,000``.

    Args:
        amount: Yen amount as an integer.

    Returns:
        The yen amount formatted with a ``¥`` prefix and thousands separators.
    """
    sign = "-" if amount < 0 else ""
    return f"{sign}\u00a5{abs(amount):,}"


class _JPYType(int):
    """Validated integer yen value.

    Behaves like ``int`` but accepts ONLY genuine integers, so currency
    principal can never silently become a float or be coerced from a surprising
    type. Specifically it rejects:

    - ``bool`` (``True``/``False`` are ints in Python but are never valid yen);
    - numeric strings (e.g. ``"1000"``) -- input must already be typed as int;
    - floats with a fractional component (e.g. ``1000.5``);
    - whole-valued floats (e.g. ``1000.0``) -- the source data must be int.

    Use the :data:`JPY` annotated alias in Pydantic models.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # strict=True rejects float/str/bool coercion at the core level; the
        # after-validator wraps the validated int in _JPYType. int serializes
        # back to a plain int for byte-stable model_dump().
        return core_schema.no_info_after_validator_function(
            cls._validate,
            core_schema.int_schema(strict=True),
            serialization=core_schema.plain_serializer_function_ser_schema(int),
        )

    @classmethod
    def _validate(cls, value: int) -> _JPYType:
        # Defensive: core strict int_schema already excludes bool, but guard
        # explicitly so the invariant holds even if the schema changes.
        if isinstance(value, bool):
            raise ValueError("JPY does not accept bool; yen must be a plain int.")
        return cls(value)

    def formatted(self) -> str:
        """Return this amount as ``¥150,000,000``."""
        return format_jpy(int(self))


# Annotated alias for use in Pydantic V2 models: ``amount: JPY``.
JPY = Annotated[int, _JPYType]

# Public, descriptive alias.
Yen = JPY
