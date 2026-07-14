"""Prepare C repos for a commit0 dataset.

For each validated C candidate:
1. Clone to a working directory.
2. Run ``validate_c.validate_c_candidate`` and reject if it fails.
3. Record ``reference_commit`` (HEAD before stubbing).
4. Run ``cstubber`` (libclang) over the source tree.
5. Commit the stubbed version on a ``commit0`` branch as ``base_commit``.
6. Emit a dataset entry compatible with ``CRepoInstance`` /
   ``create_dataset_c.py``.

Optional behaviour gated behind flags:
* ``--fork-org <org>`` — fork to a GitHub org via ``gh repo fork``.
* ``--push`` — push the ``commit0`` branch to the fork.
* ``--scrape-spec`` — scrape a PDF spec from ``--spec-url`` (best-effort),
  compress to ``spec.pdf.bz2``, and commit it into the stubbed branch so it
  becomes part of ``base_commit`` (mirrors the Python ``prepare_repo.py``).

Usage:
    python -m tools.prepare_repo_c --repo DaveGamble/cJSON \\
        --clone-dir ./repos_staging --output entries.json
    python -m tools.prepare_repo_c --repo DaveGamble/cJSON \\
        --scrape-spec --spec-url https://docs.example.org/cjson
    python -m tools.prepare_repo_c candidates.json --output entries.json
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
from kaiju.paths import datasets_dir, spec_path as consolidated_spec_path
from typing import Any, Optional
import uuid as _uuid_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR.parent))

from tools.validate_c import validate_c_candidate  # noqa: E402
from tools.cstubber.cstubber import (  # noqa: E402
    DEFAULT_FALLBACK_ARGS,
    DEFAULT_SKIP_DIR_RE,
    _iter_c_files,
    stub_directory,
)

from tools._git_auth import (  # noqa: E402
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)

# GitHub org to fork repos into (matches Rust/C++/Java/TS pipelines).
DEFAULT_ORG = "Zahgon"

# Lazy handle for the optional Playwright-backed spec scraper. Importing
# ``tools.scrape_pdf`` eagerly would pull in optional heavy deps, so defer it.
_scrape_spec = None


def _get_scrape_func():
    """Lazy-load ``scrape_spec`` to avoid importing optional deps at import time."""
    global _scrape_spec
    if _scrape_spec is None:
        from tools.scrape_pdf import scrape_spec

        _scrape_spec = scrape_spec
    return _scrape_spec




def clone_repo(slug: str, dest: Path) -> Path:
    """Clone https://github.com/<slug> into ``dest/<repo_name>``."""
    name = slug.split("/")[-1]
    target = dest / name
    if target.exists():
        # IDEMPOTENCY: a prior (possibly failed) prepare leaves a working copy on
        # the `commit0_all` branch with stubbed files, so reusing it makes the next
        # `git checkout -b commit0_all` fail (exit 128) — fatal for batch retries.
        # Re-clone fresh for a guaranteed-clean state (a small repo is a few sec).
        logger.info("Existing clone at %s — removing for a clean re-clone", target)
        shutil.rmtree(target, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning %s -> %s", slug, target)
    subprocess.run(
        ["git", "clone", "--depth", "1", f"https://github.com/{slug}.git", str(target)],
        check=True,
    )
    # Unshallow so we can checkout history-tracking branches.
    subprocess.run(
        ["git", "-C", str(target), "fetch", "--unshallow"], check=False
    )
    return target


def _ensure_compile_commands(repo_dir: Path) -> Path | None:
    """Run cmake configure to generate compile_commands.json. Best-effort."""
    build_dir = repo_dir / "build"
    try:
        subprocess.run(
            [
                "cmake",
                "-S",
                str(repo_dir),
                "-B",
                str(build_dir),
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                "-DBUILD_TESTING=ON",
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "cmake configure failed for %s — stubber will use fallback args: %s",
            repo_dir,
            exc,
        )
        return None
    cc = build_dir / "compile_commands.json"
    return cc if cc.exists() else None


def _maybe_clang_format(repo_dir: Path) -> None:
    """Run clang-format if a .clang-format config is present."""
    if not (repo_dir / ".clang-format").exists():
        return
    clang_format = shutil.which("clang-format")
    if not clang_format:
        return
    c_files = list(repo_dir.rglob("*.c"))
    if not c_files:
        return
    logger.info("Running clang-format on %d .c files", len(c_files))
    try:
        subprocess.run(
            [clang_format, "-i", *[str(p) for p in c_files]],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("clang-format timed out — continuing")


def _restore_test_files(repo_dir: Path, reference_commit: str) -> int:
    """Reset test/ and tests/ trees back to the reference commit.

    The stubber's skip-dir regex already excludes them, but in case any test
    file slipped through (e.g. via odd directory naming), this gives us a
    second line of defence.
    """
    restored = 0
    for sub in ("tests", "test"):
        d = repo_dir / sub
        if not d.exists():
            continue
        try:
            git(repo_dir, "checkout", reference_commit, "--", sub, timeout=30)
            restored += 1
        except subprocess.CalledProcessError:
            pass
    return restored


def _diff_stats(repo_dir: Path, ref: str) -> tuple[int, int]:
    """Return ``(additions, deletions)`` of working-tree changes vs ``ref``."""
    try:
        out = git(repo_dir, "diff", "--numstat", ref)
    except subprocess.CalledProcessError:
        return 0, 0
    adds = dels = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            adds += int(parts[0])
            dels += int(parts[1])
        except ValueError:
            continue
    return adds, dels


def _scrape_and_commit_spec(
    repo_dir: Path,
    repo_name: str,
    spec_url: str,
    specs_dir: Path,
) -> Optional[str]:
    """Scrape a spec PDF and commit it into the current branch.

    Mirrors the prepare_repo_cpp.py / prepare_repo_rust.py spec step:
    1. If ``spec_url`` is provided, scrape it via ``tools.scrape_pdf.scrape_spec``.
    2. If that fails OR ``spec_url`` is empty, fall back to a README-based PDF
       generated by ``tools.scrape_pdf.scrape_readme_spec`` (the same fallback
       Rust and Go pipelines now use).
    3. Compress to ``spec.pdf.bz2`` and commit it so it becomes part of the
       stubbed ``base_commit``. Best-effort: any failure returns ``None`` and
       the caller keeps the existing ``base_commit``.

    Returns the new ``base_commit`` SHA when a spec was committed, else ``None``.
    """
    spec_path: Optional[str] = None

    if spec_url:
        logger.info("  Scraping spec for %s from: %s", repo_name, spec_url)
        try:
            scrape_fn = _get_scrape_func()
            spec_path = scrape_fn(
                base_url=spec_url,
                name=repo_name,
                output_dir=str(specs_dir),
                compress=True,
            )
        except ImportError as exc:
            logger.warning(
                "  Spec scrape unavailable (%s) — will try README fallback", exc,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
            logger.warning("  Spec URL scrape failed for %s: %s", repo_name, exc)

    if not spec_path or not Path(spec_path).exists():
        try:
            from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
            readme_path, _readme_url = _scrape_readme_spec(
                repo_dir, specs_dir, repo_name
            )
            if readme_path and Path(readme_path).exists():
                spec_path = str(readme_path)
                logger.info("  README-based spec used as fallback")
        except ImportError:
            logger.warning(
                "  README spec fallback unavailable (install scrape deps)"
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
            logger.warning("  README spec fallback failed: %s", exc)

    if not spec_path or not Path(spec_path).exists():
        logger.warning("  No spec PDF produced for %s — skipping", repo_name)
        return None

    dest = repo_dir / "spec.pdf.bz2"
    shutil.copy2(spec_path, dest)
    git(repo_dir, "add", "spec.pdf.bz2")
    git(
        repo_dir,
        "commit",
        "-m",
        f"commit0: add spec PDF for {repo_name}",
        timeout=30,
    )
    new_commit = git(repo_dir, "rev-parse", "HEAD")
    logger.info("  Spec committed; base_commit now %s", new_commit[:12])
    return new_commit


def _detect_c_standard(repo_dir: Path) -> str:
    """Backward-compat wrapper around :func:`tools.cpp_version.detect_c`."""
    from tools.cpp_version import detect_c

    return detect_c(repo_dir).version or "11"


def _detect_c_standard_full(repo_dir: Path):
    """Return the full :class:`CDetectionResult` for provenance tracking."""
    from tools.cpp_version import detect_c

    return detect_c(repo_dir)


def _build_c_setup(repo_path: Path, cmake_flags: str, spec_url: str) -> dict:
    """Build the ``setup`` dict for a C entry with full version provenance."""
    det = _detect_c_standard_full(repo_path)
    return {
        "build_system": "cmake",
        "c_standard": det.version or "11",
        "packages": "",
        "cmake_flags": cmake_flags,
        "pre_install": [],
        "specification": spec_url,
        "install": (
            "cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON "
            "-DBUILD_TESTING=ON && cmake --build build -j$(nproc)"
        ),
        "version_source": det.source,
        "version_conflicts": det.conflicts,
    }


def _capture_c_test_ids(repo_dir: Path, slug: str) -> None:
    """Capture the canonical C test inventory and save it as the AUTHORITATIVE
    denominator the evaluator uses.

    ``evaluate_c`` scores ``num_passed / len(test_ids_flat)`` over the UNION of
    the fail/pass lists. ``get_c_test_ids`` reads a SINGLE
    ``commit0/data/c_test_ids/<repo>.bz2`` (when the repo name has no ``__``),
    keyed by ``repo.lower().replace(".", "-")``. Without that file the evaluator
    silently falls back to the observed test count — which mis-reports a
    timeout-truncated run and lets a model inflate its score by adding passing
    tests.

    Reuses ``generate_test_ids_c._enumerate_local`` (cmake configure+build then
    ``ctest --show-only=json-v1``, with a ``ctest -N`` fallback) so we don't
    duplicate the build logic; ``_ensure_compile_commands`` has already run a
    ``-DBUILD_TESTING=ON`` cmake configure, so the build/ dir is warm.

    Best-effort: any failure just warns and leaves the evaluator to fall back to
    the observed count. Never aborts prep.
    """
    try:
        from tools.generate_test_ids_c import _enumerate_local, write_bz2
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "test-id capture: could not import generate_test_ids_c (%s); skipping.", e
        )
        return
    try:
        ids = _enumerate_local(repo_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "test-id capture: ctest enumeration failed for %s (%s); the evaluator "
            "will use the observed test count.", slug, e,
        )
        return
    if not ids:
        logger.warning(
            "test-id capture: no C test IDs discovered for %s; the evaluator will "
            "use the observed test count.", slug,
        )
        return
    # Filename key MUST match get_c_test_ids.main(): repo.lower().replace(".", "-")
    # of the repo basename, single file (union of all tests) into c_test_ids/.
    repo_key = slug.split("/")[-1].lower().replace(".", "-")
    out_dir = (
        Path(__file__).resolve().parent.parent
        / "commit0" / "data" / "c_test_ids"
    )
    try:
        target = out_dir / f"{repo_key}.bz2"
        write_bz2(ids, target)
        logger.info("test-id capture: saved %d canonical C test IDs -> %s", len(ids), target)
    except Exception as e:  # noqa: BLE001
        logger.warning("test-id capture: failed to save test IDs for %s (%s).", slug, e)


def _c_parse_error_signature(root: Any) -> int:
    """Number of ERROR + MISSING nodes in a tree-sitter parse (structural error
    count). Used as a cheap, order-independent signature to tell whether the stub
    rewrite INTRODUCED new structural errors relative to the pristine source.
    """
    n = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or getattr(node, "is_missing", False):
            n += 1
        stack.extend(node.children)
    return n


def _stubbed_base_compiles_c(
    repo_dir: Path, reference_commit: "str | None" = None
) -> "bool | None":
    """A11 (c): best-feasible deps-free gate on the STUBBED base tree.

    A full ``gcc``/``cmake`` compile of the stubbed base needs the repo's headers
    and build environment (compile_commands.json, third-party libs), which is
    frequently unavailable at prep time — running it and branding the result
    ``False`` on a missing-header/missing-lib error would be a false gate (exactly
    the failure mode the go/python helpers guard against). So instead of a real
    compile we do the STRONGEST deps-free check available: re-parse every stubbed
    ``.c`` file with the SAME tree-sitter C grammar the stubber's recovery engine
    uses (``tree_sitter_language_pack``, no preprocessor, no headers needed) and
    verify the rewritten source is still structurally well-formed.

    IMPORTANT — tree-sitter parses WITHOUT a preprocessor, so it flags legal C
    that depends on the preprocessor as a parse ERROR/MISSING even in the PRISTINE
    source: calling-convention macros in declarators (``void (CJSON_CDECL *fn)(
    void)``), ``#ifdef __cplusplus`` / ``extern "C" {`` guards, MSVC-only ``#if``
    type blocks, X-macros, etc. A naive "any parse error -> False" therefore
    produces FALSE NEGATIVES (observed on cJSON, and it flip-flops run-to-run
    depending on which files are present). To avoid that, when ``reference_commit``
    is supplied we make the check DIFFERENTIAL: a file only fails when the stub
    rewrite INCREASED its structural-error count versus the pristine blob (i.e. the
    splice actually corrupted the syntax). Equal error counts = pre-existing
    preprocessor construct = not a stub bug.

    Semantics mirror python's quick_import_check / go's _stubbed_base_compiles_go:
      * every stubbed file parses cleanly (or matches pristine)  -> True
      * a stubbed file has MORE structural errors than pristine   -> False (agent starts broken)
      * tree-sitter unavailable / no pristine baseline to diff    -> None (unknown; no false gate)

    LIMITATION: this is a structural (syntax-level) gate, NOT a semantic compile.
    It cannot detect type errors, undeclared symbols, or link failures — those
    require the build env and are intentionally left to the container eval. A
    ``True`` here means "the stub output is syntactically well-formed", not
    "gcc succeeds". It is deliberately conservative: it only reports ``False`` when
    the stubber's OWN output introduced a structural error the source lacked.
    """
    try:
        from tree_sitter_language_pack import get_parser as _ts_get_parser
    except Exception as e:  # noqa: BLE001
        logger.info(
            "A11: tree-sitter C grammar unavailable (%s); skipping stubbed-base "
            "structural check (recording unknown).", e,
        )
        return None

    try:
        parser = _ts_get_parser("c")
    except Exception as e:  # noqa: BLE001
        logger.info(
            "A11: could not load tree-sitter C parser (%s); recording unknown.", e
        )
        return None

    checked = 0
    saw_unadjudicable = False
    for c_file in _iter_c_files(repo_dir, DEFAULT_SKIP_DIR_RE):
        try:
            source = c_file.read_bytes()
        except OSError as e:
            logger.warning(
                "A11: could not read stubbed file %s (%s); recording unknown.",
                c_file, e,
            )
            return None
        if not source.strip():
            # An empty stubber output is never valid C — treat as a real failure.
            logger.warning(
                "A11: stubbed file %s is empty; base is structurally broken.", c_file
            )
            return False
        try:
            tree = parser.parse(source)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "A11: tree-sitter failed to parse %s (%s); recording unknown.",
                c_file, e,
            )
            return None
        root = tree.root_node
        if not (root.has_error or getattr(root, "is_missing", False)):
            checked += 1
            continue

        # This file has structural parse errors. CRITICAL false-negative guard:
        # tree-sitter's C grammar runs with NO preprocessor, so it CANNOT parse
        # legal-but-preprocessor-dependent constructs — calling-convention macros
        # in declarators (`void (CJSON_CDECL *fn)(void)`), `#ifdef __cplusplus /
        # extern "C" {` guards, MSVC-only `#if` type blocks, X-macros, etc. These
        # ERROR/MISSING nodes exist in the PRISTINE source too and have nothing to
        # do with the stub rewrite. Branding the base `False` on them is a pure
        # false negative (observed on cJSON: all ERROR nodes were on the
        # `CJSON_CDECL` typedef / an `extern "C"` guard, none on any stub splice —
        # yet base_compiles flipped to False, making a good crate look impossible,
        # and it flip-flopped run-to-run depending on which files were present).
        #
        # The reliable discriminator is DIFFERENTIAL: reparse the SAME file's
        # pristine (pre-stub) blob and only fail if the stub INCREASED the
        # structural-error count. A truncated/unbalanced brace splice always adds
        # a new ERROR/MISSING node the pristine didn't have; a preprocessor macro
        # produces the identical error count before and after.
        stub_errs = _c_parse_error_signature(root)
        pristine_errs = _c_pristine_error_count(
            repo_dir, c_file, reference_commit, parser
        )
        if pristine_errs is None:
            # Couldn't get the pristine baseline (no reference commit / not in git
            # / read failure). Don't fabricate a False from an un-adjudicable error
            # — record unknown and defer to the container compile.
            saw_unadjudicable = True
            logger.info(
                "A11: %s has parse errors but its pristine baseline is unavailable; "
                "can't attribute them to the stub — recording unknown for this file.",
                c_file,
            )
            continue
        if stub_errs > pristine_errs:
            logger.warning(
                "A11: stubbed file %s introduced %d NEW structural parse error(s) "
                "(pristine had %d, stubbed has %d) — the stub rewrite corrupted the "
                "syntax (unbalanced/truncated brace splice).",
                c_file, stub_errs - pristine_errs, pristine_errs, stub_errs,
            )
            return False
        logger.info(
            "A11: %s has %d parse error(s) but the PRISTINE source has the same "
            "count — pre-existing preprocessor-dependent construct, NOT a stub bug. "
            "Not treating as broken.",
            c_file, stub_errs,
        )
        # Same error count => stub added nothing; count it as validated.
        checked += 1

    if checked == 0 and saw_unadjudicable:
        logger.info(
            "A11: files had parse errors but none were adjudicable against a "
            "pristine baseline; recording unknown (deferring to container compile)."
        )
        return None
    if checked == 0:
        logger.info(
            "A11: no .c files to validate structurally; recording unknown."
        )
        return None
    return True


def _c_pristine_error_count(
    repo_dir: Path, c_file: Path, reference_commit: "str | None", parser: Any
) -> "int | None":
    """Structural-error count of ``c_file``'s PRISTINE (pre-stub) blob, read from
    ``reference_commit`` via ``git show``. Returns None when unavailable so the
    caller records "unknown" rather than a fabricated failure.
    """
    if not reference_commit:
        return None
    try:
        rel = c_file.relative_to(repo_dir).as_posix()
    except ValueError:
        return None
    try:
        proc = subprocess.run(
            ["git", "show", f"{reference_commit}:{rel}"],
            cwd=str(repo_dir),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        # File is new in the stub commit (no pristine version) — can't diff.
        return None
    try:
        tree = parser.parse(proc.stdout)
    except Exception:  # noqa: BLE001
        return None
    return _c_parse_error_signature(tree.root_node)


def prepare_one(
    slug: str,
    clone_dir: Path,
    fork_org: str = DEFAULT_ORG,
    push: bool = False,
    branch: str = "commit0_all",
    cmake_flags: str = "",
    skip_spec: bool = False,
    spec_url: str = "",
    specs_dir: Path = Path("specs"),
) -> dict | None:
    """Prepare a single C repo. Returns a dataset entry or None if rejected."""
    repo_path = clone_repo(slug, clone_dir)

    ok, reason = validate_c_candidate(repo_path)
    if not ok:
        logger.warning("%s rejected at validation: %s", slug, reason)
        return None

    reference_commit = git(repo_path, "rev-parse", "HEAD")
    logger.info("%s: reference_commit=%s", slug, reference_commit[:12])

    # Configure a fresh build dir for compile_commands.json.
    _ensure_compile_commands(repo_path)

    # Create the commit0 branch.
    try:
        git(repo_path, "branch", "-D", branch, check=False)
    except subprocess.CalledProcessError:
        pass
    git(repo_path, "checkout", "-b", branch)

    # Run cstubber over the repo.
    report = stub_directory(
        repo_path,
        compile_db_path=repo_path / "build" / "compile_commands.json",
        fallback_args=DEFAULT_FALLBACK_ARGS,
        write_header=True,
    )

    if report.functions_stubbed == 0:
        logger.warning(
            "%s: cstubber stubbed 0 functions — refusing to commit empty stub. "
            "function_decl_count=%d, used_fallback=%s",
            slug,
            report.function_decl_count,
            report.used_fallback_args,
        )
        return None

    if (
        report.function_decl_count > 0
        and report.functions_stubbed / report.function_decl_count < 0.30
    ):
        logger.warning(
            "%s: stubbed only %d / %d functions (<30%%) — likely macro-heavy. "
            "Skipping.",
            slug,
            report.functions_stubbed,
            report.function_decl_count,
        )
        return None

    _maybe_clang_format(repo_path)

    restored = _restore_test_files(repo_path, reference_commit)
    if restored:
        logger.info("Restored %d test directories from reference", restored)

    git(repo_path, "add", "-A")
    additions, deletions = _diff_stats(repo_path, reference_commit)
    if additions == 0 and deletions == 0:
        logger.warning("%s: stub produced empty diff — skipping commit", slug)
        return None
    # NOTE: a one-sided diff (e.g. additions==0, deletions>0) is LEGITIMATE for
    # removal-heavy stubbing (bodies deleted, no `pass`/placeholder inserted), so
    # we do NOT skip on it — the empty-diff check above already catches "nothing
    # changed". (Previously an `additions==0 or deletions==0` skip false-failed
    # valid removal-only stubs.)

    git(
        repo_path,
        "commit",
        "-m",
        "commit0: stub function bodies (cstubber, libclang)",
        timeout=30,
    )
    base_commit = git(repo_path, "rev-parse", "HEAD")
    logger.info("%s: base_commit=%s (+%d/-%d)", slug, base_commit[:12], additions, deletions)

    # A11 (c): best-feasible deps-free gate on the STUBBED base. A full gcc/cmake
    # compile needs the repo's headers + build env (often unavailable at prep
    # time), so we instead structurally re-parse every stubbed .c file with the
    # tree-sitter C grammar to confirm the stubber output is well-formed (no
    # unbalanced-brace / truncated-splice corruption). True = all files parse,
    # False = a stubbed file is structurally broken (agent starts from a corrupt
    # tree; any 0% is infra, not model), None = couldn't check (grammar missing).
    # This is a SYNTAX-level gate, not a semantic compile — see the helper docstring.
    base_compiles = _stubbed_base_compiles_c(repo_path, reference_commit)
    if base_compiles is False:
        logger.warning(
            "A11: STUBBED BASE IS STRUCTURALLY BROKEN for %s — a stubbed .c file "
            "no longer parses; the agent would start from a corrupt tree. Recording "
            "base_compiles=false (any 0%% here is infra, not model). Investigate the "
            "stub output before trusting a score.",
            slug,
        )
    elif base_compiles is True:
        logger.info("A11: stubbed base is structurally well-formed for %s.", slug)

    # Capture the canonical test inventory (ctest enumeration) on the stubbed
    # base and save it as commit0/data/c_test_ids/<repo>.bz2 — the AUTHORITATIVE
    # denominator evaluate_c uses. Stub bodies keep signatures + CMake add_test()
    # registrations intact, so enumeration on the stubbed tree still lists every
    # test. Without this file the evaluator falls back to the observed count.
    _capture_c_test_ids(repo_path, slug)

    # Scrape a spec PDF and fold it into base_commit (default-on,
    # best-effort, with README fallback when spec_url is missing or fails).
    if not skip_spec:
        new_base = _scrape_and_commit_spec(
            repo_path, slug.split("/")[-1], spec_url, specs_dir
        )
        if new_base:
            base_commit = new_base

    target_repo_slug = f"{fork_org}/{slug.split('/')[-1]}" if fork_org else slug

    if push and fork_org:
        # The containerized C build clones this fork and runs
        #   git fetch origin <env_setup_commit> <base_commit>
        # so BOTH commits MUST be reachable on the fork. A failed push therefore
        # cannot be a warning: it would emit a dataset whose base_commit only
        # exists locally, and the image build later dies with the opaque
        # "upload-pack: not our ref". Fail fast here with an actionable message.
        try:
            fork_repo(slug, fork_org)
            push_to_fork(repo_path, target_repo_slug, branch, remote_name="origin")
        except Exception as exc:
            raise RuntimeError(
                f"Fork/push to {target_repo_slug} FAILED — the containerized C "
                f"build clones this fork and fetches reference_commit="
                f"{reference_commit[:12]} + base_commit={base_commit[:12]} from it, "
                f"so an un-pushed dataset is UNBUILDABLE (setup.sh would fail with "
                f"'not our ref'). Fix the push and re-run: ensure your token has "
                f"WRITE access to '{fork_org}', or pass --fork-org "
                f"<account-you-can-push-to> (run_trajectory.sh: --org / "
                f"$KAIJU_FORK_ORG).\nUnderlying error: {exc}"
            ) from exc

    entry = {
        "instance_id": f"{slug.split('/')[-1]}_c",
        "id": str(_uuid_mod.uuid4()),
        "repo": target_repo_slug,
        "original_repo": slug,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "language": "c",
        "src_dir": ".",
        "setup": _build_c_setup(repo_path, cmake_flags, spec_url),
        "test": {
            "framework": "ctest",
            "test_cmd": (
                "ctest --test-dir build --output-on-failure "
                "--output-junit /testbed/test_report.xml"
            ),
        },
        "stub_report": report.to_dict(),
        # A11 provenance (top-level, matching go/python): True = stubbed base is
        # structurally well-formed, False = a stubbed .c file no longer parses (a
        # 0% is infra, not model), None = not checked (tree-sitter C grammar
        # missing / no .c files). NOTE: this is a deps-free SYNTAX gate, not a
        # full gcc/cmake compile — see _stubbed_base_compiles_c.
        "base_compiles": base_compiles,
    }
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare C repos for commit0")
    parser.add_argument(
        "candidates_file",
        nargs="?",
        help="JSON file of candidates (output of discover_c.py)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Process a single repo by slug, e.g. DaveGamble/cJSON",
    )
    parser.add_argument(
        "--upstream",
        type=str,
        default=None,
        help="Alias for --repo (kept for cross-language consistency)",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("repos_staging"),
        help="Where to clone repos for preparation",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("c_entries.json"),
        help="Where to write the dataset entries JSON",
    )
    parser.add_argument(
        "--fork-org",
        type=str,
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push commit0 branch to the fork (requires --fork-org and gh CLI)",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default="commit0_all",
        help="Branch name for the stubbed commit",
    )
    parser.add_argument(
        "--cmake-flags",
        type=str,
        default="",
        help="Extra CMake flags to record in setup.cmake_flags",
    )
    parser.add_argument(
        "--skip-spec",
        action="store_true",
        help="Skip spec PDF generation (default: scrape via --spec-url with README fallback)",
    )
    parser.add_argument(
        "--spec-url",
        type=str,
        default="",
        help="Documentation URL to scrape into spec.pdf.bz2 (used with --scrape-spec)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path("specs"),
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

    args.repo = args.repo or args.upstream

    setup_git_credentials(dry_run=not args.push)

    candidates: list[str] = []
    if args.repo:
        candidates.append(args.repo)
    if args.candidates_file:
        data = json.loads(Path(args.candidates_file).read_text())
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    full = item.get("full_name") or item.get("repo")
                    if full:
                        candidates.append(full)
    if not candidates:
        parser.error("Provide --repo or a candidates_file with entries")

    entries: list[dict] = []
    rejected: list[dict] = []
    for slug in candidates:
        try:
            entry = prepare_one(
                slug,
                args.clone_dir,
                fork_org=args.fork_org,
                push=args.push,
                branch=args.branch,
                cmake_flags=args.cmake_flags,
                skip_spec=args.skip_spec,
                spec_url=args.spec_url,
                specs_dir=args.specs_dir,
            )
        except Exception:
            logger.exception("Failed to prepare %s", slug)
            rejected.append({"repo": slug, "reason": "exception"})
            continue
        if entry is None:
            rejected.append({"repo": slug, "reason": "validation_or_empty_stub"})
            continue
        entries.append(entry)

    if _consolidated and entries and entries[0].get("id"):
        _uuid = entries[0]["id"]
        _out_dir = datasets_dir(_uuid)
        _entries_path = _out_dir / "entries.json"
        _entries_path.write_text(json.dumps(entries, indent=2))
        logger.info("Wrote %d entries to %s (consolidated)", len(entries), _entries_path)
        Path(args.output).write_text(json.dumps(entries, indent=2))
    else:
        args.output.write_text(json.dumps(entries, indent=2))
    logger.info("Wrote %d entries to %s", len(entries), args.output)

    if rejected:
        rej_path = args.output.with_name("candidates_c_rejected.json")
        rej_path.write_text(json.dumps(rejected, indent=2))
        logger.info("Wrote %d rejections to %s", len(rejected), rej_path)

    # Exit non-zero when nothing was prepared so batch drivers / CI keying on the
    # exit code don't treat a total prepare failure as success (parity B4).
    if not entries:
        logger.error("Prepared 0 entries — nothing to build. Exiting non-zero.")
        sys.exit(1)


if __name__ == "__main__":
    main()
