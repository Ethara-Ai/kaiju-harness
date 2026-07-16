"""Docker test runner for JavaScript repos.

Clones a per-instance patch into the Docker container, runs the detected
test framework, and surfaces install / syntax failure as a first-class
signal alongside test pass/fail. JavaScript has no static compile step,
so install and ``node --check`` exit codes are the closest proxy and must
be captured separately from the test runner's exit code.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
import traceback
from pathlib import Path
from typing import Iterator, cast

import git
import git.exc

from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.constants_js import MAX_PATCH_BYTES, RUN_JS_TEST_LOG_DIR
from commit0.harness.execution_context import Docker, ExecutionBackend, LocalInplace
from commit0.harness.spec_js import make_js_spec
from commit0.harness.utils import (
    EvaluationError,
    close_logger,
    generate_patch_between_commits,
    get_hash_string,
    load_dataset_from_config,
    setup_logger,
)


_module_logger = logging.getLogger(__name__)


_FRAMEWORK_INJECTION_MARKERS: dict[str, tuple[str, ...]] = {
    "jest": (" jest",),
    "vitest": (" vitest",),
    "mocha": (" mocha",),
}

_NODE_TEST_FRAMEWORK = "node_test"


def _strip_log_unsafe_chars(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ch not in {"\x1b", "\u202e", "\u202d", "\u202b", "\u202a", "\u202c"}
    )


def _inject_test_ids(eval_script: str, test_ids: str, framework: str) -> str:
    if not test_ids:
        return eval_script
    if framework in (_NODE_TEST_FRAMEWORK, "ava"):
        # node:test and AVA select tests by pattern (`--test-name-pattern` / AVA
        # `--match`), NOT positional args (positionals are file paths/globs). Per-
        # test-ID injection is therefore unsupported — run the WHOLE suite and let
        # the TAP parser + canonical inventory supply the denominator. Running the
        # full suite is correct (just less selective); raising here would crash the
        # entire eval for any such repo that happens to have a frozen inventory.
        _module_logger.warning(
            "Per-test-ID selection unsupported for %s; running the full suite.",
            framework,
        )
        return eval_script

    sanitized = test_ids.replace("\n", " ").replace("\r", " ").replace("\x00", "")
    sanitized = _strip_log_unsafe_chars(sanitized)
    quoted = " ".join(shlex.quote(token) for token in sanitized.split() if token)
    if not quoted:
        return eval_script

    markers = _FRAMEWORK_INJECTION_MARKERS.get(framework)
    if markers is None:
        raise ValueError(
            f"Unsupported framework for test-ID injection: {framework!r}"
        )

    lines = eval_script.split("\n")
    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(marker in line for marker in markers):
            block_end = i
            while (
                block_end + 1 < len(lines)
                and _has_trailing_continuation(lines[block_end])
            ):
                block_end += 1
            for k in range(i, block_end):
                new_lines.append(lines[k])
            new_lines.append(_inject_into_test_line(lines[block_end], quoted))
            i = block_end + 1
        else:
            new_lines.append(line)
            i += 1
    return "\n".join(new_lines)


def _has_trailing_continuation(line: str) -> bool:
    stripped = line.rstrip(" \t")
    if not stripped.endswith("\\"):
        return False
    trailing_backslashes = 0
    for ch in reversed(stripped):
        if ch == "\\":
            trailing_backslashes += 1
        else:
            break
    return trailing_backslashes % 2 == 1


def _inject_into_test_line(line: str, quoted_ids: str) -> str:
    if "||" in line:
        head, _, tail = line.partition("||")
        return f"{head.rstrip()} {quoted_ids} ||{tail}"
    if _has_trailing_continuation(line):
        head = line.rstrip(" \t")[:-1].rstrip(" \t")
        trailing = line[len(line.rstrip(" \t")):]
        return f"{head} {quoted_ids} \\{trailing}"
    return line.rstrip() + " " + quoted_ids


_FETCH_TIMEOUT_SECONDS = 60


def _resolve_branch_commit(local_repo: git.Repo, branch: str) -> str:
    if branch in local_repo.branches:
        return local_repo.commit(branch).hexsha
    for remote in local_repo.remotes:
        try:
            local_repo.git.fetch(
                remote.name,
                branch,
                depth=1,
                kill_after_timeout=_FETCH_TIMEOUT_SECONDS,
            )
        except git.exc.GitCommandError:
            continue
        try:
            return local_repo.commit("FETCH_HEAD").hexsha
        except (git.exc.BadName, ValueError):
            continue
    raise Exception(f"Branch {branch} does not exist locally or remotely.")


def main(
    dataset_name: str,
    dataset_split: str,
    base_dir: str,
    repo_or_repo_dir: str,
    branch: str,
    test_ids: str,
    backend: str,
    timeout: int,
    num_cpus: int,
    rebuild_image: bool,
    verbose: int,
) -> None:
    """Run the detected JS test framework for one repo inside a Docker container.

    Captures the install, syntax-check, and test exit codes separately so
    callers (and :mod:`commit0.harness.evaluate_js`) can distinguish a
    compile/install failure from a real test failure.
    """
    dataset_list: list[RepoInstance | SimpleInstance] = list(
        load_dataset_from_config(dataset_name, split=dataset_split)
    )
    absolute = True

    spec = None
    example: RepoInstance | SimpleInstance | None = None
    repo_name: str | None = None

    repo_or_repo_dir = repo_or_repo_dir.rstrip("/")
    base = os.path.basename(repo_or_repo_dir)
    for entry in dataset_list:
        candidate_name = entry["repo"].split("/")[-1]
        if (
            candidate_name == base
            or candidate_name == repo_or_repo_dir
            or repo_or_repo_dir.endswith("/" + candidate_name)
        ):
            example = entry
            repo_name = candidate_name
            spec = make_js_spec(cast(RepoInstance, entry), absolute=absolute)
            break

    # A resolved repo path can follow a symlink — local_inplace links the repo dir
    # to the repo-image checkout at /testbed, so basename('/testbed') is 'testbed',
    # NOT the repo name, and the match above fails. Every trajectory test_cmd runs
    # exactly ONE repo, so for a single-entry dataset fall back to that entry rather
    # than raising (which silently zero-worked stage 3). git.Repo(repo_or_repo_dir)
    # below still loads the real checkout, so the symlinked path is fine.
    if spec is None and len(dataset_list) == 1:
        example = dataset_list[0]
        repo_name = example["repo"].split("/")[-1]
        spec = make_js_spec(cast(RepoInstance, example), absolute=absolute)

    if spec is None or example is None or repo_name is None:
        raise ValueError(
            f"No matching JS spec for repo_or_repo_dir={repo_or_repo_dir!r} "
            f"in dataset={dataset_name!r} ({len(dataset_list)} entries)"
        )

    hashed_test_ids = get_hash_string(test_ids)
    log_dir = (RUN_JS_TEST_LOG_DIR / repo_name / branch / hashed_test_ids).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run_js_tests.log"
    logger = setup_logger(repo_name, log_file, verbose=verbose)

    local_repo: git.Repo | None = None
    try:
        try:
            local_repo = git.Repo(repo_or_repo_dir)
            logger.info(f"Loaded a git repo from {repo_or_repo_dir}")
        except (git.exc.NoSuchPathError, git.exc.InvalidGitRepositoryError):
            fallback_dir = os.path.join(base_dir, repo_name)
            logger.error(
                f"{repo_or_repo_dir} is not a git dir, trying {fallback_dir} again"
            )
            try:
                local_repo = git.Repo(fallback_dir)
                logger.info(f"Retried succeeded. Loaded a git repo from {fallback_dir}")
            except git.exc.NoSuchPathError as e:
                raise Exception(
                    f"{fallback_dir} and {repo_or_repo_dir} are not git directories.\n"
                    "Usage: commit0 js test {repo_dir} {branch} {test_ids}"
                ) from e

        if branch == "reference":
            commit_id = example["reference_commit"]
        else:
            commit_id = _resolve_branch_commit(local_repo, branch)

        patch = generate_patch_between_commits(
            local_repo, example["base_commit"], commit_id
        )
        if len(patch.encode("utf-8", errors="ignore")) > MAX_PATCH_BYTES:
            raise EvaluationError(
                repo_name,
                (
                    f"Generated patch exceeds {MAX_PATCH_BYTES} bytes "
                    f"(got {len(patch)} chars between {example['base_commit']} "
                    f"and {commit_id}). Refusing to write to container."
                ),
                logger,
                log_file=str(log_file),
            )

        framework = spec._detect_test_framework()
        eval_script = _inject_test_ids(spec.eval_script, test_ids, framework)

        patch_file = Path(log_dir / "patch.diff")
        patch_file.write_text(patch, encoding="utf-8", errors="ignore")
        eval_file = Path(log_dir / "eval.sh")
        eval_file.write_text(eval_script)

        backend = backend.upper()
        if ExecutionBackend(backend) == ExecutionBackend.LOCAL:
            _ctx = Docker
            logger.info("Running locally via Docker")
        elif ExecutionBackend(backend) == ExecutionBackend.LOCAL_INPLACE:
            _ctx = LocalInplace
            logger.info("Running locally in-place (git worktree, no new container)")
        else:
            raise ValueError(
                f"JS pipeline supports LOCAL (Docker) or local_inplace, got {backend}. "
                f"Valid backends: {', '.join(EVAL_BACKENDS)}"
            )

        files_to_copy = Files(
            eval_script={"src": eval_file, "dest": Path("/eval.sh")},
            patch={"src": patch_file, "dest": Path("/patch.diff")},
        )
        # Repo-relative (spec_js now writes these in the repo cwd) so both the
        # Docker copy_from_container and the LocalInplace collector land them in
        # log_dir, where the reads + evaluate_js expect them.
        files_to_collect = [
            "test_results.json",
            "test_exit_code.txt",
            "install_exit_code.txt",
            "syntax_exit_code.txt",
            "test_output.txt",
        ]

        eval_command = "/bin/bash /eval.sh > test_output.txt 2>&1"
        with _ctx(
            spec,
            logger,
            timeout,
            num_cpus,
            log_dir,
            files_to_copy,
            files_to_collect,
            rebuild_image,
        ) as context:
            output, timed_out, total_runtime = context.exec_run_with_timeout(
                eval_command
            )
            logger.info(output)
            if timed_out:
                # F-A audit fix: flush test output to stdout BEFORE raising so
                # the caller (aider captures the eval subprocess stdout) sees
                # WHAT the tests produced before the timeout kill. Without this
                # the agent only saw the "Test timed out after Ns" line and had
                # zero signal to refine against.
                try:
                    _to = Path(log_dir / "test_output.txt")
                    if _to.exists():
                        print(_to.read_text(encoding="utf-8", errors="replace"))
                    else:
                        print(output or "(no test output captured before timeout)")
                except OSError:
                    pass
                print(
                    f"\n[TIMEOUT: test process killed after {timeout}s "
                    f"(bump via KAIJU_AGENT_TEST_TIMEOUT_SEC or --timeout)]",
                    file=sys.stderr,
                )
                raise EvaluationError(
                    repo_name,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                    log_file=str(log_file),
                )

        # F-A2 audit fix: always flush test output to stdout on the
        # SUCCESS path — the agent-side commit0 CLI (which spawns this
        # module) needs the test output regardless of --verbose. Prior
        # `if verbose > 0:` gate starved aider when local_inplace or a
        # subprocess call passed verbose=0.
        if True:
            test_output = Path(log_dir / "test_output.txt")
            if test_output.exists():
                print(test_output.read_text())
            # ava/node_test redirect --tap straight to test_results.json (spec_js.py
            # test_cmd dict) so test_output.txt is near-empty; without this flush the
            # agent-side cmd_test sees only the earlier "Per-test-ID selection
            # unsupported" warning and hallucinates edits against read-only test
            # files (425-block rejection storms observed on ava repos in stage 3).
            if framework in ("ava", _NODE_TEST_FRAMEWORK):
                test_results = Path(log_dir / "test_results.json")
                if test_results.exists():
                    tap = test_results.read_text(encoding="utf-8", errors="replace")
                    if tap.strip() and tap.strip() != "EMPTY_RESULTS":
                        print(f"\n--- {framework} test results (TAP) ---")
                        print(tap)

        exit_code = _read_exit_code(log_dir / "test_exit_code.txt")
        install_code = _read_exit_code(log_dir / "install_exit_code.txt")
        syntax_code = _read_exit_code(log_dir / "syntax_exit_code.txt")

        if install_code not in (0, None):
            _module_logger.warning(
                "%s: install step exited %s — tests likely meaningless",
                repo_name,
                install_code,
            )
        if syntax_code not in (0, None):
            _module_logger.warning(
                "%s: node --check exited %s — syntax errors present",
                repo_name,
                syntax_code,
            )

        compile_failed = (install_code not in (0, None)) or (
            syntax_code not in (0, None)
        )
        if compile_failed:
            sys.exit(2)
        if exit_code is None:
            _module_logger.warning(
                "%s: test_exit_code.txt missing; install/syntax succeeded but "
                "the test phase produced no exit-code artefact. Reporting as "
                "infrastructure failure (exit 3) so evaluate_js does not "
                "conflate this with a real test failure.",
                repo_name,
            )
            sys.exit(3)
        sys.exit(exit_code)
    except SystemExit:
        raise
    except EvaluationError as e:
        error_msg = (
            f"Error in running JS tests for {repo_name}: {e}\n"
            f"{traceback.format_exc()}\n"
            f"Check ({log_file}) for more information."
        )
        raise EvaluationError(
            repo_name, error_msg, logger, log_file=str(log_file)
        ) from e
    except Exception as e:
        error_msg = (
            f"General error: {e}\n"
            f"{traceback.format_exc()}\n"
            f"Check ({log_file}) for more information."
        )
        raise RuntimeError(error_msg) from e
    finally:
        if local_repo is not None:
            local_repo.close()
        close_logger(logger)


def _read_exit_code(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError) as exc:
        _module_logger.debug("Could not read exit code from %s: %s", path, exc)
        return None
