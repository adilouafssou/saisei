"""Tests for JPY money handling."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.shared.models.money import JPY, format_jpy


class _Wallet(BaseModel):
    amount: JPY


def test_format_positive() -> None:
    assert format_jpy(150_000_000) == "¥150,000,000"


def test_format_zero() -> None:
    assert format_jpy(0) == "¥0"


def test_format_negative() -> None:
    assert format_jpy(-2_500_000) == "-¥2,500,000"


def test_jpy_accepts_int() -> None:
    assert int(_Wallet(amount=1000).amount) == 1000


def test_jpy_accepts_negative_int() -> None:
    assert int(_Wallet(amount=-2_500_000).amount) == -2_500_000


def test_jpy_accepts_zero() -> None:
    assert int(_Wallet(amount=0).amount) == 0


def test_jpy_rejects_fractional_float() -> None:
    with pytest.raises(ValidationError):
        _Wallet(amount=1000.5)  # type: ignore[arg-type]


def test_jpy_rejects_whole_valued_float() -> None:
    # A whole-valued float is still a float; source yen data must be a real int.
    with pytest.raises(ValidationError):
        _Wallet(amount=1000.0)  # type: ignore[arg-type]


def test_jpy_rejects_numeric_string() -> None:
    with pytest.raises(ValidationError):
        _Wallet(amount="1000")  # type: ignore[arg-type]


def test_jpy_rejects_bool() -> None:
    # True == 1 in Python, but a bool is never a valid yen principal.
    with pytest.raises(ValidationError):
        _Wallet(amount=True)  # type: ignore[arg-type]


def test_jpy_serializes_to_plain_int() -> None:
    # model_dump must emit a plain int so serialization stays byte-stable.
    dumped = _Wallet(amount=1000).model_dump()
    assert dumped == {"amount": 1000}
    assert type(dumped["amount"]) is int
