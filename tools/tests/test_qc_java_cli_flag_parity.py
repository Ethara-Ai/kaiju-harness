"""Guard: every flag run_pipeline_java.sh passes to `commit0-java agent` must be
a real CLI option.

QC: run_pipeline_java.sh forwards ablation/config flags to the Java agent CLI
(unlike other langs, which emit a YAML config). `--no-inject-test-files-readonly`
(and --blind-tests/--names-only-tests/--strip-non-stubs) were passed by the
pipeline but NOT defined on cli_java.agent, so `commit0-java agent` died with
"No such option" the moment the flag was enabled — which then cascaded into a
cryptic `git diff base..branch` failure in evaluate (the agent branch was never
created). This pins the pipeline<->CLI flag contract so the drift cannot recur.
"""
import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _pipeline_agent_flags() -> set[str]:
    """Boolean flags run_pipeline_java.sh forwards to `commit0-java agent`."""
    src = (REPO / "run_pipeline_java.sh").read_text(encoding="utf-8")
    return set(re.findall(r"common_flags\+=\(\s*(--[a-z][a-z-]+)", src))


def _cli_agent_flags() -> set[str]:
    """Every long-form flag the cli_java.agent typer command accepts.

    Standard typer bools expose --name / --no-name (derived from the parameter
    name); explicit "--a/--b" option strings are parsed from the source."""
    from commit0 import cli_java

    flags: set[str] = set()
    for name, p in inspect.signature(cli_java.agent).parameters.items():
        opt = name.replace("_", "-")
        flags.add(f"--{opt}")
        flags.add(f"--no-{opt}")
    # also honor any explicit typer.Option("--a/--b", ...) names in the source
    src = (REPO / "commit0" / "cli_java.py").read_text(encoding="utf-8")
    for a, b in re.findall(r'"(--[a-z][a-z-]+)/(--[a-z][a-z-]+)"', src):
        flags.add(a)
        flags.add(b)
    return flags


def test_every_pipeline_agent_flag_is_a_valid_cli_option():
    pipeline = _pipeline_agent_flags()
    cli = _cli_agent_flags()
    assert pipeline, "no common_flags found in run_pipeline_java.sh (parser drift?)"
    missing = sorted(pipeline - cli)
    assert not missing, (
        f"run_pipeline_java.sh forwards flag(s) that commit0-java agent does NOT "
        f"define: {missing}. Add them to cli_java.agent (and wire to "
        f"JavaAgentConfig) or the pipeline will crash with 'No such option'."
    )


@pytest.mark.parametrize(
    "flag",
    ["--no-inject-test-files-readonly", "--blind-tests",
     "--names-only-tests", "--strip-non-stubs"],
)
def test_regressed_flags_present(flag):
    assert flag in _cli_agent_flags(), f"{flag} must be a valid commit0-java agent option"
