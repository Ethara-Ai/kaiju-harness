"""Regression tests for the stubber's empty-class bug.

A class whose body is only a docstring (exception subclasses, marker mixins,
ABC stubs) used to be left with an empty body after class-docstring removal in
``--removal-mode all``, producing an ``IndentationError`` that broke test
collection. The stubber must emit ``pass`` for such classes so the output
always parses. See ``StubTransformer.transform_source``.
"""

from __future__ import annotations

import ast

from tools.stub import StubTransformer


def _stub(src: str) -> str:
    out = StubTransformer(removal_mode="all", keep_docstrings=False).transform_source(
        src, "test.py"
    )
    assert out is not None
    ast.parse(out)  # must ALWAYS parse — that's the whole point
    return out


def _class(out: str, name: str) -> ast.ClassDef:
    return next(
        n
        for n in ast.walk(ast.parse(out))
        if isinstance(n, ast.ClassDef) and n.name == name
    )


def test_docstring_only_class_gets_pass() -> None:
    out = _stub('class Foo:\n    """only a docstring."""\n')
    assert isinstance(_class(out, "Foo").body[0], ast.Pass)


def test_docstring_only_subclass_like_signature_expired() -> None:
    src = (
        "class Base(Exception):\n"
        '    """base."""\n'
        "\n"
        "    def __init__(self, m):\n"
        '        """init."""\n'
        "        self.m = m\n"
        "\n\n"
        "class SignatureExpired(Base):\n"
        '    """Docstring-only subclass."""\n'
        "\n\n"
        "class BadHeader(Base):\n"
        '    """another."""\n'
        "\n"
        "    def f(self):\n"
        '        """f."""\n'
        "        return 1\n"
    )
    out = _stub(src)
    # The docstring-only subclass is the case that used to break collection.
    assert isinstance(_class(out, "SignatureExpired").body[0], ast.Pass)
    # A class that still has a (stubbed) method must NOT receive a spurious pass.
    assert any(isinstance(b, ast.FunctionDef) for b in _class(out, "BadHeader").body)


def test_class_with_attribute_keeps_attribute_no_spurious_pass() -> None:
    src = 'compact = object()\nclass S:\n    """doc."""\n\n    default = compact\n'
    body = _class(_stub(src), "S").body
    assert any(isinstance(b, ast.Assign) for b in body)
    assert not any(isinstance(b, ast.Pass) for b in body)


def test_two_adjacent_docstring_only_classes() -> None:
    # Mirrors url_safe.py: URLSafeSerializer + URLSafeTimedSerializer back-to-back.
    src = (
        'class A:\n    """a."""\n\n\n'
        'class B(A):\n    """b."""\n\n\n'
        'class C(A):\n    """c."""\n'
    )
    out = _stub(src)
    for name in ("A", "B", "C"):
        assert isinstance(_class(out, name).body[0], ast.Pass)


def test_module_docstring_only_still_emptied() -> None:
    # Modules may be empty; only classes need a pass. Must still parse.
    out = _stub('"""module docstring."""\n')
    assert "module docstring" not in out
