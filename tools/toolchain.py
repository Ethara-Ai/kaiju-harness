"""CLI for the kaiju toolchain provisioning subsystem.

Invocation: `python -m tools.toolchain <subcommand> [args]`
See TOOLCHAIN_PROVISIONING.md for the full architecture.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer

from tools._toolchain import (
    SUPPORTED_TOOLS,
    ToolchainError,
    doctor as _doctor,
    ensure_pm,
    list_installed,
    load_config,
    uninstall as _uninstall,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Kaiju toolchain provisioning: PM auto-install, Node version switching, "
    "state journal, diagnostics. See TOOLCHAIN_PROVISIONING.md.",
)


@app.command()
def doctor(
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Print diagnostic snapshot: PATH, installed tools, versions, state journal."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = _doctor()
    if json_out:
        payload = {
            "config": {
                "trust_mode": report.config.trust_mode.value,
                "state_dir": str(report.config.state_dir),
                "no_auto_install": report.config.no_auto_install,
                "dry_run": report.config.dry_run,
                "offline": report.config.offline,
                "allow_bun_install_script": report.config.allow_bun_install_script,
                "allow_global_npm": report.config.allow_global_npm,
                "node_switcher_preference": report.config.node_switcher_preference,
                "pin_node": report.config.pin_node,
                "version_pins": report.config.version_pins,
            },
            "tools_which": report.tools_which,
            "tools_versions": report.tools_versions,
            "node_switcher": report.node_switcher,
            "state_entries_count": len(report.state_entries),
            "warnings": report.warnings,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo("== kaiju toolchain doctor ==")
    typer.echo(f"trust_mode           : {report.config.trust_mode.value}")
    typer.echo(f"state_dir            : {report.config.state_dir}")
    typer.echo(f"allow_global_npm     : {report.config.allow_global_npm}")
    typer.echo(f"no_auto_install      : {report.config.no_auto_install}")
    typer.echo(f"dry_run              : {report.config.dry_run}")
    typer.echo(f"offline              : {report.config.offline}")
    typer.echo(f"node_switcher_pref   : {report.config.node_switcher_preference}")
    typer.echo(f"detected switcher    : {report.node_switcher or '(none)'}")
    if report.config.pin_node:
        typer.echo(f"KAIJU_PIN_NODE       : {report.config.pin_node}")
    if report.config.version_pins:
        typer.echo(f"version pins         : {report.config.version_pins}")
    typer.echo("")
    typer.echo("-- tools --")
    for tool, path in sorted(report.tools_which.items()):
        ver = report.tools_versions.get(tool) or "-"
        status = path or "(not on PATH)"
        typer.echo(f"  {tool:10s} {ver:15s} {status}")
    typer.echo("")
    typer.echo(f"-- state journal ({len(report.state_entries)} entries) --")
    if report.state_entries:
        recent = report.state_entries[-5:]
        for e in recent:
            typer.echo(
                f"  [{e.get('phase','?')}] {e.get('tool','?')}={e.get('version','?')} "
                f"via {e.get('install_method','?')} @ {e.get('finished_at') or e.get('started_at')}"
            )
        if len(report.state_entries) > 5:
            typer.echo(f"  ... ({len(report.state_entries) - 5} older)")
    else:
        typer.echo("  (empty)")
    if report.warnings:
        typer.echo("")
        typer.echo("-- warnings --")
        for w in report.warnings:
            typer.echo(f"  ! {w}")


@app.command()
def provision(
    tool: str = typer.Argument(..., help=f"one of {sorted(SUPPORTED_TOOLS)}"),
    version: str | None = typer.Option(None, "--version", help="exact version to install"),
) -> None:
    """Install a package manager (yarn/pnpm/bun) into the current environment."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = ensure_pm(tool, version=version)
    except ToolchainError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"OK: {result.tool.value}={result.version} via {result.install_method.value} "
        f"at {result.install_path}"
        + (" (cached)" if result.from_cache else "")
    )


@app.command()
def uninstall(
    tool: str = typer.Argument(..., help=f"one of {sorted(SUPPORTED_TOOLS)}"),
    force: bool = typer.Option(
        False, "--force", help="remove even if installed version doesn't match journal"
    ),
) -> None:
    """Remove a package manager previously installed by this harness."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        _uninstall(tool, force=force)
    except ToolchainError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"OK: uninstalled {tool}")


@app.command("ls")
def list_cmd() -> None:
    """List tools recorded in the state journal (phase=success)."""
    entries = list_installed()
    if not entries:
        typer.echo("(no successful installs recorded)")
        return
    for e in entries:
        typer.echo(
            f"  {e.get('tool','?'):8s} {e.get('version','?'):12s} "
            f"{e.get('install_method','?'):22s} {e.get('install_path','?')}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
