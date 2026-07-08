"""Rust agent runner — mirrors ``run_agent_no_rich.py`` for Rust repos.

Uses Rust-specific file discovery, test IDs, lint commands, and system prompts
while reusing all language-agnostic infrastructure (progress tracking, git ops,
trajectory capture, output formatting).
"""

import json
import logging
import multiprocessing
import os
import time
from pathlib import Path
from typing import cast

import yaml
from git import Repo
from tqdm import tqdm

from agent.agent_utils import create_branch, load_agent_config
from agent.agent_utils_rust import (
    extract_rust_function_stubs,
    find_rust_files_to_edit,
    get_rust_file_dependencies,
    get_target_edit_files_rust,
    run_with_compile_gate,
)
from agent.agents_rust import RustAiderAgents
from agent.class_types import AgentConfig
from agent.run_agent import DirContext, run_eval_after_each_commit
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.llm_cost_capture import capture_module_calls
from commit0.cli import read_commit0_config_file
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.constants_rust import RUST_SPLIT
from commit0.harness.split_utils import resolve_split
from commit0.harness.patch_utils_rust import filter_rust_patch
from agent.module_patch import module_file_patch
from commit0.harness.utils import load_dataset_from_config
from agent.claude_code.recovery import run_with_recovery

logger = logging.getLogger(__name__)

_RUST_PROMPT_PATH = Path(__file__).parent / "prompts" / "rust_system_prompt.md"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _load_spec_text(repo_path: str) -> str:
    """Read spec PDF from repo dir, decompressing bz2 if needed. Returns raw text or ''."""
    import bz2

    spec_pdf = Path(repo_path) / "spec.pdf"
    spec_bz2 = Path(repo_path) / "spec.pdf.bz2"

    if spec_bz2.exists() and not spec_pdf.exists():
        try:
            with bz2.open(str(spec_bz2), "rb") as fin:
                with open(str(spec_pdf), "wb") as fout:
                    fout.write(fin.read())
        except Exception as exc:
            logger.warning("Failed to decompress %s: %s", spec_bz2, exc)
            if spec_pdf.exists():
                spec_pdf.unlink()
            return ""

    if not spec_pdf.exists():
        return ""

    try:
        import fitz

        raw = ""
        with fitz.open(spec_pdf) as doc:
            for page in doc:
                raw += page.get_text()  # type: ignore[attr-defined]
        return raw
    except Exception as exc:
        logger.warning("Failed to extract spec PDF text: %s", exc)
        return ""


_MAX_FILE_CONTEXT_CHARS = 50_000
_MAX_PER_FILE_CONTEXT_CHARS = 20_000
_MAX_FUNCTION_LIST_CHARS = 30_000


def get_rust_message(
    agent_config: AgentConfig,
    repo_path: str,
    target_files: list[str],
    test_files: list[str] | None = None,
) -> tuple[str, list[SummarizerCost]]:
    """Build the Rust system prompt from the template.

    Fills ``{repo_name}``, ``{function_list}``, and ``{file_context}`` placeholders
    in ``agent/prompts/rust_system_prompt.md``.

    Args:
        agent_config: Agent configuration (controls which info sections render).
        repo_path: Absolute path to the repo's working directory.
        target_files: The stub files this invocation should focus on. **Must be
            scoped to ONE file (or a small set) in per-file callers** — passing
            every stub in the crate produces megabyte-scale prompts that exceed
            model context windows on large crates (e.g. tokio).
        test_files: Optional list of test file paths (absolute or repo-relative).
            When provided AND ``agent_config.use_unit_tests_info`` is True, the
            test bodies are concatenated and appended (capped at
            ``agent_config.max_unit_tests_info_length`` chars). Pipeline
            historically computed ``test_files_readonly`` but never threaded it
            here, so this section was silently dead. Pass it explicitly now.

    Returns ``(formatted_message, summarizer_costs)``.

    """
    repo_name = os.path.basename(repo_path)

    function_lines: list[str] = []
    fl_chars = 0
    fl_capped = False
    for fpath in target_files:
        if fl_capped:
            break
        stubs = extract_rust_function_stubs(fpath)
        rel = os.path.relpath(fpath, repo_path)
        for stub in stubs:
            line = f"- `{rel}` line {stub['line']}: `{stub['signature']}`"
            if fl_chars + len(line) > _MAX_FUNCTION_LIST_CHARS:
                function_lines.append(
                    f"... (function_list cap of {_MAX_FUNCTION_LIST_CHARS} chars reached; remaining stubs elided) ..."
                )
                fl_capped = True
                break
            function_lines.append(line)
            fl_chars += len(line) + 1

    function_list = "\n".join(function_lines) if function_lines else "(no stubs found)"

    context_parts: list[str] = []
    running_chars = 0
    truncated_files = 0
    for fpath in target_files:
        if running_chars >= _MAX_FILE_CONTEXT_CHARS:
            truncated_files += 1
            continue
        rel = os.path.relpath(fpath, repo_path)
        try:
            # E14: 'replace' (not 'ignore') so undecodable bytes become a visible
            # U+FFFD in the prompt context rather than being silently dropped from
            # code the model must reproduce.
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as exc:
            logger.warning("Could not read %s for context: %s", fpath, exc)
            continue
        if len(content) > _MAX_PER_FILE_CONTEXT_CHARS:
            content = (
                content[:_MAX_PER_FILE_CONTEXT_CHARS]
                + f"\n// … truncated ({len(content) - _MAX_PER_FILE_CONTEXT_CHARS} chars elided) …\n"
            )
        block = f"### {rel}\n```rust\n{content}\n```"
        remaining = _MAX_FILE_CONTEXT_CHARS - running_chars
        if len(block) > remaining:
            block = block[:remaining] + "\n// … file_context cap reached …\n"
        context_parts.append(block)
        running_chars += len(block)

    if truncated_files:
        context_parts.append(
            f"\n// … {truncated_files} additional file(s) elided to stay under "
            f"file_context cap of {_MAX_FILE_CONTEXT_CHARS} chars …\n"
        )

    file_context = "\n\n".join(context_parts) if context_parts else "(no files)"

    try:
        template = _RUST_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("Rust system prompt not found at %s", _RUST_PROMPT_PATH)
        template = "Implement the Rust stub functions for {repo_name}."

    message = template.format(
        repo_name=repo_name,
        function_list=function_list,
        file_context=file_context,
    )

    if agent_config.use_user_prompt and agent_config.user_prompt:
        message = agent_config.user_prompt + "\n\n" + message

    if agent_config.use_unit_tests_info and test_files:
        unit_tests_section = "\n\n>>> Here is the Unit Tests Information:\n"
        for tf in test_files:
            tf_path = Path(tf) if os.path.isabs(tf) else Path(repo_path) / tf
            if tf_path.exists():
                try:
                    unit_tests_section += (
                        f"\n### {tf_path.name}\n```rust\n"
                        + tf_path.read_text(errors="replace")
                        + "\n```\n"
                    )
                except OSError as exc:
                    logger.warning("Could not read test file %s: %s", tf_path, exc)
        max_unit = max(0, int(getattr(agent_config, "max_unit_tests_info_length", 10000)))
        if len(unit_tests_section) > max_unit:
            unit_tests_section = (
                unit_tests_section[:max_unit] + "\n... (truncated)\n"
            )
        message += unit_tests_section

    spec_costs: list[SummarizerCost] = []
    if agent_config.use_spec_info:
        spec_text = _load_spec_text(repo_path)
        if spec_text and len(spec_text) > 200:
            if len(spec_text) > int(agent_config.max_spec_info_length * 1.5):
                try:
                    from agent.agent_utils import summarize_specification

                    spec_pdf_path = Path(repo_path) / "spec.pdf"
                    processed_spec, spec_costs = summarize_specification(
                        spec_text=spec_text,
                        model=agent_config.model_name,
                        max_tokens=agent_config.spec_summary_max_tokens,
                        max_char_length=agent_config.max_spec_info_length,
                        cache_path=spec_pdf_path.parent / ".spec_summary_cache.json",
                        model_short=getattr(agent_config, "model_short", ""),
                    )
                except Exception as exc:
                    logger.warning("Spec summarization failed: %s", exc)
                    processed_spec = spec_text[: agent_config.max_spec_info_length]
            else:
                processed_spec = spec_text
            message += (
                "\n\n>>> Here is the Specification Information:\n" + processed_spec
            )
        else:
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = Path(repo_path) / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[: agent_config.max_spec_info_length]
                        message += (
                            "\n\n>>> Here is the Specification Information:\n"
                            + readme_text
                        )
                        logger.info(
                            "Using %s as spec fallback for %s", readme_name, repo_path
                        )
                        break
                    except Exception as exc:
                        logger.warning("Failed to read %s: %s", readme_path, exc)

    return message, spec_costs


_BLIND_LINT_SHELL = (
    '_out=$(cargo clippy --all-targets --all-features -- -D warnings 2>&1); '
    '_rc=$?; '
    'if [ $_rc -eq 0 ]; then echo "build clean"; '
    'else _n=$(printf "%s" "$_out" | grep -cE "^error\\[E[0-9]+\\]" 2>/dev/null); '
    '[ -z "$_n" ] && _n=0; '
    'printf "build failed: %s compile errors\\n" "$_n"; fi; '
    'exit $_rc'
)

# Portable per-test-cmd timeout that reaps the WHOLE process tree, not just the
# direct child. `cargo test` spawns its compiled test binaries as GRANDCHILDREN;
# a hung/livelocking test binary spins at 100% CPU. Signalling only cargo (the
# direct child) leaves that binary orphaned and burning a core — across a big
# batch these leaked spinners starve later stages. So every path here kills the
# entire process GROUP:
#
#   * timeout/gtimeout: GNU timeout runs COMMAND in its own process group and,
#     in the default (non-`--foreground`) mode we use, signals that whole group.
#     We send `-s KILL` because a TERM-ignoring or livelocking test binary won't
#     honour SIGTERM, and `-k` does NOT help once cargo (the direct child) has
#     itself exited on TERM — GNU timeout then stops tracking the surviving
#     grandchild. SIGKILL is unblockable and hits every process still in the
#     group. A hung test at the hard ceiling needs no graceful shutdown.
#   * manual fallback (no timeout/gtimeout): `set -m` puts the backgrounded
#     command in its OWN process group (pgid == its pid); a watcher then
#     TERM-then-KILLs the whole group via a NEGATIVE pid (`kill -KILL -"$p"`).
#     A flag file records that a timeout actually fired, so we only ever signal
#     the group when it was still ours — never a pgid the OS may have recycled
#     after a clean exit. The perl-alarm branch was removed: `alarm` only
#     signals the single exec'd process, so it leaked grandchildren identically
#     to the old direct-child kill.
#
# Falls through to running WITHOUT a timeout only if `set -m`/`kill` are somehow
# unusable; the pipeline watchdog (inactivity ~900s) is the final safety net.
# KAIJU_TEST_TIMEOUT env var (seconds) overrides the 600s default.
# (No single quotes — these strings are wrapped in bash -c '...'.)
_TIMEOUT_PREAMBLE = (
    '_run_to() { '
    'local s="$1"; shift; '
    # GNU timeout / gtimeout: SIGKILL the whole process group on expiry.
    'if command -v timeout >/dev/null 2>&1; then timeout -s KILL "$s" "$@"; return $?; fi; '
    'if command -v gtimeout >/dev/null 2>&1; then gtimeout -s KILL "$s" "$@"; return $?; fi; '
    # Manual fallback: run in its own process group and reap the group.
    'local _flag; _flag="${TMPDIR:-/tmp}/.kaiju_run_to.$$.$RANDOM"; '
    'set -m 2>/dev/null; '
    '"$@" & local p=$!; '
    'set +m 2>/dev/null; '
    '( sleep "$s"; kill -0 "$p" 2>/dev/null || exit 0; : > "$_flag"; '
    'kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null; '
    'sleep 5; '
    'kill -KILL -"$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null; '
    ') >/dev/null 2>&1 & local k=$!; '
    'wait "$p" 2>/dev/null; local r=$?; '
    # If the watcher fired, the command timed out: hard-kill the whole group
    # (reaps any grandchild that ignored TERM) and report the standard 124 rc.
    'if [ -e "$_flag" ]; then kill -KILL -"$p" 2>/dev/null; r=124; fi; '
    'kill -KILL "$k" 2>/dev/null; wait "$k" 2>/dev/null; '
    'rm -f "$_flag" 2>/dev/null; '
    'return $r; '
    '}; '
    'TS="${KAIJU_TEST_TIMEOUT:-600}"; '
)


_BLIND_TEST_SHELL = (
    _TIMEOUT_PREAMBLE +
    '_out=$(_run_to "$TS" cargo test --all-features 2>&1); '
    '_rc=$?; '
    '_summary=$(printf "%s" "$_out" | grep -E "^test result:" | tail -1); '
    'if [ -n "$_summary" ]; then printf "%s\\n" "$_summary"; '
    'else _n=$(printf "%s" "$_out" | grep -cE "^error\\[E[0-9]+\\]" 2>/dev/null); '
    '[ -z "$_n" ] && _n=0; '
    'printf "compilation failed: %s errors\\n" "$_n"; fi; '
    'exit $_rc'
)


def _make_blind_lint_cmd() -> str:
    """Wrap clippy so output is just \"build clean\" or \"build failed: N errors\"."""
    return f"bash -c '{_BLIND_LINT_SHELL}' --"


def _make_blind_test_cmd() -> str:
    """Wrap cargo test so output is just the summary line, no per-test failures."""
    return f"bash -c '{_BLIND_TEST_SHELL}'"

_NAMES_ONLY_TEST_SHELL = (
    _TIMEOUT_PREAMBLE +
    '_out=$(_run_to "$TS" cargo test --all-features 2>&1); '
    '_rc=$?; '
    'if [ $_rc -eq 0 ]; then printf "tests pass\\n"; '
    'else '
    '_failed=$(printf "%s" "$_out" | sed -nE "s/^test (.+) \\.\\.\\. FAILED$/- \\1/p"); '
    '_n_failed=$(printf "%s" "$_failed" | grep -cE "^- " 2>/dev/null); _n_failed=${_n_failed:-0}; '
    '_n_passed=$(printf "%s" "$_out" | grep -oE "[0-9]+ passed" | head -1 | cut -d" " -f1); _n_passed=${_n_passed:-0}; '
    '_total=$((_n_failed + _n_passed)); '
    'if [ -n "$_failed" ]; then printf "%s/%s tests failed:\\n%s\\n" "$_n_failed" "$_total" "$_failed"; '
    'elif [ "$_n_passed" -gt 0 ]; then printf "tests pass (non-zero rc, likely coverage/lint gate): %s passed, rc=%s\\n" "$_n_passed" "$_rc"; '
    'else printf "tests failed (no per-test names parsed): rc=%s\\n" "$_rc"; fi; '
    'fi; exit $_rc'
)


def _make_names_only_test_cmd() -> str:
    """Wrap cargo test so agent sees only failed test names + counts, no tracebacks."""
    return f"bash -c '{_NAMES_ONLY_TEST_SHELL}'"


# Default Stage 3 test command. Same timeout protection as blind/names-only,
# but emits cargo test's raw output unchanged so aider sees the standard
# per-test lines + summary. Without this wrapper a single hung test (e.g. a
# fake-socket listener) blocks `coder.commands.cmd_test()` indefinitely;
# the agent then makes zero LLM calls until the outer watchdog kills it.
_DEFAULT_TEST_SHELL = (
    _TIMEOUT_PREAMBLE +
    '_out=$(_run_to "$TS" cargo test --all-features 2>&1); '
    '_rc=$?; '
    'printf "%s" "$_out"; '
    'exit $_rc'
)


def _make_default_test_cmd() -> str:
    """Wrap cargo test with a portable timeout, preserving full output for aider."""
    return f"bash -c '{_DEFAULT_TEST_SHELL}'"


def get_rust_lint_cmd(repo_path: str) -> str:
    """Return the cargo clippy lint command for the repo at *repo_path*.

    NOTE: aider's Linter.run_cmd() always appends the filename to the command
    (``cmd += " " + quote(fname)``).  ``cargo clippy`` does not accept source
    file arguments — it lints the whole crate.  We use ``bash -c '...' --`` so
    the appended filename is harmlessly consumed as a positional arg to bash
    (after ``--``) instead of being passed to cargo.
    """
    return "bash -c 'cargo clippy --all-targets --all-features -- -D warnings' --"


# ---------------------------------------------------------------------------
# Progress tracking helpers (imported pattern from run_agent_no_rich)
# ---------------------------------------------------------------------------


def _topological_module_order(files: list[str], repo_path: str) -> list[str]:
    """E12: order modules so a file is implemented AFTER the modules it imports.

    Lexicographic order implements modules before their dependencies exist, so the
    agent reconstructs callers blind to callees. We build a best-effort dependency
    DAG from `use crate::…`/`mod …` references and Kahn-topo-sort it (ties broken
    lexicographically for determinism). This is HEURISTIC (module-path → file
    resolution is approximate), so we enforce a hard invariant: the result must be
    a PERMUTATION of the input — on any cycle, exception, or set mismatch we fall
    back to the original order rather than risk dropping a module."""
    try:
        # Map each file to a normalized module key derived from its path.
        def _module_key(f: str) -> str:
            rel = os.path.relpath(f, repo_path)
            for prefix in ("src/", ""):
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                    break
            rel = rel[:-3] if rel.endswith(".rs") else rel  # strip .rs
            parts = [p for p in rel.split("/") if p not in ("", "mod", "lib", "main")]
            return "::".join(parts)

        key_to_file: dict[str, str] = {}
        for f in files:
            key_to_file.setdefault(_module_key(f), f)

        # Edge: file -> set of files it depends on (within our file set).
        deps_of: dict[str, set[str]] = {f: set() for f in files}
        for f in files:
            try:
                dep_paths = get_rust_file_dependencies(f)
            except Exception:  # noqa: BLE001
                dep_paths = []
            for dp in dep_paths:
                norm = dp.replace("super::", "")
                # Match a dependency to a file by exact or suffix module-key match.
                for key, target in key_to_file.items():
                    if target == f or not key:
                        continue
                    if key == norm or key.endswith("::" + norm) or norm.endswith("::" + key):
                        deps_of[f].add(target)

        # Kahn's algorithm: emit a node once all its deps are emitted.
        emitted: list[str] = []
        emitted_set: set[str] = set()
        remaining = list(files)
        while remaining:
            ready = [f for f in remaining if deps_of[f] <= emitted_set]
            if not ready:
                # Cycle — emit the rest in original order and stop (still a perm).
                emitted.extend(remaining)
                break
            ready.sort()  # deterministic tie-break
            for f in ready:
                emitted.append(f)
                emitted_set.add(f)
            remaining = [f for f in remaining if f not in emitted_set]

        if set(emitted) == set(files) and len(emitted) == len(files):
            return emitted
        logger.warning("E12: topo-sort produced a non-permutation; using original order.")
        return files
    except Exception as exc:  # noqa: BLE001
        logger.warning("E12: topo-sort failed (%s); using original order.", exc)
        return files


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Clear any stale .needs_retry from a prior failed attempt so a now-successful
    # module isn't ambiguously marked both done AND needs-retry.
    (log_dir / ".needs_retry").unlink(missing_ok=True)
    (log_dir / ".done").touch()


def _finalize_module(log_dir: Path, gate_result: "Optional[dict]" = None) -> None:
    """E4/E5: decide .done vs .needs_retry from the compile-gate outcome.

    Marking a module .done when its edits were REVERTED (gate found regressions)
    or UNVERIFIED (cargo check couldn't run) freezes it as an unimplemented stub:
    on resume `_is_module_done` skips it forever and it scores 0. For those
    outcomes we write `.needs_retry` (and ensure no stale `.done`) so a later run
    re-attempts the module instead of permanently abandoning it.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    status = (gate_result or {}).get("status") if gate_result else None
    # "kept" is the only outcome that proves a real, compiling edit landed.
    # reverted/unverified/clean_no_op all leave the module unproven → retry,
    # never freeze it as .done (which would skip it forever on resume).
    if status in ("reverted", "unverified", "clean_no_op"):
        (log_dir / ".done").unlink(missing_ok=True)
        (log_dir / ".needs_retry").write_text(str(status), encoding="utf-8")
        logger.info("Module %s left .needs_retry (gate status=%s)", log_dir.name, status)
        return
    _mark_module_done(log_dir)


def _get_stable_log_dir(log_dir: str, repo_name: str, branch: str) -> Path:
    stable_dir = Path(log_dir) / repo_name / branch / "current"
    stable_dir.mkdir(parents=True, exist_ok=True)
    return stable_dir


def _module_file_patch(local_repo, base_commit: str, post_sha: str,
                       rel_path: str) -> str:
    """Rust wrapper over the shared ``module_file_patch`` (strips ``target/``)."""
    return module_file_patch(local_repo, base_commit, post_sha, rel_path,
                             filter_fn=filter_rust_patch, logger=logger)


# ---------------------------------------------------------------------------
# Per-repo worker
# ---------------------------------------------------------------------------


def run_rust_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run aider for a single Rust repository."""
    _, repo_name = example["repo"].split("/")

    repo_path = os.path.abspath(os.path.join(repo_base_dir, repo_name))

    try:
        local_repo = Repo(repo_path)
    except Exception:
        logger.error("Failed to open repo at %s: not a git repo", repo_path, exc_info=True)
        raise Exception(
            f"{repo_path} is not a git repo. Check if base_dir is correctly specified."
        ) from None

    agent = RustAiderAgents(
        agent_config.max_iteration,
        agent_config.model_name,
        agent_config.cache_prompts,
    )

    thinking_capture = (
        ThinkingCapture() if getattr(agent_config, "capture_thinking", False) else None
    )

    if local_repo.is_dirty(untracked_files=True):
        # Discard stale leftovers so create_branch's checkout succeeds. The old
        # code committed them with `git add -A` onto whatever branch happened to
        # be checked out, polluting an unexpected ref. In an automated agent run
        # the tree should start clean from base, so reset+clean is correct.
        logger.warning("Discarding uncommitted changes in %s before branching", repo_path)
        local_repo.git.reset("--hard")
        # E18: preserve the expensive-to-regenerate spec artifacts. The decompressed
        # spec.pdf and the LLM spec-summary cache are UNTRACKED, so a bare
        # `clean -fd` deletes them every run — forcing a re-summarization (cost +
        # latency) on each stage. Exclude them so the cache survives.
        local_repo.git.clean(
            "-fd",
            "-e", ".spec_summary_cache.json",
            "-e", "spec.pdf",
        )

    create_branch(local_repo, branch, example["base_commit"])

    latest_commit = local_repo.commit(branch)
    if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
        logger.warning(
            "Resetting %s to base commit %s (override_previous_changes=True)",
            repo_name,
            example["base_commit"],
        )
        local_repo.git.reset("--hard", example["base_commit"])

    target_edit_files = get_target_edit_files_rust(repo_path)
    all_source_files = find_rust_files_to_edit(repo_path)
    if agent_config.strip_non_stubs:
        # Use base_commit's stub list (persistent across stages). The current
        # target_edit_files becomes empty after Stage 1 fills the stubs, which
        # would leave Stage 2/3 with no files to iterate. Reading the base_commit
        # state keeps the strip filter consistent across all 3 stages.
        import subprocess as _sp
        _base = example["base_commit"]
        _stubbed_at_base: set[str] = set()
        try:
            # E11: ONE `git grep -l` instead of O(files) `git show` calls (each a
            # fork+exec with no timeout — minutes on a large crate, and a single
            # hung git could wedge the whole stage). git grep scans the base tree
            # for the stub marker in a single pass.
            _grep = _sp.run(
                ["git", "grep", "-l", "--fixed-strings",
                 'panic!("STUB: not implemented")', _base, "--", "*.rs"],
                cwd=repo_path, capture_output=True, text=True, timeout=120,
            )
            # git grep exits 1 (no matches) or 0 (matches); >1 is a real error.
            if _grep.returncode > 1:
                raise _sp.CalledProcessError(_grep.returncode, "git grep", _grep.stderr)
            for _line in _grep.stdout.splitlines():
                # Output format: "<base>:<path>" — strip the leading "<rev>:".
                _rel = _line.split(":", 1)[1] if ":" in _line else _line
                if _rel.endswith(".rs"):
                    _stubbed_at_base.add(os.path.join(repo_path, _rel))
            all_source_files = [f for f in all_source_files if f in _stubbed_at_base]
            logger.info(
                "strip_non_stubs: kept %d/%d source files (filtered against base_commit %s)",
                len(all_source_files), len(_stubbed_at_base), _base[:8],
            )
        except (_sp.CalledProcessError, OSError, _sp.TimeoutExpired) as _e:
            logger.warning(
                "strip_non_stubs: failed to compute base-commit stub list (%s). Falling back to current target_edit_files (may break Stage 2/3).",
                _e,
            )
            all_source_files = list(target_edit_files)


    # E12: implement modules in dependency order (callees before callers) instead
    # of the lexicographic order find_rust_files_to_edit returns. Falls back to the
    # original order if the heuristic can't produce a clean permutation.
    all_source_files = _topological_module_order(all_source_files, repo_path)

    test_files_readonly = sorted(
        str(p) for p in Path(repo_path).rglob("*.rs")
        if "/tests/" in str(p) or p.parent.name == "tests"
    )
    # E9: injecting EVERY integration-test file as read-only context blows the
    # window on test-heavy crates (and most are irrelevant to a single module).
    # Cap the total injected bytes (configurable via KAIJU_TEST_CTX_BUDGET_BYTES,
    # default 256 KiB) and LOG what was dropped — never silently truncate, which
    # would read as "the agent saw all the tests" when it didn't.
    _test_ctx_budget = int(os.environ.get("KAIJU_TEST_CTX_BUDGET_BYTES", str(256 * 1024)) or 0)
    if _test_ctx_budget > 0 and test_files_readonly:
        _kept: list[str] = []
        _used = 0
        for _tf in test_files_readonly:
            try:
                _sz = os.path.getsize(_tf)
            except OSError:
                _sz = 0
            if _kept and _used + _sz > _test_ctx_budget:
                continue
            _kept.append(_tf)
            _used += _sz
        if len(_kept) < len(test_files_readonly):
            logger.warning(
                "E9: capped read-only test context to %d/%d files (~%d KiB of %d KiB budget); "
                "%d test file(s) DROPPED to protect the context window. Raise "
                "KAIJU_TEST_CTX_BUDGET_BYTES to include more.",
                len(_kept), len(test_files_readonly), _used // 1024,
                _test_ctx_budget // 1024, len(test_files_readonly) - len(_kept),
            )
        test_files_readonly = _kept

    experiment_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)
    eval_results = {}

    agent_config_log_file = experiment_log_dir / ".agent.yaml"
    try:
        with open(agent_config_log_file, "w") as agent_config_file:
            yaml.dump(agent_config, agent_config_file)
    except OSError as e:
        logger.error("Failed to write agent config to %s: %s", agent_config_log_file, e)
        raise

    message = ""


    from agent.openhands_formatter import write_module_output_json

    instance_id = ""
    metadata: dict = {}
    if thinking_capture is not None:
        from agent.output_writer import build_metadata

        commit0_config_for_meta = read_commit0_config_file(commit0_config_file)
        instance_id = (
            example["instance_id"]
            if "instance_id" in example.keys()
            else f"commit-0/{repo_name}"
        )
        metadata = build_metadata(
            model_name=agent_config.model_name,
            dataset_path=commit0_config_for_meta.get("dataset_name", ""),
            max_iterations=agent_config.max_iteration,
            model_short=getattr(agent_config, "model_short", "") or "",
            dataset_id=example.get("id"),
        )

    with DirContext(repo_path):

        if agent_config.run_tests:
            for src_file in all_source_files:
                src_file_name = os.path.relpath(src_file, repo_path).replace(".rs", "").replace("/", "__")
                test_log_dir = experiment_log_dir / src_file_name

                if _is_module_done(test_log_dir):
                    logger.info("Skipping already-completed test module: %s", src_file_name)
                    continue

                # E6: flush each turn live so a killed worker keeps a partial trajectory.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(test_log_dir) / "turns.jsonl")

                if agent_config.blind_tests:
                    test_cmd = _make_blind_test_cmd()
                elif agent_config.names_only_tests:
                    test_cmd = _make_names_only_test_cmd()
                else:
                    test_cmd = _make_default_test_cmd()
                lint_cmd = get_rust_lint_cmd(repo_path) if agent_config.use_lint_info else ""
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd()
                message, spec_costs = get_rust_message(
                    agent_config,
                    repo_path,
                    target_files=[src_file],
                    test_files=test_files_readonly,
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                def _invoke_agent_test():
                    return run_with_recovery(agent.run,
                        "",
                        test_cmd,
                        lint_cmd,
                        # Scope aider's editable fnames to THIS module (matches the
                        # per-file prompt and the draft/lint stages). Passing
                        # all_source_files loaded every file into the chat and blew
                        # the context window on large crates (e.g. tokio).
                        [src_file],
                        test_log_dir,
                        test_first=True,
                        thinking_capture=thinking_capture,
                        current_stage="test",
                        current_module=src_file_name,
                        max_test_output_length=agent_config.max_test_output_length,
                        spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                        repo_map_tokens=agent_config.repo_map_tokens,
                        inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        test_files_readonly=test_files_readonly,
                        _kaiju_log_dir=test_log_dir,
                    )
                with capture_module_calls(
                    model_short=getattr(agent_config, "model_short", "") or "",
                    thinking_capture=thinking_capture,
                    module=src_file_name,
                    log_dir=test_log_dir,
                ):
                    _gate_result = None
                    if getattr(agent_config, "per_edit_compile_gate", False):
                        def _reprompt_test(err_text):
                            return run_with_recovery(agent.run,
                                f"cargo check failed after your edits. Fix the regressions below WITHOUT changing public signatures.\n\n{err_text}",
                                test_cmd,
                                lint_cmd,
                                [src_file],
                                test_log_dir,
                                test_first=False,
                                thinking_capture=thinking_capture,
                                current_stage="test",
                                current_module=src_file_name,
                                max_test_output_length=agent_config.max_test_output_length,
                                spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                                repo_map_tokens=agent_config.repo_map_tokens,
                                inject_test_files_readonly=agent_config.inject_test_files_readonly,
                                test_files_readonly=test_files_readonly,
                                _kaiju_log_dir=test_log_dir,
                            )
                        _gate_result = run_with_compile_gate(
                            _invoke_agent_test,
                            repo_path=repo_path,
                            local_repo=local_repo,
                            pre_sha=pre_sha,
                            max_retries=getattr(agent_config, "compile_gate_max_retries", 2),
                            re_prompt_callback=_reprompt_test,
                        )
                        try:
                            (Path(test_log_dir) / ".compile_gate.json").write_text(
                                json.dumps(_gate_result, indent=2), encoding="utf-8",
                            )
                        except OSError as _io:
                            logger.warning("CompileGate: could not persist gate result: %s", _io)
                    else:
                        _ = _invoke_agent_test()
                module_elapsed = time.time() - module_start
                _finalize_module(Path(test_log_dir), _gate_result)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    # Scope to THIS module's own file only (see _module_file_patch).
                    module_patch = _module_file_patch(
                        local_repo, example["base_commit"], post_sha,
                        os.path.relpath(src_file, repo_path))
                    module_turns = thinking_capture.get_module_turns(src_file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(test_log_dir),
                            module_turns=module_turns,
                            module=src_file_name,
                            instance_id=f"{instance_id}__{src_file_name}"
                            if instance_id
                            else src_file_name,
                            git_patch=module_patch,
                            instruction=message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(src_file_name),
                            stage="test",
                            module_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

        elif agent_config.run_entire_dir_lint:
            lint_files = all_source_files
            for lint_file in lint_files:
                lint_file_name = os.path.relpath(lint_file, repo_path).replace(".rs", "").replace("/", "__")
                lint_log_dir = experiment_log_dir / lint_file_name

                if _is_module_done(lint_log_dir):
                    logger.info("Skipping already-linted file: %s", lint_file_name)
                    continue

                # E6: flush each turn live so a killed worker keeps a partial trajectory.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(lint_log_dir) / "turns.jsonl")

                message, spec_costs = get_rust_message(
                    agent_config,
                    repo_path,
                    target_files=[lint_file],
                    test_files=test_files_readonly,
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                lint_cmd = get_rust_lint_cmd(repo_path) if agent_config.use_lint_info else ""
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd()

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                def _invoke_agent_lint():
                    return run_with_recovery(agent.run,
                        "",
                        "",
                        lint_cmd,
                        [lint_file],
                        lint_log_dir,
                        lint_first=True,
                        thinking_capture=thinking_capture,
                        current_stage="lint",
                        current_module=lint_file_name,
                        repo_map_tokens=agent_config.repo_map_tokens,
                        inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        test_files_readonly=test_files_readonly,
                        _kaiju_log_dir=lint_log_dir,
                    )
                with capture_module_calls(
                    model_short=getattr(agent_config, "model_short", "") or "",
                    thinking_capture=thinking_capture,
                    module=lint_file_name,
                    log_dir=lint_log_dir,
                ):
                    _gate_result = None
                    if getattr(agent_config, "per_edit_compile_gate", False):
                        def _reprompt_lint(err_text):
                            return run_with_recovery(agent.run,
                                f"cargo check failed after your edits. Fix the regressions below WITHOUT changing public signatures.\n\n{err_text}",
                                "",
                                lint_cmd,
                                [lint_file],
                                lint_log_dir,
                                lint_first=False,
                                thinking_capture=thinking_capture,
                                current_stage="lint",
                                current_module=lint_file_name,
                                repo_map_tokens=agent_config.repo_map_tokens,
                                inject_test_files_readonly=agent_config.inject_test_files_readonly,
                                test_files_readonly=test_files_readonly,
                                _kaiju_log_dir=lint_log_dir,
                            )
                        _gate_result = run_with_compile_gate(
                            _invoke_agent_lint,
                            repo_path=repo_path,
                            local_repo=local_repo,
                            pre_sha=pre_sha,
                            max_retries=getattr(agent_config, "compile_gate_max_retries", 2),
                            re_prompt_callback=_reprompt_lint,
                        )
                        try:
                            (Path(lint_log_dir) / ".compile_gate.json").write_text(
                                json.dumps(_gate_result, indent=2), encoding="utf-8",
                            )
                        except OSError as _io:
                            logger.warning("CompileGate: could not persist gate result: %s", _io)
                    else:
                        _ = _invoke_agent_lint()
                module_elapsed = time.time() - module_start
                _finalize_module(Path(lint_log_dir), _gate_result)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    # Scope to THIS module's own file only (see _module_file_patch).
                    module_patch = _module_file_patch(
                        local_repo, example["base_commit"], post_sha,
                        os.path.relpath(lint_file, repo_path))
                    module_turns = thinking_capture.get_module_turns(lint_file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(lint_log_dir),
                            module_turns=module_turns,
                            module=lint_file_name,
                            instance_id=f"{instance_id}__{lint_file_name}"
                            if instance_id
                            else lint_file_name,
                            git_patch=module_patch,
                            instruction=message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(lint_file_name),
                            stage="lint",
                            module_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

        else:
            for f in target_edit_files:
                file_name = os.path.relpath(f, repo_path).replace(".rs", "").replace("/", "__")
                file_log_dir = experiment_log_dir / file_name

                if _is_module_done(file_log_dir):
                    logger.info("Skipping already-drafted file: %s", file_name)
                    continue

                # E6: flush each turn live so a killed worker keeps a partial trajectory.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(file_log_dir) / "turns.jsonl")

                iter_message, spec_costs = get_rust_message(
                    agent_config,
                    repo_path,
                    target_files=[f],
                    test_files=test_files_readonly,
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                lint_cmd = get_rust_lint_cmd(repo_path) if agent_config.use_lint_info else ""
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd()
                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                def _invoke_agent_draft():
                    return run_with_recovery(agent.run,
                        iter_message,
                        "",
                        lint_cmd,
                        [f],
                        file_log_dir,
                        thinking_capture=thinking_capture,
                        current_stage="draft",
                        current_module=file_name,
                        repo_map_tokens=agent_config.repo_map_tokens,
                        inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        test_files_readonly=test_files_readonly,
                        _kaiju_log_dir=file_log_dir,
                    )
                with capture_module_calls(
                    model_short=getattr(agent_config, "model_short", "") or "",
                    thinking_capture=thinking_capture,
                    module=file_name,
                    log_dir=file_log_dir,
                ):
                    _gate_result = None
                    if getattr(agent_config, "per_edit_compile_gate", False):
                        def _reprompt_draft(err_text):
                            return run_with_recovery(agent.run,
                                f"cargo check failed after your edits. Fix the regressions below WITHOUT changing public signatures.\n\n{err_text}",
                                "",
                                lint_cmd,
                                [f],
                                file_log_dir,
                                thinking_capture=thinking_capture,
                                current_stage="draft",
                                current_module=file_name,
                                repo_map_tokens=agent_config.repo_map_tokens,
                                inject_test_files_readonly=agent_config.inject_test_files_readonly,
                                test_files_readonly=test_files_readonly,
                                _kaiju_log_dir=file_log_dir,
                            )
                        _gate_result = run_with_compile_gate(
                            _invoke_agent_draft,
                            repo_path=repo_path,
                            local_repo=local_repo,
                            pre_sha=pre_sha,
                            max_retries=getattr(agent_config, "compile_gate_max_retries", 2),
                            re_prompt_callback=_reprompt_draft,
                        )
                        try:
                            (Path(file_log_dir) / ".compile_gate.json").write_text(
                                json.dumps(_gate_result, indent=2), encoding="utf-8",
                            )
                        except OSError as _io:
                            logger.warning("CompileGate: could not persist gate result: %s", _io)
                    else:
                        _ = _invoke_agent_draft()
                module_elapsed = time.time() - module_start
                _finalize_module(Path(file_log_dir), _gate_result)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    # Scope to THIS module's own file only (see _module_file_patch).
                    module_patch = _module_file_patch(
                        local_repo, example["base_commit"], post_sha,
                        os.path.relpath(f, repo_path))
                    module_turns = thinking_capture.get_module_turns(file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(file_log_dir),
                            module_turns=module_turns,
                            module=file_name,
                            instance_id=f"{instance_id}__{file_name}"
                            if instance_id
                            else file_name,
                            git_patch=module_patch,
                            instruction=iter_message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(file_name),
                            stage="draft",
                            module_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

    if agent_config.record_test_for_each_commit:
        try:
            with open(experiment_log_dir / "eval_results.json", "w") as f:
                json.dump(eval_results, f)
        except OSError as e:
            logger.error("Failed to write eval results: %s", e)
            raise

    if thinking_capture is not None:
        try:
            from agent.trajectory_writer import write_trajectory_md

            logger.info(
                "Per-module output written: %d turns across %d modules",
                len(thinking_capture.turns),
                len(set(t.module for t in thinking_capture.turns)),
            )

            if getattr(agent_config, "trajectory_md", True):
                write_trajectory_md(
                    output_path=experiment_log_dir / "trajectory.md",
                    repo_name=repo_name,
                    turns=thinking_capture.turns,
                )

            logger.info(
                f"Wrote thinking capture: {len(thinking_capture.turns)} turns, "
                f"{thinking_capture.get_metrics()['total_thinking_tokens']} thinking tokens"
            )
        except Exception as e:
            logger.warning(f"Failed to write thinking capture output: {e}")

    # Stage-wise cumulative patch (see agent.stage_patch). Strips target/ via
    # the same filter the eval uses.
    from agent.stage_patch import write_stage_patch
    write_stage_patch(local_repo, example["base_commit"], experiment_log_dir,
                      logger, filter_fn=filter_rust_patch)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_rust_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Main function to run aider for Rust repositories.

    Filters dataset by ``RUST_SPLIT`` instead of ``SPLIT``.
    Spawns a multiprocessing pool of ``run_rust_agent_for_repo`` workers.

    Note: fully-containerized runs do not go through a flag here — the whole
    pipeline (this agent + eval) runs inside the repo image via
    ``agent.container.run_pipeline_containerized``, which invokes this same code
    path with KAIJU_IN_CONTAINER=1.
    """
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    commit0_config = read_commit0_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )
    repo_split = commit0_config.get("repo_split", "all")
    dataset = list(dataset)
    allowed_repos = set(resolve_split(repo_split, dataset, curated=RUST_SPLIT))
    filtered_dataset = [
        example
        for example in dataset
        if isinstance(example, dict)
        and isinstance(example.get("repo"), str)
        and example["repo"].split("/")[-1] in allowed_repos
    ]

    assert len(filtered_dataset) > 0, (
        f"No Rust examples available for repo_split={repo_split!r}. "
        f"Ensure the dataset contains Rust repos from RUST_SPLIT."
    )

    with tqdm(
        total=len(filtered_dataset), smoothing=0, desc="Running aider for Rust repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            results = []
            for example in filtered_dataset:
                result = pool.apply_async(
                    run_rust_agent_for_repo,
                    args=(
                        commit0_config["base_dir"],
                        agent_config,
                        cast(RepoInstance, example),
                        branch,
                        override_previous_changes,
                        backend,
                        log_dir,
                        commit0_config_file,
                    ),
                    callback=lambda _: pbar.update(1),
                )
                results.append(result)

            _n_failed = 0
            # E8: per-worker wall-clock so one wedged repo (e.g. a hung cargo test
            # with no timeout binary) can't block the whole batch on result.get()
            # forever. Generous default; override via KAIJU_PER_REPO_BUDGET_SEC.
            _per_repo_budget = int(os.environ.get("KAIJU_PER_REPO_BUDGET_SEC", "0") or 0) or None
            for result in results:
                # Collect every worker. The old `result.get()` re-raised the
                # FIRST failing worker and abandoned the rest, losing their
                # outcomes; isolate failures so one bad repo can't sink the batch.
                try:
                    result.get(timeout=_per_repo_budget)
                except multiprocessing.TimeoutError:
                    _n_failed += 1
                    logger.error("Rust agent worker exceeded per-repo budget (%ss) — abandoning it",
                                 _per_repo_budget)
                except Exception as _werr:  # noqa: BLE001
                    _n_failed += 1
                    logger.error("Rust agent worker failed: %s", _werr, exc_info=True)
            logger.info(
                "All %d Rust agent workers completed (%d failed)",
                len(results), _n_failed,
            )
            if _n_failed:
                # E8: a PARTIAL failure must not sink the batch — the successful
                # repos already produced trajectories worth keeping, and the
                # `with` Pool block tears the pool down on exit anyway (no manual
                # terminate() needed). Only a TOTAL wipeout (every worker failed)
                # signals a systemic fault (bad config, missing bridge) worth
                # aborting on; a partial failure is logged loudly and tolerated.
                if _n_failed == len(results):
                    raise RuntimeError(
                        f"All {len(results)} Rust agent workers failed — "
                        f"systemic fault, aborting."
                    )
                logger.error(
                    "%d/%d Rust agent workers failed; keeping the %d successful "
                    "repos and continuing.",
                    _n_failed, len(results), len(results) - _n_failed,
                )
