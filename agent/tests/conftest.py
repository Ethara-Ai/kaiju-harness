"""Install ``sys.modules`` stubs for ``aider.*`` when the optional
``[agent]`` extras are not installed. Lets ``unittest.mock.patch`` resolve
targets like ``aider.models.register_models`` even without a real aider.

When aider IS installed (production / CI with ``pip install -e ".[agent]"``)
this conftest does nothing.
"""

from __future__ import annotations

import sys
import types


def _install_aider_stubs() -> None:
    try:
        import aider  # noqa: F401

        return
    except ImportError:
        pass

    class _StubClass:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, *args, **kwargs):
            return self

        def __getattr__(self, name):
            return _StubClass()

    class _StubException(Exception):
        pass

    import importlib.machinery

    def _module(name: str, attrs: dict) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    aider_io = _module("aider.io", {"InputOutput": _StubClass})
    aider_coders_base = _module(
        "aider.coders.base_coder",
        {"FinishReasonLength": _StubException, "Coder": _StubClass},
    )
    aider_coders = _module(
        "aider.coders",
        {"Coder": _StubClass, "base_coder": aider_coders_base},
    )
    aider_models = _module(
        "aider.models",
        {"Model": _StubClass, "register_models": lambda *a, **k: None},
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

    sys.modules.update(
        {
            "aider": aider_pkg,
            "aider.io": aider_io,
            "aider.coders": aider_coders,
            "aider.coders.base_coder": aider_coders_base,
            "aider.models": aider_models,
            "aider.types": aider_types,
            "aider.types.utils": aider_types_utils,
        }
    )


_install_aider_stubs()
