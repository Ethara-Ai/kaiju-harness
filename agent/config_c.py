"""C agent configuration CLI for commit0.

Mirrors agent/config_go.py but uses C-specific defaults:
- config file: .agent.c.yaml
- commit0 config: .commit0.c.yaml
- C-specific user prompt (STUB_PANIC marker, test_*.c read-only)
"""

import logging

import typer

from agent.agent_utils_c import write_agent_config
from agent.run_agent_c import run_agent, RUN_AGENT_LOG_DIR

logger = logging.getLogger(__name__)

agent_c_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
    help="C agent for Commit-0. Configure and run LLM agents on C repositories.",
)


@agent_c_app.command()
def config(
    agent_name: str = typer.Argument(
        ...,
        help="Agent to use (only 'aider' supported)",
    ),
    model_name: str = typer.Option(
        "claude-3-5-sonnet-20240620",
        help="Model to use, check https://aider.chat/docs/llms.html",
    ),
    use_user_prompt: bool = typer.Option(False, help="Use custom user prompt"),
    user_prompt: str = typer.Option(
        "You need to complete the implementations for all stubbed functions "
        "(those whose body calls `STUB_PANIC(\"...\")`) and pass the unit tests.\n"
        "Do not change function signatures or modify any test file "
        "(files under tests/, test/, or matching test_*.c / *_test.c / "
        "check_*.c). Code MUST compile before any test will run: if a function "
        "is incomplete, leave a minimal placeholder that builds.",
        help="User prompt to use",
    ),
    topo_sort_dependencies: bool = typer.Option(
        False, help="Not used for C (no module-graph equivalent)"
    ),
    add_import_module_to_context: bool = typer.Option(
        False, help="Not used for C"
    ),
    run_tests: bool = typer.Option(False, help="Run tests after agent finishes"),
    max_iteration: int = typer.Option(3, help="Maximum iterations"),
    use_repo_info: bool = typer.Option(False, help="Include repository structure"),
    max_repo_info_length: int = typer.Option(10000, help="Max repo info length"),
    use_unit_tests_info: bool = typer.Option(
        False, help="Include test file contents"
    ),
    max_unit_tests_info_length: int = typer.Option(
        10000, help="Max test info length"
    ),
    use_spec_info: bool = typer.Option(False, help="Include spec information"),
    max_spec_info_length: int = typer.Option(10000, help="Max spec info length"),
    spec_summary_max_tokens: int = typer.Option(
        4000, help="Max tokens for spec summarization LLM call"
    ),
    use_lint_info: bool = typer.Option(False, help="Include lint results"),
    max_lint_info_length: int = typer.Option(10000, help="Max lint info length"),
    run_entire_dir_lint: bool = typer.Option(
        True, help="Lint entire project (C default)"
    ),
    record_test_for_each_commit: bool = typer.Option(
        False, help="Record test per commit"
    ),
    cache_prompts: bool = typer.Option(True, help="Enable prompt caching"),
    max_test_output_length: int = typer.Option(
        15000, help="Max test output before summarization"
    ),
    pre_commit_config_path: str = typer.Option("", help="Not used for C"),
    agent_config_file: str = typer.Option(
        ".agent.c.yaml", help="Agent config file path"
    ),
) -> None:
    """Configure the C agent."""
    if use_user_prompt:
        user_prompt = typer.prompt("Please enter your user prompt")

    agent_config = {
        "agent_name": agent_name,
        "model_name": model_name,
        "use_user_prompt": use_user_prompt,
        "user_prompt": user_prompt,
        "run_tests": run_tests,
        "use_topo_sort_dependencies": topo_sort_dependencies,
        "add_import_module_to_context": add_import_module_to_context,
        "max_iteration": max_iteration,
        "use_repo_info": use_repo_info,
        "max_repo_info_length": max_repo_info_length,
        "use_unit_tests_info": use_unit_tests_info,
        "max_unit_tests_info_length": max_unit_tests_info_length,
        "use_spec_info": use_spec_info,
        "max_spec_info_length": max_spec_info_length,
        "spec_summary_max_tokens": spec_summary_max_tokens,
        "use_lint_info": use_lint_info,
        "max_lint_info_length": max_lint_info_length,
        "run_entire_dir_lint": run_entire_dir_lint,
        "pre_commit_config_path": pre_commit_config_path,
        "record_test_for_each_commit": record_test_for_each_commit,
        "cache_prompts": cache_prompts,
        "max_test_output_length": max_test_output_length,
    }

    write_agent_config(agent_config_file, agent_config)


@agent_c_app.command()
def run(
    branch: str = typer.Argument(..., help="Branch for the agent to commit changes"),
    override_previous_changes: bool = typer.Option(
        False, help="Override previous agent changes"
    ),
    backend: str = typer.Option(
        "docker", help="Test backend (docker/modal/e2b)"
    ),
    agent_config_file: str = typer.Option(
        ".agent.c.yaml", help="Agent config file"
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.c.yaml", help="Commit0 C config file"
    ),
    log_dir: str = typer.Option(
        str(RUN_AGENT_LOG_DIR.resolve()), help="Log directory"
    ),
    max_parallel_repos: int = typer.Option(1, help="Max parallel repos"),
    display_repo_progress_num: int = typer.Option(5, help="Display progress count"),
) -> None:
    """Run the C agent on repositories."""
    run_agent(
        branch,
        override_previous_changes,
        backend,
        agent_config_file,
        commit0_config_file,
        log_dir,
        max_parallel_repos,
        display_repo_progress_num,
    )


if __name__ == "__main__":
    agent_c_app()
