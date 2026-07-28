"""Verification portability + rubric hygiene.

Pins three behaviors for run dirs produced on OTHER machines and for rubric
artifacts consumed outside the harness:

1. RUBRIC IS CONTRACT-REFERENCED, NEVER FILE-REFERENCED: no criterion text,
   generation prompt, or judge prompt names 'TRUTH.md' (the internal answer-key
   file). sanitize_criterion_text scrubs model output AND legacy frozen
   rubrics on load; from_dict still reads the pre-rename `truth_ref` key.
2. EMPTY STAGING SELF-HEALS: build_inputs falls back from datasets/entries.json
   to any *_dataset.json, and _ensure_repo clones into <uuid_root>/repos_cache
   when no local checkout has both commits (fork first, upstream fetched into
   the same clone for missing commits). Nothing usable -> actionable message,
   None (never a crash).
3. HARBOR BACKFILL: scripts/backfill_atif.py replays the exact in-pipeline
   ATIF invocation for completed runs, skips incomplete ones with a reason,
   and derives the task name the way run_pipeline.sh does.

CI-safe: no network (git/subprocess mocked), no models.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiju.verification import build_inputs as BI
from kaiju.verification import rubric as R
from kaiju.verification import judge as J

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. rubric hygiene
# ---------------------------------------------------------------------------
class TestRubricContractHygiene:
    def test_backbone_criteria_never_name_the_answer_key_file(self):
        for c in R.BACKBONE_CRITERIA:
            assert "truth" not in c["text"].lower(), c["id"]

    def test_generation_and_judge_prompts_never_name_the_file(self):
        sys_r, user_r = R.build_rubric_prompt("contract body")
        sys_j, user_j = J.build_judge_prompt("contract body", R.Rubric(R.backbone_rubric()), "digest")
        for blob in (sys_r, user_r, sys_j, user_j):
            assert "truth.md" not in blob.lower()

    def test_sanitizer_scrubs_file_references(self):
        assert R.sanitize_criterion_text("per TRUTH.md section 3") == \
            "per the behavioral contract section 3"
        assert R.sanitize_criterion_text("per truth.MD's items") == \
            "per the behavioral contract's items"
        assert R.sanitize_criterion_text("no reference here") == "no reference here"

    def test_parse_scrubs_model_output_and_accepts_both_ref_keys(self):
        text = json.dumps([
            {"id": "ts.a", "text": "Implements TRUTH.md section 2 ordering",
             "contract_ref": "TRUTH.md #2"},
            {"id": "ts.b", "text": "Handles bytes input", "truth_ref": "#3"},
        ])
        crits = R.parse_rubric_response(text)
        assert crits[0].text == "Implements the behavioral contract section 2 ordering"
        assert crits[0].contract_ref == "the behavioral contract #2"
        assert crits[1].contract_ref == "#3"   # legacy key accepted

    def test_from_dict_reads_legacy_frozen_rubrics(self):
        legacy = {"id": "ts.x", "text": "per TRUTH.md item 1", "truth_ref": "item 1"}
        c = R.Criterion.from_dict(legacy)
        assert c.contract_ref == "item 1"
        assert "truth.md" not in c.text.lower()
        # round-trip emits the new key only
        assert "contract_ref" in c.to_dict() and "truth_ref" not in c.to_dict()

    def test_frozen_rubric_artifacts_on_disk_load_clean(self):
        # Any rubric.json already frozen in this repo must load TRUTH-free.
        for p in REPO.glob("outputs/*/verification/verifiers/rubric/rubric.json"):
            rub = R.Rubric.from_dict(json.loads(p.read_text()))
            for c in rub.criteria:
                assert "truth.md" not in c.text.lower(), f"{p}: {c.id}"


# ---------------------------------------------------------------------------
# 2. empty-staging portability
# ---------------------------------------------------------------------------
def _entry(**kw):
    e = {"repo": "org/fork", "original_repo": "up/name",
         "base_commit": "a" * 40, "reference_commit": "b" * 40}
    e.update(kw)
    return e


class TestEntriesFallback:
    def test_canonical_entries_json(self, tmp_path, capsys):
        (tmp_path / "datasets").mkdir()
        (tmp_path / "datasets" / "entries.json").write_text(json.dumps([_entry()]))
        assert BI._entries(tmp_path)["repo"] == "org/fork"

    def test_falls_back_to_dataset_json_at_root(self, tmp_path, capsys):
        (tmp_path / "datasets").mkdir()
        (tmp_path / "ringbuf_dataset.json").write_text(json.dumps(_entry(repo="o/r")))
        e = BI._entries(tmp_path)
        assert e["repo"] == "o/r"
        assert "entries.json missing" in capsys.readouterr().out

    def test_nothing_usable_prints_actionable_message(self, tmp_path, capsys):
        (tmp_path / "datasets").mkdir()
        assert BI._entries(tmp_path) is None
        out = capsys.readouterr().out
        assert "entries.json" in out and "copy it from the machine" in out

    def test_incomplete_record_not_accepted(self, tmp_path):
        (tmp_path / "datasets").mkdir()
        (tmp_path / "datasets" / "entries.json").write_text(
            json.dumps([{"repo": "o/r", "base_commit": "a" * 40}]))  # no reference
        assert BI._entries(tmp_path) is None


class TestEnsureRepo:
    def test_clone_urls_fork_first_then_upstream(self):
        urls = BI._clone_urls(_entry())
        assert urls == ["https://github.com/org/fork.git",
                        "https://github.com/up/name.git"]
        # explicit URLs pass through untouched
        assert BI._clone_urls(_entry(repo="git@github.com:o/r.git"))[0] == \
            "git@github.com:o/r.git"

    def test_local_checkout_with_both_commits_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(BI, "_find_repo", lambda root, e: tmp_path / "local")
        monkeypatch.setattr(BI, "_has_commit", lambda repo, sha: True)
        cloned = []
        monkeypatch.setattr(BI, "_git", lambda a, timeout=600: cloned.append(a) or True)
        got = BI._ensure_repo(tmp_path, _entry())
        assert got == tmp_path / "local" and not cloned

    def test_missing_local_clones_fork_into_repos_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(BI, "_find_repo", lambda root, e: None)
        calls = []

        def fake_git(args, timeout=600):
            calls.append(args)
            if args[0] == "clone":
                dest = Path(args[2])
                (dest / ".git").mkdir(parents=True)
            return True
        monkeypatch.setattr(BI, "_git", fake_git)
        monkeypatch.setattr(BI, "_has_commit", lambda repo, sha: True)
        got = BI._ensure_repo(tmp_path, _entry())
        assert got == tmp_path / "repos_cache" / "up__name"
        assert calls[0][:2] == ["clone", "https://github.com/org/fork.git"]

    def test_missing_commit_fetches_upstream_then_fails_actionably(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(BI, "_find_repo", lambda root, e: None)

        def fake_git(args, timeout=600):
            if args[0] == "clone":
                (Path(args[2]) / ".git").mkdir(parents=True)
            return True
        monkeypatch.setattr(BI, "_git", fake_git)
        monkeypatch.setattr(BI, "_has_commit", lambda repo, sha: False)
        got = BI._ensure_repo(tmp_path, _entry())
        assert got is None
        assert "not reachable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3. harbor backfill
# ---------------------------------------------------------------------------
class TestBackfill:
    def _load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backfill_atif", REPO / "scripts" / "backfill_atif.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_task_name_matches_pipeline_sanitization(self):
        m = self._load()
        assert m._task_name({"dataset_short": "dataset"}) == "dataset"
        assert m._task_name({"dataset_short": "my set!"}) == "myset"
        assert m._task_name({}) == "dataset"

    def test_incomplete_run_skipped_with_reason(self, tmp_path, monkeypatch, capsys):
        m = self._load()
        run = tmp_path / "runs" / "m1" / "agent" / "run_1"
        run.mkdir(parents=True)          # no pipeline_results.json
        invoked = []
        monkeypatch.setattr(m.subprocess, "run",
                            lambda *a, **k: invoked.append(a) or
                            type("R", (), {"returncode": 0})())
        rc = m.backfill(tmp_path)
        assert not invoked
        assert "resume it first" in capsys.readouterr().out

    def test_verification_data_exported_into_harbor_tree(self, tmp_path, monkeypatch):
        # The export must carry the verification data (frozen verifiers +
        # per-run reports) at task level, and must sync even when the
        # trajectory conversion itself is skipped as already-converted.
        m = self._load()
        run = tmp_path / "runs" / "gpt-5.5" / "agent" / "run_1"
        run.mkdir(parents=True)
        (run / "pipeline_results.json").write_text(json.dumps({"dataset_short": "dataset"}))
        # verification artifacts to travel with the export
        (tmp_path / "verification" / "verifiers").mkdir(parents=True)
        (tmp_path / "verification" / "verifiers" / "TRUTH.md").write_text("contract")
        rr = tmp_path / "verification" / "results" / "gpt-5.5" / "agent" / "run_1"
        rr.mkdir(parents=True)
        (rr / "report.json").write_text(json.dumps({"gate": "accept"}))
        # pre-mark as converted so the conversion is SKIPPED
        pre = tmp_path / "Harbor_Data" / "Trajectory" / "dataset" / "gpt-5.5" / "agent" / "m"
        pre.mkdir(parents=True)
        (pre / "trajectory.json").write_text("{}")
        monkeypatch.setattr(m.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0})())
        m.backfill(tmp_path)
        exp = tmp_path / "Harbor_Data" / "Trajectory" / "dataset" / "verification"
        assert (exp / "verifiers" / "TRUTH.md").read_text() == "contract"
        assert (exp / "results" / "gpt-5.5" / "agent" / "run_1" / "report.json").exists()

    def test_completed_run_invokes_converter_with_pipeline_args(
            self, tmp_path, monkeypatch):
        m = self._load()
        run = tmp_path / "runs" / "gpt-5.5" / "agent" / "run_1"
        run.mkdir(parents=True)
        (run / "pipeline_results.json").write_text(json.dumps({"dataset_short": "dataset"}))
        cmds = []
        monkeypatch.setattr(m.subprocess, "run",
                            lambda cmd, cwd=None: cmds.append(cmd) or
                            type("R", (), {"returncode": 0})())
        m.backfill(tmp_path)
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "--kaiju-mode" in cmd and "--task-name" in cmd
        assert str(run) in cmd and str(run / "pipeline_results.json") in cmd
        assert str(tmp_path / "Harbor_Data" / "Trajectory") in cmd

# ---------------------------------------------------------------------------
# 4. language-agnostic solution digests (anchor-collapse fix)
# ---------------------------------------------------------------------------
class TestSolutionCodeExtensions:
    def test_exts_derived_from_stub_files(self):
        from kaiju.verification.solution_code import exts_from_stub_files
        assert exts_from_stub_files(["a/uuid.go", "b/hash.go"]) == (".go",)
        assert exts_from_stub_files(["x.rs", "y/z.rs"]) == (".rs",)
        assert exts_from_stub_files(["m.py"]) == (".py",)
        assert exts_from_stub_files([]) == (".py",)          # safe fallback
        assert exts_from_stub_files(["a.c", "a.h"]) == (".c", ".h")

    def test_manifest_exts_reads_per_stage_lists(self, tmp_path, monkeypatch):
        from kaiju.verification import rubric_anchor as RA
        from kaiju.verification import layout
        mp = tmp_path / "verification" / "verifiers" / "target_manifest.json"
        mp.parent.mkdir(parents=True)
        mp.write_text(json.dumps({"stage1": ["dce.go", "uuid.go"], "stage3": ["sql.go"]}))
        assert RA._manifest_exts(tmp_path) == (".go",)
        mp.write_text(json.dumps({"files": [{"path": "lib.rs"}]}))
        assert RA._manifest_exts(tmp_path) == (".rs",)
        mp.write_text("not json")
        assert RA._manifest_exts(tmp_path) == (".py",)       # fallback, never crash

