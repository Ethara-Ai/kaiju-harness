"""Runtime resolution for running test collection in a repo-correct Python.

The bug this module fixes: ``tools/generate_test_ids.py`` previously called
``subprocess.run([sys.executable, "-m", "pytest", ...])``, which always used
the harness's own Python (3.13 in the build venv). That's wrong — the repo
might require 3.10, and ``pytest --collect-only`` will fail with cryptic
``ModuleNotFoundError`` on stdlib modules removed in newer Python versions
(e.g. ``distutils`` in 3.12+).

Three resolution tiers (preference order verified by Oracle):

1. **commit0 venv** — if ``commit0 build`` already installed a per-repo venv,
   reuse it. Has all the dependencies pre-installed; cheapest path.
2. **uv** — install the repo's required Python via ``uv python install``,
   then ``uv pip install .`` to make ``pytest --collect-only`` work. Works
   anywhere uv is on PATH.
3. **Docker** — pre-built ``commit0.repo.<name>`` image. Most reliable for
   repos with system-level deps (QGIS, PyQt5, GTK) but slowest.

The chosen runtime exposes a uniform :meth:`Runtime.collect_test_ids` API
so :mod:`tools.generate_test_ids` doesn't branch on runtime kind.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "DockerRuntime",
    "LocalRuntime",
    "NoRuntimeError",
    "Runtime",
    "RuntimeResolutionError",
    "TestCollectionResult",
    "TestCollectionStatus",
    "UvRuntime",
    "classify_failure",
    "resolve_runtime",
]


# ---------------------------------------------------------------------------
# Failure taxonomy (used by both runtimes and downstream upload policy)
# ---------------------------------------------------------------------------


class TestCollectionStatus(str, Enum):
    """Classification of the outcome of ``pytest --collect-only``.

    String values are written into dataset entries as
    ``setup.test_collection_status`` so downstream consumers can filter.
    """

    OK = "ok"
    NO_TESTS = "no_tests"
    IMPORT_ERROR = "import_error"
    VERSION_MISMATCH = "version_mismatch"
    MISSING_SYSTEM_DEPS = "missing_system_deps"
    TIMEOUT = "timeout"
    COLLECTION_FAILED = "collection_failed"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


@dataclass(frozen=True)
class TestCollectionResult:
    """What a runtime returned from a single collection attempt.

    Attributes
    ----------
    test_ids
        List of pytest node IDs (e.g. ``"tests/test_foo.py::test_bar"``).
        Empty when status is anything other than :attr:`TestCollectionStatus.OK`.
    status
        Coarse-grained outcome — see :class:`TestCollectionStatus`.
    stderr_snippet
        Tail of the combined stdout/stderr stream. Bounded to ~2000 chars to
        keep dataset entries small.
    failing_module
        For ``IMPORT_ERROR`` / ``VERSION_MISMATCH`` / ``MISSING_SYSTEM_DEPS``,
        the name of the module Python couldn't import. ``None`` otherwise.
    """

    test_ids: list[str]
    status: TestCollectionStatus
    stderr_snippet: str = ""
    failing_module: str | None = None


# ---------------------------------------------------------------------------
# Module classification — used by classify_failure
# ---------------------------------------------------------------------------


# Modules whose absence indicates a Python version mismatch (not a missing
# dependency). The current value is which Python REMOVED the module — repos
# whose tests import it must run on Python < that version.
_STDLIB_REMOVED_IN: dict[str, str] = {
    "distutils": "3.12",
    "imp": "3.12",
    "asynchat": "3.12",
    "asyncore": "3.12",
    "smtpd": "3.12",
    "binhex": "3.11",
    "audioop": "3.13",
    "cgi": "3.13",
    "cgitb": "3.13",
    "chunk": "3.13",
    "crypt": "3.13",
    "imghdr": "3.13",
    "mailcap": "3.13",
    "msilib": "3.13",
    "nis": "3.13",
    "nntplib": "3.13",
    "pipes": "3.13",
    "sndhdr": "3.13",
    "spwd": "3.13",
    "sunau": "3.13",
    "telnetlib": "3.13",
    "uu": "3.13",
    "xdrlib": "3.13",
}

# Modules backed by system libraries (apt-only, no pip path). Their absence
# means the runtime is missing system deps — not a Python version problem.
# Single source of truth lives in ``tools.system_deps_scanner.SYSTEM_DEP_MODULES``
# so the pre-flight gate and the failure classifier always agree.
from tools.system_deps_scanner import SYSTEM_DEP_MODULES as _SYSTEM_DEP_MODULES  # noqa: E402


_MODULE_NOT_FOUND_RE = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]?([\w.]+)['\"]?",
    re.IGNORECASE,
)


def classify_failure(
    stdout: str,
    stderr: str,
    exit_code: int,
    *,
    timed_out: bool = False,
) -> tuple[TestCollectionStatus, str | None]:
    """Categorize a pytest --collect-only failure.

    Returns ``(status, failing_module)``. ``failing_module`` is set when the
    failure is import-related, so callers can log a precise reason.
    """
    if timed_out:
        return TestCollectionStatus.TIMEOUT, None

    combined = f"{stdout}\n{stderr}"

    # Look for ModuleNotFoundError — most informative signal
    m = _MODULE_NOT_FOUND_RE.search(combined)
    if m:
        module = m.group(1)
        top = module.split(".")[0]
        if top in _SYSTEM_DEP_MODULES:
            return TestCollectionStatus.MISSING_SYSTEM_DEPS, top
        if top in _STDLIB_REMOVED_IN:
            return TestCollectionStatus.VERSION_MISMATCH, top
        return TestCollectionStatus.IMPORT_ERROR, top

    # pytest "collected 0 items" / exit 5 = no tests
    if "no tests ran" in combined.lower() or exit_code == 5:
        return TestCollectionStatus.NO_TESTS, None

    return TestCollectionStatus.COLLECTION_FAILED, None


def next_lower_supported(
    current: str,
    supported: set[str],
    allowed: set[str] | None = None,
) -> str | None:
    """Return the highest ``X.Y`` in ``supported`` that is strictly below
    ``current`` and (optionally) intersects with ``allowed``. ``None`` if
    no such version exists.
    """

    def _key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    try:
        current_key = _key(current)
    except ValueError:
        return None
    pool = {v for v in supported if _key(v) < current_key}
    if allowed is not None:
        pool &= allowed
    if not pool:
        return None
    return max(pool, key=_key)


# ---------------------------------------------------------------------------
# Runtime abstraction
# ---------------------------------------------------------------------------


class NoRuntimeError(RuntimeError):
    """Raised when no runtime tier is available for the requested version."""


class RuntimeResolutionError(RuntimeError):
    """Raised when a specific runtime tier failed setup mid-way (e.g. uv
    couldn't install the requested Python). Distinct from
    :class:`NoRuntimeError` so callers can decide whether to try the next
    tier or give up.
    """


# Constants for pytest collection invocation (kept in one place)
_PYTEST_VERBOSE_FLAGS = [
    "-m",
    "pytest",
    "--collect-only",
    "--override-ini=addopts=",
    "-p",
    "no:cacheprovider",
]
_PYTEST_QUIET_FLAGS = [
    "-m",
    "pytest",
    "--collect-only",
    "-q",
    "--no-header",
    "--override-ini=addopts=",
    "-p",
    "no:cacheprovider",
]


class Runtime:
    """Base class for test-collection runtimes. Subclass to implement
    :meth:`_run` and the runtime sets up its own interpreter.

    Subclasses must set ``self.kind``, ``self.description``, and implement
    :meth:`_run_pytest_collect`.
    """

    kind: str = "base"
    description: str = ""

    def describe(self) -> str:
        return self.description or self.kind

    def collect_test_ids(
        self,
        repo_dir: Path,
        test_dir: str,
        timeout: int,
        parse_fn: "Callable[[str], list[str]]",
    ) -> TestCollectionResult:
        """Run pytest --collect-only twice (verbose then quiet) and classify.

        Parameters
        ----------
        repo_dir
            Repo working directory.
        test_dir
            Subdirectory containing tests (passed as the positional arg to pytest).
        timeout
            Per-attempt timeout in seconds.
        parse_fn
            Callable ``(stdout: str) -> list[str]`` that extracts test IDs.
            Injected to avoid a circular import with ``generate_test_ids``.
        """
        try:
            stdout, stderr, exit_code = self._run_pytest_collect(
                repo_dir=repo_dir,
                test_dir=test_dir,
                timeout=timeout,
                verbose=True,
            )
        except subprocess.TimeoutExpired:
            return TestCollectionResult(
                test_ids=[],
                status=TestCollectionStatus.TIMEOUT,
                stderr_snippet=f"timeout after {timeout}s",
            )

        test_ids = parse_fn(stdout)

        # If verbose yielded nothing, try quiet mode (handles non-unittest layouts)
        if not test_ids:
            try:
                stdout_q, stderr_q, exit_code_q = self._run_pytest_collect(
                    repo_dir=repo_dir,
                    test_dir=test_dir,
                    timeout=timeout,
                    verbose=False,
                )
                test_ids = parse_fn(stdout_q)
                if not test_ids:
                    # Use the more informative output for classification
                    stdout = stdout_q if len(stdout_q) > len(stdout) else stdout
                    stderr = stderr_q if len(stderr_q) > len(stderr) else stderr
                    exit_code = exit_code_q
            except subprocess.TimeoutExpired:
                pass  # keep what we have

        if test_ids:
            return TestCollectionResult(
                test_ids=test_ids,
                status=TestCollectionStatus.OK,
            )

        status, failing_module = classify_failure(stdout, stderr, exit_code)
        return TestCollectionResult(
            test_ids=[],
            status=status,
            stderr_snippet=(stdout + "\n" + stderr)[-2000:],
            failing_module=failing_module,
        )

    def _run_pytest_collect(
        self,
        *,
        repo_dir: Path,
        test_dir: str,
        timeout: int,
        verbose: bool,
    ) -> tuple[str, str, int]:
        """Subclass hook: invoke pytest and return (stdout, stderr, exit_code).

        ``verbose=True`` requests the wrapped-node output format;
        ``verbose=False`` requests the quiet ``-q`` format.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete runtimes
# ---------------------------------------------------------------------------


class LocalRuntime(Runtime):
    """Use an existing local Python interpreter (e.g. a commit0 build venv)."""

    kind = "local"

    def __init__(self, interpreter: Path):
        if not interpreter.exists():
            raise RuntimeResolutionError(
                f"LocalRuntime interpreter not found: {interpreter}"
            )
        self.interpreter = interpreter
        self.description = f"local interpreter at {interpreter}"

    def _run_pytest_collect(
        self,
        *,
        repo_dir: Path,
        test_dir: str,
        timeout: int,
        verbose: bool,
    ) -> tuple[str, str, int]:
        flags = _PYTEST_VERBOSE_FLAGS if verbose else _PYTEST_QUIET_FLAGS
        result = subprocess.run(
            [str(self.interpreter), *flags, test_dir],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode


def _detect_install_extras(repo_dir: Path) -> list[str]:
    """Detect which ``[project.optional-dependencies]`` extras to install.

    Picks test-related extras (``test``, ``tests``, ``testing``) so that
    ``conftest.py`` imports of test-only deps (matplotlib, pyhf, etc.)
    resolve. Falls back to ``dev`` / ``develop`` when no explicit test extra
    exists. Returns an empty list when no relevant extras are declared.
    """
    pyproject = repo_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []
    import tomllib

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    extras_dict = (data.get("project") or {}).get("optional-dependencies") or {}
    if not isinstance(extras_dict, dict):
        return []
    available = set(extras_dict.keys())
    # Priority: explicit test extras > dev > generic 'all'
    preferred = [n for n in ("test", "tests", "testing") if n in available]
    if preferred:
        return preferred
    if "dev" in available:
        return ["dev"]
    if "develop" in available:
        return ["develop"]
    return []



class UvRuntime(Runtime):
    """Resolve a Python via ``uv`` and install the repo so collection works.

    ``uv pip install .`` is required because ``pytest --collect-only`` imports
    conftests and the package itself — a bare interpreter without the repo
    installed will fail.
    """

    kind = "uv"

    def __init__(self, version: str, repo_dir: Path):
        if not shutil.which("uv"):
            raise NoRuntimeError("uv is not on PATH")
        self.version = version
        self.repo_dir = repo_dir
        # Ensure the requested Python is downloaded via uv.
        # We DON'T install into this interpreter directly; we use a per-repo
        # ephemeral venv to avoid cross-run package pollution.
        self._uv_python = self._ensure_interpreter()
        # ``interpreter`` switches to the venv python after the first install.
        self.interpreter = self._uv_python
        self._venv_python: Path | None = None
        self._installed = False
        # Pre-flight short-circuit reason (e.g. detected system-only deps).
        # When set, _run_pytest_collect skips install + collection and surfaces
        # a synthetic stderr that classify_failure() maps to MISSING_SYSTEM_DEPS.
        self._short_circuit: tuple[str, str] | None = None
        self.description = f"uv-managed python{version} at {self.interpreter}"

    def _ensure_interpreter(self) -> Path:
        try:
            subprocess.run(
                ["uv", "python", "install", self.version],
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeResolutionError(
                f"uv python install {self.version} failed: {exc.stderr or exc.stdout}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeResolutionError(
                f"uv python install {self.version} timed out"
            ) from exc

        try:
            result = subprocess.run(
                ["uv", "python", "find", self.version],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeResolutionError(
                f"uv python find {self.version} failed: {exc.stderr or exc.stdout}"
            ) from exc
        path = Path(result.stdout.strip())
        if not path.exists():
            raise RuntimeResolutionError(f"uv reported non-existent path: {path}")
        return path

    def _scan_for_system_deps(self) -> list[str]:
        """Pre-flight: walk test imports for QGIS/PyQt5/cv2/etc."""
        try:
            from tools.system_deps_scanner import scan_repo_for_system_deps

            return scan_repo_for_system_deps(self.repo_dir)
        except Exception:  # noqa: BLE001 - scanner failures must not block install
            return []

    def _ensure_collect_venv(self) -> Path | None:
        """Create (or reuse) an ephemeral venv keyed by Python version."""
        venv_dir = self.repo_dir / f".kaiju-collect-venv-py{self.version}"
        if (venv_dir / "bin" / "python").exists():
            return venv_dir / "bin" / "python"
        try:
            subprocess.run(
                ["uv", "venv", str(venv_dir), "--python", self.version],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("uv venv creation failed: %s", exc)
            return None
        return venv_dir / "bin" / "python"

    def _install_repo_if_needed(self) -> None:
        if self._installed:
            return

        # --- Pre-flight: system deps gate ---
        sys_deps = self._scan_for_system_deps()
        if sys_deps:
            logger.warning(
                "System-only deps detected (%s); skipping install — will surface "
                "as MISSING_SYSTEM_DEPS without burning time on a doomed install.",
                ", ".join(sys_deps),
            )
            self._short_circuit = ("system_deps", sys_deps[0])
            self._installed = True
            return

        # --- Create ephemeral venv so different runs don't share site-packages ---
        venv_python = self._ensure_collect_venv()
        if venv_python is not None:
            self._venv_python = venv_python
            self.interpreter = venv_python
            self.description = f"uv-managed python{self.version} at {venv_python} (ephemeral venv)"

        # --- Editable install with test extras so conftest imports resolve ---
        # Most repos put pytest/matplotlib/etc. in [project.optional-dependencies]
        # under a 'test'/'tests'/'testing' extra. Install with those so
        # `import matplotlib` in conftest.py actually works.
        extras = _detect_install_extras(self.repo_dir)
        spec = f".[{','.join(extras)}]" if extras else "."
        logger.info("  uv pip install -e %s", spec)
        try:
            install_result = subprocess.run(
                ["uv", "pip", "install", "--python", str(self.interpreter), "-e", spec],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            if install_result.returncode != 0:
                logger.warning(
                    "  uv pip install -e %s failed (rc=%d); last 400 chars: %s",
                    spec, install_result.returncode,
                    (install_result.stderr or install_result.stdout)[-400:],
                )
                if extras:
                    logger.info("  Retrying without extras: uv pip install -e .")
                    subprocess.run(
                        ["uv", "pip", "install", "--python", str(self.interpreter), "-e", "."],
                        cwd=self.repo_dir,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        check=False,
                    )
        except subprocess.TimeoutExpired:
            logger.warning(
                "uv pip install . timed out in %s; collection may fail",
                self.repo_dir,
            )

        # --- Always install pytest, even if editable install above failed ---
        try:
            subprocess.run(
                ["uv", "pip", "install", "--python", str(self.interpreter), "pytest"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("uv pip install pytest timed out")
        self._installed = True

    def _run_pytest_collect(
        self,
        *,
        repo_dir: Path,
        test_dir: str,
        timeout: int,
        verbose: bool,
    ) -> tuple[str, str, int]:
        self._install_repo_if_needed()

        # Short-circuit: pre-flight detected system-only deps. Synthesize a
        # ModuleNotFoundError stderr line so classify_failure() returns
        # MISSING_SYSTEM_DEPS without us having to teach it a new path.
        if self._short_circuit is not None:
            kind, module = self._short_circuit
            stderr = (
                f"ModuleNotFoundError: No module named '{module}'\n"
                f"(kaiju-pre-flight: {kind})\n"
            )
            return "", stderr, 1

        flags = _PYTEST_VERBOSE_FLAGS if verbose else _PYTEST_QUIET_FLAGS
        result = subprocess.run(
            [str(self.interpreter), *flags, test_dir],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode


class DockerRuntime(Runtime):
    """Run pytest inside a pre-built ``commit0.repo.*`` Docker image."""

    kind = "docker"

    def __init__(
        self,
        image: str,
        reference_commit: str | None = None,
        repo_dir_in_container: str = "/testbed",
    ):
        import docker  # imported lazily — docker SDK is heavy
        import docker.errors  # noqa: F401 - registers errors submodule on `docker`

        self.image = image
        self.reference_commit = reference_commit
        self.repo_dir_in_container = repo_dir_in_container
        self._docker = docker
        self._client = docker.from_env()
        self.description = f"docker image {image}"

    def _run_pytest_collect(
        self,
        *,
        repo_dir: Path,
        test_dir: str,
        timeout: int,
        verbose: bool,
    ) -> tuple[str, str, int]:
        import requests.exceptions  # lazy

        from commit0.harness.docker_utils import get_docker_platform

        flags = "-q --no-header" if not verbose else ""
        checkout = (
            f"git checkout {self.reference_commit} -- . && "
            if self.reference_commit
            else ""
        )
        bash_cmd = (
            f"cd {self.repo_dir_in_container} && {checkout}"
            f"python -m pytest --collect-only {flags} --override-ini='addopts=' "
            f"-p no:cacheprovider {test_dir} 2>&1; echo __EXIT__:$?"
        )
        try:
            raw = self._client.containers.run(
                self.image,
                command=["bash", "-c", bash_cmd],
                remove=True,
                platform=get_docker_platform(),
            )
            output = (
                raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            )
            exit_code = _extract_exit_code(output)
            output = _strip_exit_marker(output)
            return output, "", exit_code
        except self._docker.errors.ContainerError as e:
            raw_err = e.stderr
            output = (
                raw_err.decode("utf-8", errors="replace")
                if isinstance(raw_err, bytes)
                else (raw_err or "")
            )
            return output, "", e.exit_status if hasattr(e, "exit_status") else 1
        except requests.exceptions.ReadTimeout as exc:
            raise subprocess.TimeoutExpired(self.image, timeout) from exc


def _extract_exit_code(output: str) -> int:
    m = re.search(r"__EXIT__:(\d+)\s*$", output)
    return int(m.group(1)) if m else 0


def _strip_exit_marker(output: str) -> str:
    return re.sub(r"__EXIT__:\d+\s*$", "", output)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def find_commit0_venv(repo_dir: Path, python_version: str) -> Path | None:
    """Locate a per-repo venv that ``commit0 build`` may have created.

    Checks a few well-known locations. Returns the interpreter path or ``None``.
    """
    candidates = [
        repo_dir / ".venv" / "bin" / "python",
        repo_dir / "venv" / "bin" / "python",
        repo_dir / "env" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            try:
                result = subprocess.run(
                    [str(c), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                if result.stdout.strip() == python_version:
                    return c
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
    return None


def find_docker_image_for_repo(repo_name: str) -> str | None:
    """Search local Docker daemon for a ``commit0.repo.<short_name>.*`` tag."""
    try:
        import docker

        client = docker.from_env()
    except Exception:  # noqa: BLE001 - any docker error → no image
        return None
    short_name = repo_name.split("__")[-1].split("-")[0].lower()
    needle = f"commit0.repo.{short_name}."
    fallback = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"
    try:
        for image in client.images.list():
            for tag in image.tags:
                if tag.startswith(needle):
                    return tag
                if tag == fallback:
                    return tag
    except Exception:  # noqa: BLE001
        return None
    return None


def resolve_runtime(
    python_version: str,
    repo_dir: Path,
    *,
    repo_name: str | None = None,
    reference_commit: str | None = None,
    prefer: list[str] | None = None,
    explicit_interpreter: Path | None = None,
) -> Runtime:
    """Pick the best available runtime for ``python_version``.

    Parameters
    ----------
    python_version
        Target ``X.Y`` version (e.g. ``"3.10"``).
    repo_dir
        Local working directory of the repo.
    repo_name
        ``org/repo`` string used to locate a Docker image. ``None`` to skip
        Docker tier.
    reference_commit
        If set and Docker is chosen, a ``git checkout`` to this commit is run
        before pytest collection (so stubbed code doesn't break imports).
    prefer
        Override the default tier order (``["local", "uv", "docker"]``).
    explicit_interpreter
        Force a specific interpreter path (e.g. from a CLI flag). Skips
        auto-resolution entirely.

    Raises
    ------
    NoRuntimeError
        If no tier produced a usable runtime.
    """
    if explicit_interpreter is not None:
        return LocalRuntime(interpreter=explicit_interpreter)

    tiers = prefer or ["local", "uv", "docker"]
    errors: list[str] = []

    for tier in tiers:
        if tier == "local":
            interp = find_commit0_venv(repo_dir, python_version)
            if interp is not None:
                logger.info("Using local commit0 venv: %s", interp)
                return LocalRuntime(interpreter=interp)
            # Fallback: if the host Python matches the requested version exactly,
            # use it (lets ad-hoc invocations work without uv/docker).
            host_match = _host_python_if_matches(python_version)
            if host_match is not None and os.environ.get("KAIJU_ALLOW_HOST_PYTHON"):
                logger.info("Using host Python %s (KAIJU_ALLOW_HOST_PYTHON=1)", host_match)
                return LocalRuntime(interpreter=host_match)
            errors.append("local: no per-repo venv found")
            continue

        if tier == "uv":
            try:
                return UvRuntime(version=python_version, repo_dir=repo_dir)
            except NoRuntimeError as exc:
                errors.append(f"uv: {exc}")
                continue
            except RuntimeResolutionError as exc:
                errors.append(f"uv: {exc}")
                continue

        if tier == "docker":
            if not repo_name:
                errors.append("docker: no repo_name supplied")
                continue
            image = find_docker_image_for_repo(repo_name)
            if image is None:
                errors.append(f"docker: no image found for {repo_name}")
                continue
            try:
                return DockerRuntime(
                    image=image,
                    reference_commit=reference_commit,
                )
            except Exception as exc:  # noqa: BLE001 - docker init errors
                errors.append(f"docker: init failed: {exc}")
                continue

    raise NoRuntimeError(
        f"No runtime available for Python {python_version} in {repo_dir}. "
        f"Attempts: {'; '.join(errors)}"
    )


def _host_python_if_matches(python_version: str) -> Path | None:
    """Return ``Path(sys.executable)`` if the host Python's X.Y matches."""
    import sys

    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    if host == python_version:
        return Path(sys.executable)
    return None
