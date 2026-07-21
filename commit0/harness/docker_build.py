import logging
import os
import platform as _platform
import re
import subprocess
import tarfile
import time
import traceback
import docker
import docker.errors
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from commit0.harness.constants import (
    BASE_IMAGE_BUILD_DIR,
    REPO_IMAGE_BUILD_DIR,
    OCI_IMAGE_DIR,
)
from commit0.harness.spec import get_specs_from_dataset
from commit0.harness.utils import setup_logger, close_logger

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_logger = logging.getLogger(__name__)


def _native_platform() -> str:
    """Return the Docker platform string for the current machine architecture."""
    machine = _platform.machine()
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    return "linux/amd64"


def _safe_builder_args() -> list[str]:
    """Return ``['--builder', name]`` for a builder that supports ``--load``.

    ``docker-container`` builders cannot ``--load`` into the daemon.  If the
    current default builder uses that driver, fall back to the well-known
    ``default`` or ``desktop-linux`` builders which always use the ``docker``
    driver.  When no such builder exists, return an empty list so buildx
    uses whatever is currently active (best-effort).
    """
    try:
        result = subprocess.run(
            ["docker", "buildx", "inspect"],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        if "docker-container" in output:
            # Current builder uses docker-container driver — can't --load.
            # Try builders that use the docker driver.
            for candidate in ("desktop-linux", "default"):
                probe = subprocess.run(
                    ["docker", "buildx", "inspect", candidate],
                    capture_output=True,
                    text=True,
                )
                probe_out = probe.stdout + probe.stderr
                if probe.returncode == 0 and "docker-container" not in probe_out:
                    return ["--builder", candidate]
            return []  # best-effort: let buildx figure it out
    except Exception:
        _logger.debug("_safe_builder_args: failed to probe builders", exc_info=True)
    return []  # current builder is fine


MULTIARCH_BUILDER_NAME = "commit0-multiarch"


def _multiarch_builder_args() -> list[str]:
    """Return ``['--builder', name]`` for a builder that supports multi-platform builds.

    Multi-arch OCI exports require the ``docker-container`` driver.  This
    function checks for an existing builder, falls back to the current default
    if it already uses that driver, and finally creates a new builder if
    needed.
    """
    # 1. Check if our dedicated builder already exists
    try:
        probe = subprocess.run(
            ["docker", "buildx", "inspect", MULTIARCH_BUILDER_NAME],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and "docker-container" in (
            probe.stdout + probe.stderr
        ):
            return ["--builder", MULTIARCH_BUILDER_NAME]
    except Exception:
        _logger.debug(
            "multiarch: failed to inspect dedicated builder %s",
            MULTIARCH_BUILDER_NAME,
            exc_info=True,
        )

    # 2. Check if the current default builder already uses docker-container
    try:
        result = subprocess.run(
            ["docker", "buildx", "inspect"],
            capture_output=True,
            text=True,
        )
        if "docker-container" in (result.stdout + result.stderr):
            return []  # current default is suitable
    except Exception:
        _logger.debug("multiarch: failed to inspect default builder", exc_info=True)

    # 3. Create a new docker-container builder
    try:
        subprocess.run(
            [
                "docker",
                "buildx",
                "create",
                "--name",
                MULTIARCH_BUILDER_NAME,
                "--driver",
                "docker-container",
                "--bootstrap",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return ["--builder", MULTIARCH_BUILDER_NAME]
    except Exception:
        _logger.debug(
            "multiarch: failed to create builder %s",
            MULTIARCH_BUILDER_NAME,
            exc_info=True,
        )
        return []  # best-effort: let buildx figure it out


def _ensure_oci_layout(oci_tar: Path) -> Path | None:
    """Extract an OCI tarball to an OCI layout directory (idempotent)."""
    layout_dir = oci_tar.parent / "oci-layout"
    if (layout_dir / "index.json").exists():
        return layout_dir
    if not oci_tar.exists() or oci_tar.stat().st_size == 0:
        return None
    try:
        layout_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(oci_tar) as tf:
            tf.extractall(layout_dir, filter="data")
        return layout_dir
    except Exception:
        _logger.debug("Failed to extract OCI layout from %s", oci_tar, exc_info=True)
        return None


def _check_qemu_support(platform_str: str) -> bool:
    """Check if QEMU/binfmt is set up for the given Docker platform."""
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform_str,
                "alpine",
                "echo",
                "ok",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        _logger.debug("Failed to check Docker availability", exc_info=True)
        return False


PROXY_ENV_KEYS = [
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "no_proxy",
    "NO_PROXY",
]


def _mitm_disabled() -> bool:
    return os.environ.get("COMMIT0_MITM_DISABLED", "").strip() in ("1", "true", "yes")


def get_proxy_env() -> dict[str, str]:
    """Collect proxy-related env vars from the host. Used for both build args and runtime env.

    Returns empty dict if COMMIT0_MITM_DISABLED=1.
    """
    if _mitm_disabled():
        return {}
    return {k: os.environ[k] for k in PROXY_ENV_KEYS if os.environ.get(k)}


def _is_pem_cert(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            first_line = f.readline()
        return b"-----BEGIN CERTIFICATE-----" in first_line
    except (OSError, IOError):
        return False


def _resolve_mitm_ca_cert() -> Optional[Path]:
    """Find the MITM CA certificate.

    Search order:
      1. MITM_CA_CERT env var (explicit path)
      2. ~/.mitmproxy/mitmproxy-ca-cert.pem (mitmproxy default)

    Returns None if disabled via COMMIT0_MITM_DISABLED=1 or no valid cert found.
    """
    if _mitm_disabled():
        return None

    env_path = os.environ.get("MITM_CA_CERT")
    if env_path:
        p = Path(env_path)
        if p.is_file() and _is_pem_cert(p):
            return p

    default_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if default_path.is_file() and _is_pem_cert(default_path):
        return default_path

    return None


class BuildImageError(Exception):
    def __init__(self, image_name: str, message: str, logger: logging.Logger):
        super().__init__(message)
        self.super_str = super().__str__()
        self.image_name = image_name
        self.log_path = ""  # logger.log_file
        self.logger = logger

    def __str__(self):
        return (
            f"Error building image {self.image_name}: {self.super_str}\n"
            f"Check ({self.log_path}) for more information."
        )


def _build_image_once(
    image_name: str,
    setup_scripts: dict,
    dockerfile: str,
    platform: str,
    client: docker.DockerClient,
    build_dir: Path,
    nocache: bool = False,
    mitm_ca_cert: Optional[Path] = None,
) -> None:
    """Builds a docker image with the given name, setup scripts, dockerfile, and platform.

    Single attempt. Callers use the public :func:`build_image`, which wraps this with
    transient-network retries.

    Produces two outputs:
      1. A multi-arch OCI tarball (linux/amd64 + linux/arm64) for pushing to a container registry.
      2. A native-arch image loaded into the local Docker daemon for immediate use.

    Args:
    ----
        image_name (str): Name of the image to build
        setup_scripts (dict): Dictionary of setup script names to setup script contents
        dockerfile (str): Contents of the Dockerfile
        platform (str): Comma-separated platforms for the OCI tarball (e.g. "linux/amd64,linux/arm64")
        client (docker.DockerClient): Docker client to use for building the image
        build_dir (Path): Directory for the build context (will also contain logs, scripts, and artifacts)
        nocache (bool): Whether to use the cache when building
        mitm_ca_cert (Path): Pre-resolved path to a MITM CA certificate PEM file

    """
    logger = setup_logger(image_name, build_dir / "build_image.log")
    logger.info(
        f"Building image {image_name}\n"
        f"Using dockerfile:\n{dockerfile}\n"
        f"Adding ({len(setup_scripts)}) setup scripts to image build repo"
    )

    for setup_script_name, setup_script in setup_scripts.items():
        logger.info(f"[SETUP SCRIPT] {setup_script_name}:\n{setup_script}")
    try:
        for setup_script_name, setup_script in setup_scripts.items():
            setup_script_path = build_dir / setup_script_name
            with open(setup_script_path, "w") as f:
                f.write(setup_script)
            if setup_script_name not in dockerfile:
                logger.warning(
                    f"Setup script {setup_script_name} may not be used in Dockerfile"
                )

        dockerfile_path = build_dir / "Dockerfile"
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile)

        # MITM CA cert is passed via BuildKit --secret (not COPY into context)
        secret_flags: list[str] = []
        if mitm_ca_cert:
            secret_flags = ["--secret", f"id=mitm_ca,src={mitm_ca_cert}"]
            logger.info(
                f"Injecting MITM CA cert via BuildKit secret from {mitm_ca_cert}"
            )

        buildargs = get_proxy_env()
        if buildargs:
            logger.info(f"Forwarding proxy build args: {list(buildargs.keys())}")

        logger.info(
            f"Building docker image {image_name} in {build_dir} with platform {platform}"
        )

        buildarg_flags: list[str] = []
        for k, v in buildargs.items():
            buildarg_flags.extend(["--build-arg", f"{k}={v}"])

        nocache_flags = ["--no-cache"] if nocache else []

        # Step 1: Build multi-arch OCI tarball for ECR push (non-fatal)
        oci_dir = OCI_IMAGE_DIR / image_name.replace(":", "__")
        oci_dir.mkdir(parents=True, exist_ok=True)
        oci_tar_path = oci_dir / f"{image_name.replace(':', '__')}.tar"

        multiarch_flags = _multiarch_builder_args()
        build_context_flags: list[str] = []
        base_image_match = re.search(r"FROM\s+(commit0\.base\.\S+)", dockerfile)
        if multiarch_flags and base_image_match:
            base_image_ref = base_image_match.group(1)
            base_oci_key = base_image_ref.replace(":", "__")
            base_oci_tar = OCI_IMAGE_DIR / base_oci_key / f"{base_oci_key}.tar"
            layout_dir = _ensure_oci_layout(base_oci_tar)
            if layout_dir:
                build_context_flags = [
                    "--build-context",
                    f"{base_image_ref}=oci-layout://{layout_dir}",
                ]
            else:
                _logger.warning(
                    "Base OCI layout not available at %s; "
                    "docker-container builder may fail to resolve FROM %s",
                    base_oci_tar,
                    base_image_ref,
                )
        if "," in platform:
            native = _native_platform()
            for plat in platform.split(","):
                if plat.strip() != native and not _check_qemu_support(plat.strip()):
                    _logger.warning(
                        "QEMU/binfmt not available for %s — OCI build may fail",
                        plat.strip(),
                    )

        oci_cmd = [
            "docker",
            "buildx",
            "build",
            *multiarch_flags,
            *build_context_flags,
            "--platform",
            platform,
            "--tag",
            image_name,
            "--output",
            f"type=oci,dest={oci_tar_path}",
            *nocache_flags,
            *buildarg_flags,
            *secret_flags,
            str(build_dir),
        ]
        logger.info(f"Building OCI tarball: {' '.join(oci_cmd)}")
        oci_result = subprocess.run(oci_cmd, capture_output=True, text=True)
        for line in (oci_result.stderr or "").splitlines():
            logger.info(ansi_escape.sub("", line))
        if oci_result.returncode != 0:
            is_multiarch = "," in platform
            if is_multiarch:
                raise BuildImageError(
                    image_name,
                    f"Multi-arch OCI build failed (fatal for multi-arch): "
                    f"rc={oci_result.returncode} "
                    f"{oci_result.stderr.splitlines()[-1] if oci_result.stderr else 'unknown error'}",
                    logger,
                )
            logger.warning(
                f"OCI tarball build failed (non-fatal for single-arch): "
                f"rc={oci_result.returncode} "
                f"{oci_result.stderr.splitlines()[-1] if oci_result.stderr else 'unknown error'}"
            )
            if oci_tar_path.exists() and oci_tar_path.stat().st_size == 0:
                oci_tar_path.unlink()
                _logger.debug("Removed 0-byte OCI tarball: %s", oci_tar_path)
            if oci_dir.exists() and not any(oci_dir.iterdir()):
                oci_dir.rmdir()
                _logger.debug("Removed empty OCI dir: %s", oci_dir)
        else:
            logger.info(f"OCI tarball saved to {oci_tar_path}")

        # Step 2: Load native-arch image into local daemon for immediate use
        native = _native_platform()
        load_builder_flags = _safe_builder_args()
        load_cmd = [
            "docker",
            "buildx",
            "build",
            *load_builder_flags,
            "--platform",
            native,
            "--tag",
            image_name,
            "--load",
            *nocache_flags,
            *buildarg_flags,
            *secret_flags,
            str(build_dir),
        ]
        logger.info(f"Loading native image ({native}): {' '.join(load_cmd)}")
        load_result = subprocess.run(load_cmd, capture_output=True, text=True)
        for line in (load_result.stderr or "").splitlines():
            logger.info(ansi_escape.sub("", line))
        if load_result.returncode != 0:
            raise BuildImageError(image_name, load_result.stderr, logger)

        logger.info("Image built successfully!")
    except BuildImageError:
        raise
    except Exception as e:
        logger.error(f"Error building image {image_name}: {e}")
        raise BuildImageError(image_name, str(e), logger) from e
    finally:
        close_logger(logger)


# Substrings (lower-cased) that identify a TRANSIENT build failure — a network
# reset/timeout/DNS blip while cloning or installing packages inside the build,
# NOT a real defect in the repo or Dockerfile. A single such blip should not fail
# an otherwise-healthy build, so build_image_with_retries re-runs the build (the
# successful FROM/COPY layers stay cached; only the failed RUN re-executes).
_TRANSIENT_BUILD_ERROR_MARKERS = (
    "econnreset",
    "etimedout",
    "enetunreach",
    "enotfound",
    "eai_again",
    "network aborted",
    "network is unreachable",
    "connection reset",
    "socket hang up",
    "temporary failure in name resolution",
    "timed out",
    "i/o timeout",
    "tls handshake timeout",
    "failed to fetch",
    "could not resolve host",
    "503 service unavailable",
    "429 too many requests",
    "unexpected eof",
    "reset by peer",
    # buildx/daemon socket dropped mid-build (e.g. a parallel shard tore down the
    # shared builder, or the daemon briefly restarted). buildkit surfaces this as a
    # gRPC handshake/socket error at listWorkers; the build itself is healthy, so a
    # re-run on the same cached layers succeeds. Observed: "listing workers for
    # Build: ... error reading server preface: read unix @->/run/docker.sock: use
    # of closed network connection".
    "use of closed network connection",
    "error reading server preface",
    "server preface",
)


# Template placeholder convention shared by every language's Dockerfile
# (`__GO_VERSION__`, `__C_GCC_VERSION__`, `__CPP_UBUNTU_VERSION__`,
# `__RUST_VERSION__`, `__CARGO_NEXTEST_VERSION__`, ...). Each spec's
# base_dockerfile is responsible for substituting these before build; a missed
# substitution otherwise reaches Docker as e.g. `FROM gcc:__C_GCC_VERSION__-...`
# and fails with a cryptic "not found" that hides the real cause. This ALL-CAPS,
# double-underscore-delimited pattern is narrow enough not to match real
# Dockerfile content (lowercase `__pycache__`, `$__foo` shell vars, etc.).
_UNSUBSTITUTED_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


def _assert_dockerfile_fully_substituted(image_name: str, dockerfile: str) -> None:
    """Fail LOUD before building if a template placeholder was never substituted.

    Universal guard for every language: converts the class of "version
    placeholder leaked into the Dockerfile" bugs from a cryptic Docker Hub
    resolve error into an actionable message naming the exact token(s) and the
    spec responsible. Applies to base AND repo images since all flow through
    :func:`build_image`.
    """
    if not dockerfile:
        return
    leftovers = sorted(set(_UNSUBSTITUTED_PLACEHOLDER_RE.findall(dockerfile)))
    if leftovers:
        raise BuildImageError(
            image_name,
            "Dockerfile contains unsubstituted template placeholder(s): "
            f"{', '.join(leftovers)}. The spec that produced this image "
            "(base_dockerfile/repo_dockerfile) must .replace() every "
            "__PLACEHOLDER__ with its constant before building.",
            _logger,
        )


def _is_transient_build_error(message: str) -> bool:
    """True if *message* looks like a transient network failure worth retrying."""
    low = (message or "").lower()
    return any(marker in low for marker in _TRANSIENT_BUILD_ERROR_MARKERS)


def _build_max_attempts() -> int:
    """Total build attempts (>=1). Override with COMMIT0_BUILD_MAX_ATTEMPTS."""
    raw = os.environ.get("COMMIT0_BUILD_MAX_ATTEMPTS", "3").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def build_image(*args: Any, **kwargs: Any) -> None:
    """Build a docker image, retrying ONLY on transient network failures.

    Public entry point for all languages (Python/JS/TS/Rust/C++). Delegates to
    :func:`_build_image_once`; a network reset/timeout while cloning or installing
    packages inside the build is retried with exponential backoff (the successful
    FROM/COPY layers stay cached, so only the failed RUN re-executes). Non-transient
    failures — a genuine compile/setup defect — raise immediately; retries never mask
    a real bug. Image name is args[0] (or the image_name kwarg) for logging.
    """
    image_name = args[0] if args else kwargs.get("image_name", "<unknown>")
    # _build_image_once signature: (image_name, setup_scripts, dockerfile, ...);
    # dockerfile is positional index 2 or the `dockerfile` kwarg.
    dockerfile = kwargs.get("dockerfile")
    if dockerfile is None and len(args) >= 3:
        dockerfile = args[2]
    _assert_dockerfile_fully_substituted(image_name, dockerfile or "")
    max_attempts = _build_max_attempts()
    for attempt in range(1, max_attempts + 1):
        try:
            return _build_image_once(*args, **kwargs)
        except BuildImageError as e:
            if attempt >= max_attempts or not _is_transient_build_error(str(e)):
                raise
            backoff = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s, ...
            _logger.warning(
                "Transient network failure building %s (attempt %d/%d); "
                "retrying in %ds. Cause: %s",
                image_name,
                attempt,
                max_attempts,
                backoff,
                str(e).splitlines()[-1] if str(e) else "unknown",
            )
            time.sleep(backoff)
    return None


def build_base_images(
    client: docker.DockerClient,
    dataset: list,
    dataset_type: str,
    mitm_ca_cert: Optional[Path] = None,
) -> None:
    """Builds the base images required for the dataset if they do not already exist.

    Args:
    ----
        client (docker.DockerClient): Docker client to use for building the images
        dataset (list): List of test specs or dataset to build images for
        dataset_type(str): The type of dataset. Choices are commit0 and swebench
        mitm_ca_cert (Path): Pre-resolved MITM CA cert path (or None)

    """
    test_specs = get_specs_from_dataset(dataset, dataset_type, absolute=True)
    base_images = {
        x.base_image_key: (x.base_dockerfile, x.platform) for x in test_specs
    }

    # Ensure multiarch builder exists before building base images
    _multiarch_builder_args()

    for image_name, (dockerfile, platform) in base_images.items():
        oci_key = image_name.replace(":", "__")
        oci_tar_path = OCI_IMAGE_DIR / oci_key / f"{oci_key}.tar"
        daemon_exists = False
        try:
            client.images.get(image_name)
            daemon_exists = True
        except docker.errors.ImageNotFound:
            pass

        if daemon_exists and oci_tar_path.exists():
            if mitm_ca_cert:
                _logger.warning(
                    "Base image %s already exists but MITM CA cert "
                    "was found at %s. If the cert was added after the base "
                    "image was built, delete the old image: docker rmi %s",
                    image_name,
                    mitm_ca_cert,
                    image_name,
                )
            else:
                _logger.info(
                    "Base image %s already exists, skipping build.", image_name
                )
            continue
        elif daemon_exists:
            _logger.info(
                "Base image %s in daemon but OCI tarball missing, rebuilding.",
                image_name,
            )

        _logger.info("Building base image (%s)", image_name)
        build_image(
            image_name=image_name,
            setup_scripts={},
            dockerfile=dockerfile,
            platform=platform,
            client=client,
            build_dir=BASE_IMAGE_BUILD_DIR / image_name.replace(":", "__"),
            mitm_ca_cert=mitm_ca_cert,
        )
    _logger.info("Base images built successfully.")


def _get_image_created_timestamp(client: docker.DockerClient, image_name: str) -> str:
    """Return the Created timestamp of a Docker image, or empty string if not found."""
    try:
        img = client.images.get(image_name)
        return img.attrs.get("Created", "")
    except docker.errors.ImageNotFound:
        return ""
    except docker.errors.APIError:
        return ""


def get_repo_configs_to_build(
    client: docker.DockerClient, dataset: list, dataset_type: str
) -> dict[str, Any]:
    """Returns a dictionary of image names to build scripts and dockerfiles for repo images.
    Returns only the repo images that need to be built.

    Args:
    ----
        client (docker.DockerClient): Docker client to use for building the images
        dataset (list): List of test specs or dataset to build images for
        dataset_type(str): The type of dataset. Choices are commit0 and swebench

    """
    image_scripts = dict()
    test_specs = get_specs_from_dataset(dataset, dataset_type, absolute=True)

    base_timestamps: dict[str, str] = {}

    for test_spec in test_specs:
        try:
            client.images.get(test_spec.base_image_key)
        except docker.errors.ImageNotFound as e:
            raise Exception(
                f"Base image {test_spec.base_image_key} not found for {test_spec.repo_image_key}\n."
                "Please build the base images first."
            ) from e

        if test_spec.base_image_key not in base_timestamps:
            base_timestamps[test_spec.base_image_key] = _get_image_created_timestamp(
                client, test_spec.base_image_key
            )

        image_exists = False
        try:
            client.images.get(test_spec.repo_image_key)
            image_exists = True
        except docker.errors.ImageNotFound:
            pass

        if image_exists:
            repo_ts = _get_image_created_timestamp(client, test_spec.repo_image_key)
            base_ts = base_timestamps[test_spec.base_image_key]
            if base_ts and repo_ts:
                from datetime import datetime

                try:
                    base_dt = datetime.fromisoformat(base_ts.replace("Z", "+00:00"))
                    repo_dt = datetime.fromisoformat(repo_ts.replace("Z", "+00:00"))
                    if base_dt > repo_dt:
                        _logger.warning(
                            "Repo image %s is stale (built %s, base rebuilt %s) — scheduling rebuild",
                            test_spec.repo_image_key,
                            repo_ts[:19],
                            base_ts[:19],
                        )
                        image_exists = False
                except (ValueError, TypeError):
                    _logger.debug(
                        "Could not parse timestamps for stale check on %s",
                        test_spec.repo_image_key,
                    )

        if not image_exists:
            image_scripts[test_spec.repo_image_key] = {
                "setup_script": test_spec.setup_script,
                "dockerfile": test_spec.repo_dockerfile,
                "platform": test_spec.platform,
            }
    return image_scripts


def build_repo_images(
    client: docker.DockerClient,
    dataset: list,
    dataset_type: str,
    max_workers: int = 4,
    verbose: int = 1,
) -> tuple[list[str], list[str]]:
    """Builds the repo images required for the dataset if they do not already exist.

    Args:
    ----
        client (docker.DockerClient): Docker client to use for building the images
        dataset (list): List of test specs or dataset to build images for
        dataset_type(str): The type of dataset. Choices are commit0 and swebench
        max_workers (int): Maximum number of workers to use for building images
        verbose (int): Level of verbosity

    Return:
    ------
        successful: a list of docker image keys for which build were successful
        failed: a list of docker image keys for which build failed

    """
    # Resolve MITM cert ONCE — consistent across all parallel builds
    mitm_ca_cert = _resolve_mitm_ca_cert()
    if mitm_ca_cert:
        _logger.info("MITM CA cert: %s", mitm_ca_cert)
    proxy_env = get_proxy_env()
    if proxy_env:
        _logger.info("Proxy env vars detected: %s", list(proxy_env.keys()))
    if mitm_ca_cert and not proxy_env:
        _logger.warning(
            "MITM CA cert found but no proxy env vars (http_proxy/https_proxy) "
            "are set. The cert will be installed but traffic won't route through a proxy."
        )

    build_base_images(client, dataset, dataset_type, mitm_ca_cert=mitm_ca_cert)
    configs_to_build = get_repo_configs_to_build(client, dataset, dataset_type)
    if len(configs_to_build) == 0:
        _logger.info("No repo images need to be built.")
        return [], []
    _logger.info("Total repo images to build: %d", len(configs_to_build))

    # Pre-create multiarch builder in main thread to avoid race in worker threads
    _multiarch_builder_args()

    successful, failed = list(), list()
    with tqdm(
        total=len(configs_to_build), smoothing=0, desc="Building repo images"
    ) as pbar:
        # P14: ThreadPoolExecutor here is safe wrt log files because each thread
        # gets a unique build_dir keyed by `image_name.replace(":", "__")`.
        # Image names are unique per-repo (dict keys from configs_to_build), so
        # `setup_logger(image_name, build_dir / "build_image.log")` inside each
        # thread writes to a distinct file. If future refactors reuse image
        # names across workers, add a per-image lock here.
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    build_image,
                    image_name,
                    {"setup.sh": config["setup_script"]},
                    config["dockerfile"],
                    config["platform"],
                    client,
                    REPO_IMAGE_BUILD_DIR / image_name.replace(":", "__"),
                    False,  # nocache
                    mitm_ca_cert,
                ): image_name
                for image_name, config in configs_to_build.items()
            }

            for future in as_completed(futures):
                pbar.update(1)
                try:
                    future.result()
                    successful.append(futures[future])
                except BuildImageError as e:
                    _logger.error("BuildImageError %s", e.image_name, exc_info=True)
                    failed.append(futures[future])
                    continue
                except Exception:
                    _logger.error(f"Error building image {futures[future]}")
                    traceback.print_exc()
                    failed.append(futures[future])
                    continue

    if len(failed) == 0:
        _logger.info("All repo images built successfully.")
    else:
        _logger.warning("%d repo images failed to build.", len(failed))

    return successful, failed


__all__ = []
