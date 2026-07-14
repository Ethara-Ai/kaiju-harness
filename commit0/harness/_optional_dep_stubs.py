"""Single source of truth for stubbing the optional ``[agent]`` dependencies
(``aider`` and ``import_deps``) in test environments where they are not installed.

``agent.agents`` (``from aider.coders import Coder`` ...) and ``agent.agent_utils``
(``from import_deps import ModuleSet``) hard-import these packages at module load.
In a base (non-``[agent]``) test environment they are absent, so any test importing
an ``agent.*`` module fails at collection — and *which* test fails becomes
order-dependent, because a stub leaked into ``sys.modules`` by one test can silently
change another's outcome.

Two rules make this robust:

1. **Proper module objects, not bare ``MagicMock``.** A ``MagicMock`` placed in
   ``sys.modules`` for a *package* (``aider``, with submodules) has no valid
   ``__spec__``; import machinery that validates the parent spec raises
   ``ValueError: aider.__spec__ is not set``. Every stub here is a real
   ``types.ModuleType`` with an explicit ``ModuleSpec``.
2. **Idempotent via ``setdefault``.** Never clobber a genuinely-installed package,
   and never overwrite a stub another conftest already installed — so whichever
   test directory is collected first wins, and the stub is always the valid one.

Both ``commit0/harness/tests/conftest.py`` and ``agent/tests/conftest.py`` call
:func:`install_missing_optional_dep_stubs` at import time, so the stubs are present
before any test module is collected regardless of collection order.
"""

from __future__ import annotations

import importlib.machinery
import sys
import types


class _StubClass:
    """A permissive stand-in for an aider class (Coder, Model, InputOutput...)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name: str):
        return _StubClass()


class _StubInputOutput:
    """Stand-in for ``aider.io.InputOutput`` that ``GuardedInputOutput`` subclasses.

    Unlike the bare ``_StubClass`` (which only has instance-level ``__getattr__``),
    this exposes REAL, patchable methods matching aider's InputOutput surface.
    ``unittest.mock.patch.object(InputOutput, "confirm_ask", ...)`` targets the
    CLASS, and ``__getattr__`` does not apply at the class level — so tests that
    patch ``confirm_ask`` on the base need it to exist as a real method here.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.yes = kwargs.get("yes", False)

    def confirm_ask(
        self,
        question,
        default="y",
        subject=None,
        explicit_yes_required=False,
        group=None,
        allow_never=False,
    ):
        return "y" if self.yes else "n"

    def tool_output(self, *args, **kwargs):
        pass

    def tool_error(self, *args, **kwargs):
        pass

    def tool_warning(self, *args, **kwargs):
        pass

    def __getattr__(self, name: str):
        return _StubClass()


class _StubError(Exception):
    """Stand-in for aider exception types (e.g. FinishReasonLength)."""


class _StubModuleSet:
    """Stand-in for ``import_deps.ModuleSet`` (imported by ``agent.agent_utils``)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __getattr__(self, name: str):
        return _StubModuleSet()


def _module(name: str, attrs: dict) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _install_import_deps() -> None:
    try:
        import import_deps  # noqa: F401

        return
    except ImportError:
        pass
    sys.modules.setdefault(
        "import_deps", _module("import_deps", {"ModuleSet": _StubModuleSet})
    )


def _install_aider() -> None:
    try:
        import aider  # noqa: F401

        return
    except ImportError:
        pass

    aider_io = _module("aider.io", {"InputOutput": _StubInputOutput})
    aider_coders_base = _module(
        "aider.coders.base_coder",
        {"FinishReasonLength": _StubError, "Coder": _StubClass},
    )
    aider_coders = _module(
        "aider.coders", {"Coder": _StubClass, "base_coder": aider_coders_base}
    )
    aider_models = _module(
        "aider.models", {"Model": _StubClass, "register_models": lambda *a, **k: None}
    )
    aider_types_utils = _module(
        "aider.types.utils",
        {
            "Delta": _StubClass,
            "ModelResponseStream": _StubClass,
            "StreamingChoices": _StubClass,
        },
    )
    aider_types = _module("aider.types", {"utils": aider_types_utils})
    aider_pkg = _module(
        "aider",
        {
            "io": aider_io,
            "coders": aider_coders,
            "models": aider_models,
            "types": aider_types,
        },
    )

    for name, mod in {
        "aider": aider_pkg,
        "aider.io": aider_io,
        "aider.coders": aider_coders,
        "aider.coders.base_coder": aider_coders_base,
        "aider.models": aider_models,
        "aider.types": aider_types,
        "aider.types.utils": aider_types_utils,
    }.items():
        sys.modules.setdefault(name, mod)


def install_missing_optional_dep_stubs() -> None:
    """Install proper module stubs for any uninstalled optional ``[agent]`` dep.

    No-op for a dependency that is genuinely installed. Safe to call repeatedly and
    from multiple conftests — ``setdefault`` never overwrites an existing entry.
    """
    _install_import_deps()
    _install_aider()
