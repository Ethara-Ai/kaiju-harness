from enum import Enum
from pathlib import Path
from typing import Dict, ItemsView, KeysView
from pydantic import BaseModel

# Supported Python versions for per-version base Docker images
SUPPORTED_PYTHON_VERSIONS = {"3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "3.13"}

# Default Python version when a dataset entry has no `setup.python` field.
# Should only be used as a last-resort fallback — `prepare_repo.py` is
# responsible for populating this field for every entry. If you see this
# default being hit at runtime, something upstream skipped detection.
DEFAULT_PYTHON_VERSION = "3.12"

# Path to the directory containing per-version Dockerfile templates
DOCKERFILES_DIR = Path(__file__).parent / "dockerfiles"

# N28: centralized exit codes emitted by coreutils `timeout` (used by every
# language's eval script). Previously every language module (evaluate_rust.py,
# evaluate_go.py, etc.) inlined these numbers; centralize so a change to the
# timeout wrapper only edits one place.
#   124 = command hit the --time limit and got SIGTERM.
#   137 = 128 + 9 (SIGKILL, sent after --kill-after grace period).
#   143 = 128 + 15 (SIGTERM surfaced through an intermediate shell wrapper on
#         some configurations — same root cause as 124).
TIMEOUT_EXIT_SIGTERM = 124
TIMEOUT_EXIT_SIGKILL = 137
TIMEOUT_EXIT_SIGTERM_SHELL = 143
TIMEOUT_EXIT_CODES = (TIMEOUT_EXIT_SIGTERM, TIMEOUT_EXIT_SIGKILL, TIMEOUT_EXIT_SIGTERM_SHELL)


class RepoInstance(BaseModel):
    instance_id: str
    repo: str
    base_commit: str
    reference_commit: str
    setup: dict
    test: Dict[str, str]
    src_dir: str

    def __getitem__(self, item: str):
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def keys(self) -> KeysView[str]:
        """Return the field names of the model as dictionary keys."""
        return type(self).model_fields.keys()


class SimpleInstance(BaseModel):
    instance_id: str
    prompt: str
    canonical_solution: str
    test: str

    def __getitem__(self, item: str):
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def keys(self) -> KeysView[str]:
        """Return the field names of the model as dictionary keys."""
        return type(self).model_fields.keys()


class Files(BaseModel):
    eval_script: Dict[str, Path]
    patch: Dict[str, Path]

    def __getitem__(self, item: str):
        return getattr(self, item)

    def items(self) -> ItemsView[str, object]:
        """Using self.dict() to obtain the underlying data as a dictionary,
        which is then iterated to yield key-value pairs.
        """
        return self.model_dump().items()


BASE_BRANCH = "commit0"

import os as _os
from kaiju.paths import is_consolidated as _is_consolidated, build_logs_dir as _build_logs_dir


def _resolve_build_dir(subdir: str) -> Path:
    """Route build-image logs to outputs/<uuid>/build_logs/<subdir> when consolidated + KAIJU_EXPERIMENT_UUID set; legacy logs/build_images/<subdir> otherwise."""
    uuid = _os.environ.get("KAIJU_EXPERIMENT_UUID", "")
    if uuid and _is_consolidated():
        return _build_logs_dir(uuid) / subdir
    return Path(f"logs/build_images/{subdir}")


def base_image_build_dir() -> Path:
    return _resolve_build_dir("base")


def repo_image_build_dir() -> Path:
    return _resolve_build_dir("repo")


def oci_image_dir() -> Path:
    return _resolve_build_dir("oci")


BASE_IMAGE_BUILD_DIR = base_image_build_dir()
REPO_IMAGE_BUILD_DIR = repo_image_build_dir()
OCI_IMAGE_DIR = oci_image_dir()
RUN_PYTEST_LOG_DIR = Path("logs/pytest")
RUN_AGENT_LOG_DIR = Path("logs/agent")

# Constants - Test Types, Statuses, Commands
FAIL_TO_PASS = "FAIL_TO_PASS"
FAIL_TO_FAIL = "FAIL_TO_FAIL"
PASS_TO_PASS = "PASS_TO_PASS"
PASS_TO_FAIL = "PASS_TO_FAIL"

# Evaluation backends.
# - local          : create a fresh Docker container per eval (host orchestrates).
# - local_inplace  : run eval.sh in an isolated git worktree INSIDE the current
#                    process/container (no new container, no docker socket). Used
#                    by fully-containerized inference so the agent container scores
#                    itself without docker-in-docker. Reward-hack safe: the eval
#                    still reconstructs from the patch in a clean worktree and
#                    reverts test/manifest paths (see spec.make_eval_script_list).
EVAL_BACKENDS = ["local", "local_inplace", "modal", "e2b"]
# Use absolute for docker and modal. Backends with sudo access
ABSOLUTE_REPO_DIR = "/testbed"
# Use relative for e2b, with no sudo access
RELATIVE_REPO_DIR = "testbed"

# available commands
COMMANDS = [
    "clone",
    "build",
    "test",
    "test-reference",
    "get-tests",
    "evaluate",
    "evaluate-reference",
    "lint",
    "save",
]

# repo splits
SPLIT_LITE = [
    "tinydb",
    "simpy",
    "deprecated",
    "wcwidth",
    "voluptuous",
    "cachetools",
    "imapclient",
    "marshmallow",
    "jinja",
    "cookiecutter",
    "portalocker",
    "parsel",
    "pyjwt",
    "chardet",
    "babel",
    "minitorch",
]
# NOTE: The "all" subset is derived dynamically from the loaded dataset by
# ``commit0.harness.split_utils.resolve_split``. Only curated subsets
# (``lite``, ``ethara``, ``ethara-lite``) and individual repo names stay below.
# Individual SPLIT_XXX variables below stay hand-curated for fine-grained pinning.
SPLIT_MINITORCH = ["minitorch"]
SPLIT_SIMPY = ["simpy"]
SPLIT_STATSMODELS = ["statsmodels"]
SPLIT_PYTHON_PROGRESSBAR = ["python-progressbar"]
SPLIT_XARRAY = ["xarray"]
SPLIT_IMBALANCED_LEARN = ["imbalanced-learn"]
SPLIT_WEB3_PY = ["web3.py"]
SPLIT_SCRAPY = ["scrapy"]
SPLIT_SEABORN = ["seaborn"]
SPLIT_PYPDF = ["pypdf"]
SPLIT_PEXPECT = ["pexpect"]
SPLIT_PYTEST = ["pytest"]
SPLIT_PYLINT = ["pylint"]
SPLIT_JOBLIB = ["joblib"]
SPLIT_DULWICH = ["dulwich"]
SPLIT_VIRTUALENV = ["virtualenv"]
SPLIT_NETWORKX = ["networkx"]
SPLIT_REQUESTS = ["requests"]
SPLIT_SPHINX = ["sphinx"]
SPLIT_JEDI = ["jedi"]
SPLIT_MOVIEPY = ["moviepy"]
SPLIT_LOGURU = ["loguru"]
SPLIT_PARAMIKO = ["paramiko"]
SPLIT_GEOPANDAS = ["geopandas"]
SPLIT_BITSTRING = ["bitstring"]
SPLIT_FASTAPI = ["fastapi"]
SPLIT_CHARDET = ["chardet"]
SPLIT_TORNADO = ["tornado"]
SPLIT_PYTHON_PROMPT_TOOLKIT = ["python-prompt-toolkit"]
SPLIT_ATTRS = ["attrs"]
SPLIT_PYBOY = ["PyBoy"]
SPLIT_PYDANTIC = ["pydantic"]
SPLIT_FILESYSTEM_SPEC = ["filesystem_spec"]
SPLIT_TLSLITE_NG = ["tlslite-ng"]
SPLIT_GRAPHENE = ["graphene"]
SPLIT_MIMESIS = ["mimesis"]
SPLIT_BABEL = ["babel"]
SPLIT_DNSPYTHON = ["dnspython"]
SPLIT_PORTALOCKER = ["portalocker"]
SPLIT_COOKIECUTTER = ["cookiecutter"]
SPLIT_PYJWT = ["pyjwt"]
SPLIT_PYTHON_RSA = ["python-rsa"]
SPLIT_MORE_ITERTOOLS = ["more-itertools"]
SPLIT_CLICK = ["click"]
SPLIT_FABRIC = ["fabric"]
SPLIT_JINJA = ["jinja"]
SPLIT_FLASK = ["flask"]
SPLIT_SQLPARSE = ["sqlparse"]
SPLIT_MARSHMALLOW = ["marshmallow"]
SPLIT_IMAPCLIENT = ["imapclient"]
SPLIT_TINYDB = ["tinydb"]
SPLIT_CACHETOOLS = ["cachetools"]
SPLIT_VOLUPTUOUS = ["voluptuous"]
SPLIT_PARSEL = ["parsel"]
SPLIT_WCWIDTH = ["wcwidth"]
SPLIT_DEPRECATED = ["deprecated"]

# Ethara-Ai custom dataset splits (8 repos, strict Section 3.1 compliance)
# Criteria: >95% Python, native implementations, docs website, >90% coverage, <30min tests
SPLIT_ETHARA = [
    "rich",
    "black",
    "typer",
    "httpx",
    "starlette",
    "boto3",
    "arrow",
    "gspread",
]
SPLIT_ETHARA_LITE = [
    "arrow",
    "gspread",
    "typer",
    "httpx",
]
SPLIT_RICH = ["rich"]
SPLIT_BLACK = ["black"]
SPLIT_TYPER = ["typer"]
SPLIT_HTTPX = ["httpx"]
SPLIT_STARLETTE = ["starlette"]
SPLIT_BOTO3 = ["boto3"]
SPLIT_ARROW = ["arrow"]
SPLIT_GSPREAD = ["gspread"]

SPLIT = {
    "lite": SPLIT_LITE,
    "ethara": SPLIT_ETHARA,
    "ethara-lite": SPLIT_ETHARA_LITE,
    "statsmodels": SPLIT_STATSMODELS,
    "python-progressbar": SPLIT_PYTHON_PROGRESSBAR,
    "xarray": SPLIT_XARRAY,
    "imbalanced-learn": SPLIT_IMBALANCED_LEARN,
    "web3.py": SPLIT_WEB3_PY,
    "scrapy": SPLIT_SCRAPY,
    "seaborn": SPLIT_SEABORN,
    "pypdf": SPLIT_PYPDF,
    "pexpect": SPLIT_PEXPECT,
    "pytest": SPLIT_PYTEST,
    "pylint": SPLIT_PYLINT,
    "joblib": SPLIT_JOBLIB,
    "dulwich": SPLIT_DULWICH,
    "virtualenv": SPLIT_VIRTUALENV,
    "minitorch": SPLIT_MINITORCH,
    "networkx": SPLIT_NETWORKX,
    "requests": SPLIT_REQUESTS,
    "sphinx": SPLIT_SPHINX,
    "jedi": SPLIT_JEDI,
    "moviepy": SPLIT_MOVIEPY,
    "loguru": SPLIT_LOGURU,
    "paramiko": SPLIT_PARAMIKO,
    "geopandas": SPLIT_GEOPANDAS,
    "bitstring": SPLIT_BITSTRING,
    "fastapi": SPLIT_FASTAPI,
    "chardet": SPLIT_CHARDET,
    "tornado": SPLIT_TORNADO,
    "python-prompt-toolkit": SPLIT_PYTHON_PROMPT_TOOLKIT,
    "attrs": SPLIT_ATTRS,
    "PyBoy": SPLIT_PYBOY,
    "pydantic": SPLIT_PYDANTIC,
    "filesystem_spec": SPLIT_FILESYSTEM_SPEC,
    "tlslite-ng": SPLIT_TLSLITE_NG,
    "graphene": SPLIT_GRAPHENE,
    "mimesis": SPLIT_MIMESIS,
    "babel": SPLIT_BABEL,
    "dnspython": SPLIT_DNSPYTHON,
    "portalocker": SPLIT_PORTALOCKER,
    "cookiecutter": SPLIT_COOKIECUTTER,
    "pyjwt": SPLIT_PYJWT,
    "python-rsa": SPLIT_PYTHON_RSA,
    "more-itertools": SPLIT_MORE_ITERTOOLS,
    "simpy": SPLIT_SIMPY,
    "click": SPLIT_CLICK,
    "fabric": SPLIT_FABRIC,
    "jinja": SPLIT_JINJA,
    "flask": SPLIT_FLASK,
    "sqlparse": SPLIT_SQLPARSE,
    "marshmallow": SPLIT_MARSHMALLOW,
    "imapclient": SPLIT_IMAPCLIENT,
    "tinydb": SPLIT_TINYDB,
    "cachetools": SPLIT_CACHETOOLS,
    "voluptuous": SPLIT_VOLUPTUOUS,
    "parsel": SPLIT_PARSEL,
    "wcwidth": SPLIT_WCWIDTH,
    "deprecated": SPLIT_DEPRECATED,
    "rich": SPLIT_RICH,
    "black": SPLIT_BLACK,
    "typer": SPLIT_TYPER,
    "httpx": SPLIT_HTTPX,
    "starlette": SPLIT_STARLETTE,
    "boto3": SPLIT_BOTO3,
    "arrow": SPLIT_ARROW,
    "gspread": SPLIT_GSPREAD,
}


class ResolvedStatus(Enum):
    NO = "RESOLVED_NO"
    PARTIAL = "RESOLVED_PARTIAL"
    FULL = "RESOLVED_FULL"


class TestStatus(Enum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


# Constants - Logging
INSTALL_FAIL = ">>>>> Init Failed"
INSTALL_PASS = ">>>>> Init Succeeded"
INSTALL_TIMEOUT = ">>>>> Init Timed Out"
RESET_FAILED = ">>>>> Reset Failed"
TESTS_ERROR = ">>>>> Tests Errored"
TESTS_FAILED = ">>>>> Some Tests Failed"
TESTS_PASSED = ">>>>> All Tests Passed"
TESTS_TIMEOUT = ">>>>> Tests Timed Out"


# Constants - Miscellaneous
NON_TEST_EXTS = [
    ".json",
    ".png",
    ".csv",
    ".txt",
    ".md",
    ".jpg",
    ".jpeg",
    ".pkl",
    ".yml",
    ".yaml",
    ".toml",
]
