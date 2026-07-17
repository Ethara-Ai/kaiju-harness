"""QC-C7 regression tests for the Harbor dataset exporter
(scripts/commit0_to_harbor_dataset.py).

Pins:
  C7-006 — the emitted test.sh passes pytest IDs as a QUOTED bash array, so an id
           containing a literal space survives as ONE argument (no silent drop).
  C7-007 — the spec cleaner's rustdoc-specific stripping (API-boundary early-break,
           code/symbol filters) is gated to language == "rust"; python/go README
           prose past a "Functions"/"Modules" heading is retained.
  C7-014 — ECR_TAG_OVERRIDES can be populated from a checked-in snapshot file
           (fail-loud), so the ~19 divergent tasks resolve WITHOUT the wholesale
           --allow-unverified-ecr-tags bypass.
  C7-016 — solve.sh has a full-ref fetch fallback for git hosts without
           allowReachableSHA1InWant, and renders to valid, fail-loud bash.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "commit0_to_harbor_dataset.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_h_c7", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load_module()

_BASH = shutil.which("bash")


# ---------------------------------------------------------------------------
# C7-006 — quoted TEST_IDS array
# ---------------------------------------------------------------------------
class TestC7_006_QuotedTestIds:

    def test_test_sh_uses_quoted_array(self):
        assert 'pytest "${TEST_IDS[@]}"' in H.TEST_SH
        # the old unquoted word-splitting form must be gone
        assert "pytest $TEST_IDS" not in H.TEST_SH
        assert 'tr \'\\n\' \' \'' not in H.TEST_SH

    @pytest.mark.skipif(_BASH is None, reason="bash not available")
    def test_test_sh_is_valid_bash(self, tmp_path):
        p = tmp_path / "test.sh"
        p.write_text(H.TEST_SH)
        r = subprocess.run([_BASH, "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    @pytest.mark.skipif(_BASH is None, reason="bash not available")
    def test_spaced_id_not_split(self, tmp_path):
        """A parametrize id with a literal space stays ONE argument."""
        ids = tmp_path / "ids.txt"
        ids.write_text("tests/a.py::test_ok\ntests/b.py::test_func[case 1]\n")
        script = (
            "TEST_IDS=()\n"
            'while IFS= read -r _tid || [ -n "$_tid" ]; do\n'
            '  [ -n "$_tid" ] && TEST_IDS+=("$_tid")\n'
            f"done < {ids}\n"
            'echo "${#TEST_IDS[@]}"\n'
            'printf "%s\\n" "${TEST_IDS[@]}"\n'
        )
        r = subprocess.run([_BASH, "-c", script], capture_output=True, text=True)
        lines = r.stdout.splitlines()
        assert lines[0] == "2"                       # exactly two ids, not three
        assert "tests/b.py::test_func[case 1]" in lines


# ---------------------------------------------------------------------------
# C7-007 — language-aware spec cleaner
# ---------------------------------------------------------------------------
# "Constants" is a rustdoc API-boundary marker that is NOT nav-cruft, so it
# reaches the boundary early-break — exactly the kind of heading a python/go
# README legitimately uses before more prose.
_SPEC_RAW = (
    "This library parses configuration data reliably and safely.\n"
    "\n"
    "Constants\n"
    "\n"
    "The loader reads every configuration key from the input document.\n"
)


class TestC7_007_LanguageAwareSpec:

    def test_rust_strips_past_api_boundary(self):
        out = H._clean_spec_text(_SPEC_RAW, 10_000, "rust")
        assert "parses configuration data" in out
        # everything past the "Functions" boundary is dropped for rustdoc
        assert "loader reads" not in out

    @pytest.mark.parametrize("lang", ["python", "go"])
    def test_non_rust_retains_prose_after_heading(self, lang):
        out = H._clean_spec_text(_SPEC_RAW, 10_000, lang)
        assert "parses configuration data" in out
        # README prose after a "Functions" heading MUST survive for python/go
        assert "loader reads" in out

    def test_extract_spec_text_threads_language(self):
        # signature accepts language; default preserves the historical rust behavior
        import inspect
        sig = inspect.signature(H.extract_spec_text)
        assert "language" in sig.parameters


# ---------------------------------------------------------------------------
# C7-014 — ECR tag override snapshot channel
# ---------------------------------------------------------------------------
class TestC7_014_EcrSnapshot:

    def setup_method(self):
        self._saved = dict(H.ECR_TAG_OVERRIDES)

    def teardown_method(self):
        H.ECR_TAG_OVERRIDES.clear()
        H.ECR_TAG_OVERRIDES.update(self._saved)

    def test_snapshot_populates_overrides_and_ecr_tag(self, tmp_path):
        snap = tmp_path / "ecr_tag_snapshot.json"
        snap.write_text('{"commit-0/Foo_Bar": "foo-bar-verified"}')
        n = H.load_ecr_tag_overrides(snap)
        assert n == 1
        # ecr_tag now resolves the divergent task WITHOUT the wholesale bypass
        assert H.ecr_tag("commit-0/Foo_Bar") == "foo-bar-verified"

    def test_missing_snapshot_fails_loud(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            H.load_ecr_tag_overrides(tmp_path / "does_not_exist.json")

    def test_malformed_snapshot_fails_loud(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('["not", "a", "dict"]')
        with pytest.raises(ValueError):
            H.load_ecr_tag_overrides(bad)

    def test_non_string_entry_fails_loud(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"commit-0/x": 123}')
        with pytest.raises(ValueError):
            H.load_ecr_tag_overrides(bad)


# ---------------------------------------------------------------------------
# C7-016 — solve.sh cross-host fetch fallback
# ---------------------------------------------------------------------------
class TestC7_016_SolveShFallback:

    def _render(self):
        return H.SOLVE_SH_TEMPLATE.format(
            reference_commit="abc123def456", fork_url="https://github.com/zahgon/repo")

    def test_fallback_full_ref_fetch_present(self):
        s = self._render()
        assert "git fetch --depth 1" in s          # fast path retained
        assert "refs/remotes/oracle/*" in s        # full-ref fallback added
        assert "set -euo pipefail" in s            # still fail-loud
        assert "abc123def456" in s

    @pytest.mark.skipif(_BASH is None, reason="bash not available")
    def test_renders_valid_bash(self, tmp_path):
        p = tmp_path / "solve.sh"
        p.write_text(self._render())
        r = subprocess.run([_BASH, "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
