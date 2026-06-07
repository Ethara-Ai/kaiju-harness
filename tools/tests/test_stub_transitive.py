from __future__ import annotations

import textwrap
from pathlib import Path


from tools.stub import collect_import_time_names


def _write(path: Path, code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code))


# ---------------------------------------------------------------------------
# Test 1 — transitive resolution (rq-style: decorator calls helper)
# ---------------------------------------------------------------------------
def test_transitive_via_decorator_in_tests(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    tests = tmp_path / "tests"

    _write(pkg / "__init__.py", "")
    _write(
        pkg / "utils.py",
        """\
        def get_version(conn):
            return "1.0"
        """,
    )
    _write(
        tests / "__init__.py",
        """\
        from pkg.utils import get_version

        def min_redis_version(version_tuple):
            v = get_version(None)
            return lambda cls: cls
        """,
    )
    _write(
        tests / "test_foo.py",
        """\
        from tests import min_redis_version

        @min_redis_version((5, 0, 0))
        class TestFoo:
            pass
        """,
    )

    result = collect_import_time_names(pkg, extra_scan_dirs=[tests])
    assert "min_redis_version" in result
    assert "get_version" in result


# ---------------------------------------------------------------------------
# Test 2 — sibling package scanning (plotly-style)
# ---------------------------------------------------------------------------
def test_sibling_package_scanning(tmp_path: Path) -> None:
    plotly = tmp_path / "plotly"
    utils = tmp_path / "_plotly_utils"

    _write(plotly / "__init__.py", "")
    _write(plotly / "graph_objects.py", "def make_trace(): ...\n")
    _write(utils / "__init__.py", "")
    _write(
        utils / "basevalidators.py",
        """\
        validator = create_validator()
        """,
    )

    result = collect_import_time_names(plotly, extra_scan_dirs=[utils])
    assert "create_validator" in result


# ---------------------------------------------------------------------------
# Test 3 — fixed-point convergence (chain: a -> b -> c)
# ---------------------------------------------------------------------------
def test_fixed_point_chain(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"

    _write(
        pkg / "__init__.py",
        """\
        result = a()
        """,
    )
    _write(
        pkg / "funcs.py",
        """\
        def a():
            return b()

        def b():
            return c()

        def c():
            return 42
        """,
    )

    result = collect_import_time_names(pkg)
    assert "a" in result
    assert "b" in result
    assert "c" in result


# ---------------------------------------------------------------------------
# Test 4 — defined_funcs filtering (attribute calls not preserved)
# ---------------------------------------------------------------------------
def test_defined_funcs_filtering(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"

    _write(
        pkg / "__init__.py",
        """\
        val = foo()
        """,
    )
    _write(
        pkg / "impl.py",
        """\
        def foo():
            return bar.method()
        """,
    )

    result = collect_import_time_names(pkg)
    assert "foo" in result
    assert "method" not in result


# ---------------------------------------------------------------------------
# Test 5 — cycle safety (a -> b -> a does not loop forever)
# ---------------------------------------------------------------------------
def test_cycle_does_not_infinite_loop(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"

    _write(
        pkg / "__init__.py",
        """\
        val = a()
        """,
    )
    _write(
        pkg / "cycle.py",
        """\
        def a():
            return b()

        def b():
            return a()
        """,
    )

    result = collect_import_time_names(pkg)
    assert "a" in result
    assert "b" in result


# ---------------------------------------------------------------------------
# Test 6 — backward compatibility (no extra_scan_dirs)
# ---------------------------------------------------------------------------
def test_backward_compat_no_extra_dirs(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"

    _write(
        pkg / "__init__.py",
        """\
        setup()
        """,
    )
    _write(
        pkg / "helpers.py",
        """\
        def setup():
            ...
        """,
    )

    result = collect_import_time_names(pkg)
    assert "setup" in result


# ---------------------------------------------------------------------------
# Test 7 — ast.ImportFrom scanning (wtforms-style: from X import Y)
# ---------------------------------------------------------------------------
def test_import_from_detected(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"

    _write(pkg / "__init__.py", "")
    _write(
        pkg / "core.py",
        """\
        def clean_key(key):
            return key.strip()
        """,
    )
    _write(
        pkg / "widgets.py",
        """\
        from pkg.core import clean_key

        DEFAULT = clean_key("hello")
        """,
    )

    result = collect_import_time_names(pkg)
    assert "clean_key" in result


# ---------------------------------------------------------------------------
# Test 8 — TYPE_CHECKING imports excluded
# ---------------------------------------------------------------------------
def test_type_checking_imports_excluded(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"

    _write(pkg / "__init__.py", "")
    _write(
        pkg / "module.py",
        """\
        from __future__ import annotations
        from typing import TYPE_CHECKING
        from pkg.core import real_func

        if TYPE_CHECKING:
            from pkg.models import MyModel

        val = real_func()
        """,
    )
    _write(
        pkg / "core.py",
        """\
        def real_func():
            return 42
        """,
    )
    _write(
        pkg / "models.py",
        """\
        class MyModel:
            pass
        """,
    )

    result = collect_import_time_names(pkg)
    assert "real_func" in result
    assert "MyModel" not in result
