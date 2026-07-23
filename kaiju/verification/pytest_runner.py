"""Execute a generated pytest module against a solution checkout and parse the
result. Pure-Python repos run host-side; others run in the pipeline container.

The generated suite is run three ways for meta-verification:
  * against the GOLDEN solution  -> should PASS (the tests are sound), and
  * against the unmodified STUB  -> should FAIL (the tests actually discriminate),
  * against the AGENT's solution -> the graded oracle-strength / held-out signal.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

_SUMMARY_RE = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "error": re.compile(r"(\d+) errors?"),
    "skipped": re.compile(r"(\d+) skipped"),
}
# `-rA` short-summary lines: "PASSED <nodeid>" / "FAILED <nodeid> - <reason>".
# pytest appends " - <reason>" to FAILED/ERROR lines when the message is short, so
# the nodeid must be captured with an OPTIONAL trailing reason (not anchored to EOL).
_PERTEST_RE = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+?)(?:\s+-\s.*)?\s*$", re.M)
_OUTCOME_MAP = {"PASSED": "pass", "XPASS": "pass", "FAILED": "fail", "ERROR": "error",
                "SKIPPED": "skip", "XFAIL": "skip"}
_TESTFILE = "_kaiju_verify_generated_test.py"


def test_key(nodeid: str) -> str:
    """Stable per-test key across golden/stub/solution runs (drop the filename)."""
    return nodeid.split("::", 1)[1] if "::" in nodeid else nodeid


def parse_per_test(text: str) -> dict[str, str]:
    """{test_key: 'pass'|'fail'|'error'|'skip'} from the -rA short summary."""
    out: dict[str, str] = {}
    for m in _PERTEST_RE.finditer(text):
        out[test_key(m.group(2))] = _OUTCOME_MAP.get(m.group(1), "fail")
    return out


@dataclass
class PytestResult:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: int = 0
    status: str = "OK"        # OK | COLLECTION_ERROR | NO_TESTS | INFRA
    detail: str = ""
    output: str = ""          # full pytest stdout+stderr (which tests failed + tracebacks)
    per_test: dict = None     # {test_key: pass|fail|error|skip}

    def __post_init__(self):
        if self.per_test is None:
            self.per_test = {}

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.total if self.total else None

    def to_dict(self, *, include_output: bool = False) -> dict:
        d = asdict(self)
        d["total"] = self.total
        d["pass_rate"] = self.pass_rate
        if not include_output:
            d.pop("output", None)   # kept in a .log file, not inlined by default
        return d


def _parse(text: str, returncode: int) -> PytestResult:
    def n(key):
        m = _SUMMARY_RE[key].search(text)
        return int(m.group(1)) if m else 0
    passed, failed, errors, skipped = n("passed"), n("failed"), n("error"), n("skipped")
    status = "OK"
    if returncode == 5 or (passed + failed + errors == 0):
        status = "NO_TESTS"
    if "errors during collection" in text or "ERROR collecting" in text or errors and not (passed + failed):
        status = "COLLECTION_ERROR"
    return PytestResult(passed=passed, failed=failed, errors=errors, skipped=skipped,
                        collected=passed + failed + errors + skipped, status=status,
                        detail=text.strip().splitlines()[-1] if text.strip() else "",
                        output=text, per_test=parse_per_test(text))


def run_pytest_in_dir(solution_dir: str | Path, test_code: str, *,
                      timeout: int = 180, python: str = "python") -> PytestResult:
    """Write *test_code* into *solution_dir* and run pytest on just that file, with
    the solution on sys.path. Isolated to that one file so we only run the generated
    tests, not the repo's own suite. Cleans up afterward."""
    solution_dir = Path(solution_dir)
    if not solution_dir.is_dir():
        return PytestResult(status="INFRA", detail=f"solution dir missing: {solution_dir}")
    test_path = solution_dir / _TESTFILE
    test_path.write_text(test_code, encoding="utf-8")
    try:
        r = subprocess.run(
            [python, "-m", "pytest", _TESTFILE, "-p", "no:cacheprovider",
             "--no-header", "--tb=short", "-rA", "-o", "addopts="],
            cwd=str(solution_dir), capture_output=True, text=True, timeout=timeout,
            env=_env_with_pythonpath(solution_dir))
        return _parse(r.stdout + "\n" + r.stderr, r.returncode)
    except subprocess.TimeoutExpired:
        return PytestResult(status="INFRA", detail="pytest timed out")
    except (OSError, subprocess.SubprocessError) as e:
        return PytestResult(status="INFRA", detail=str(e))
    finally:
        try:
            test_path.unlink()
        except OSError:
            pass


def _env_with_pythonpath(solution_dir: Path) -> dict:
    import os
    env = dict(os.environ)
    src = solution_dir / "src"
    parts = [str(solution_dir)]
    if src.is_dir():
        parts.append(str(src))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


# --------------------------------------------------------------------------- #
# Prepare a solution checkout (git worktree at a commit, optional patch applied)
# --------------------------------------------------------------------------- #
def checkout_solution(repo_dir: str | Path, commit: str, *,
                      patch: str | None = None) -> Path | None:
    """Create a temp worktree of *repo_dir* at *commit* (optionally with *patch*
    applied) for running the generated tests against. Caller removes it."""
    repo_dir = Path(repo_dir)
    dest = Path(tempfile.mkdtemp(prefix="kaiju_verify_wt_"))
    try:
        subprocess.run(["git", "-C", str(repo_dir), "worktree", "add", "--detach",
                        str(dest), commit], capture_output=True, text=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if patch:
        pf = dest / "_kaiju_patch.diff"
        pf.write_text(patch, encoding="utf-8")
        subprocess.run(["git", "-C", str(dest), "apply", "--reject", "--whitespace=nowarn",
                        str(pf)], capture_output=True, text=True, timeout=60)
        pf.unlink(missing_ok=True)
    return dest


def remove_worktree(repo_dir: str | Path, dest: Path) -> None:
    subprocess.run(["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(dest)],
                   capture_output=True, text=True, timeout=60)
