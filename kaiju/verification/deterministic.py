"""Layer-0 (legitimacy gate) and Layer-1 (per-stage structure) deterministic
checks — the P1 checks that run against an *already-produced* trajectory with no
LLM and no re-execution.

Each check is a pure function ``(TrajectoryBundle) -> _Outcome`` keyed by concern
id in ``CHECKS``. Concerns whose phase has not shipped have no entry here and are
emitted as ``PENDING`` by the evaluator, so the taxonomy stays provably complete
without pretending unshipped checks are enforced.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Callable

from .content import module_content
from .schemas import CheckStatus
from .trajectory import TrajectoryBundle

# Test/scoring/build files that, if the agent's patch modifies them, indicate
# test-neutering or scoring-config tampering (the canonical cheat surface). This is
# the UNION across all 8 languages, mirroring the frozen-restore patterns the eval
# harness (`spec_*.py`/`eval.sh`) resets from the reference — so a cheat on any
# language's scoring file is caught even when the run's `language` field is unset.
# Matched with fnmatch (so `jest.config.*`, `*.gradle` work as globs).
_CHEAT_CONFIG_GLOBS = (
    # python
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "setup.py", "pyproject.toml",
    ".coveragerc", "noxfile.py", "sitecustomize.py", "usercustomize.py", "*.pth",
    # make / cmake / c-cpp build
    "Makefile", "GNUmakefile", "CMakeLists.txt", "CMakePresets.json", "configure",
    "configure.ac", "meson.build", "meson_options.txt",
    # js / ts
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
    "yarn.lock", "jest.config.*", "vitest.config.*", ".mocharc.*", "karma.conf.*",
    "babel.config.*", ".babelrc*", ".npmrc",
    # go
    "go.mod", "go.sum", "go.work", "go.work.sum", ".golangci.yml", ".golangci.yaml",
    "tools.go",
    # rust
    "Cargo.toml", "Cargo.lock", "build.rs", "rust-toolchain", "rust-toolchain.toml",
    # java
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat",
    "mvnw", "mvnw.cmd",
    # cross-language repo/scoring metadata
    ".env", ".gitattributes", ".gitmodules",
)
# Test-file basename globs (union across languages).
_TEST_FILE_GLOBS = (
    "test_*.py", "*_test.py",                                    # python
    "*_test.go",                                                 # go
    "*.test.js", "*.test.mjs", "*.test.cjs", "*.test.jsx",       # js
    "*.spec.js", "*.spec.mjs", "*.spec.cjs", "*.spec.jsx",
    "*.test.ts", "*.test.tsx", "*.spec.ts", "*.spec.tsx",        # ts
    "*Test.java", "*Tests.java", "*IT.java", "*Spec.groovy",     # java
    "*_test.c", "test_*.c", "test_*.h",                          # c
    "*_test.cpp", "test_*.cpp", "*_test.cc", "test_*.cc",        # cpp
)
# Path segments that mark a test/fixture tree (any file beneath them is frozen).
_TEST_DIR_SEGMENTS = {
    "tests", "test", "__tests__", "testdata", "benches", "__snapshots__",
}
# Java-style nested test roots matched as substrings.
_TEST_PATH_SUBSTRINGS = ("src/test/", "src/androidtest/", "src/integrationtest/")

# Diff header forms. We tolerate: standard `a/`+`b/` prefixes, `--no-prefix`
# (bare paths), git-quoted paths ("b/a b.py"), and `-u` trailing tab-timestamps.
_DIFF_PLUSPLUS_RE = re.compile(r'^\+\+\+ (.+?)\s*$', re.MULTILINE)
_DIFF_MINUS_RE = re.compile(r'^--- (.+?)\s*$', re.MULTILINE)
_DIFF_GIT_RE = re.compile(r'^diff --git (.+?) (.+?)\s*$', re.MULTILINE)


@dataclass
class _Outcome:
    status: CheckStatus
    summary: str = ""
    evidence: dict[str, Any] | None = None


def _norm_diff_path(raw: str) -> str | None:
    """Normalise one diff-header path token to a repo-relative path, or None for a
    non-path (`/dev/null`). Handles quoting, tab timestamps, and a/ b/ prefixes."""
    s = raw.strip()
    if s.endswith("\t") or "\t" in s:            # `-u` timestamp: cut at first tab
        s = s.split("\t", 1)[0].strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':   # git-quoted path
        s = s[1:-1].encode().decode("unicode_escape", errors="replace")
    if s in ("/dev/null", ""):
        return None
    if s[:2] in ("a/", "b/"):                     # strip standard prefix
        s = s[2:]
    return s.replace("\\", "/")


def _is_cheatable_path(path: str) -> str | None:
    """Return a reason string if *path* is a test/scoring/build file, else None."""
    p = path.replace("\\", "/").lower()
    parts = [seg for seg in p.split("/") if seg and seg != "."]
    base = parts[-1] if parts else p
    if any(fnmatch.fnmatch(base, g.lower()) for g in _CHEAT_CONFIG_GLOBS):
        return f"scoring/build/config file ({base})"
    if any(fnmatch.fnmatch(base, g.lower()) for g in _TEST_FILE_GLOBS):
        return f"test file ({base})"
    if any(seg in _TEST_DIR_SEGMENTS for seg in parts[:-1]):
        return f"file under a test directory ({path})"
    if any(sub in p for sub in _TEST_PATH_SUBSTRINGS):
        return f"file under a test source root ({path})"
    return None


def _patched_paths(patch: str) -> set[str]:
    """Every repo path the diff touches, across +++/---/diff-git headers so a
    rename or a header-only change can't hide a touched test file."""
    paths: set[str] = set()
    for rx, groups in ((_DIFF_PLUSPLUS_RE, (1,)), (_DIFF_MINUS_RE, (1,)),
                       (_DIFF_GIT_RE, (1, 2))):
        for m in rx.finditer(patch):
            for g in groups:
                norm = _norm_diff_path(m.group(g))
                if norm:
                    paths.add(norm)
    return paths


# ALLOWLIST, not blocklist. A final-stage signal is trustworthy ONLY if it is an
# explicit success or a LEGITIMATE NEGATIVE outcome (the code was fairly built/run
# and genuinely failed). Anything else — CHEAT_DETECTED, OUTPUT_MISSING,
# PARSER_NO_MATCH, GO_TEST_CRASH, INFRA_*, NO_TESTS_DEFINED, UNCLASSIFIED, an
# unknown/novel status — must QUARANTINE. Compared uppercased (real data has "ok").
_TRUSTWORTHY_OK = {"OK", "TESTS_RAN"}
# The code was fairly attempted and legitimately failed (real solve, low score):
_LEGIT_NEGATIVE = {"COMPILE_FAILED", "COMPILE_FAILED_MODEL", "BUILD_FAILED"}
_TRUSTWORTHY_STATUSES = _TRUSTWORTHY_OK | _LEGIT_NEGATIVE
# Explicit forgery — never trust, and never treat as a mere "recovered" hiccup.
_CHEAT_STATUS = "CHEAT_DETECTED"
_OUTCOME_STAGE_ORDER = ("stage3", "stage2", "stage1")


def _status_trustworthy(st) -> bool:
    """A stage's signal is trustworthy iff it is an allowlisted status, OR it
    carries a real numeric score with no status string (the java convention)."""
    s = st.eval_status
    if s == _CHEAT_STATUS:
        return False
    if s in _TRUSTWORTHY_STATUSES:
        return True
    if s == "":                       # no status field -> rely on score presence
        return st.has_score
    return False                      # any other non-empty status is untrusted


# --------------------------------------------------------------------------- #
# Layer 0 — legitimacy gate
# --------------------------------------------------------------------------- #
def _outcome_signal_trustworthy(b: TrajectoryBundle) -> _Outcome:
    # The FINAL stage's signal decides trust; an earlier untrusted status that the
    # pipeline recovered from (final is trustworthy) is a normal intermediate — but
    # an explicit CHEAT_DETECTED at ANY stage is fatal (never "recovered").
    cheating = {k: st.eval_status for k, st in b.stages.items()
                if st.present and st.eval_status == _CHEAT_STATUS}
    if cheating:
        return _Outcome(CheckStatus.FAIL,
                        f"CHEAT_DETECTED by the eval: {list(cheating)}",
                        {"cheat_stages": cheating})
    final = next((b.stages[k] for k in _OUTCOME_STAGE_ORDER
                  if b.stages[k].present and b.stages[k].record), None)
    if final is None:
        return _Outcome(CheckStatus.ERROR, "no stage records to check")
    if not _status_trustworthy(final):
        return _Outcome(CheckStatus.FAIL,
                        f"final stage '{final.key}' signal is untrustworthy "
                        f"(status={final.eval_status or 'MISSING'}, has_score={final.has_score})",
                        {"final_stage": final.key, "final_status": final.eval_status or "MISSING",
                         "has_score": final.has_score})
    untrusted_earlier = {k: (st.eval_status or "MISSING") for k, st in b.stages.items()
                         if st.present and st.key != final.key and not _status_trustworthy(st)}
    status_label = final.eval_status or ("scored" if final.has_score else "no-status")
    msg = f"final stage '{final.key}' produced a trustworthy signal ({status_label})"
    if untrusted_earlier:
        msg += f"; earlier non-trustworthy (recovered): {untrusted_earlier}"
    return _Outcome(CheckStatus.PASS, msg,
                    {"final_stage": final.key, "final_status": status_label,
                     "recovered_earlier": untrusted_earlier})


def _pipeline_complete(b: TrajectoryBundle) -> _Outcome:
    if not b.pipeline_results:
        return _Outcome(CheckStatus.FAIL, "pipeline_results.json missing/empty")
    missing = [k for k in ("stage1", "stage2", "stage3") if not b.stages[k].record]
    err = b.top_level_error
    if err:
        return _Outcome(CheckStatus.FAIL, f"fatal error: {err}", {"error": err})
    if missing:
        return _Outcome(CheckStatus.FAIL,
                        f"missing stage records: {missing}", {"missing_stages": missing})
    return _Outcome(CheckStatus.PASS, "all three stages recorded, no fatal error")


def _modules_done(b: TrajectoryBundle) -> _Outcome:
    mods = b.all_modules()
    if not mods:
        return _Outcome(CheckStatus.ERROR, "no module directories found to verify")
    incomplete = [m.name for m in mods if not m.done or m.needs_retry]
    if incomplete:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(incomplete)} module(s) not cleanly done",
                        {"incomplete": incomplete[:20], "total_modules": len(mods)})
    return _Outcome(CheckStatus.PASS, f"all {len(mods)} modules .done, none needs_retry")


def _module_artifacts_present(b: TrajectoryBundle) -> _Outcome:
    mods = [m for m in b.all_modules() if m.done]
    if not mods:
        return _Outcome(CheckStatus.ERROR, "no done modules to inspect")
    gaps = []
    for m in mods:
        required = [
            ("turns.jsonl", m.has_turns),
            ("output.json", m.has_output_json),
            ("aider.log", m.has_aider_log),
        ]
        # llm_history.txt is aider's native LLM I/O record — present only when the
        # module actually made an LLM call. A zero-turn module (e.g. a stage-3 test
        # already green) legitimately has none. `did_llm_work` is harness-computed
        # from the LLM call log (not agent-controllable), so it is the trustworthy
        # signal here — NOT git_patch, which is the cumulative repo diff duplicated
        # onto every module (incl. zero-turn ones), so it cannot indicate per-module work.
        if m.did_llm_work:
            required.append(("llm_history.txt", m.has_llm_history))
        missing = [name for name, has in required if not has]
        if missing:
            gaps.append({"module": m.name, "missing": missing, "did_llm_work": m.did_llm_work})
    if gaps:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(gaps)} module(s) missing required artifacts", {"gaps": gaps[:20]})
    return _Outcome(CheckStatus.PASS,
                    f"all {len(mods)} done modules have their required artifacts")


def _output_json_integrity(b: TrajectoryBundle) -> _Outcome:
    mods = [m for m in b.all_modules() if m.has_output_json]
    if not mods:
        return _Outcome(CheckStatus.ERROR, "no output.json files to inspect")
    bad = []
    for m in mods:
        if m.output_json is None:
            bad.append({"module": m.name, "reason": "unparseable"})
        elif "total_cost" not in m.metrics:
            bad.append({"module": m.name, "reason": "metrics.total_cost missing"})
    if bad:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(bad)} output.json integrity issue(s)", {"bad": bad[:20]})
    return _Outcome(CheckStatus.PASS, f"all {len(mods)} output.json parse and carry metrics")


def _test_files_untouched(b: TrajectoryBundle) -> _Outcome:
    # Only the full-tree diff (model_changes.diff) can reveal a test-file edit; the
    # per-module output.json git_patch is scoped to the module's own source files.
    has_full_diff = any(st.has_model_changes_diff for st in b.stages.values())
    did_work = [m for m in b.all_modules() if m.git_patch.strip() or m.did_llm_work]
    if not has_full_diff:
        if did_work:
            # cannot decide -> must NOT PASS-by-absence (a test edit could be hidden)
            return _Outcome(CheckStatus.ERROR,
                            "cannot scan for test tampering: no model_changes.diff "
                            "(full-tree diff) recorded although modules did work")
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no edits and no full-tree diff to scan")
    hits: dict[str, str] = {}
    for patch in b.all_agent_patches():
        for path in _patched_paths(patch):
            reason = _is_cheatable_path(path)
            if reason:
                hits[path] = reason
    if hits:
        return _Outcome(CheckStatus.FAIL,
                        f"agent patch touches {len(hits)} test/scoring file(s)",
                        {"tampered": hits})
    return _Outcome(CheckStatus.PASS, "agent patch confined to non-test source files")


def _denominator_sanity(b: TrajectoryBundle) -> _Outcome:
    # Catch only UNAMBIGUOUS denominator contradictions (the false-0/N class):
    #   (a) eval_status == OK yet no tests were counted  -> inventory collapsed;
    #   (b) num_passed > num_tests                        -> impossible score.
    # A zero denominator with a "code didn't build/run" status (COMPILE_FAILED) is
    # a LEGITIMATE negative outcome, not a collapse, and must not fire here.
    problems = []
    for k, st in b.stages.items():
        if not st.present:
            continue
        if st.eval_status in _TRUSTWORTHY_OK and (st.num_tests is None or st.num_tests <= 0):
            problems.append({"stage": k, "reason": "eval_status OK but no tests counted",
                             "num_tests": st.num_tests})
        elif (st.num_passed is not None and st.num_tests is not None
              and st.num_passed > st.num_tests):
            problems.append({"stage": k, "reason": "num_passed exceeds num_tests",
                             "num_passed": st.num_passed, "num_tests": st.num_tests})
    if problems:
        return _Outcome(CheckStatus.FAIL,
                        "stage(s) with a contradictory test inventory", {"problems": problems})
    return _Outcome(CheckStatus.PASS, "no denominator contradictions")


# --------------------------------------------------------------------------- #
# Layer 1 — per-stage outcome + structure
# --------------------------------------------------------------------------- #
def _stage_dirs_present(b: TrajectoryBundle) -> _Outcome:
    missing = [k for k, st in b.stages.items() if st.dir is None or not st.modules]
    if missing:
        return _Outcome(CheckStatus.FAIL,
                        f"stage(s) with no module output: {missing}",
                        {"empty_stages": missing})
    counts = {k: len(st.modules) for k, st in b.stages.items()}
    return _Outcome(CheckStatus.PASS, f"all stages produced modules: {counts}")


def _draft_modules_addressed(b: TrajectoryBundle) -> _Outcome:
    st = b.stages["stage1"]
    if st.dir is None or not st.modules:
        return _Outcome(CheckStatus.ERROR, "draft stage produced no modules")
    # If a persisted target manifest exists, verify EVERY expected file was touched
    # (the complete "all N modules addressed" check). Else fall back to "produced edits".
    from .manifest import load_manifest
    manifest = load_manifest(b.run_dir)
    if manifest and manifest.get("stage1"):
        expected = set(manifest["stage1"])
        touched: set[str] = set()
        for patch in ([m.git_patch for m in st.modules] + st.model_changes_diffs):
            touched |= _patched_paths(patch)
        missing = sorted(expected - touched)
        if missing:
            return _Outcome(CheckStatus.FAIL,
                            f"draft did not touch {len(missing)}/{len(expected)} target file(s)",
                            {"missing": missing[:20], "expected": len(expected)})
        return _Outcome(CheckStatus.PASS,
                        f"draft touched all {len(expected)} manifest target files")
    has_any_patch = any(m.git_patch for m in st.modules) or any(
        d.strip() for d in st.model_changes_diffs)
    empty = [m.name for m in st.modules if not m.git_patch]
    if not has_any_patch:
        return _Outcome(CheckStatus.FAIL, "draft stage produced no code edits at all",
                        {"modules": [m.name for m in st.modules]})
    if empty:
        return _Outcome(CheckStatus.PASS,
                        f"draft produced edits ({len(empty)} module(s) had no own-patch; "
                        "cumulative diff present)", {"no_own_patch": empty})
    return _Outcome(CheckStatus.PASS, f"all {len(st.modules)} draft modules produced a patch")


def _no_regression(b: TrajectoryBundle) -> _Outcome:
    scored = [(k, b.stages[k]) for k in ("stage1", "stage2", "stage3")
              if b.stages[k].present and b.stages[k].has_score]
    if len(scored) < 2:
        return _Outcome(CheckStatus.NOT_APPLICABLE, "fewer than two scored stages to compare")
    # The frozen inventory must be constant across stages; a shifting denominator
    # is itself an anomaly (and would let num_passed rise while the true rate falls).
    denoms = {st.num_tests for _, st in scored}
    if len(denoms) > 1:
        return _Outcome(CheckStatus.FAIL,
                        "test inventory (num_tests) changed across stages — not frozen",
                        {"num_tests_by_stage": {k: st.num_tests for k, st in scored}})
    seq = [(k, st.num_passed) for k, st in scored]
    drops = [{"from": ka, "to": kb, "passed": [va, vb]}
             for (ka, va), (kb, vb) in zip(seq, seq[1:]) if vb < va]
    if drops:
        return _Outcome(CheckStatus.FAIL, "passing-test count regressed across stages",
                        {"drops": drops})
    return _Outcome(CheckStatus.PASS, "passing-test count non-decreasing on a frozen inventory",
                    {"sequence": [{"stage": k, "num_passed": v} for k, v in seq]})


def _test_stage_outcome(b: TrajectoryBundle) -> _Outcome:
    st = b.stages["stage3"]
    if not st.present or not st.has_score or st.pass_rate is None:
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no scored test-refine stage")
    ev = {"pass_rate": round(st.pass_rate, 4), "num_passed": st.num_passed, "num_tests": st.num_tests}
    if st.pass_rate >= 1.0:
        return _Outcome(CheckStatus.PASS, "final stage passed all frozen tests", ev)
    return _Outcome(CheckStatus.FAIL,
                    f"final stage pass_rate={st.pass_rate:.3f} (partial solve)", ev)


def _stage_handoff_continuity(b: TrajectoryBundle) -> _Outcome:
    # Stages 2/3 run with override=false and build on stage 1's tree. A light,
    # artifact-only proxy: the later stages carry a non-empty cumulative diff
    # (they operated on an already-populated tree, not a reset one).
    later = []
    for k in ("stage2", "stage3"):
        st = b.stages[k]
        if st.dir is None:
            continue
        later.append((k, any(d.strip() for d in st.model_changes_diffs)))
    if not later:
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no later-stage dirs to inspect")
    empty = [k for k, ok in later if not ok]
    if empty:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        f"no cumulative diff recorded for {empty} (cannot confirm handoff)")
    return _Outcome(CheckStatus.PASS, "later stages carry a cumulative diff (built on prior tree)")


# --------------------------------------------------------------------------- #
# Layer 0/1 — trajectory-CONTENT checks (from the structured OpenHands event log
# in each module's output.json; verified identical across all 8 languages).
# --------------------------------------------------------------------------- #
def _iter_content(b: TrajectoryBundle):
    for st in b.stages.values():
        for m in st.modules:
            c = module_content(m, st.key)
            if c is not None and c.has_conversation:
                yield st.key, m, c


def _fabricated_observation(b: TrajectoryBundle) -> _Outcome:
    # A legitimate trajectory's every observation is caused by a recorded action.
    # An observation with no causing action (orphan), or one whose tool disagrees
    # with the action it cites, indicates a fabricated/tampered record. (0 across
    # 782 real modules, so this gates without false positives.)
    orphans, mism, seen = [], [], 0
    for stage, m, c in _iter_content(b):
        seen += 1
        for o in c.orphan_observations():
            orphans.append({"stage": stage, "module": m.name, "action_id": o.action_id,
                            "text": o.text[:80]})
        for o in c.mismatched_observations():
            mism.append({"stage": stage, "module": m.name, "obs_tool": o.tool_name})
    if not seen:
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no recorded conversation to check")
    if orphans or mism:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(orphans)} orphan + {len(mism)} mismatched observation(s) "
                        "— fabricated/tampered record",
                        {"orphan_observations": orphans[:15], "mismatched": mism[:15]})
    return _Outcome(CheckStatus.PASS,
                    f"every observation across {seen} module(s) is caused by a real action")


def _toolcall_correctness(b: TrajectoryBundle) -> _Outcome:
    # Tool-call integrity: no hallucinated tools (a tool not declared to the agent),
    # every action has a call id, and each module made at least one SUCCESSFUL edit
    # if it made any edit attempts (all-failed edits = a module that never applied a
    # change). Failed edits mid-run are normal (retries) and only reported.
    halluc, no_id, all_failed, seen, total_failed = [], [], [], 0, 0
    for stage, m, c in _iter_content(b):
        seen += 1
        for a in c.hallucinated_actions():
            halluc.append({"stage": stage, "module": m.name, "tool": a.tool_name})
        if c.actions_without_call_id():
            no_id.append({"stage": stage, "module": m.name,
                          "count": len(c.actions_without_call_id())})
        failed = c.failed_observations()
        total_failed += len(failed)
        # a module that ATTEMPTED edits (had file-editor errors) but never landed one
        if failed and not c.edit_observations():
            all_failed.append({"stage": stage, "module": m.name, "failed": len(failed)})
    if not seen:
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no recorded tool calls to check")
    if halluc or no_id:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(halluc)} hallucinated-tool + {len(no_id)} missing-id module(s)",
                        {"hallucinated": halluc[:15], "missing_call_id": no_id[:15],
                         "total_failed_tool_calls": total_failed})
    ev = {"modules": seen, "total_failed_tool_calls": total_failed,
          "modules_with_only_failed_edits": all_failed[:15]}
    return _Outcome(CheckStatus.PASS,
                    f"tool calls well-formed across {seen} module(s) "
                    f"({total_failed} transient failed edit(s))", ev)


import json as _json

_SCAN_FILE_CAP = 4 * 1024 * 1024      # per-file bytes scanned
# The golden breadcrumb file the harness must never expose to the agent. Only the
# concrete file path is matched (not the word "reference_commit", which a repo's
# own source could legitimately contain).
_BREADCRUMB_MARKERS = (".kaiju/entries", ".kaiju\\entries")


def _read_text_bounded(path, cap: int = _SCAN_FILE_CAP) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(cap).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _side_channel_clean(b: TrajectoryBundle) -> _Outcome:
    # The agent must DERIVE the solution, not RETRIEVE it. Two unambiguous,
    # low-false-positive leak signals (both verified absent on real runs):
    #   * the golden reference_commit SHA appearing in the agent's content, or
    #   * the .kaiju/entries breadcrumb (which carries reference_commit) being read.
    # Retrieval-command heuristics (curl/git-show/...) are deliberately NOT gated on
    # — a repo's own source can contain them, so they'd false-quarantine good runs.
    refs = set(b.reference_commits)
    ref_needles = set()
    for r in refs:
        ref_needles.add(r)
        if len(r) >= 12:
            ref_needles.add(r[:12])           # 48-bit prefix: collision-safe

    scanned = 0
    leaks: list[dict] = []

    def _scan(label: str, text: str):
        if not text:
            return
        low = text  # SHAs/paths are case-sensitive enough; keep as-is
        for n in ref_needles:
            if n in low:
                leaks.append({"where": label, "signal": "golden reference_commit leaked",
                              "needle": n[:12]})
                break
        for mk in _BREADCRUMB_MARKERS:
            if mk in low:
                leaks.append({"where": label, "signal": f"golden breadcrumb read ({mk})"})
                break

    # 1) the produced code must not embed the golden sha
    for i, patch in enumerate(b.all_agent_patches()):
        _scan(f"patch#{i}", patch)
    # 2) the recorded conversation (history thoughts/observations + turns) must not
    #    show the agent seeing the golden ref or reading the breadcrumb
    for st in b.stages.values():
        for m in st.modules:
            if isinstance(m.output_json, dict):
                _scan(f"{st.key}/{m.name}/history",
                      _json.dumps(m.output_json.get("history") or []))
            tj = m.dir / "turns.jsonl"
            if tj.exists() and scanned < 60 * 1024 * 1024:
                txt = _read_text_bounded(tj)
                scanned += len(txt)
                _scan(f"{st.key}/{m.name}/turns", txt)

    if not any(m.output_json or (m.dir / "turns.jsonl").exists()
               for st in b.stages.values() for m in st.modules):
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no trajectory content to scan")
    if leaks:
        return _Outcome(CheckStatus.FAIL,
                        f"side-channel leak: {len(leaks)} signal(s) — agent may have "
                        "retrieved rather than derived the solution",
                        {"leaks": leaks[:15]})
    note = "" if refs else " (no reference_commit available — golden-sha scan skipped)"
    return _Outcome(CheckStatus.PASS,
                    f"no golden/breadcrumb side-channel detected{note}")


CHECKS: dict[str, Callable[[TrajectoryBundle], _Outcome]] = {
    "L0.OUTCOME_SIGNAL_TRUSTWORTHY": _outcome_signal_trustworthy,
    "L0.PIPELINE_COMPLETE": _pipeline_complete,
    "L0.MODULES_DONE": _modules_done,
    "L0.MODULE_ARTIFACTS_PRESENT": _module_artifacts_present,
    "L0.OUTPUT_JSON_INTEGRITY": _output_json_integrity,
    "L0.TEST_FILES_UNTOUCHED": _test_files_untouched,
    "L0.FABRICATED_OBSERVATION": _fabricated_observation,
    "L0.SIDE_CHANNEL_CLEAN": _side_channel_clean,
    "L0.DENOMINATOR_SANITY": _denominator_sanity,
    "L1.TOOLCALL_CORRECTNESS": _toolcall_correctness,
    "L1.STAGE_DIRS_PRESENT": _stage_dirs_present,
    "L1.DRAFT_MODULES_ADDRESSED": _draft_modules_addressed,
    "L1.NO_REGRESSION": _no_regression,
    "L1.TEST_STAGE_OUTCOME": _test_stage_outcome,
    "L1.STAGE_HANDOFF_CONTINUITY": _stage_handoff_continuity,
}

# Wire checks whose modules import `_Outcome` lazily (kept out of the top-level to
# avoid an import cycle with this module).
from .reexec import independent_reexec_check as _independent_reexec_check  # noqa: E402
from .feedback import feedback_causality_check as _fb_causality  # noqa: E402
from .feedback import lint_monotone_check as _lint_monotone  # noqa: E402
from .pytest_exec import heldout_gap_check as _heldout_gap  # noqa: E402
CHECKS["L0.INDEPENDENT_REEXEC"] = _independent_reexec_check
CHECKS["L1.FEEDBACK_CAUSALITY"] = _fb_causality
CHECKS["L1.LINT_MONOTONE"] = _lint_monotone
CHECKS["L1.HELDOUT_GAP"] = _heldout_gap
