"""Assemble TRUTH.md authoring inputs for a task from its ``<uuid>/datasets/``
context (base/reference commits, spec, test inventory) + the checked-out repo,
then author + freeze the bundle. Best-effort: returns None if the golden diff
cannot be computed (e.g. the repo isn't present at verify time), so ``--build``
degrades gracefully instead of failing.

Pre-registration note: ideally this runs at PREPARE time (before the trajectory,
where the golden diff is freshly available). It also works post-hoc as long as
the repo + both commits are still resolvable.
"""
from __future__ import annotations

import bz2
import json
import subprocess
from pathlib import Path

from .truth import TruthInputs
from .orchestrate import build_bundle, freeze_bundle, verification_dir

_REPO_SEARCH = ("repos", "repos_staging", "clones")


def _entries(uuid_root: Path) -> dict | None:
    p = uuid_root / "datasets" / "entries.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else [data]
    return entries[0] if entries else None


def _repo_dir_candidates(entry: dict) -> list[str]:
    """The clone-dir name varies: `<org>__<name>` (un33k__python-slugify) OR just
    `<name>` (cJSON). Try every convention from both repo fields."""
    names: list[str] = []
    for field in ("original_repo", "repo"):
        full = entry.get(field) or ""
        if not full:
            continue
        names.append(full.replace("/", "__"))   # org__name
        names.append(full.split("/")[-1])        # name
    # de-dup, preserve order
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n); out.append(n)
    return out


def _has_commit(repo: Path, sha: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _find_repo(uuid_root: Path, entry: dict) -> Path | None:
    ref = entry.get("reference_commit") or ""
    candidates = _repo_dir_candidates(entry)
    fallback: Path | None = None
    for base in (uuid_root, *uuid_root.parents):
        for sub in _REPO_SEARCH:
            for name in candidates:
                cand = base / sub / name
                if (cand / ".git").is_dir():
                    # Prefer a checkout that actually CONTAINS the golden commit.
                    if ref and _has_commit(cand, ref):
                        return cand
                    fallback = fallback or cand
    return fallback


def _git_diff(repo: Path, base: str, ref: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "diff", f"{base}..{ref}"],
                           capture_output=True, text=True, timeout=120)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _read_bz2_lines(p: Path) -> list[str]:
    try:
        return [ln for ln in bz2.open(p, "rt", encoding="utf-8",
                                      errors="replace").read().splitlines() if ln.strip()]
    except OSError:
        return []


def _spec_text(uuid_root: Path) -> str:
    specs = list((uuid_root / "datasets").glob("*_spec.pdf.bz2"))
    if not specs:
        return ""
    try:
        import fitz  # PyMuPDF, best-effort
        raw = bz2.open(specs[0], "rb").read()
        doc = fitz.open(stream=raw, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)[:40000]
    except Exception:
        return ""


def _stub_files_from_diff(diff: str) -> list[str]:
    out = []
    for line in diff.splitlines():
        if line.startswith("+++ b/") and line[6:] != "/dev/null":
            out.append(line[6:].strip())
    return sorted(set(out))


def build_inputs_for_uuid(uuid_root: str | Path) -> TruthInputs | None:
    uuid_root = Path(uuid_root)
    entry = _entries(uuid_root)
    if not entry or not entry.get("base_commit") or not entry.get("reference_commit"):
        return None
    repo = _find_repo(uuid_root, entry)
    if repo is None:
        return None
    golden = _git_diff(repo, entry["base_commit"], entry["reference_commit"])
    if not golden:
        return None
    test_ids = []
    for tp in (uuid_root / "datasets").glob("*_test_ids.bz2"):
        test_ids = _read_bz2_lines(tp)
        break
    return TruthInputs(
        repo=str(entry.get("repo") or ""),
        language=str(entry.get("language") or ""),
        spec_text=_spec_text(uuid_root),
        golden_diff=golden,
        fail_to_pass=test_ids,          # flat frozen inventory (fail/pass split not stored)
        stub_files=_stub_files_from_diff(golden),
    )


def _first_run_model(uuid_root: Path) -> str:
    import glob, json as _json
    for f in glob.glob(str(uuid_root / "runs" / "*" / "agent" / "run_*" / "pipeline_results.json")):
        try:
            d = _json.loads(Path(f).read_text())
            return str(d.get("model") or d.get("model_short") or "")
        except (OSError, ValueError):
            continue
    return ""


def build_and_freeze_from_uuid(uuid_root: str | Path, client=None) -> str | None:
    """Author + freeze a SOUND, COMPLETE verifier set (via the closed generate →
    validate-on-golden/stub → prune/regenerate loop) if not already frozen."""
    from . import layout
    uuid_root = Path(uuid_root)
    if layout.truth_path(uuid_root).exists():
        return str(layout.truth_path(uuid_root))   # already pre-registered
    inp = build_inputs_for_uuid(uuid_root)
    if inp is None:
        return None

    from .model_client import default_generation_client, judge_client_for_run
    from .truth import generate_truth
    from .predicates import generate_predicates
    from .orchestrate import Bundle, freeze_bundle
    from .solution_code import read_solution_code
    from .pytest_runner import run_pytest_in_dir, checkout_solution, remove_worktree
    from .judge import judge_trajectory
    from .rubric_anchor import _code_digest, _anchors_path
    from .verifier_loop import sound_pytest_loop, sound_rubric_loop

    gen = client or default_generation_client()
    # The build's rubric-validation judge MUST be the same cross-family model the
    # candidate judge will use, so the golden/stub anchors are consistent with the
    # candidate scoring (gap-scoring only cancels strictness under one judge).
    judge_client = judge_client_for_run(_first_run_model(uuid_root))
    entry = _entries(uuid_root)
    repo = _find_repo(uuid_root, entry)
    base, ref = entry["base_commit"], entry["reference_commit"]
    src_dir = str(entry.get("src_dir") or ".")

    truth = generate_truth(inp, gen)
    golden_code = read_solution_code(repo, ref, src_dir)
    stub_code = read_solution_code(repo, base, src_dir)

    # SOUND PYTEST loop — real golden/stub execution decides which tests survive.
    def _run_pytest(code, which):
        wt = checkout_solution(repo, ref if which == "golden" else base)
        if wt is None:
            return {}
        r = run_pytest_in_dir(wt, code)
        remove_worktree(repo, wt)
        return r.per_test
    pytest_code, pt_loop = sound_pytest_loop(truth.text, inp.stub_files, golden_code,
                                             _run_pytest, gen)
    # DIFFERENTIAL oracle tests (golden defines the expected output -> cannot be
    # wrong-oracle). Prepended to the sound LLM tests.
    try:
        from .differential import generate_differential_tests
        from .pytest_gen import infer_import_hint
        diff_code = generate_differential_tests(
            truth.text, infer_import_hint(inp.stub_files), repo, ref, src_dir, gen)
        if diff_code.strip():
            pytest_code = diff_code + "\n\n# ---- LLM-authored (golden-validated) tests ----\n" + pytest_code
    except Exception:
        pass

    # SOUND RUBRIC loop — golden/stub judging decides which criteria survive.
    def _judge_code(rubric, code):
        return judge_trajectory(truth.text, rubric,
                                _code_digest(code, "solution code"), judge_client)
    rb = sound_rubric_loop(truth.text, golden_code, stub_code, _judge_code, gen)
    if rb is None:
        return None
    rubric, golden_jr, stub_jr, rb_loop = rb

    predicates = generate_predicates(truth.text, gen)
    bundle = Bundle(truth=truth, rubric=rubric, predicates=predicates, pytest_code=pytest_code)
    paths = freeze_bundle(uuid_root, bundle)
    if inp.stub_files:
        from .manifest import write_manifest
        write_manifest(uuid_root, inp.stub_files)
    # store the rubric anchors (already computed by the loop) + the soundness report
    ap = _anchors_path(uuid_root)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(json.dumps({"golden": golden_jr.to_dict(), "stub": stub_jr.to_dict()}, indent=2),
                  encoding="utf-8")
    sp = layout.verifiers_dir(uuid_root) / "verifier_soundness.json"
    sp.write_text(json.dumps({"pytest": pt_loop.to_dict(), "rubric": rb_loop.to_dict()}, indent=2),
                  encoding="utf-8")
    _meta_verify_and_store(uuid_root, inp, bundle, gen)
    return paths.get("truth")


def _meta_verify_and_store(uuid_root: Path, inp: TruthInputs, bundle, client) -> None:
    """Mutation-meta-verify the generated PREDICATES (two-sided kill/spare) and store
    the report. Uses the self-contained predicate runner (no container); the
    test-execution side is added in-pipeline via combined_mutant_runner."""
    try:
        import json
        from .mutation import generate_mutants, validate_verifiers
        from .mutation_runner import predicate_mutant_runner
        from .predicates import added_code
        from .orchestrate import verification_dir
        golden_code = added_code([inp.golden_diff])
        runner = predicate_mutant_runner(golden_code, bundle.predicates)
        mutants = generate_mutants(inp.golden_diff, bundle.truth.text, client)
        report = validate_verifiers(mutants, runner)
        from . import layout
        out = layout.pytest_meta_path(uuid_root)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    except Exception:
        pass  # meta-verification is advisory at build time; never block the freeze
