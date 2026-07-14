"""Shared helper to build a bare ``SaiseiUIState`` for offline unit tests.

The UI-state unit tests exercise the PURE parts of the Reflex state (computed
vars, tab/route logic, watchlist capture) without a Reflex app, event loop, or
browser session. To do that they need a state instance that is NOT wired into a
Reflex runtime.

Why not just ``SaiseiUIState()`` or ``SaiseiUIState.__new__(...)``?
------------------------------------------------------------------
Under the installed Reflex (on Python 3.14), a state built without going through
the full app wiring has its per-instance bookkeeping left as ``None``. Reflex's
``BaseState.__setattr__`` calls ``self.dirty_vars.add(name)`` on every base-var
assignment, so the FIRST ``inst.some_var = ...`` raises
``AttributeError: 'NoneType' object has no attribute 'add'``.

The fix is to seed exactly the instance-level mutable bookkeeping that the
assignment path touches (``dirty_vars`` — a set), bypassing Reflex's guarded
``__setattr__`` via ``object.__setattr__``. This keeps the tests pure and
offline while making field assignment and event-handler invocation behave
normally. Computed-var reads (``Var.fget``) need nothing extra.
"""

from __future__ import annotations

from typing import Any

from app.frontend.state import SaiseiUIState

__all__ = ["bare_ui_state"]


def _base_var_defaults() -> dict[str, object]:
    """Return each declared base var's default value, factories resolved.

    The authoritative source for a base var's default is the pydantic
    ``FieldInfo`` -- NOT the Reflex ``Var`` object. Reflex's ``BaseState`` is
    pydantic-backed and exposes its fields via ``get_fields()`` (the accessor
    the installed Reflex surfaces in place of pydantic's ``model_fields``).

    Reading the default off the ``Var`` instead (``getattr(var, "default")`` /
    ``"_var_default"``) is wrong on the installed Reflex (>=0.6, pydantic v2):
    those carriers hand back the class-level ``Var`` DESCRIPTOR rather than a
    plain Python value, so seeding them onto a ``__new__``-built instance left
    every field resolving to a ``Var``. The first computed-var read (``Var.fget``)
    or event-handler invocation that touched such a field then raised
    ``AttributeError: type object 'SaiseiUIState' has no attribute ...``.

    We read it via an ``Any``-typed handle on the state class (Reflex's
    ``BaseState`` is not a statically-typed pydantic model from mypy's point of
    view) and materialise a concrete value per field -- calling any zero-arg
    ``default_factory`` -- so a ``__new__``-built instance is seeded with real
    Python values, never ``Var`` descriptors.
    """
    from dataclasses import MISSING

    from pydantic_core import PydanticUndefined

    def _is_missing(value: object) -> bool:
        """True when ``value`` is a "no default" sentinel, not a real default.

        Different field carriers use different sentinels for "no default":
        pydantic v2 uses ``PydanticUndefined`` (type ``PydanticUndefinedType``);
        dataclass-style / Reflex ``Var`` fields use ``dataclasses.MISSING``
        (type ``_MISSING_TYPE``). Seeding either sentinel as if it were a value
        is what made ``x in self.<var>`` raise
        ``TypeError: argument of type '_MISSING_TYPE' is not a container``.
        Match by identity AND by type name so we are robust to either carrier.
        """
        if value is None or value is PydanticUndefined or value is MISSING:
            return True
        return type(value).__name__ in {"_MISSING_TYPE", "PydanticUndefinedType"}

    cls: Any = SaiseiUIState
    # Reflex exposes its pydantic fields via ``get_fields()`` (name -> FieldInfo).
    # Fall back to an empty mapping defensively so the helper degrades to the
    # dirty_vars-only seed rather than raising.
    get_fields = getattr(cls, "get_fields", None)
    fields = get_fields() if callable(get_fields) else {}

    defaults: dict[str, object] = {}
    for name, field in fields.items():
        # Prefer a real declared default; skip every "no default" sentinel
        # (PydanticUndefined / dataclasses.MISSING / _MISSING_TYPE) so we never
        # seed a sentinel where the test code expects a real container/value.
        default = getattr(field, "default", PydanticUndefined)
        if not _is_missing(default):
            defaults[name] = default
            continue
        # No usable default value -> materialise from the zero-arg factory.
        factory = getattr(field, "default_factory", None)
        if factory is not None and not _is_missing(factory) and callable(factory):
            try:
                defaults[name] = factory()
            except Exception:  # noqa: BLE001 - skip uninstantiable factories
                continue
    return defaults


def bare_ui_state() -> SaiseiUIState:
    """Return a bare ``SaiseiUIState`` safe for offline field READ and WRITE.

    Built via ``__new__`` (no Reflex runtime). Two pieces of per-instance state
    are seeded directly (bypassing Reflex's guarded ``__setattr__`` via
    ``object.__setattr__``):

    * ``dirty_vars`` — the mutable set Reflex's ``__setattr__`` adds to on every
      base-var assignment (without it the FIRST ``inst.x = ...`` raises
      ``AttributeError: 'NoneType' object has no attribute 'add'``); and
    * every declared base var's DEFAULT value — so a plain attribute READ on the
      instance returns a concrete Python value instead of the class-level
      ``Var`` descriptor. Without this, reading an unset base var inside an event
      handler / pure method (e.g. ``self.servicing_loan_id`` in a ``not``
      context, or iterating ``self.origination_book``) raises ``VarTypeError``
      under the installed Reflex on Python 3.14.

    Callers then overwrite whatever fields their test needs.
    """
    inst = SaiseiUIState.__new__(SaiseiUIState)
    object.__setattr__(inst, "dirty_vars", set())
    # Seed concrete base-var defaults so attribute reads return real values
    # (not the class-level Var), making field reads behave normally offline.
    for name, value in _base_var_defaults().items():
        object.__setattr__(inst, name, value)
    return inst
