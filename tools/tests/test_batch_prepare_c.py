"""Regression tests for tools/batch_prepare_c.py -> prepare_repo_c.prepare_one call.

QC-C1-001: batch_prepare_c.prepare_single_repo previously passed an unknown
kwarg ``scrape_spec_flag=`` to ``prepare_one``, raising a ``TypeError`` that the
outer ``except Exception`` swallowed into the failures dict -> every non-dry-run
C repo silently failed to prepare. These tests pin the call to the real
signature so the drift cannot recur.
"""

import inspect
from pathlib import Path

import pytest

import tools.batch_prepare_c as bpc
from tools.prepare_repo_c import prepare_one


def test_prepare_single_repo_only_uses_valid_prepare_one_kwargs(monkeypatch):
    """Every kwarg prepare_single_repo forwards must be a real prepare_one param.

    Catches future kwarg drift statically (no fabricated names, no removed
    params).
    """
    valid_params = set(inspect.signature(prepare_one).parameters)

    captured = {}

    def _capture(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"repo": "Zahgon/demo", "base_commit": "a" * 40}

    monkeypatch.setattr(bpc, "prepare_one", _capture)

    bpc.prepare_single_repo(
        full_name="octocat/demo",
        clone_dir=Path("/tmp/does-not-matter"),
        org="Zahgon",
        cmake_flags="",
        spec_url="",
        scrape_spec=False,
        specs_dir=Path("specs"),
        dry_run=False,
    )

    unknown = set(captured["kwargs"]) - valid_params
    assert not unknown, (
        f"prepare_single_repo forwards kwarg(s) not accepted by prepare_one: "
        f"{sorted(unknown)}"
    )


@pytest.mark.parametrize(
    "scrape_spec,expected_skip_spec",
    [(True, False), (False, True)],
)
def test_scrape_spec_maps_to_skip_spec_polarity(
    monkeypatch, scrape_spec, expected_skip_spec
):
    """scrape_spec (opt-in) must invert to prepare_one's skip_spec (opt-out)."""
    captured = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return {"repo": "Zahgon/demo", "base_commit": "a" * 40}

    monkeypatch.setattr(bpc, "prepare_one", _capture)

    bpc.prepare_single_repo(
        full_name="octocat/demo",
        clone_dir=Path("/tmp/does-not-matter"),
        org="Zahgon",
        cmake_flags="",
        spec_url="",
        scrape_spec=scrape_spec,
        specs_dir=Path("specs"),
        dry_run=False,
    )

    assert captured["skip_spec"] is expected_skip_spec
    assert "scrape_spec_flag" not in captured


def test_branch_is_canonical_commit0_all(monkeypatch):
    """The dataset branch must be the canonical commit0_all, not commit0."""
    captured = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return {"repo": "Zahgon/demo", "base_commit": "a" * 40}

    monkeypatch.setattr(bpc, "prepare_one", _capture)

    bpc.prepare_single_repo(
        full_name="octocat/demo",
        clone_dir=Path("/tmp/does-not-matter"),
        org="Zahgon",
        cmake_flags="",
        spec_url="",
        scrape_spec=False,
        dry_run=False,
    )

    assert captured["branch"] == "commit0_all"
