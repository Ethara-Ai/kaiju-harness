from dataclasses import dataclass


# SCHEMA CONTRACT: New fields MUST have defaults to maintain backward
# compatibility with existing .agent.yaml files.
@dataclass
class AgentConfig:
    agent_name: str
    model_name: str
    use_user_prompt: bool
    user_prompt: str
    use_topo_sort_dependencies: bool
    add_import_module_to_context: bool
    use_repo_info: bool
    max_repo_info_length: int
    use_unit_tests_info: bool
    max_unit_tests_info_length: int
    use_spec_info: bool
    max_spec_info_length: int
    use_lint_info: bool
    run_entire_dir_lint: bool
    max_lint_info_length: int
    pre_commit_config_path: str
    run_tests: bool
    max_iteration: int
    record_test_for_each_commit: bool
    cache_prompts: bool = True
    spec_summary_max_tokens: int = 4000
    max_test_output_length: int = 15000
    model_short: str = ""  # Client-safe short model name (e.g. "opus4.6")

    # --- Thinking capture fields ---
    capture_thinking: bool = False  # Whether to capture reasoning tokens
    trajectory_md: bool = True  # Whether to write trajectory.md
    output_jsonl: bool = False  # Whether to write output.jsonl

    # --- Rust-specific difficulty levers (Stage 1 zero-shot evaluation) ---
    repo_map_tokens: int = 1024  # Aider auto repo-map budget; 0 disables
    strip_aux_docs: bool = False  # If True, README/CHANGELOG/etc hidden from agent
    blind_lint: bool = False  # If True, Stage 2 sees only "build failed: N errors" not full output
    blind_tests: bool = False  # If True, Stage 3 sees only summary line not per-test failures
    names_only_tests: bool = False  # If True, Stage 3 sees only failed test node IDs + counts (no tracebacks)
    strip_non_stubs: bool = False  # If True, hide non-stubbed source from agent context
    inject_test_files_readonly: bool = False  # QC default OFF: test source bodies are NOT injected as aider read-only context (removes oracle access to test assertions + avoids prompt bloat). Anti-cheat protection of test files is independent of this. Opt in via the pipeline --test-files-readonly flag.

    # --- Per-edit compile gate (Rust-specific; opt-in via --per-edit-compile-gate)
    # When True, agent.run() is wrapped: after each aider edit we run `cargo check`,
    # and if the edit broke a file that previously compiled we feed the errors back
    # and let the agent retry. After `compile_gate_max_retries` failed retries we
    # `git reset --hard` to the pre-edit SHA. Prevents regression cascades like the
    # 4 -> 43 compile-error storm observed on virtio-drivers Stage 2.
    per_edit_compile_gate: bool = False
    compile_gate_max_retries: int = 2

    def __post_init__(self):
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ValueError(
                f"model_name must be a non-empty string, got: {self.model_name!r}"
            )
        if not isinstance(self.agent_name, str) or not self.agent_name.strip():
            raise ValueError(
                f"agent_name must be a non-empty string, got: {self.agent_name!r}"
            )
        if not isinstance(self.max_iteration, int) or self.max_iteration < 1:
            raise ValueError(
                f"max_iteration must be a positive integer, got: {self.max_iteration!r}"
            )
        for field_name in (
            "max_repo_info_length",
            "max_unit_tests_info_length",
            "max_spec_info_length",
            "max_lint_info_length",
            "max_test_output_length",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, int) or val < 0:
                raise ValueError(
                    f"{field_name} must be a non-negative integer, got: {val!r}"
                )
