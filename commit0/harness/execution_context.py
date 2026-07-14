"""Remote code execution contexts

Implements the interface for local docker containers, remote modal sandboxes,
and HTTP servers.
"""

from abc import ABC, abstractmethod
import docker
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from enum import auto
from strenum import StrEnum
from pathlib import Path
from typing import Optional, Type
from types import TracebackType

from commit0.harness.constants import Files
from commit0.harness.spec import Spec
from commit0.harness.docker_build import (
    close_logger,
    get_proxy_env,
)
from commit0.harness.docker_utils import (
    cleanup_container,
    create_container,
    copy_from_container,
    copy_to_container,
    exec_run_with_timeout,
    sandbox_hardening_kwargs,
)

# Lazy-loaded optional dependency sentinels (set on first use).
# Module-level attributes allow test code to patch via @patch("...modal").
modal = None  # type: ignore[assignment]
Sandbox = None  # type: ignore[assignment]


class ExecutionBackend(StrEnum):
    LOCAL = auto()
    LOCAL_INPLACE = auto()
    MODAL = auto()
    E2B = auto()


class ExecutionContext(ABC):
    def __init__(
        self,
        spec: Spec,
        logger: logging.Logger,
        timeout: int,
        num_cpus: int,
        log_dir: Path,
        files_to_copy: Optional[Files] = None,
        files_to_collect: Optional[list[str]] = None,
        rebuild_image: bool = False,
    ):
        """Create the remote execution context

        The execution context can be a Docker container or Modal sandbox.
        The execution context may not persist for the lifetime of this object.
        """
        self.spec = spec
        self.logger = logger
        self.timeout = timeout
        self.num_cpus = num_cpus
        self.log_dir = log_dir
        self.files_to_collect = files_to_collect

    @abstractmethod
    def exec_run_with_timeout(self, command: str) -> tuple[str, bool, float]:
        """Execute a test command"""
        raise NotImplementedError

    def __enter__(self):
        return self

    @abstractmethod
    def __exit__(
        self,
        exctype: Optional[Type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        raise NotImplementedError


class Docker(ExecutionContext):
    def __init__(
        self,
        spec: Spec,
        logger: logging.Logger,
        timeout: int,
        num_cpus: int,
        log_dir: Path,
        files_to_copy: Optional[Files] = None,
        files_to_collect: Optional[list[str]] = None,
        rebuild_image: bool = False,
    ):
        super().__init__(
            spec,
            logger,
            timeout,
            num_cpus,
            log_dir,
            files_to_copy=files_to_copy,
            files_to_collect=files_to_collect,
        )

        logger.debug("Connecting to Docker daemon")
        self.client = docker.from_env()
        proxy_env = get_proxy_env() or None
        # Forward the eval test-timeout knobs into the container so a raised
        # --timeout (which run_go_tests derives into these) — or an explicit
        # operator override — actually reaches eval.sh's `${EVAL_TEST_TIMEOUT:-…}`
        # / `${GO_TEST_TIMEOUT:-…}` bounds instead of always using the in-script
        # default. Only added when set, so the default create_container call is
        # unchanged otherwise. (Issue 7)
        _timeout_env = {
            k: os.environ[k]
            for k in ("EVAL_TEST_TIMEOUT", "GO_TEST_TIMEOUT")
            if os.environ.get(k)
        }
        if _timeout_env:
            proxy_env = {**(proxy_env or {}), **_timeout_env}
        # Only forward hardening kwargs when opt-in is enabled, so the default
        # create_container call is byte-for-byte unchanged.
        hardening = sandbox_hardening_kwargs()
        extra_kwargs = {"sandbox_hardening": hardening} if hardening else {}
        self.container = create_container(
            client=self.client,
            image_name=spec.repo_image_key,
            container_name=spec.get_container_name(run_id=uuid.uuid4().hex[:8]),
            nano_cpus=num_cpus,
            logger=logger,
            environment=proxy_env,
            **extra_kwargs,
        )
        self.container.start()
        if files_to_copy:
            for key, f in files_to_copy.items():
                logger.debug(
                    "Copying %s to container: %s -> %s", key, f["src"], f["dest"]
                )
                copy_to_container(self.container, f["src"], f["dest"])  # type: ignore

    def exec_run_with_timeout(self, command: str) -> tuple[str, bool, float]:
        """Exec"""
        output = exec_run_with_timeout(self.container, command, self.timeout)

        if self.files_to_collect:
            for fname in self.files_to_collect:
                file = Path(self.spec.repo_directory) / fname
                # Run the test command inside the container to check if the file exists
                exit_code, test_output = self.container.exec_run(
                    f"test -e {file}", demux=True
                )
                # Check the exit code of the command
                if exit_code == 0:
                    copy_from_container(self.container, file, self.log_dir / fname)
        return output

    def __exit__(
        self,
        exctype: Optional[Type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        try:
            cleanup_container(self.client, self.container, self.logger)
        except Exception as e:
            self.logger.error(f"Container cleanup failed: {e}")
            if excinst is None:
                raise
        close_logger(self.logger)


def _instance_get(instance: object, key: str) -> object:
    """Read `key` from a RepoInstance whether it's a dict or a dataclass/model."""
    if isinstance(instance, dict):
        return instance.get(key)
    return getattr(instance, key, None)


class LocalInplace(ExecutionContext):
    """Run the eval script in an isolated git worktree in the CURRENT process.

    This is the backend for fully-containerized inference: the agent already
    runs *inside* the repo image's container, so spinning up another Docker
    container to score a test iteration would require docker-in-docker (a socket
    mount = a host-escape hole). Instead we reconstruct the eval in a throwaway
    `git worktree` rooted at `base_commit`, apply the model's patch there, and
    run the tests — never touching the agent's live checkout at
    `spec.repo_directory`, and never touching the host Docker daemon.

    Reward-hacking is preserved unchanged: the worktree runs the SAME
    `eval.sh` (`git reset --hard base` → `git apply patch` → revert
    test/manifest paths → CHEAT-GUARD verify) that the Docker backend runs. The
    only rewrite is the working directory (`cd <worktree>` instead of
    `cd <repo_directory>`) and the patch-file path (a scratch file instead of
    the container-absolute `/patch.diff`) so the backend is safe to run on a
    developer host too, not only as root inside a container.
    """

    def __init__(
        self,
        spec: Spec,
        logger: logging.Logger,
        timeout: int,
        num_cpus: int,
        log_dir: Path,
        files_to_copy: Optional[Files] = None,
        files_to_collect: Optional[list[str]] = None,
        rebuild_image: bool = False,
    ):
        super().__init__(
            spec,
            logger,
            timeout,
            num_cpus,
            log_dir,
            files_to_copy=files_to_copy,
            files_to_collect=files_to_collect,
        )
        eval_entry = getattr(files_to_copy, "eval_script", None)
        if not files_to_copy or not eval_entry:
            raise ValueError("LocalInplace requires files_to_copy with an eval_script")

        self.repo_dir = str(spec.repo_directory)
        base_commit = _instance_get(spec.instance, "base_commit")
        if not isinstance(base_commit, str) or not base_commit.strip():
            raise ValueError("LocalInplace requires a string base_commit on the spec")
        self.base_commit = base_commit.strip()

        # Scratch root holds the worktree + rewritten eval.sh + scratch patch.
        self.work_root = tempfile.mkdtemp(prefix="commit0-inplace-")
        self.worktree = os.path.join(self.work_root, "tree")
        # A failure anywhere below leaves the object partly built and __exit__
        # never runs (the context manager was never entered) — leaking the git
        # worktree registration AND the scratch tmpdir per failed task. On a large
        # batch that accretes leaked worktrees in the shared repo + fills /tmp.
        # Tear it all down here so a construction failure self-cleans.
        try:
            self._build(logger, files_to_copy, eval_entry)
        except BaseException:
            self._cleanup()
            raise

    def _build(self, logger, files_to_copy, eval_entry) -> None:
        # Create the isolated worktree at base_commit (detached HEAD). This does
        # NOT disturb the agent's live checkout or its branch — a worktree shares
        # the object DB but has its own index/HEAD/working files.
        logger.debug(
            "LocalInplace: adding worktree %s @ %s (repo=%s)",
            self.worktree,
            self.base_commit,
            self.repo_dir,
        )
        subprocess.run(
            ["git", "-C", self.repo_dir, "worktree", "add", "--detach",
             self.worktree, self.base_commit],
            check=True,
            capture_output=True,
            text=True,
        )

        for _dep_dir in ("node_modules", "target", "vendor"):
            _src = os.path.join(self.repo_dir, _dep_dir)
            _dst = os.path.join(self.worktree, _dep_dir)
            if os.path.isdir(_src) and not os.path.exists(_dst):
                try:
                    os.symlink(_src, _dst)
                    logger.debug("LocalInplace: symlinked %s -> %s", _dep_dir, _src)
                except OSError as _e:
                    logger.warning("LocalInplace: could not symlink %s: %s", _dep_dir, _e)

        # Stage the patch at a scratch path (not the container-absolute dest, so
        # this is safe to run unprivileged on a host).
        patch_scratch = os.path.join(self.work_root, "patch.diff")
        patch_entry = getattr(files_to_copy, "patch", None)
        if patch_entry:
            shutil.copyfile(patch_entry["src"], patch_scratch)
            patch_dest_orig = str(patch_entry["dest"])
        else:
            Path(patch_scratch).write_text("")
            patch_dest_orig = "/patch.diff"

        # Rewrite eval.sh for the worktree. We must repoint EVERY absolute
        # reference to the container repo dir (repo_dir, e.g. /testbed) at the
        # throwaway worktree — NOT just the `cd`. A hardcoded output path inside a
        # test command (e.g. `ctest --output-junit /testbed/test_report.xml`)
        # otherwise writes the report OUTSIDE the worktree; the collector then
        # finds no report and the eval falsely reports OUTPUT_MISSING even though
        # the tests ran and passed (observed on C/cJSON: 18/19 passing scored as
        # 0/19). repo_dir is a distinctive absolute path, so a global replace is
        # safe and subsumes the old single `cd` rewrite. Everything else
        # (reset/apply/revert/cheat-guard) is untouched.
        eval_src = Path(eval_entry["src"]).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        eval_src = eval_src.replace(self.repo_dir, self.worktree)
        eval_src = eval_src.replace(patch_dest_orig, patch_scratch)
        self.eval_script_path = os.path.join(self.work_root, "eval.sh")
        Path(self.eval_script_path).write_text(eval_src, encoding="utf-8")

    def _cleanup(self) -> None:
        """Remove the git worktree registration and delete the scratch root.

        Idempotent and exception-safe: used by both a failed __init__ and the
        normal __exit__, so neither path can leak the worktree or tmpdir.
        """
        worktree = getattr(self, "worktree", None)
        if worktree:
            try:
                subprocess.run(
                    ["git", "-C", self.repo_dir, "worktree", "remove", "--force",
                     worktree],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                self.logger.debug("worktree remove failed: %s", e)
        work_root = getattr(self, "work_root", None)
        if work_root:
            shutil.rmtree(work_root, ignore_errors=True)
        # Prune any dangling worktree admin entry so the repo stays clean.
        try:
            subprocess.run(
                ["git", "-C", self.repo_dir, "worktree", "prune"],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:  # noqa: BLE001
            pass

    def _terminate_group(self, proc: "subprocess.Popen") -> None:
        """Kill the eval's whole process group (SIGTERM, grace, then SIGKILL).

        Reaps the bash child AND its `cargo test` / test-binary grandchildren so
        a timed-out eval can't leave a runaway process spinning at 100% CPU.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                return
            try:
                proc.wait(timeout=10)
                return  # exited on this signal; no need to escalate
            except subprocess.TimeoutExpired:
                continue

    def exec_run_with_timeout(self, command: str) -> tuple[str, bool, float]:
        """Run the prepared eval script in the worktree, then collect artifacts.

        `command` is accepted for interface parity but ignored — the script to
        run is the rewritten one prepared in __init__.
        """
        timed_out = False
        start = time.time()
        # Run the eval in its OWN process group (start_new_session=True) so that a
        # timeout can reap the ENTIRE tree — bash + the `timeout ... cargo test`
        # child + any spawned test binaries. `subprocess.run(timeout=...)` (and a
        # plain Popen.kill()) only signal the direct child (bash); the cargo/test
        # grandchildren survive as orphans spinning at 100% CPU. Across a large
        # batch that leaks a runaway process per timed-out eval and starves the
        # host. We therefore killpg the whole group: SIGTERM, grace, then SIGKILL.
        # The eval must run the REPO's tests in the repo image's SYSTEM python —
        # that is where the repo, its test deps, and the pytest-json-report plugin
        # (needed for `--json-report`) are installed. The containerized pipeline,
        # however, bakes the harness virtualenv FIRST on PATH (image ENV
        # `PATH=/opt/kaiju/.venv/bin:...`), so a bare `pytest` resolves to the
        # harness venv's pytest, which lacks pytest-json-report. That makes pytest
        # exit 4 ("unrecognized arguments: --json-report") BEFORE running anything,
        # producing no report.json — which evaluate.py then silently scores as a
        # legitimate 0/N for EVERY repo. Strip the active virtualenv's bin from the
        # eval subprocess PATH so `pytest`/`python` resolve to the system tools.
        # (No-op when the harness itself runs outside a venv, e.g. the Docker
        # backend's fresh container.)
        eval_env = dict(os.environ)
        if sys.prefix != sys.base_prefix:
            _venv_bin = os.path.join(sys.prefix, "bin")
            eval_env["PATH"] = os.pathsep.join(
                p for p in eval_env.get("PATH", "").split(os.pathsep)
                if p and os.path.normpath(p) != os.path.normpath(_venv_bin)
            )
            eval_env.pop("VIRTUAL_ENV", None)
        proc = subprocess.Popen(
            ["/bin/bash", self.eval_script_path],
            cwd=self.worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=eval_env,
        )
        try:
            out, err = proc.communicate(timeout=self.timeout)
            output = (out or "") + (err or "")
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_group(proc)
            # Drain whatever the (now-dead) pipes buffered so partial output isn't
            # lost. communicate() after kill returns promptly.
            try:
                out, err = proc.communicate(timeout=30)
            except Exception:  # noqa: BLE001 - pipes may already be closed
                out, err = "", ""
            output = (out or "") + (err or "")
        runtime = time.time() - start

        # Collect result artifacts from the worktree into log_dir, mirroring the
        # Docker backend's copy_from_container (dst = log_dir / fname). Handle the
        # cases copy_from_container does but a naive copyfile does not:
        #   - a DIRECTORY collect target (e.g. Java's target/surefire-reports),
        #   - a NESTED path whose dst parent doesn't exist yet,
        #   - an ABSOLUTE fname (e.g. JS's /tmp/test_results.json) where
        #     log_dir / fname == the absolute source -> skip (already in place),
        #     matching the Docker backend's effective no-op.
        if self.files_to_collect:
            for fname in self.files_to_collect:
                src = Path(self.worktree) / fname
                if not src.exists():
                    continue
                dst = self.log_dir / fname
                try:
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    elif src.resolve() != dst.resolve():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
                except Exception as e:  # noqa: BLE001 - best-effort artifact collection
                    self.logger.debug("LocalInplace: collect %s failed: %s", fname, e)
        return output, timed_out, runtime

    def __exit__(
        self,
        exctype: Optional[Type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        # Remove the worktree from git's registry, then delete the scratch root.
        self._cleanup()
        close_logger(self.logger)


class Modal(ExecutionContext):
    def __init__(
        self,
        spec: Spec,
        logger: logging.Logger,
        timeout: int,
        num_cpus: int,
        log_dir: Path,
        files_to_copy: Optional[Files] = None,
        files_to_collect: Optional[list[str]] = None,
        rebuild_image: bool = False,
    ):
        global modal
        if modal is None:
            import modal as _modal

            modal = _modal

        super().__init__(
            spec,
            logger,
            timeout,
            num_cpus,
            log_dir,
            files_to_copy=files_to_copy,
            files_to_collect=files_to_collect,
        )

        logger.debug("Looking up Modal app 'commit0'")
        self.app = modal.App.lookup("commit0", create_if_missing=True)

        reponame = spec.repo.split("/")[-1]
        image_name = f"wentingzhao/{reponame}:v0".lower()
        image = modal.Image.from_registry(image_name, force_build=rebuild_image)
        if files_to_copy:
            for _, f in files_to_copy.items():
                image = image.add_local_file(str(f["src"]), str(f["dest"]))  # type: ignore
        self.image = image

    def exec_run_with_timeout(self, command: str) -> tuple[str, bool, float]:
        """Execute command on modal sandbox"""
        start_time = time.time()
        with modal.Volume.ephemeral() as vol:
            if self.files_to_collect:
                command += " && "
                for fname in self.files_to_collect:
                    remote_file = Path(self.spec.repo_directory) / fname
                    cp_cmd = f"test -e {str(remote_file)} && cp {str(remote_file)} /vol/{fname}; "
                    command += cp_cmd
            self.sandbox = modal.Sandbox.create(
                "bash",
                "-c",
                command,
                image=self.image,
                cpu=self.num_cpus,
                timeout=self.timeout,
                app=self.app,
                volumes={"/vol": vol},
            )
            self.logger.debug("Waiting for Modal sandbox to complete (timeout=%ds)", self.timeout)
            self.sandbox.wait()

            return_code = self.sandbox.returncode
            # https://github.com/modal-labs/modal-client/blob/d577b2916b5c3bf4ebbcb58fadced84d85e1cf8c/modal/sandbox.py#L413
            if return_code == 124:
                timed_out = True
            else:
                timed_out = False

            if self.files_to_collect:
                fnames = vol.listdir("")
                for fname in fnames:
                    fname = fname.path
                    self.logger.debug("Collecting file from Modal volume: %s", fname)
                    with (self.log_dir / fname).open("wb") as f:
                        for data in vol.read_file(fname):
                            f.write(data)

            self.sandbox.terminate()
            end_time = time.time()
            return self.sandbox.stderr.read(), timed_out, end_time - start_time

    def __exit__(
        self,
        exctype: Optional[Type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        close_logger(self.logger)


class E2B(ExecutionContext):
    def __init__(
        self,
        spec: Spec,
        logger: logging.Logger,
        timeout: int,
        num_cpus: int,
        log_dir: Path,
        files_to_copy: Optional[Files] = None,
        files_to_collect: Optional[list[str]] = None,
        rebuild_image: bool = False,
    ):
        super().__init__(
            spec,
            logger,
            timeout,
            num_cpus,
            log_dir,
            files_to_copy=files_to_copy,
            files_to_collect=files_to_collect,
        )

        # in modal, we create a sandbox for each operation. this seems super slow.
        # let's try having a single sandbox for multiple operations
        # assume the sandbox needs to be alive for an hour, the max duration
        global Sandbox
        if Sandbox is None:
            from e2b_code_interpreter import Sandbox as _Sandbox

            Sandbox = _Sandbox

        logger.info("Creating E2B sandbox for %s", spec.repo)
        self.sb = Sandbox(timeout=60 * 60)
        logger.debug("E2B: running pip install --upgrade pip")
        self.sb.commands.run("pip install --upgrade pip")

        # setup sandbox env
        logger.debug("E2B: writing and running setup.sh")
        self.sb.files.write("setup.sh", spec.setup_script)
        self.sb.commands.run("bash setup.sh")

        # prepare for eval
        if files_to_copy:
            for key, f in files_to_copy.items():
                logger.debug("E2B: copying %s -> %s", f["src"], f["dest"].name)
                with open(f["src"], "r") as fp:  # type: ignore
                    content = fp.read()
                    self.sb.files.write(f["dest"].name, content)  # type: ignore

    def exec_run_with_timeout(self, command: str) -> tuple[str, bool, float]:
        """Execute command on E2B sandbox
        For timeouts, we could maybe use the error code or check whether the
        sandbox is still alive.

        The exit code is given by: result.exit_code

        For now, we can just check if the sandbox is still alive.
        """
        start_time = time.time()
        # half-hour timeout per operation
        result = self.sb.commands.run(command, timeout=self.timeout)
        if self.files_to_collect is not None:
            for fname in self.files_to_collect:
                with (self.log_dir / fname).open("w") as f:
                    f.write(self.sb.files.read(f"testbed/{fname}"))
        timed_out = not self.sb.is_running()
        end_time = time.time()
        return result.stderr, timed_out, end_time - start_time

    def __exit__(
        self,
        exctype: Optional[Type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        self.sb.kill()
        close_logger(self.logger)
