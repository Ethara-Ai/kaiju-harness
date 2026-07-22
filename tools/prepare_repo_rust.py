"""Prepare Rust repos for a commit0 dataset.

For each repo:
1. Fork to Zahgon GitHub org
2. Clone locally, record reference_commit (HEAD)
3. Create 'commit0_all' branch
4. Run ruststubber on source files
5. Commit stubbed version as base_commit
6. Push commit0_all branch to fork
7. Collect test IDs via cargo test --list
8. Save test IDs as .bz2
9. Append entry to rust_dataset.json
10. Generate per-repo YAML config in commit0/data/

Usage:
    python3 -m tools.prepare_repo_rust \
        --repo open-telemetry/opentelemetry-rust \
        --crate opentelemetry-http \
        --src-dir opentelemetry-http/src \
        --test-cmd "cargo test -p opentelemetry-http"

    # Dry run (no fork, no push):
    python3 -m tools.prepare_repo_rust \
        --repo serde-rs/serde \
        --crate serde \
        --src-dir serde/src \
        --test-cmd "cargo test -p serde" \
        --dry-run

Requires:
    - gh CLI installed (for forking)
    - ruststubber binary built at tools/ruststubber/target/release/ruststubber
    - cargo installed (for cargo test --list)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from kaiju.paths import datasets_dir
import uuid as _uuid_mod

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)
from commit0.harness.constants import REMOTE_BRANCH

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Paths
TOOLS_DIR = Path(__file__).parent
PROJECT_ROOT = TOOLS_DIR.parent
RUSTSTUBBER = TOOLS_DIR / "ruststubber" / "target" / "release" / "ruststubber"
RUSTSTUBBER_CRATE = TOOLS_DIR / "ruststubber"
SPECS_DIR = PROJECT_ROOT / "specs"


def _ensure_stubber_fresh() -> None:
    """Rebuild the ruststubber if its binary is missing or STALE relative to the
    crate source.

    A stale binary silently corrupts the benchmark: e.g. a doc-strip fix that
    lives in the source but not the compiled artifact leaves the agent looking at
    the crate's full documentation (answer leak) while the run looks normal. We
    compare the binary mtime against the newest `.rs`/`Cargo.toml` under the
    crate and `cargo build --release` when the source is newer (or the binary is
    absent). Raises RuntimeError if the rebuild fails — better to stop than to
    prep a whole dataset with a broken stubber.
    """
    src_root = RUSTSTUBBER_CRATE
    newest_src = 0.0
    for f in list(src_root.rglob("*.rs")) + [src_root / "Cargo.toml", src_root / "Cargo.lock"]:
        try:
            if "target" in f.parts:
                continue
            newest_src = max(newest_src, f.stat().st_mtime)
        except OSError:
            continue
    bin_mtime = RUSTSTUBBER.stat().st_mtime if RUSTSTUBBER.exists() else -1.0
    if RUSTSTUBBER.exists() and bin_mtime >= newest_src:
        return  # up to date
    reason = "missing" if not RUSTSTUBBER.exists() else "stale (source newer than binary)"
    logger.info("ruststubber binary %s — rebuilding (cargo build --release)…", reason)
    try:
        r = subprocess.run(
            ["cargo", "build", "--release"],
            cwd=str(src_root), capture_output=True, text=True, timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"failed to invoke cargo to rebuild ruststubber: {e}") from e
    if r.returncode != 0 or not RUSTSTUBBER.exists():
        raise RuntimeError(
            "ruststubber rebuild failed — refusing to prep with a stale/missing "
            f"stubber.\nstderr:\n{r.stderr[-2000:]}"
        )
    logger.info("ruststubber rebuilt: %s", RUSTSTUBBER)

DEFAULT_ORG = "Zahgon"


# ─── Git Helpers ──────────────────────────────────────────────────────────────
# git(), fork_repo(), push_to_fork() are imported from tools._git_auth
# (the single source of truth for all prepare_repo_* pipelines).


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


# ─── Fork & Clone ────────────────────────────────────────────────────────────


def clone_repo(full_name: str, clone_dir: Path) -> Path:
    """Full clone of a repo. Returns repo dir."""
    repo_name = full_name.split("/")[-1]
    repo_dir = clone_dir / repo_name

    if repo_dir.exists():
        # A reused staging clone is left on the stubbed `commit0_all` branch from
        # a prior prep. If we return it as-is, `reference_commit = HEAD` records a
        # STUB as the gold solution, and each re-prep chains its reference off the
        # previous stubbed base (observed: ref SHAs walking forward every run).
        # Reset it to a PRISTINE upstream default-branch state so reference_commit
        # is always the real implementation, making re-prep idempotent.
        logger.info("Clone already exists: %s — resetting to pristine default branch", repo_dir)
        try:
            git(repo_dir, "fetch", "origin", "--prune")
            default_branch = get_default_branch(repo_dir)
            git(repo_dir, "checkout", "-f", default_branch)
            git(repo_dir, "reset", "--hard", f"origin/{default_branch}")
            git(repo_dir, "clean", "-fdx")
            # Drop any local commit0_all so the later checkout -b starts clean.
            try:
                git(repo_dir, "branch", "-D", REMOTE_BRANCH)
            except subprocess.CalledProcessError:
                pass
        except subprocess.CalledProcessError as e:
            # If the reset fails, a stale clone is worse than a fresh one — re-clone.
            logger.warning("Could not reset reused clone (%s); re-cloning fresh.", e)
            shutil.rmtree(repo_dir, ignore_errors=True)
        else:
            return repo_dir

    url = f"https://github.com/{full_name}.git"
    logger.info("Cloning %s...", full_name)
    result = subprocess.run(
        ["git", "clone", url, str(repo_dir)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone failed (exit {result.returncode}) for {full_name}:\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return repo_dir


# ─── Stubbing ────────────────────────────────────────────────────────────────


def stub_source_dir(repo_dir: Path, src_dir_relative: str, strip_docs: bool = True) -> tuple[int, int]:
    """Stub all .rs files in src_dir using ruststubber --in-place.

    The ruststubber binary walks the directory, skips target/ directories,
    stubs .rs files, and copies non-.rs files unchanged. Returns (success_count, fail_count).

    `strip_docs` defaults to True so the agent sees only signatures, non-doc
    attributes, test code, and stub bodies. Pass strip_docs=False (which maps
    to `--keep-docs` on the binary) only when upstream doc comments must
    survive in the stubbed output.
    """
    src_dir = repo_dir / src_dir_relative
    if not src_dir.is_dir():
        logger.error("Source directory not found: %s", src_dir)
        return 0, 0

    logger.info("Running ruststubber --in-place on %s (strip_docs=%s)", src_dir_relative, strip_docs)

    def _doc_comment_count(d: Path) -> int:
        n = 0
        for f in d.rglob("*.rs"):
            if "target" in f.parts:
                continue
            try:
                for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = ln.lstrip()
                    if s.startswith("///") or s.startswith("//!") or s.startswith("#[doc"):
                        n += 1
            except OSError:
                pass
        return n

    # Guarantee we run a FRESH stubber: a stale binary silently leaks the crate's
    # docs (answer leak) even though the source + tests are correct. Rebuild if
    # the binary is older than the crate source.
    _ensure_stubber_fresh()

    docs_before = _doc_comment_count(src_dir) if strip_docs else 0

    _rss_timeout = int(os.environ.get("KAIJU_RUSTSTUBBER_TIMEOUT_SEC", "120"))
    try:
        result = subprocess.run(
            [str(RUSTSTUBBER), "--input-dir", str(src_dir), "--in-place"] + ([] if strip_docs else ["--keep-docs"]),
            capture_output=True,
            text=True,
            timeout=_rss_timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("ruststubber timed out after %ds on %s (override via KAIJU_RUSTSTUBBER_TIMEOUT_SEC)", _rss_timeout, src_dir_relative)
        return 0, 1

    ok, fail = 0, 0
    for line in result.stderr.splitlines():
        if line.startswith("ruststubber:"):
            m_ok = re.search(r"(\d+)\s+(?:files?\s+)?stubbed", line)
            m_err = re.search(r"(\d+)\s+errors?", line)
            if m_ok:
                ok = int(m_ok.group(1))
            if m_err:
                fail = int(m_err.group(1))

    if result.returncode != 0:
        logger.warning(
            "ruststubber exited with code %d: %s",
            result.returncode,
            result.stderr.strip(),
        )

    logger.info("Stubbed %d files (%d errors)", ok, fail)

    # A2: verify strip_docs actually stripped. A silent no-strip (observed on
    # rust-raknet: 179 doc comments before AND after) hands the agent upstream
    # docs + examples it was meant to be denied, changing task difficulty with no
    # flag. Fail prep loudly so the dataset is never built on a mis-stubbed tree.
    if strip_docs and docs_before > 0:
        docs_after = _doc_comment_count(src_dir)
        if docs_after >= docs_before:
            raise RuntimeError(
                f"strip_docs requested but doc comments were NOT removed in {src_dir_relative} "
                f"({docs_before} before, {docs_after} after). The ruststubber binary at "
                f"{RUSTSTUBBER} may be stale or built without --keep-docs support. "
                f"Rebuild it (cd tools/ruststubber && cargo build --release) and re-run, "
                f"or pass --keep-docs intentionally."
            )
        if docs_after > 0:
            remaining_frac = docs_after / docs_before
            # A near-total no-op (the stale-binary symptom, e.g. mdns-sd's
            # 1191/1192) should be LOUD even though it's technically > 0 stripped.
            # We don't hard-fail on a partial strip because docs inside unknown
            # macros can legitimately survive, but >50% remaining after a fresh
            # rebuild is almost always a real bug worth surfacing at ERROR.
            level = logging.ERROR if remaining_frac > 0.5 else logging.WARNING
            logger.log(
                level,
                "strip_docs: %d/%d doc comments REMAIN (%.0f%%) in %s after a fresh "
                "stubber build. >50%% remaining usually means a stubbing bug (docs on "
                "item kinds the visitor misses, or docs inside unknown macros). The "
                "agent will see these docs — task difficulty is reduced.",
                docs_after, docs_before, remaining_frac * 100, src_dir_relative,
            )

    return ok, fail


def _stubbed_base_compiles(repo_dir: Path, timeout: int = 600) -> "bool | None":
    """A11: check whether the STUBBED base tree compiles.

    A correctly-stubbed crate replaces function bodies with `panic!("STUB: ...")`
    (which diverges to `!` and TYPECHECKS anywhere) — so the base should compile.
    If it does not, the agent starts from a broken tree and any 0% score is an
    impossible-task / infra artifact, not a model failure.

    Two prep-only pitfalls this probe must NOT misattribute to the stubber:

    * ``deny`` LINTS. The stubber's contract strips doc comments (agent sees only
      signatures + stub bodies). Crates with ``#![deny(missing_docs)]`` (byteorder)
      — or ``#![cfg_attr(test, deny(rust_2018_idioms, ...))]`` (concurrent-map) —
      then FAIL to compile purely because a lint fires on the now-undocumented /
      idiom-flagged code. That is not a broken stub. We pass ``--cap-lints=warn``
      via RUSTFLAGS so every lint (incl. deny/forbid) is downgraded to a warning,
      matching the intent of the gate (does the stub TYPECHECK) without editing the
      source the agent sees.

    * FEATURE SCOPE. The scored test command is plain ``cargo test`` /
      ``cargo test -p <crate>`` (DEFAULT features). Probing with ``--all-features``
      is stricter than what's ever scored and can enable a latently-broken optional
      feature (observed: concurrent-map's ``timing`` feature references
      ``AtomicU64`` with no import — it fails to compile in the PRISTINE upstream
      too, and only ``--all-features`` surfaces it). So we probe with DEFAULT
      features to mirror the real evaluation.

    Returns True (compiles), False (does not), or None (couldn't determine —
    cargo missing, timeout, network, etc.) so the caller can record provenance.
    """
    import shutil as _shutil
    import os as _os
    if _shutil.which("cargo") is None:
        # F2c: fail-fast — prep host is expected to have cargo (bootstrap_ec2.sh
        # installs it). Silently skipping shipped un-verified stubs to the dataset,
        # which downstream agents then fail on cryptically. Escape hatch preserves
        # old behavior for one-off dev machines that intentionally lack the toolchain.
        if _os.environ.get("KAIJU_PREPARE_ALLOW_MISSING_TOOLCHAIN") == "1":
            logger.warning("A11: cargo not on PATH; SKIPPING stubbed-base compile check "
                           "(KAIJU_PREPARE_ALLOW_MISSING_TOOLCHAIN=1). The dataset may "
                           "contain uncompilable stubs — do NOT ship to production.")
            return None
        raise RuntimeError(
            "prepare_repo_rust: cargo not on PATH. Prep host must have the Rust "
            "toolchain installed to verify stubbed base compiles. Install via "
            "scripts/bootstrap_ec2.sh, or set KAIJU_PREPARE_ALLOW_MISSING_TOOLCHAIN=1 "
            "to skip (dataset quality will degrade)."
        )
    # Downgrade deny/forbid lints to warnings so a legitimately doc-stripped stub
    # isn't branded broken by a `deny(missing_docs)`-style lint (append so we don't
    # clobber operator-set RUSTFLAGS).
    _env = dict(_os.environ)
    _env["RUSTFLAGS"] = (_env.get("RUSTFLAGS", "") + " --cap-lints=warn").strip()
    try:
        proc = subprocess.run(
            # DEFAULT features (mirror the scored `cargo test`), NOT --all-features.
            ["cargo", "check", "--tests", "--message-format=short"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("A11: stubbed-base compile check could not run (%s); recording unknown.", e)
        return None
    if proc.returncode == 0:
        return True
    # A non-zero exit is NOT necessarily "base doesn't compile" — a network/
    # registry failure (deps not yet fetched at prep time) also exits non-zero.
    # Distinguish that as "unknown" (None) so a transient prep-time network blip
    # doesn't permanently brand a perfectly good crate as impossible-to-solve.
    _combined = (proc.stderr or "") + (proc.stdout or "")
    _net_markers = (
        "failed to download", "failed to fetch", "failed to get",
        "could not resolve host", "network failure", "spurious network error",
        "failed to load source for dependency", "error: failed to load source",
        "unable to get packages", "no matching package",
    )
    if any(m in _combined.lower() for m in _net_markers):
        logger.warning(
            "A11: stubbed-base compile check hit a fetch/network error (not a real "
            "compile failure); recording unknown for %s.", repo_dir,
        )
        return None
    return False


# ─── Spec Scraping ───────────────────────────────────────────────────────────


def scrape_spec(crate: str, repo_dir: Path) -> Path | None:
    """Scrape docs.rs documentation for a crate into a compressed PDF.

    Places spec.pdf.bz2 at the repo root. Returns the path on success, None
    on failure. Delegates to the shared tools.scrape_pdf crawler, which has
    Fixes 1/4/5 applied (transient-error retry, min-page threshold,
    parse-vs-network error classification). Deps (playwright + PyMuPDF +
    PyPDF2 + beautifulsoup4) must be pre-installed via
    ``scripts/bootstrap_ec2.sh`` or ``uv sync``; a missing dep surfaces as
    an ImportError, which is the correct fail-fast behavior for a batch
    where the operator needs to know infrastructure is broken (rather than
    silently proceeding with README fallback and degrading dataset quality).
    """
    from tools.scrape_pdf import scrape_spec as _shared_scrape_spec

    tmp_specs = repo_dir / "_spec_tmp"
    try:
        result = _shared_scrape_spec(
            base_url=f"https://docs.rs/{crate}/latest/{crate}/",
            name=crate,
            output_dir=str(tmp_specs),
            compress=True,
        )
        if not result:
            logger.warning("Spec scraping produced no output for %s", crate)
            return None

        src_path = Path(result)
        dest_path = repo_dir / "spec.pdf.bz2"
        shutil.move(str(src_path), str(dest_path))
        logger.info("Spec placed at repo root: %s", dest_path.name)
        return dest_path
    except ImportError as e:
        logger.error(
            "Spec-scrape deps missing (%s). Run `scripts/bootstrap_ec2.sh` "
            "or `uv sync` to install playwright/PyMuPDF/PyPDF2/beautifulsoup4.", e,
        )
        return None
    except Exception as e:
        logger.warning("Spec scraping failed for %s: %s", crate, e)
        return None
    finally:
        if tmp_specs.exists():
            shutil.rmtree(tmp_specs, ignore_errors=True)


# ─── Dataset Entry ───────────────────────────────────────────────────────────


def create_dataset_entry(
    upstream: str,
    fork_name: str,
    crate: str,
    src_dir: str,
    test_cmd: str,
    base_commit: str,
    reference_commit: str,
    rust_version: str = "stable",
    edition: str = "2021",
    packages: str = "pkg-config libssl-dev",
    specification: str = "",
    version_source: str = "default",
    version_conflicts: list[str] | None = None,
    spec_source: str = "unknown",
    repo_dir: "Path | None" = None,
    base_compiles: "bool | None" = None,
) -> dict:
    """Create a dataset entry compatible with RustRepoInstance."""
    # Derive test_dir from src_dir layout:
    #   - workspace member (e.g. 'foo/src')       -> 'foo' (the member dir)
    #   - single-crate (src_dir == 'src')          -> 'tests' (Rust integration-test convention)
    # The harness uses test_dir as a hash key for log paths and (for
    # workspace members) as the cd target for test runs. Falling back to the
    # crate name was wrong: it would create paths like logs/<crate>/<crate>/
    # and break workspace-member assumptions.
    if "/src" in src_dir:
        test_dir = src_dir.rsplit("/src", 1)[0]
        crate_root = test_dir
    else:
        test_dir = "tests"
        crate_root = "."
    # A6: don't FABRICATE test_dir="tests" for an in-src crate (tests live in
    # src/*.rs as `#[cfg(test)]`, no tests/ dir exists). A non-existent cd target
    # silently breaks test runs / log paths. When we can see the repo, probe for a
    # real integration-test dir and fall back to the crate root if it's absent.
    if repo_dir is not None:
        candidate = (repo_dir / test_dir) if test_dir != "tests" else (repo_dir / "tests")
        if not candidate.is_dir():
            logger.info(
                "A6: no '%s' directory in repo — tests are likely in-src; "
                "using crate root %r as test_dir instead of a fabricated 'tests'.",
                candidate.name, crate_root,
            )
            test_dir = crate_root


    return {
        "instance_id": f"commit-0/{crate}",
        "id": str(_uuid_mod.uuid4()),
        "repo": fork_name,
        "original_repo": upstream,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        # A11 provenance at the TOP LEVEL (consumers read e["base_compiles"], like
        # go/python/c/java) — kept in `setup` too for backward compat. Without this
        # top-level copy the rust A11 signal was invisible to run_trajectory.sh etc.
        "base_compiles": base_compiles,
        "setup": {
            "rust_version": rust_version,
            "edition": edition,
            "packages": packages,
            "pre_install": [],
            "install": "cargo fetch",
            "specification": specification,
            # A9: provenance of the spec. A silent docs.rs->README fallback
            # materially changes task difficulty; record it so the dataset (and
            # any cross-crate comparison) makes the difference visible instead of
            # pretending every instance got full API docs.
            "spec_source": spec_source,
            "version_source": version_source,
            "version_conflicts": version_conflicts or [],
            # A11: provenance of the stubbed-base compile check. None = not checked
            # (cargo missing/timeout); False = base does NOT compile (task likely
            # impossible — a 0% here is infra, not a model failure); True = clean.
            "base_compiles": base_compiles,
        },
        "test": {
            "test_cmd": test_cmd,
            "test_dir": test_dir,
        },
        "src_dir": src_dir,
        "language": "rust",
    }


def get_dataset_path(repo_name: str) -> Path:
    """Return the per-repo dataset file path: PROJECT_ROOT/<reponame>_rust_dataset.json."""
    return PROJECT_ROOT / f"{repo_name}_dataset.json"


def append_to_dataset(entry: dict, repo_name: str) -> Path:
    """Write entry to <reponame>_dataset.json in project root.

    Returns the dataset file path.
    """
    dataset_file = get_dataset_path(repo_name)

    existing = []
    if dataset_file.exists():
        raw = dataset_file.read_text().strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, list):
                existing = data
            elif isinstance(data, dict):
                existing = [data]

    # Remove existing entry with same instance_id (update in place)
    existing = [e for e in existing if e.get("instance_id") != entry["instance_id"]]
    existing.append(entry)

    content = json.dumps(existing, indent=2) + "\n"
    dataset_file.write_text(content)
    logger.info("Updated %s (%d entries)", dataset_file, len(existing))

    entries_file = PROJECT_ROOT / f"{repo_name}_entries.json"
    entries_file.write_text(content)
    logger.info("Updated %s", entries_file)

    return dataset_file


# ─── Per-Repo YAML Config ───────────────────────────────────────────────────


def generate_commit0_yaml(crate: str, repo_name: str, entry: dict) -> Path:
    """Generate .commit0_rust.yaml in project root (single config file, overwritten each run)."""
    yaml_path = PROJECT_ROOT / ".commit0_rust.yaml"
    dataset_file = f"./{repo_name}_dataset.json"

    content = f"""# commit0 Rust config for {crate}
dataset_name: {dataset_file}
dataset_split: test
repo_split: all
base_dir: repos

# Repo details
# upstream: {entry["original_repo"]}
# fork: {entry["repo"]}
# crate: {crate}
# language: rust
# test_cmd: {entry["test"]["test_cmd"]}
# src_dir: {entry["src_dir"]}
"""
    yaml_path.write_text(content)
    logger.info("Generated config: %s", yaml_path)
    return yaml_path


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def _capture_rust_test_ids(repo_dir: Path, test_cmd: str, repo_basename: str) -> None:
    """Capture the canonical Rust test inventory via `cargo test --list` on the
    stubbed base (which A11 verified compiles — `--list` compiles the test targets
    against the stub signatures without running them) and save it to
    ``commit0/data/rust_test_ids/<repo_basename>.bz2``. Best-effort: any failure
    just leaves the evaluator to fall back to the observed count.
    """
    try:
        from tools.generate_test_ids_rust import (
            collect_test_ids_local,
            save_test_ids,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Step 7/8: could not import test-id capture (%s); skipping.", e)
        return
    out_dir = Path(__file__).resolve().parent.parent / "commit0" / "data" / "rust_test_ids"

    def _warn_if_stale_remains(reason: str) -> None:
        # Staleness guard: on a FAILED/empty capture during a RE-prep, an
        # inventory from a PRIOR prep may still sit on disk under the normalized
        # key. If the repo's tests changed, that .bz2 is now a stale denominator
        # the evaluator would silently trust. We do NOT delete it (that would drop
        # the denominator to the observed count), but we surface it loudly so the
        # operator can decide whether to purge it before scoring a large batch.
        try:
            from kaiju.paths import normalize_test_ids_key
            prior = out_dir / f"{normalize_test_ids_key(repo_basename)}.bz2"
            if prior.exists():
                logger.error(
                    "Step 7/8: capture %s for %s, but a PRIOR inventory still exists at "
                    "%s. If the repo's tests changed since it was written it is now a "
                    "STALE denominator — delete it before scoring if unsure.",
                    reason, repo_basename, prior,
                )
        except Exception:  # noqa: BLE001
            pass

    try:
        ids = collect_test_ids_local(repo_dir, test_cmd)
    except Exception as e:  # noqa: BLE001
        logger.warning("Step 7/8: `cargo test --list` failed for %s (%s); the "
                       "evaluator will use the observed test count.", repo_basename, e)
        _warn_if_stale_remains("FAILED")
        return
    if not ids:
        logger.warning("Step 7/8: no test IDs discovered for %s; the evaluator "
                       "will use the observed test count.", repo_basename)
        _warn_if_stale_remains("found no tests")
        return
    try:
        path = save_test_ids(ids, repo_basename, out_dir)
        logger.info("Step 8: saved %d canonical test IDs -> %s", len(ids), path)
    except Exception as e:  # noqa: BLE001
        logger.warning("Step 8: failed to save test IDs for %s (%s).", repo_basename, e)


def prepare_rust_repo(
    upstream: str,
    crate: str,
    src_dir: str,
    test_cmd: str,
    org: str = DEFAULT_ORG,
    clone_dir: Path | None = None,
    dry_run: bool = False,
    rust_version: str = "stable",
    edition: str = "2021",
    packages: str = "pkg-config libssl-dev",
    skip_spec: bool = False,
    specs_dir: Path = SPECS_DIR,
    strip_docs: bool = True,
) -> dict | None:
    """Run the full preparation pipeline for a single Rust repo/crate.

    Returns the dataset entry dict on success, None on failure.
    """
    repo_name = upstream.split("/")[-1]

    if clone_dir is None:
        clone_dir = Path("repos_staging")

    logger.info("=" * 60)
    logger.info("Preparing: %s (crate: %s)", upstream, crate)
    logger.info("=" * 60)

    # Step 1: Fork
    if dry_run:
        fork_name = f"{org}/{repo_name}"
        logger.info("[DRY RUN] Would fork %s to %s", upstream, org)
    else:
        fork_name = fork_repo(upstream, org)

    # Step 2: Clone (from fork so we can push)
    repo_dir = clone_repo(fork_name, clone_dir)

    # Detect rust toolchain + edition from the cloned repo. CLI kwargs win
    # only when the user explicitly overrode the defaults; otherwise the
    # detected values flow through.
    from tools.rust_version import detect as _detect_rust

    det = _detect_rust(repo_dir)
    detected_version = det.version or rust_version
    detected_edition = det.edition
    version_source = det.source
    version_conflicts = det.conflicts
    if rust_version == "stable":  # only override on default
        rust_version = detected_version
    if edition == "2021":  # only override on default
        edition = detected_edition
    logger.info(
        "Rust detection: version=%s (src=%s) edition=%s (src=%s) conflicts=%s",
        rust_version,
        version_source,
        edition,
        det.edition_source,
        det.conflicts or "(none)",
    )

    # Step 3: Record reference commit
    reference_commit = get_head_sha(repo_dir)
    logger.info("Reference commit: %s", reference_commit[:12])

    # Step 4: Create commit0_all branch
    default_branch = get_default_branch(repo_dir)
    try:
        git(repo_dir, "checkout", "-b", REMOTE_BRANCH)
    except subprocess.CalledProcessError:
        # Branch may already exist
        git(repo_dir, "checkout", REMOTE_BRANCH)
        git(repo_dir, "reset", "--hard", default_branch)

    # Step 5: Stub source files
    ok, fail = stub_source_dir(repo_dir, src_dir, strip_docs=strip_docs)
    if ok == 0:
        logger.error("No files were stubbed. Aborting.")
        return None

    # Step 7: Commit
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-m", f"Commit 0: stub {crate} source")
    base_commit = get_head_sha(repo_dir)
    logger.info("Base commit (stubbed): %s", base_commit[:12])

    # Step 7.1: A11 — verify the stubbed base compiles. A broken base makes the
    # task impossible; record provenance so a resulting 0% isn't read as a real
    # model failure.
    base_compiles = _stubbed_base_compiles(repo_dir)
    if base_compiles is False:
        logger.warning(
            "A11: STUBBED BASE DOES NOT COMPILE for %s — the agent would start from a "
            "broken tree; recording base_compiles=false (any 0%% here is infra, not model).",
            crate,
        )
    elif base_compiles is True:
        logger.info("A11: stubbed base compiles cleanly for %s.", crate)

    # Step 7.2 / 8: capture the canonical test inventory (`cargo test --list`) and
    # save it as commit0/data/rust_test_ids/<repo>.bz2 — the AUTHORITATIVE
    # denominator the evaluator uses. Without it the evaluator falls back to the
    # observed count, which (a) mis-reports a timeout-truncated run and (b) lets a
    # model inflate its score by adding passing tests. Keyed by the repo basename
    # (what evaluate_rust looks up).
    _capture_rust_test_ids(repo_dir, test_cmd, upstream.split("/")[-1])

    # Step 7.5: Scrape spec PDF
    spec_filename = ""
    readme_spec_url = ""
    spec_path = None
    if not skip_spec:
        spec_path = scrape_spec(crate, repo_dir)
        if spec_path:
            spec_filename = spec_path.name
            git(repo_dir, "add", spec_filename)
            git(repo_dir, "commit", "-m", f"Add {crate} API spec (docs.rs PDF)")
            # Save a local copy under specs/ with the CRATE-NAMED filename that
            # copy_inference_inputs looks for (specs/<crate>.pdf.bz2) — mirroring
            # python. The old generic 'spec.pdf.bz2' matched none of the staging
            # lookup paths (so datasets/ lacked <repo>_spec.pdf.bz2) AND was
            # overwritten by every repo in a batch. The repo-root copy stays the
            # agent-canonical 'spec.pdf.bz2'.
            specs_dir.mkdir(parents=True, exist_ok=True)
            local_spec = specs_dir / f"{crate}.pdf.bz2"
            shutil.copy2(str(spec_path), str(local_spec))
            logger.info("Local spec copy: %s", local_spec)
            base_commit = get_head_sha(repo_dir)  # include spec PDF in agent's branching point
        else:
            if not dry_run:
                try:
                    from tools.scrape_pdf import (
                        scrape_readme_spec as _scrape_readme_spec,
                    )

                    readme_spec_path, readme_spec_url = _scrape_readme_spec(
                        repo_dir, specs_dir, crate
                    )
                except ImportError:
                    readme_spec_path = None
                if readme_spec_path:
                    try:
                        git(repo_dir, "checkout", REMOTE_BRANCH)
                        shutil.copy2(
                            str(readme_spec_path), str(repo_dir / "spec.pdf.bz2")
                        )
                        git(repo_dir, "add", "spec.pdf.bz2")
                        git(
                            repo_dir,
                            "commit",
                            "-m",
                            f"Add README-based spec for {crate}",
                        )
                        base_commit = get_head_sha(repo_dir)
                        spec_filename = "spec.pdf.bz2"
                        logger.info("  README spec committed")
                    except Exception as e:
                        logger.warning("  README spec fallback failed: %s", e)
    else:
        logger.info("Skipping spec generation (--skip-spec)")

    # Step 8: Push
    if dry_run:
        logger.info("[DRY RUN] Would push commit0_all to %s", fork_name)
    else:
        try:
            push_to_fork(repo_dir, fork_name, REMOTE_BRANCH, remote_name="origin")
        except Exception as e:
            raise RuntimeError(
                f"Push to {fork_name} FAILED — the container build clones this fork "
                f"and fetches base/reference commits from it, so an un-pushed dataset "
                f"is UNBUILDABLE ('not our ref'). Ensure your token has WRITE access "
                f"to the fork org (run_trajectory.sh: --org / $KAIJU_FORK_ORG).\n"
                f"Original push error: {e}"
            ) from e

    # Step 9: Test ID collection removed — use tools/generate_test_ids_rust.py separately

    # Step 10: Create dataset entry
    _final_spec_source = "readme" if readme_spec_url else ("docs.rs" if spec_path else "none")
    from tools.scrape_pdf import enforce_strict_spec_mode as _enforce_strict_spec
    _enforce_strict_spec(_final_spec_source, crate)

    entry = create_dataset_entry(
        upstream=upstream,
        fork_name=fork_name,
        crate=crate,
        src_dir=src_dir,
        test_cmd=test_cmd,
        base_commit=base_commit,
        reference_commit=reference_commit,
        rust_version=rust_version,
        edition=edition,
        version_source=version_source,
        version_conflicts=version_conflicts,
        packages=packages,
        specification=readme_spec_url or f"https://docs.rs/{crate}",
        spec_source=("readme" if readme_spec_url else ("docs.rs" if spec_path else "none")),
        repo_dir=repo_dir,
        base_compiles=base_compiles,
    )

    if not dry_run:
        append_to_dataset(entry, repo_name)
    else:
        logger.info("[DRY RUN] Dataset entry:\n%s", json.dumps(entry, indent=2))

    # Step 11: Generate .commit0.yaml
    if not dry_run:
        generate_commit0_yaml(crate, repo_name, entry)
    else:
        logger.info("[DRY RUN] Would generate .commit0.yaml")

    logger.info("=" * 60)
    logger.info("SUCCESS: %s prepared", crate)
    logger.info("  fork:       %s", fork_name)
    logger.info("  reference:  %s", reference_commit[:12])
    logger.info("  base:       %s", base_commit[:12])
    logger.info("  stubbed:    %d files", ok)
    logger.info("=" * 60)

    return entry


def _fetch_cargo_toml(upstream: str, sub_path: str = "") -> str | None:
    import urllib.request

    suffix = f"/{sub_path.strip('/')}" if sub_path else ""
    for branch in ("main", "master"):
        url = (
            f"https://raw.githubusercontent.com/{upstream}/{branch}{suffix}/Cargo.toml"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kaiju-prepare"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return r.read().decode("utf-8")
        except Exception:
            continue
    return None


def _derive_rust_defaults(upstream: str) -> dict:
    short = upstream.split("/")[-1]
    fallback = {"crate": short, "src_dir": "src", "test_cmd": "cargo test"}
    raw = _fetch_cargo_toml(upstream)
    if not raw:
        return fallback
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        data = tomllib.loads(raw)
    except Exception:
        return fallback

    if "package" in data and "name" in data["package"]:
        return {
            "crate": data["package"]["name"],
            "src_dir": "src",
            "test_cmd": "cargo test",
        }

    if "workspace" in data:
        members = data["workspace"].get("members") or []
        for member in members:
            if "*" in member:
                continue
            sub_raw = _fetch_cargo_toml(upstream, sub_path=member)
            if not sub_raw:
                continue
            try:
                sub_data = tomllib.loads(sub_raw)
            except Exception:
                continue
            if "package" in sub_data and "name" in sub_data["package"]:
                crate = sub_data["package"]["name"]
                return {
                    "crate": crate,
                    "src_dir": f"{member.rstrip('/')}/src",
                    "test_cmd": f"cargo test -p {crate}",
                }
    return fallback


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a Rust repo for commit0 dataset"
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Single repo to prepare (owner/name, e.g. dtolnay/syn).",
    )
    parser.add_argument(
        "--upstream",
        default=None,
        help="Alias for --repo (kept for backwards compatibility).",
    )
    parser.add_argument(
        "--output",
        default="dataset_entries_rust.json",
        help="Output JSON file (default: dataset_entries.json)",
    )
    parser.add_argument(
        "--crate",
        default=None,
        help="Crate name to stub. Auto-detected from Cargo.toml if omitted.",
    )
    parser.add_argument(
        "--src-dir",
        default=None,
        help="Source dir relative to repo root. Defaults to 'src' (or '<member>/src' for workspaces).",
    )
    parser.add_argument(
        "--test-cmd",
        default=None,
        help="Test command. Defaults to 'cargo test' (or 'cargo test -p <crate>' for workspaces).",
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("repos_staging"),
        help="Directory for local clones (default: ./repos_staging)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path("specs"),
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip fork, push, and dataset writes",
    )
    parser.add_argument(
        "--rust-version",
        default="stable",
        help="Rust version for setup (default: stable)",
    )
    parser.add_argument(
        "--edition",
        default="2021",
        help="Rust edition (default: 2021)",
    )
    parser.add_argument(
        "--packages",
        default="pkg-config libssl-dev",
        help="System packages needed (default: pkg-config libssl-dev)",
    )
    parser.add_argument(
        "--skip-spec",
        action="store_true",
        help="Skip scraping docs.rs spec PDF",
    )
    parser.add_argument(
        "--keep-docs",
        action="store_true",
        help=(
            "Preserve upstream doc comments, //! inner module docs, "
            "#[doc=...] attributes, and ordinary // / /* */ comments in the "
            "stubbed output. Default is to strip them so the agent sees only "
            "signatures, non-doc attributes, test code, and stub bodies."
        ),
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

    if args.repo is None:
        args.repo = args.upstream
    if not args.repo:
        parser.error("--repo is required")

    setup_git_credentials(dry_run=args.dry_run)

    if not all([args.crate, args.src_dir, args.test_cmd]):
        derived = _derive_rust_defaults(args.repo)
        if not args.crate:
            args.crate = derived["crate"]
        if not args.src_dir:
            args.src_dir = derived["src_dir"]
        if not args.test_cmd:
            args.test_cmd = derived["test_cmd"]
        logger.info(
            "Resolved Rust args — crate=%s src_dir=%s test_cmd=%r",
            args.crate,
            args.src_dir,
            args.test_cmd,
        )

    if not RUSTSTUBBER.exists():
        logger.error(
            "ruststubber binary not found at %s\n"
            "Build it first: cd tools/ruststubber && cargo build --release",
            RUSTSTUBBER,
        )
        sys.exit(1)

    entry = prepare_rust_repo(
        upstream=args.repo,
        crate=args.crate,
        src_dir=args.src_dir,
        test_cmd=args.test_cmd,
        org=args.org,
        clone_dir=args.clone_dir,
        specs_dir=args.specs_dir,
        dry_run=args.dry_run,
        rust_version=args.rust_version,
        edition=args.edition,
        packages=args.packages,
        skip_spec=args.skip_spec,
        strip_docs=not args.keep_docs,
    )

    if entry is None:
        sys.exit(1)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if _consolidated and entry.get("id"):
        _uuid = entry["id"]
        _out_dir = datasets_dir(_uuid)
        _entries_path = _out_dir / "entries.json"
        _entries_path.write_text(json.dumps([entry], indent=2))
        _dataset_path = _out_dir / "dataset.json"
        _dataset_path.write_text(json.dumps([entry], indent=2))
        try:
            from kaiju.paths import copy_inference_inputs as _cii
            _cii(_uuid, entry["repo"].split("/")[-1], test_ids_subdir="rust_test_ids", repo_base="repos")
        except Exception as _e:
            logger.warning("copy_inference_inputs failed: %s", _e)
        logger.info("Wrote consolidated entries+dataset to %s", _out_dir)
        out_path.write_text(json.dumps([entry], indent=2))
    else:
        out_path.write_text(json.dumps([entry], indent=2))
    logger.info("Wrote dataset entry to %s", out_path)


if __name__ == "__main__":
    main()
