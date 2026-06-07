"""Sweep tool that surfaces incomplete dataset entries.

Walks a directory tree (either a local staging dir produced by
``--output-dir`` or a checked-out HuggingFace dataset) and reports every
entry that is *missing the* ``<repo>.bz2`` *test-ID file*, plus any entries
whose accompanying ``.status.json`` shows ``status != "ok"``.

This is the read-side of the ``.status.json`` artifact written by
:mod:`tools.generate_test_ids` (see ``MISSING_TEST_IDS_BZ2_ISSUE.md`` and the
Oracle review for context).

Two layouts are recognized:

1. **HuggingFace dataset layout** (Argo-wrapper output)::

       datasets/python/<org>_<repo>/
           <repo>.bz2                  ← the test-ID file
           <repo>_entries.json
           spec.pdf.bz2
           Docker/Dockerfile
           Docker/Dockerfile.base
           Docker/setup.sh

2. **Flat ``--output-dir`` layout** (kaiju-local)::

       <output-dir>/
           <name>.bz2
           <name>.status.json

The tool auto-detects which layout it's looking at. ``--output PATH`` writes
a machine-readable report (CSV / JSON / Markdown by file extension). Without
``--output``, a human-readable summary is printed to stdout.

Usage::

    python -m tools.report_incomplete /path/to/datasets/python
    python -m tools.report_incomplete ./test_ids --output report.csv
    python -m tools.report_incomplete /path/to/staging --output report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

__all__ = [
    "EntryReport",
    "Layout",
    "build_report",
    "detect_layout",
    "summarize",
]


class Layout:
    HF_DATASET = "hf-dataset"
    FLAT_OUTPUT_DIR = "flat-output-dir"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EntryReport:
    """One row in the report.

    Attributes
    ----------
    name
        Repo identifier (``<org>_<repo>`` for HF layout, ``<name>`` for flat).
    folder
        Path of the entry folder (HF) or output dir (flat).
    bz2_present
        Whether ``<name>.bz2`` exists.
    entries_json_present
        Whether ``<name>_entries.json`` (or breadcrumb) exists.
    spec_pdf_present
        Whether ``spec.pdf.bz2`` exists (HF layout only).
    docker_files_present
        Number of Docker/* files present (HF only — 3 expected).
    status
        Value of ``.status.json[status]`` if present, else ``"unknown"`` /
        ``"ok-inferred"`` when only ``.bz2`` is present.
    failing_module
        From ``.status.json``.
    test_count
        From ``.status.json``.
    python_version
        From ``.status.json``.
    reference_commit
        From ``.status.json``.
    system_deps_hint
        From ``.status.json``.
    stderr_snippet
        From ``.status.json``, truncated to 240 chars for report tables.

    """

    name: str
    folder: str
    bz2_present: bool
    entries_json_present: bool
    spec_pdf_present: bool
    docker_files_present: int
    status: str
    failing_module: str | None = None
    test_count: int | None = None
    python_version: str | None = None
    reference_commit: str | None = None
    system_deps_hint: list[str] = field(default_factory=list)
    stderr_snippet: str = ""

    @property
    def is_complete(self) -> bool:
        return self.bz2_present and self.status == "ok"

    @property
    def is_recoverable(self) -> bool:
        """Statuses that downstream tooling can retry by re-running prepare."""
        return self.status in {
            "no_tests",
            "import_error",
            "version_mismatch",
            "timeout",
            "collection_failed",
            "runtime_unavailable",
            "missing_bz2",  # never ran; re-prepare almost always recovers
            "unknown",
        }


# ---------------------------------------------------------------------------
# Layout detection + folder walkers
# ---------------------------------------------------------------------------


def detect_layout(root: Path) -> str:
    """Heuristically determine which directory layout ``root`` represents.

    Returns one of the ``Layout`` constants.
    """
    if not root.is_dir():
        return Layout.UNKNOWN

    # HF dataset layout: subdirectories named <org>_<repo> containing .bz2s
    subdirs = [d for d in root.iterdir() if d.is_dir() and "_" in d.name]
    if subdirs:
        # Heuristic: at least one subdir contains a .bz2 OR a Docker/ subdir OR entries.json
        for d in subdirs[:50]:
            if any(d.rglob("*.bz2")):
                return Layout.HF_DATASET
            if (d / "Docker").is_dir() or any(d.glob("*_entries.json")):
                return Layout.HF_DATASET

    # Flat layout: .bz2 / .status.json directly in root
    has_flat = bool(list(root.glob("*.bz2"))) or bool(list(root.glob("*.status.json")))
    if has_flat:
        return Layout.FLAT_OUTPUT_DIR

    return Layout.UNKNOWN


def _load_status_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _strip_repo_suffix(name: str) -> str:
    """Convert ``<repo>_entries.json`` filename stem back to ``<name>`` form."""
    if name.endswith("_entries"):
        return name[: -len("_entries")]
    return name


def _name_from_bz2(stem: str) -> str:
    """Reverse the save_test_ids normalization (``.`` → ``-``, lowercased)
    to recover a likely original name. Best-effort \u2014 collisions possible
    (e.g. ``web3-py`` vs ``web3.py``), but the report tags the canonical
    on-disk form, not the upstream repo name.
    """
    return stem


def _walk_hf_dataset(root: Path) -> list[EntryReport]:
    """Walk ``root`` assuming the HF dataset layout."""
    reports: list[EntryReport] = []
    for folder in sorted(d for d in root.iterdir() if d.is_dir() and "_" in d.name):
        name = folder.name
        bz2_candidates = list(folder.glob("*.bz2"))
        # Filter spec.pdf.bz2 out of the bz2 set
        test_id_bz2s = [b for b in bz2_candidates if b.name != "spec.pdf.bz2"]
        bz2_present = bool(test_id_bz2s)
        entries_json = next(folder.glob("*_entries.json"), None)
        entries_json_present = entries_json is not None
        spec_pdf_present = (folder / "spec.pdf.bz2").is_file()
        docker_dir = folder / "Docker"
        docker_files_present = sum(
            (docker_dir / f).is_file()
            for f in ("Dockerfile", "Dockerfile.base", "setup.sh")
        )

        # Look for a status.json alongside (kaiju may write one here in the future)
        status_data = None
        for candidate in folder.glob("*.status.json"):
            status_data = _load_status_json(candidate)
            if status_data:
                break

        status = "ok" if (status_data is None and bz2_present) else (
            (status_data or {}).get("status", "unknown")
        )
        if status_data is None and not bz2_present:
            status = "missing_bz2"

        reports.append(
            EntryReport(
                name=name,
                folder=str(folder),
                bz2_present=bz2_present,
                entries_json_present=entries_json_present,
                spec_pdf_present=spec_pdf_present,
                docker_files_present=docker_files_present,
                status=status,
                failing_module=(status_data or {}).get("failing_module"),
                test_count=(status_data or {}).get("test_count"),
                python_version=(status_data or {}).get("python_version"),
                reference_commit=(status_data or {}).get("reference_commit"),
                system_deps_hint=(status_data or {}).get("system_deps_hint") or [],
                stderr_snippet=((status_data or {}).get("stderr_snippet") or "")[:240],
            )
        )
    return reports


def _walk_flat_output_dir(root: Path) -> list[EntryReport]:
    """Walk ``root`` assuming the flat ``--output-dir`` layout."""
    # Union of names found in either *.bz2 or *.status.json
    by_name: dict[str, dict[str, Path]] = {}
    for path in root.glob("*.bz2"):
        if path.name == "spec.pdf.bz2":
            continue
        by_name.setdefault(path.stem, {})["bz2"] = path
    for path in root.glob("*.status.json"):
        stem = path.name[: -len(".status.json")]
        by_name.setdefault(stem, {})["status"] = path

    reports: list[EntryReport] = []
    for name in sorted(by_name):
        files = by_name[name]
        bz2_present = "bz2" in files
        status_data = _load_status_json(files["status"]) if "status" in files else None

        status = (status_data or {}).get("status")
        if status is None:
            status = "ok" if bz2_present else "missing_bz2"

        reports.append(
            EntryReport(
                name=name,
                folder=str(root),
                bz2_present=bz2_present,
                entries_json_present=False,
                spec_pdf_present=False,
                docker_files_present=0,
                status=status,
                failing_module=(status_data or {}).get("failing_module"),
                test_count=(status_data or {}).get("test_count"),
                python_version=(status_data or {}).get("python_version"),
                reference_commit=(status_data or {}).get("reference_commit"),
                system_deps_hint=(status_data or {}).get("system_deps_hint") or [],
                stderr_snippet=((status_data or {}).get("stderr_snippet") or "")[:240],
            )
        )
    return reports


def build_report(root: Path, *, layout: str | None = None) -> list[EntryReport]:
    """Scan ``root`` and return one :class:`EntryReport` per discovered entry.

    Automatic layout detection by default; pass ``layout`` to force one.
    """
    resolved = layout or detect_layout(root)
    if resolved == Layout.HF_DATASET:
        return _walk_hf_dataset(root)
    if resolved == Layout.FLAT_OUTPUT_DIR:
        return _walk_flat_output_dir(root)
    logger.warning("Unknown layout for %s — returning empty report", root)
    return []


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def summarize(reports: list[EntryReport]) -> dict:
    """Build a counts-and-buckets summary for human consumption."""
    total = len(reports)
    complete = sum(1 for r in reports if r.is_complete)
    missing_bz2 = [r for r in reports if not r.bz2_present]
    non_ok = [r for r in reports if r.bz2_present and r.status != "ok"]
    status_breakdown = Counter(r.status for r in reports)

    missing_both = [
        r for r in missing_bz2
        if not r.spec_pdf_present and not r.entries_json_present
    ]

    by_failing_module = Counter(
        r.failing_module for r in reports if r.failing_module
    )

    return {
        "total": total,
        "complete": complete,
        "incomplete": total - complete,
        "missing_bz2": len(missing_bz2),
        "missing_both_bz2_and_spec": len(missing_both),
        "non_ok_status": len(non_ok),
        "status_breakdown": dict(status_breakdown),
        "by_failing_module": dict(by_failing_module),
        "incomplete_recoverable": sum(1 for r in reports if not r.is_complete and r.is_recoverable),
        "incomplete_unrecoverable": sum(
            1 for r in reports
            if not r.is_complete and not r.is_recoverable
        ),
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


_CSV_FIELDS = (
    "name", "folder", "status", "bz2_present", "entries_json_present",
    "spec_pdf_present", "docker_files_present", "failing_module",
    "test_count", "python_version", "reference_commit",
    "system_deps_hint", "stderr_snippet",
)


def write_csv(reports: list[EntryReport], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_FIELDS)
        for r in reports:
            writer.writerow([
                r.name,
                r.folder,
                r.status,
                r.bz2_present,
                r.entries_json_present,
                r.spec_pdf_present,
                r.docker_files_present,
                r.failing_module or "",
                r.test_count if r.test_count is not None else "",
                r.python_version or "",
                r.reference_commit or "",
                ";".join(r.system_deps_hint),
                r.stderr_snippet.replace("\n", " "),
            ])


def write_json(reports: list[EntryReport], summary: dict, path: Path) -> None:
    payload = {
        "summary": summary,
        "entries": [_report_to_dict(r) for r in reports],
        "_schema": "kaiju-incomplete-report/1",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _report_to_dict(r: EntryReport) -> dict:
    return {
        "name": r.name,
        "folder": r.folder,
        "status": r.status,
        "bz2_present": r.bz2_present,
        "entries_json_present": r.entries_json_present,
        "spec_pdf_present": r.spec_pdf_present,
        "docker_files_present": r.docker_files_present,
        "failing_module": r.failing_module,
        "test_count": r.test_count,
        "python_version": r.python_version,
        "reference_commit": r.reference_commit,
        "system_deps_hint": r.system_deps_hint,
        "stderr_snippet": r.stderr_snippet,
    }


def write_markdown(reports: list[EntryReport], summary: dict, path: Path) -> None:
    lines: list[str] = []
    lines.append("# Kaiju test-ID completeness report")
    lines.append("")
    lines.append(f"- **Total entries:** {summary['total']}")
    lines.append(f"- **Complete (status=ok + .bz2 present):** {summary['complete']}")
    lines.append(f"- **Incomplete:** {summary['incomplete']}")
    lines.append(f"- **Missing `.bz2`:** {summary['missing_bz2']}")
    lines.append(f"- **Missing both `.bz2` AND `spec.pdf.bz2`:** {summary['missing_both_bz2_and_spec']}")
    lines.append("")
    lines.append("## Status breakdown")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for status, count in sorted(summary["status_breakdown"].items()):
        lines.append(f"| `{status}` | {count} |")
    lines.append("")
    if summary["by_failing_module"]:
        lines.append("## Top failing modules")
        lines.append("")
        lines.append("| Module | Count |")
        lines.append("|---|---|")
        for mod, count in sorted(summary["by_failing_module"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{mod}` | {count} |")
        lines.append("")
    lines.append("## Incomplete entries")
    lines.append("")
    lines.append("| Name | Status | bz2 | Failing module | Notes |")
    lines.append("|---|---|---|---|---|")
    for r in reports:
        if r.is_complete:
            continue
        notes = []
        if r.system_deps_hint:
            notes.append("sys-deps: " + ", ".join(r.system_deps_hint))
        if r.python_version:
            notes.append(f"py={r.python_version}")
        if r.stderr_snippet:
            notes.append("stderr: " + r.stderr_snippet[:80].replace("|", "\\|"))
        lines.append(
            f"| `{r.name}` | `{r.status}` | {'✓' if r.bz2_present else '✗'} | "
            f"`{r.failing_module or '-'}` | {' / '.join(notes) or '-'} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary_stdout(reports: list[EntryReport], summary: dict) -> None:
    print(f"\n{'=' * 70}")
    print("Kaiju test-ID completeness report")
    print(f"{'=' * 70}")
    print(f"  Total:                       {summary['total']}")
    print(f"  Complete (OK + .bz2):        {summary['complete']}")
    print(f"  Incomplete:                  {summary['incomplete']}")
    print(f"  Missing .bz2:                {summary['missing_bz2']}")
    print(f"  Missing both .bz2 + spec:    {summary['missing_both_bz2_and_spec']}")
    print(f"  Recoverable (retry-able):    {summary['incomplete_recoverable']}")
    print(f"  Unrecoverable (sys-deps):    {summary['incomplete_unrecoverable']}")
    print("\n  Status breakdown:")
    for status, count in sorted(summary["status_breakdown"].items()):
        print(f"    {status:25} {count}")
    if summary["by_failing_module"]:
        print("\n  Top failing modules:")
        for mod, count in sorted(summary["by_failing_module"].items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {mod:25} {count}")
    print()
    incomplete = [r for r in reports if not r.is_complete]
    if incomplete:
        print("  Incomplete entries (first 30):")
        for r in incomplete[:30]:
            module = f" [{r.failing_module}]" if r.failing_module else ""
            print(f"    {r.status:22} {r.name}{module}")
        if len(incomplete) > 30:
            print(f"    ... and {len(incomplete) - 30} more (use --output for full list)")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report incomplete kaiju dataset entries (missing test-IDs)."
    )
    parser.add_argument(
        "root",
        type=str,
        help="Directory to scan (HF dataset layout or flat output-dir).",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Write report to PATH (format inferred from extension: .csv, .json, .md). "
             "Without --output, a summary is printed to stdout.",
    )
    parser.add_argument(
        "--layout",
        choices=[Layout.HF_DATASET, Layout.FLAT_OUTPUT_DIR],
        default=None,
        help="Force a specific layout (default: auto-detect).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        logger.error("Directory not found: %s", root)
        return 2

    layout = args.layout or detect_layout(root)
    if layout == Layout.UNKNOWN:
        logger.error(
            "Could not detect a recognized layout in %s. "
            "Use --layout {%s,%s} to force one.",
            root, Layout.HF_DATASET, Layout.FLAT_OUTPUT_DIR,
        )
        return 3

    logger.info("Scanning %s as %s ...", root, layout)
    reports = build_report(root, layout=layout)
    summary = summarize(reports)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        suffix = out.suffix.lower()
        if suffix == ".csv":
            write_csv(reports, out)
        elif suffix in {".json", ".jsonl"}:
            write_json(reports, summary, out)
        elif suffix in {".md", ".markdown"}:
            write_markdown(reports, summary, out)
        else:
            logger.error("Unknown output format: %s (use .csv, .json, or .md)", suffix)
            return 4
        logger.info("Wrote %d entries to %s", len(reports), out)
    else:
        print_summary_stdout(reports, summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
