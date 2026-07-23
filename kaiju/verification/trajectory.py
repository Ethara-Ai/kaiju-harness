"""Reader that loads a produced trajectory from its consolidated run directory
into a structured, verifier-friendly bundle.

Layout consumed (grounded in the real harness output):

    <run_dir>/                                  e.g. outputs/<uuid>/runs/<model>/agent/run_1
      pipeline_results.json                     top-level + per-stage {stage1,stage2,stage3}
      stage1_draft/  stage2_lint/  stage3_tests/     (name suffix varies by language)
        <repo>/<branch>/current/<module>/
          llm_history.txt turns.jsonl output.json aider.log .done|.needs_retry|error.log
        <repo>/<branch>/current/model_changes.diff
      stage{1,2,3}_eval_artifacts/<repo>/       report.json | test_report.xml, patch.diff, ...

The reader is deliberately defensive: a crashed/partial run has missing stage
keys, missing module artifacts, or absent dirs, and the verifier must be able to
*report* that rather than crash.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_STAGE_PREFIX = {"stage1": "stage1_", "stage2": "stage2_", "stage3": "stage3_"}
_EVAL_SUFFIX = "_eval_artifacts"

# A per-module leaf directory carries at least one of these. `error.log` is
# included so a crashed module that emitted ONLY an error.log is still discovered
# (else an incomplete module would be invisible → false accept).
_MODULE_MARKERS = ("llm_history.txt", "output.json", ".done", ".needs_retry", "error.log")
# The stage-level `current/` container carries these (NOT module artifacts); a dir
# holding any of them is skipped so it isn't mistaken for a module — identified by
# structure, not by the literal name "current" (a real module could be named that).
_CONTAINER_MARKERS = ("model_changes.diff", ".agent.yaml", "eval_results.json")
_WALK_FILE_CAP = 200_000  # bound the walk so a pathological tree can't stall verify


def _read_json(p: Path) -> Any | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class ModuleInfo:
    name: str
    dir: Path
    done: bool
    needs_retry: bool
    has_turns: bool
    has_llm_history: bool
    has_output_json: bool
    has_aider_log: bool
    output_json: dict[str, Any] | None

    @property
    def git_patch(self) -> str:
        oj = self.output_json or {}
        tr = oj.get("test_result") or {}
        return tr.get("git_patch") or ""

    @property
    def metrics(self) -> dict[str, Any]:
        return (self.output_json or {}).get("metrics") or {}

    @property
    def did_llm_work(self) -> bool:
        """Whether the module made any LLM call. aider only writes its native
        ``llm_history.txt`` when it did — a module that completed with zero turns
        (e.g. a stage-3 test already green) legitimately has none."""
        m = self.metrics
        return bool(m.get("total_llm_calls") or m.get("num_turns")
                    or m.get("num_agent_turns"))


@dataclass
class StageInfo:
    key: str                       # "stage1" | "stage2" | "stage3"
    record: dict[str, Any]         # the pipeline_results.json stage record (may be {})
    dir: Path | None               # on-disk stage dir, if present
    eval_dir: Path | None
    modules: list[ModuleInfo] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.record) or self.dir is not None

    @property
    def eval_status(self) -> str:
        # NOTE: only some languages populate eval_status (java omits it entirely).
        # Trust/score decisions therefore key off *score presence*, and use
        # eval_status only to detect an explicit untrustworthy/infra flag.
        return str(self.record.get("eval_status", "")).upper()

    @property
    def num_passed(self) -> int | None:
        v = self.record.get("num_passed")
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    @property
    def num_tests(self) -> int | None:
        v = self.record.get("num_tests")
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    @property
    def pass_rate(self) -> float | None:
        v = self.record.get("pass_rate")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        # Fall back to the ratio when pass_rate is absent but counts exist.
        if self.num_passed is not None and self.num_tests and self.num_tests > 0:
            return self.num_passed / self.num_tests
        return None

    @property
    def has_score(self) -> bool:
        """The stage produced a real numeric score (a non-empty denominator).
        This — not the presence of eval_status — is what makes a stage's outcome
        verifiable, so it works uniformly across languages (incl. java)."""
        return (self.num_passed is not None
                and self.num_tests is not None and self.num_tests > 0)

    @property
    def attempted_eval(self) -> bool:
        """The pipeline tried to evaluate this stage (so a missing/zero denominator
        is a real collapse, not simply an unrun stage)."""
        r = self.record
        return ("num_passed" in r) or ("eval_time_s" in r) or bool(self.eval_status)

    @property
    def fatal(self) -> bool:
        return bool(self.record.get("sample_failed")) or "failure_reason" in self.record

    @property
    def _model_changes_paths(self) -> list[Path]:
        if self.dir is None:
            return []
        out: list[Path] = []
        for root, _dirs, files in os.walk(self.dir, followlinks=False):
            if "model_changes.diff" in files:
                out.append(Path(root) / "model_changes.diff")
        return out

    @property
    def has_model_changes_diff(self) -> bool:
        return bool(self._model_changes_paths)

    @property
    def model_changes_diffs(self) -> list[str]:
        """The cumulative stage patch(es) written under each current/ dir. This is
        the ONLY diff that can reveal a test-file edit (per-module output.json
        git_patch is scoped to the module's own source files)."""
        out: list[str] = []
        for diff in self._model_changes_paths:
            try:
                out.append(diff.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return out


@dataclass
class TrajectoryBundle:
    run_dir: Path
    pipeline_results: dict[str, Any]
    stages: dict[str, StageInfo]
    # Golden reference(s) for this task, loaded from <uuid>/datasets/entries.json.
    # reference_commit is the golden solution commit the agent must NEVER have
    # read/used; base_commit is the legitimate starting point (excluded from leak
    # detection). Empty when entries.json isn't resolvable.
    reference_commits: frozenset[str] = frozenset()
    base_commits: frozenset[str] = frozenset()

    @property
    def language(self) -> str:
        return str(self.pipeline_results.get("language") or "").lower()

    @property
    def top_level_error(self) -> str | None:
        pr = self.pipeline_results
        if pr.get("error"):
            return str(pr.get("error"))
        for key, st in self.stages.items():
            if st.fatal:
                return f"{key}:{st.record.get('failure_reason', 'sample_failed')}"
        return None

    def stage(self, key: str) -> StageInfo:
        return self.stages[key]

    def all_modules(self) -> list[ModuleInfo]:
        return [m for st in self.stages.values() for m in st.modules]

    def all_agent_patches(self) -> list[str]:
        """Every DISTINCT diff attributable to the agent (per-module git_patch +
        cumulative stage model_changes.diff), deduplicated — the per-module
        git_patch is the cumulative repo diff copied onto every module, so without
        dedup a run scans dozens of identical multi-100KB blobs."""
        out: list[str] = []
        seen: set[str] = set()
        for patch in ([m.git_patch for m in self.all_modules() if m.git_patch]
                      + [d for st in self.stages.values() for d in st.model_changes_diffs]):
            if patch and patch not in seen:
                seen.add(patch)
                out.append(patch)
        return out


def _discover_stage_dir(run_dir: Path, prefix: str) -> Path | None:
    for child in sorted(run_dir.glob(f"{prefix}*")):
        if child.is_dir() and _EVAL_SUFFIX not in child.name:
            return child
    return None


def _discover_modules(stage_dir: Path | None) -> list[ModuleInfo]:
    if stage_dir is None:
        return []
    mods: list[ModuleInfo] = []
    seen = 0
    # os.walk(followlinks=False): do NOT follow directory symlinks (an adversarial
    # symlink cycle must not hang or crash the gatekeeper).
    for root, _dirs, files in os.walk(stage_dir, followlinks=False):
        seen += len(files)
        if seen > _WALK_FILE_CAP:
            break
        fs = set(files)
        if not any(m in fs for m in _MODULE_MARKERS):
            continue
        # Skip the stage-level container dir (identified by STRUCTURE — it holds
        # model_changes.diff/.agent.yaml — not by the literal name "current", so a
        # real module legitimately named "current" is still verified).
        if any(c in fs for c in _CONTAINER_MARKERS):
            continue
        mdir = Path(root)
        done = ".done" in fs
        # A stale error.log left behind after a SUCCESSFUL resume is not an
        # incompletion (`_mark_module_done` clears .needs_retry but not error.log).
        # Only treat it as needs-retry when the module is NOT done.
        needs_retry = (".needs_retry" in fs) or ("error.log" in fs and not done)
        oj_path = mdir / "output.json"
        mods.append(ModuleInfo(
            name=mdir.name,
            dir=mdir,
            done=done,
            needs_retry=needs_retry,
            has_turns="turns.jsonl" in fs,
            has_llm_history="llm_history.txt" in fs,
            has_output_json="output.json" in fs,
            has_aider_log="aider.log" in fs,
            output_json=_read_json(oj_path) if "output.json" in fs else None,
        ))
    return mods


def load_trajectory(run_dir: str | Path) -> TrajectoryBundle:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    pr = _read_json(run_dir / "pipeline_results.json") or {}
    stages: dict[str, StageInfo] = {}
    for key, prefix in _STAGE_PREFIX.items():
        record = pr.get(key) or {}
        sdir = _discover_stage_dir(run_dir, prefix)
        edir = run_dir / f"{key}{_EVAL_SUFFIX}"
        stages[key] = StageInfo(
            key=key,
            record=record if isinstance(record, dict) else {},
            dir=sdir,
            eval_dir=edir if edir.is_dir() else None,
            modules=_discover_modules(sdir),
        )
    refs, bases = _load_golden_refs(run_dir)
    return TrajectoryBundle(run_dir=run_dir, pipeline_results=pr, stages=stages,
                            reference_commits=refs, base_commits=bases)


def _load_golden_refs(run_dir: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Read reference_commit / base_commit from the task's datasets/entries.json,
    found by walking up from run_dir (run_dir = <uuid>/runs/<model>/agent/run_N)."""
    entries_file = None
    for p in (run_dir, *run_dir.parents):
        cand = p / "datasets" / "entries.json"
        if cand.exists():
            entries_file = cand
            break
    if entries_file is None:
        return frozenset(), frozenset()
    data = _read_json(entries_file)
    if data is None:
        return frozenset(), frozenset()
    entries = data if isinstance(data, list) else [data]
    refs, bases = set(), set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("reference_commit"), str) and e["reference_commit"].strip():
            refs.add(e["reference_commit"].strip())
        if isinstance(e.get("base_commit"), str) and e["base_commit"].strip():
            bases.add(e["base_commit"].strip())
    return frozenset(refs), frozenset(bases)
