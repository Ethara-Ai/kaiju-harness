"""Unit tests for ``tools.python_version``.

Covers every signal collector, the intersection algorithm, Poetry caret/tilde
conversion, and the conflict semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from tools.python_version import (
    DetectionResult,
    NoSignalsError,
    Signal,
    SignalSource,
    VersionConflictError,
    collect_signals,
    detect,
    detect_from_signals,
    poetry_constraint_to_pep440,
)

SUPPORTED = {"3.9", "3.10", "3.11", "3.12", "3.13"}


# ---------------------------------------------------------------------------
# Poetry constraint conversion
# ---------------------------------------------------------------------------


class TestPoetryConstraintToPep440:
    @pytest.mark.parametrize(
        "poetry,expected",
        [
            ("^3.10", ">=3.10.0,<4.0.0"),
            ("^3.10.5", ">=3.10.5,<4.0.0"),
            ("^3", ">=3.0.0,<4.0.0"),
            ("~3.10", ">=3.10.0,<3.11.0"),
            ("~3.10.5", ">=3.10.5,<3.11.0"),
            ("~3", ">=3.0.0,<4.0.0"),
            ("3.10", "==3.10.*"),
            ("3.10.5", "==3.10.5"),
            ("3", "==3.*"),
            (">=3.8,<3.13", ">=3.8,<3.13"),
            (">=3.10", ">=3.10"),
            (">=3.8, <3.13", ">=3.8,<3.13"),
            ("*", ""),
            ("any", ""),
            ("", ""),
        ],
    )
    def test_conversion(self, poetry: str, expected: str) -> None:
        assert poetry_constraint_to_pep440(poetry) == expected

    def test_caret_zero_variant(self) -> None:
        # ^0.2.3 → >=0.2.3,<0.3.0 (Poetry semver rule for major=0)
        assert poetry_constraint_to_pep440("^0.2.3") == ">=0.2.3,<0.3.0"
        assert poetry_constraint_to_pep440("^0.0.3") == ">=0.0.3,<0.0.4"

    def test_invalid_constraint_raises(self) -> None:
        with pytest.raises(ValueError):
            poetry_constraint_to_pep440("^abc")

    def test_result_parses_as_specifier_set(self) -> None:
        # Every conversion that returns non-empty must be a valid PEP 440 spec
        for poetry in ["^3.10", "~3.10.5", "3.10", ">=3.8,<3.13"]:
            converted = poetry_constraint_to_pep440(poetry)
            SpecifierSet(converted)  # raises on invalid


# ---------------------------------------------------------------------------
# detect_from_signals — pure algorithm tests
# ---------------------------------------------------------------------------


def _tier_a(source: SignalSource, spec: str, raw: str | None = None) -> Signal:
    return Signal(source=source, constraint=SpecifierSet(spec), raw=raw or spec)


def _tier_b(source: SignalSource, versions: tuple[str, ...]) -> Signal:
    return Signal(source=source, versions=versions, raw=",".join(versions))


class TestDetectFromSignalsNoSignals:
    def test_no_signals_returns_none_version(self) -> None:
        result = detect_from_signals([], SUPPORTED)
        assert result.version is None
        assert result.source == "default"
        assert result.conflicts == []


class TestDetectFromSignalsTierAOnly:
    def test_pep621_min_only_picks_lowest(self) -> None:
        sig = _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10")
        result = detect_from_signals([sig], SUPPORTED)
        assert result.version == "3.10"
        assert "pyproject.toml" in result.source

    def test_intersection_of_min_and_max(self) -> None:
        sig = _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.9,<3.12")
        result = detect_from_signals([sig], SUPPORTED)
        # candidates: 3.9, 3.10, 3.11 — picks 3.9
        assert result.version == "3.9"

    def test_two_constraints_intersect(self) -> None:
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10"),
            _tier_a(SignalSource.SETUP_CFG, "<3.13"),
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        assert result.version == "3.10"

    def test_exclusion_specifier(self) -> None:
        sig = _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.9,!=3.10.*")
        result = detect_from_signals([sig], SUPPORTED)
        assert result.version == "3.9"  # 3.10 excluded; 3.9 is lowest survivor

    def test_exact_pin(self) -> None:
        sig = _tier_a(SignalSource.PYTHON_VERSION_FILE, "==3.11.*", raw="3.11")
        result = detect_from_signals([sig], SUPPORTED)
        assert result.version == "3.11"

    def test_constraint_outside_supported_picks_only_match(self) -> None:
        sig = _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.13")
        result = detect_from_signals([sig], SUPPORTED)
        assert result.version == "3.13"

    def test_no_supported_version_satisfies_raises(self) -> None:
        sig = _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=4.0")
        with pytest.raises(VersionConflictError) as exc:
            detect_from_signals([sig], SUPPORTED)
        assert exc.value.candidates == set()
        assert all(">=4.0" in r for r in exc.value.rejecting_sources.values())

    def test_prereleases_excluded_by_default(self) -> None:
        # ">=3.10" should accept 3.10.0 but not 3.10.0a1; we compare against X.Y.0
        # so this is implicitly covered, but verify the prereleases=False kwarg works:
        sig = _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10.0b1")
        result = detect_from_signals([sig], SUPPORTED)
        assert result.version == "3.10"  # 3.10.0 final satisfies >=3.10.0b1


class TestDetectFromSignalsTierB:
    def test_tier_b_narrows_tier_a(self) -> None:
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.9"),
            _tier_b(SignalSource.GHA_MATRIX, ("3.11", "3.12")),
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        assert result.version == "3.11"  # lowest in matrix that also satisfies Tier A

    def test_tier_b_only_picks_min(self) -> None:
        sig = _tier_b(SignalSource.TOX_ENVLIST, ("3.10", "3.11"))
        result = detect_from_signals([sig], SUPPORTED)
        assert result.version == "3.10"

    def test_tier_b_disjoint_from_tier_a_keeps_tier_a(self) -> None:
        # GHA matrix only lists 3.8 (unsupported); Tier A says >=3.10 → keep Tier A
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10"),
            _tier_b(SignalSource.GHA_MATRIX, ("3.8",)),
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        assert result.version == "3.10"
        assert any("matrix" in c for c in result.conflicts)


class TestDetectFromSignalsTierC:
    def test_dockerfile_breaks_tie(self) -> None:
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10"),
            Signal(
                source=SignalSource.DOCKERFILE_FROM,
                versions=("3.12",),
                raw="3.12",
            ),
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        # Without Dockerfile we'd pick 3.10 (lowest). Dockerfile hint forces 3.12.
        assert result.version == "3.12"

    def test_dockerfile_outside_candidates_ignored(self) -> None:
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10,<3.12"),
            Signal(
                source=SignalSource.DOCKERFILE_FROM,
                versions=("3.13",),
                raw="3.13",
            ),
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        # Dockerfile says 3.13 but Tier A excludes it; keep lowest Tier A candidate
        assert result.version == "3.10"


class TestDetectFromSignalsConflicts:
    def test_conflicts_listed_when_signals_disagree(self) -> None:
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10"),
            _tier_a(SignalSource.PYTHON_VERSION_FILE, "==3.11.*", raw="3.11"),
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        # Intersection: >=3.10 AND ==3.11.* → only 3.11
        assert result.version == "3.11"
        # No conflict — both satisfied
        assert result.conflicts == []

    def test_real_conflict_reported(self) -> None:
        sigs = [
            _tier_a(SignalSource.PEP621_REQUIRES_PYTHON, ">=3.10"),
            _tier_b(SignalSource.GHA_MATRIX, ("3.9",)),  # disjoint
        ]
        result = detect_from_signals(sigs, SUPPORTED)
        assert result.version == "3.10"
        assert len(result.conflicts) == 1
        assert "matrix" in result.conflicts[0]


# ---------------------------------------------------------------------------
# collect_signals — file I/O tests via tmp_path
# ---------------------------------------------------------------------------


class TestCollectPep621:
    def test_basic_requires_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\nrequires-python = '>=3.10'\n"
        )
        sigs = collect_signals(tmp_path)
        assert any(s.source == SignalSource.PEP621_REQUIRES_PYTHON for s in sigs)

    def test_no_pyproject_no_signal(self, tmp_path: Path) -> None:
        sigs = collect_signals(tmp_path)
        assert not any(s.source == SignalSource.PEP621_REQUIRES_PYTHON for s in sigs)

    def test_malformed_pyproject_silently_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[[[ not valid toml")
        sigs = collect_signals(tmp_path)
        assert not any(s.source == SignalSource.PEP621_REQUIRES_PYTHON for s in sigs)

    def test_pep621_with_lower_and_upper(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\nrequires-python = '>=3.9,<3.13'\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.PEP621_REQUIRES_PYTHON)
        assert sig.constraint is not None
        # Upper bound is preserved (not silently dropped like the old regex)
        from packaging.version import Version

        assert Version("3.13.0") not in sig.constraint


class TestCollectPoetry:
    def test_caret_constraint(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry.dependencies]\npython = '^3.10'\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.POETRY_PYTHON)
        assert sig.constraint is not None
        from packaging.version import Version

        assert Version("3.10.0") in sig.constraint
        assert Version("4.0.0") not in sig.constraint

    def test_dict_form_constraint(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry.dependencies]\npython = {version = '~3.10'}\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.POETRY_PYTHON)
        assert sig.constraint is not None
        from packaging.version import Version

        assert Version("3.10.0") in sig.constraint
        assert Version("3.11.0") not in sig.constraint

    def test_star_constraint_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry.dependencies]\npython = '*'\n"
        )
        sigs = collect_signals(tmp_path)
        assert not any(s.source == SignalSource.POETRY_PYTHON for s in sigs)


class TestCollectSetupCfg:
    def test_python_requires(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text(
            "[options]\npython_requires = >=3.9\n"
        )
        sigs = collect_signals(tmp_path)
        assert any(s.source == SignalSource.SETUP_CFG for s in sigs)


class TestCollectSetupPy:
    def test_python_requires_kwarg(self, tmp_path: Path) -> None:
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            "setup(name='x', python_requires='>=3.10,<3.13')\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.SETUP_PY)
        from packaging.version import Version

        assert Version("3.10.0") in sig.constraint
        assert Version("3.13.0") not in sig.constraint


class TestCollectPythonVersionFile:
    def test_short_form(self, tmp_path: Path) -> None:
        (tmp_path / ".python-version").write_text("3.11\n")
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.PYTHON_VERSION_FILE)
        from packaging.version import Version

        assert Version("3.11.0") in sig.constraint
        assert Version("3.12.0") not in sig.constraint

    def test_long_form(self, tmp_path: Path) -> None:
        (tmp_path / ".python-version").write_text("3.10.5\n")
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.PYTHON_VERSION_FILE)
        from packaging.version import Version

        # Should pin to minor (3.10.*) — not to specific patch (patch is dev preference)
        assert Version("3.10.0") in sig.constraint
        assert Version("3.10.99") in sig.constraint
        assert Version("3.11.0") not in sig.constraint

    def test_pypy_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".python-version").write_text("pypy3.10\n")
        sigs = collect_signals(tmp_path)
        assert not any(s.source == SignalSource.PYTHON_VERSION_FILE for s in sigs)


class TestCollectRuntimeTxt:
    def test_heroku_format(self, tmp_path: Path) -> None:
        (tmp_path / "runtime.txt").write_text("python-3.10.5\n")
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.RUNTIME_TXT)
        from packaging.version import Version

        assert Version("3.10.0") in sig.constraint


class TestCollectTox:
    def test_envlist_simple(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text(
            "[tox]\nenvlist = py310, py311\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.TOX_ENVLIST)
        assert set(sig.versions) == {"3.10", "3.11"}

    def test_envlist_with_braces(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text(
            "[tox]\nenvlist = {py310,py311}-{lint,test}\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.TOX_ENVLIST)
        assert set(sig.versions) == {"3.10", "3.11"}

    def test_envlist_dotted_form(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py3.10, py3.11\n")
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.TOX_ENVLIST)
        assert set(sig.versions) == {"3.10", "3.11"}


class TestCollectNoxfile:
    def test_list_form(self, tmp_path: Path) -> None:
        (tmp_path / "noxfile.py").write_text(
            "import nox\n@nox.session(python=['3.10', '3.11'])\ndef tests(s): ...\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.NOXFILE)
        assert set(sig.versions) == {"3.10", "3.11"}

    def test_scalar_form(self, tmp_path: Path) -> None:
        (tmp_path / "noxfile.py").write_text(
            "import nox\n@nox.session(python='3.11')\ndef tests(s): ...\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.NOXFILE)
        assert set(sig.versions) == {"3.11"}


class TestCollectGhaMatrix:
    def test_inline_list(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            '        python-version: ["3.10", "3.11", "3.12"]\n'
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.GHA_MATRIX)
        assert set(sig.versions) == {"3.10", "3.11", "3.12"}

    def test_block_list(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "matrix:\n"
            "  python-version:\n"
            "      - '3.10'\n"
            "      - '3.11'\n"
        )
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.GHA_MATRIX)
        assert set(sig.versions) == {"3.10", "3.11"}


class TestCollectDockerfile:
    def test_from_python(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\n")
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.DOCKERFILE_FROM)
        assert set(sig.versions) == {"3.11"}

    def test_from_python_with_registry(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM docker.io/library/python:3.12\n")
        sigs = collect_signals(tmp_path)
        sig = next(s for s in sigs if s.source == SignalSource.DOCKERFILE_FROM)
        assert set(sig.versions) == {"3.12"}


# ---------------------------------------------------------------------------
# detect() — top-level integration tests
# ---------------------------------------------------------------------------


class TestDetect:
    def test_empty_repo_returns_none_no_fallback(self, tmp_path: Path) -> None:
        result = detect(tmp_path, SUPPORTED)
        assert result.version is None

    def test_empty_repo_uses_fallback(self, tmp_path: Path) -> None:
        result = detect(tmp_path, SUPPORTED, fallback="3.12")
        assert result.version == "3.12"
        assert result.source == "default"

    def test_strict_mode_raises_on_empty(self, tmp_path: Path) -> None:
        with pytest.raises(NoSignalsError):
            detect(tmp_path, SUPPORTED, strict=True)

    def test_realistic_pep621_project_picks_lowest(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'foo'\nrequires-python = '>=3.9,<3.13'\n"
        )
        result = detect(tmp_path, SUPPORTED)
        assert result.version == "3.9"

    def test_qaequilibrae_style_no_signals(self, tmp_path: Path) -> None:
        """qaequilibrae has no PEP 621 metadata. With fallback, the entry should
        not silently advertise 3.13 (the old buggy behavior)."""
        result = detect(tmp_path, SUPPORTED, fallback="3.10")
        assert result.version == "3.10"
        assert "default" in result.source

    def test_layered_signals_intersect(self, tmp_path: Path) -> None:
        # PEP 621 says >=3.9, GHA matrix tests 3.11/3.12 → resolver picks 3.11
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\nrequires-python='>=3.9'\n"
        )
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            'jobs:\n  t:\n    strategy:\n      matrix:\n        python-version: ["3.11","3.12"]\n'
        )
        result = detect(tmp_path, SUPPORTED)
        assert result.version == "3.11"
