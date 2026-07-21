"""Regressions for the still-live harness bugs in BUGS_AND_ROOT_CAUSES.md.

Covers:
  * Bug #5 — run_commit0_build must persist stdout/stderr on failure (was
    discarded, so shard-wide build failures left no diagnostic).
  * Bug #7 — tools/scrape_pdf must expose --repo-dir and wire it to
    scrape_readme_spec() with the CORRECT argument order (the report's own fix
    directive mis-ordered specs_dir vs repo_name).

(Bug #4's collision regression lives in test_test_ids_pipeline.py.)
"""
import subprocess
import sys
from pathlib import Path

import tools.batch_prepare as bp
import tools.scrape_pdf as sp


# ---- Bug #5: run_commit0_build persists build output on failure ----

def _completed(rc, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def test_run_commit0_build_persists_output_on_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        bp, "_run",
        lambda *a, **k: _completed(1, out="BUILD STDOUT XYZ", err="daemon boom"),
    )
    dataset = tmp_path / "batch.json"
    dataset.write_text("[]")
    import logging
    caplog.set_level(logging.ERROR)

    ok = bp.run_commit0_build(dataset)

    assert ok is False
    # a .build.log sits next to the dataset with the real output
    log = dataset.with_suffix(".build.log")
    assert log.exists()
    body = log.read_text()
    assert "BUILD STDOUT XYZ" in body and "daemon boom" in body
    # and it was logged (root-causable from the console too)
    assert any("commit0 build failed" in r.message for r in caplog.records)


def test_run_commit0_build_success_writes_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "_run", lambda *a, **k: _completed(0, out="ok"))
    dataset = tmp_path / "batch.json"
    dataset.write_text("[]")
    assert bp.run_commit0_build(dataset) is True
    assert not dataset.with_suffix(".build.log").exists()


# ---- Bug #7: scrape_pdf --repo-dir wires scrape_readme_spec correctly ----

def test_scrape_pdf_repo_dir_calls_readme_with_correct_arg_order(monkeypatch, capsys):
    calls = {}

    def _fake_readme(repo_dir, specs_dir="specs", repo_name="", compress=True):
        calls.update(repo_dir=repo_dir, specs_dir=specs_dir,
                     repo_name=repo_name, compress=compress)
        return ("/specs/foo_readme_spec.pdf.bz2", "")

    monkeypatch.setattr(sp, "scrape_readme_spec", _fake_readme)
    monkeypatch.setattr(
        sys, "argv",
        ["scrape_pdf", "--repo-dir", "/clones/foo", "--name", "foo",
         "--output-dir", "/out/specs"],
    )
    sp.main()

    # output dir must NOT be passed as the repo name (the report's directive bug)
    assert calls["repo_dir"] == "/clones/foo"
    assert calls["specs_dir"] == "/out/specs"
    assert calls["repo_name"] == "foo"
    assert calls["compress"] is True
    assert "Done:" in capsys.readouterr().out


def test_scrape_pdf_repo_dir_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(sp, "scrape_readme_spec", lambda *a, **k: (None, ""))
    monkeypatch.setattr(
        sys, "argv",
        ["scrape_pdf", "--repo-dir", "/clones/foo", "--name", "foo"],
    )
    try:
        sp.main()
        assert False, "expected SystemExit on README-scrape failure"
    except SystemExit as e:
        assert e.code == 1
