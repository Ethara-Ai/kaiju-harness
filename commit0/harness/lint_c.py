"""Lint C repos using clang-tidy and cppcheck.

Runs linters inside the Docker container where the C toolchain is available.
Does NOT modify the original lint.py. Advisory: lint output is informational;
exit codes are surfaced but never block downstream stages.
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import docker
import docker.errors

from commit0.harness.constants_c import CRepoInstance, C_SPLIT
from commit0.harness.spec_c import make_c_spec
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)

del C_SPLIT  # imported for cli_c parity; consumed by callers via constants_c


def _run_in_container(
    client: docker.DockerClient,
    image_key: str,
    commands: list[str],
    workdir: str = "/testbed",
    timeout: int = 300,
) -> tuple[int, str]:
    full_cmd = " && ".join(commands)
    try:
        container = client.containers.run(
            image_key,
            command=["bash", "-c", full_cmd],
            working_dir=workdir,
            detach=True,
            remove=False,
        )
        result = container.wait(timeout=timeout)
        logs = container.logs(stdout=True, stderr=True).decode(
            "utf-8", errors="replace"
        )
        exit_code = result.get("StatusCode", 1)
        try:
            container.remove(force=True)
        except Exception:
            pass
        return exit_code, logs
    except docker.errors.ContainerError as e:
        return 1, str(e)
    except Exception as e:
        logger.error("Error running lint container: %s", e)
        return 1, str(e)


def _run_locally(commands: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    """Run the lint commands DIRECTLY (no container) in *cwd*.

    Used when there is no Docker daemon — the agent's lint-refine runs INSIDE the
    pipeline container, where a nested ``docker.from_env()`` fails. clang-tidy and
    cppcheck are installed in that image, so a nested container is both
    unnecessary and impossible. Same commands as the container path.
    """
    full_cmd = " && ".join(commands)
    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", full_cmd],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"lint timed out after {timeout}s\n{e.stdout or ''}{e.stderr or ''}"
    except Exception as e:  # noqa: BLE001 - lint is advisory, never block downstream
        return 1, f"lint failed to run locally: {e}"


def main(
    dataset_name: str,
    dataset_split: str,
    repo_or_repo_dir: str,
    base_dir: str,
    timeout: int = 300,
) -> None:
    """Lint a C repo using clang-tidy and cppcheck. Advisory."""
    dataset: Iterator[CRepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore

    example = None
    repo_name = None
    for ex in dataset:
        name = ex["repo"].split("/")[-1]
        if repo_or_repo_dir.rstrip("/").endswith(name) or name == repo_or_repo_dir:
            example = ex
            repo_name = name
            break

    if example is None or repo_name is None:
        logger.error("No matching C repo found for %r", repo_or_repo_dir)
        sys.exit(1)

    spec = make_c_spec(example, absolute=True)

    # Local repo dir — present when running IN-CONTAINER (the agent's lint-refine)
    # or after a host build. repo_or_repo_dir may already be an absolute path.
    repo_dir = Path(base_dir) / repo_name
    if not repo_dir.is_dir():
        _cand = Path(repo_or_repo_dir)
        if _cand.is_dir():
            repo_dir = _cand

    # Use compile_commands.json if present so clang-tidy resolves -I and -D flags.
    lint_commands = [
        (
            "if [ ! -f compile_commands.json ] && [ ! -f build/compile_commands.json ]; then "
            "cmake -S . -B build -G Ninja "
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON 2>/dev/null || true; "
            "fi"
        ),
        (
            "echo '=== clang-tidy ===' && "
            "find . -name '*.c' -not -path './build/*' -not -path './tests/*' "
            "-not -path './test/*' -not -path './third_party/*' -not -path './vendor/*' "
            "-not -path './deps/*' -not -path './external/*' "
            "| head -100 | xargs -r clang-tidy --quiet "
            # The repo's compile flags (from compile_commands.json, -p build) often
            # include GCC-only warning flags like -Wformat-overflow that clang does
            # not know. Without this, clang-tidy floods the report with
            # `error: unknown warning option '-Wformat-overflow'
            # [clang-diagnostic-unknown-warning-option]` noise that isn't a real
            # code issue and drowns out actionable findings. Tolerate GCC-only
            # flags instead of erroring on them.
            "--extra-arg=-Wno-unknown-warning-option "
            "--extra-arg=-Wno-unknown-argument "
            "--extra-arg=-Wno-error "
            "-p build 2>&1 || true"
        ),
        (
            "echo '=== cppcheck ===' && "
            "cppcheck --enable=warning,performance,portability "
            # Exclude vendored/test/build trees: the model owns the LIBRARY source,
            # not the bundled test framework (e.g. tests/unity) or third-party deps.
            # Scanning them produced errors like `Memory leak` / `unknown macro` in
            # tests/unity/* that are not the model's code — pure noise that made the
            # lint stage feedbackless. Suppress config-dependent macro/include noise
            # too (cppcheck has no preprocessor context here).
            "-itests -itest -ithird_party -ivendor -ibuild -ideps -iexternal -iextern "
            "--suppress=unknownMacro --suppress=missingInclude "
            "--suppress=missingIncludeSystem --suppress=unmatchedSuppression "
            "--suppress=toomanyconfigs --suppress=checkersReport "
            "--error-exitcode=0 --quiet . 2>&1 || true"
        ),
    ]

    # Docker on the HOST (linters live in the image, not on the host); LOCAL
    # in-container (no Docker daemon, linters installed right here). Trying Docker
    # first preserves host behaviour; DockerException -> lint locally instead of
    # dying with "Cannot connect to Docker" (which left the lint stage feedbackless).
    output, exit_code, use_local, client = "", 0, False, None
    try:
        client = docker.from_env()
        client.images.get(spec.repo_image_key)
    except docker.errors.ImageNotFound:
        if not repo_dir.is_dir():
            logger.error("Docker image %s not found. Run C build first.", spec.repo_image_key)
            sys.exit(1)
        use_local = True
    except docker.errors.DockerException:
        if not repo_dir.is_dir():
            logger.error("Cannot connect to Docker and no local repo dir for %s.", repo_name)
            sys.exit(1)
        use_local = True

    if use_local:
        logger.info("Running C linters LOCALLY on %s (%s)", repo_name, repo_dir)
        exit_code, output = _run_locally(lint_commands, cwd=repo_dir, timeout=timeout)
    else:
        logger.info("Running C linters on %s (image: %s)", repo_name, spec.repo_image_key)
        exit_code, output = _run_in_container(
            client, spec.repo_image_key, lint_commands, workdir="/testbed", timeout=timeout,
        )

    print(output)

    if exit_code != 0:
        logger.warning("C lint completed with issues (exit code %d) — advisory", exit_code)
    else:
        logger.info("C lint completed successfully")

    sys.exit(exit_code)


__all__: list[str] = []
