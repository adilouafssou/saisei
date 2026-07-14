"""Offline guard: every production module must import cleanly.

Why this exists
---------------
!4 fixed a NameError that took the app down in production: ``graph.py`` called
``resolve_secret(...)`` without importing it. The ENTIRE test suite stayed green
because the only code path that reaches that line runs in persistence mode
(Postgres), which CI never exercises -- so a missing import in an unexercised
branch was invisible.

That is a whole class: any production module with a missing/typo'd import, an
undefined name at module scope, or a broken top-level statement stays green until
it runs in prod. This guard retires the class by simply IMPORTING every module
under ``app/`` -- which executes its top-level statements and resolves its
imports -- and failing if any of OUR modules is broken.

False-positive discipline (the senior call)
-------------------------------------------
The goal is to catch *our* mistakes, never to flake on the CI environment. So:

* A missing THIRD-PARTY optional dependency (e.g. ``fpdf2`` for the PDF
  exporter) is SKIPPED, not failed -- it is an environment fact, not a code bug.
* A failure that names one of OUR modules (an ``app.*`` import that does not
  resolve) or any ``NameError`` (the !4 signature: a name used but never
  imported/defined) is a HARD FAILURE with the offending module + error.
* ``app/app.py`` is excluded: importing it has heavy top-level side effects
  (it constructs the Reflex ``rx.App`` and the FastAPI app). The leaf modules
  it depends on are all covered individually, which is where this class of bug
  actually lives.

Pure and offline: importing a module runs no network / DB (every module's live
paths are lazily guarded behind config), mirroring the rest of the suite.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

#: Repository ``app`` package root (``<repo>/app``).
_APP_DIR = Path(__file__).resolve().parent.parent / "app"

#: Modules excluded from the import sweep, with the reason.
#: ``app.app`` builds the Reflex app + FastAPI app at import time (heavy
#: top-level side effects); its leaf dependencies are all swept individually.
_EXCLUDED: frozenset[str] = frozenset({"app.app"})


def _iter_app_modules() -> list[str]:
    """Return the dotted names of every importable module under ``app/``."""
    names: list[str] = []
    for info in pkgutil.walk_packages([str(_APP_DIR)], prefix="app."):
        if info.name in _EXCLUDED:
            continue
        names.append(info.name)
    return sorted(names)


def _names_app_module(exc: ModuleNotFoundError) -> bool:
    """Whether a ModuleNotFoundError is about one of OUR modules (a real bug).

    A missing ``app.*`` module is our mistake (a broken intra-project import); a
    missing third-party module is an environment fact we skip on.
    """
    missing = exc.name or ""
    return missing == "app" or missing.startswith("app.")


_MODULES = _iter_app_modules()


def test_discovers_modules() -> None:
    """Sanity: the sweep actually found a substantial set of modules."""
    assert len(_MODULES) > 20, f"only discovered {_MODULES!r}"
    # The module whose missing import motivated this guard must be in scope.
    assert "app.backend.graph" in _MODULES


@pytest.mark.parametrize("module_name", _MODULES)
def test_module_imports_cleanly(module_name: str) -> None:
    """Importing the module resolves all its names / top-level statements.

    Hard-fails on the !4 signature (NameError, or an unresolved ``app.*``
    import). Skips only on a genuinely-absent third-party optional dependency,
    which is an environment fact rather than a code defect.
    """
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if _names_app_module(exc):
            pytest.fail(
                f"{module_name} fails to import: missing intra-project module "
                f"{exc.name!r}. This is a broken import in our own code."
            )
        pytest.skip(
            f"optional third-party dependency {exc.name!r} not installed; "
            f"skipping import of {module_name}"
        )
    except NameError as exc:
        # The exact !4 failure mode: a name used but never imported/defined.
        pytest.fail(
            f"{module_name} raises NameError on import ({exc}). A name is used "
            f"but never imported or defined (the bug class !4 fixed)."
        )
