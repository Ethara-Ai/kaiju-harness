from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class JavaAgentConfig:
    agent_name: str = "aider"
    model: str = "gpt-4"
    system_prompt_path: str = "agent/prompts/java_system_prompt.md"
    user_prompt: str = (
        "Implement the stubbed methods in this Java file. "
        "Replace all methods that `throw new UnsupportedOperationException(\"STUB: not implemented\")` "
        "with working implementations that pass the existing tests. Follow the existing code style."
    )
    timeout: int = 1800
    max_iteration: int = 3
    file_extensions: List[str] = field(default_factory=lambda: [".java"])
    skip_dirs: List[str] = field(
        default_factory=lambda: ["target", "build", ".gradle", ".mvn"]
    )
    skip_files: List[str] = field(
        default_factory=lambda: ["module-info.java", "package-info.java"]
    )
    compile_check: bool = True
    run_tests: bool = True
    # Stage 2 (lint) selector — mirrors python/go/rust/c/js. When True the agent
    # runs the compile/lint command FIRST and drives fixes from its errors
    # (lint_first) instead of re-sending the draft "implement stubs" prompt. Stage 1
    # (draft) and stage 3 (tests) leave this False.
    run_entire_dir_lint: bool = False
    build_system: Optional[str] = None
    java_version: str = "17"
    cache_prompts: bool = True
    use_repo_info: bool = False
    max_repo_info_length: int = 10000
    use_unit_tests_info: bool = True
    max_unit_tests_info_length: int = 15000
    max_test_output_length: int = 15000
    use_spec_info: bool = False
    max_spec_info_length: int = 10000
    spec_summary_max_tokens: int = 4000
    # Trajectory / thinking capture
    capture_thinking: bool = False
    trajectory_md: bool = True
    output_jsonl: bool = False
    record_test_for_each_commit: bool = False
    model_short: str = ""
    blind_lint: bool = False
    blind_tests: bool = False
    names_only_tests: bool = False
    strip_non_stubs: bool = False
    inject_test_files_readonly: bool = True

    # QC-C5-001/C5-002: canonical accessor parity. Every other language reuses
    # agent.class_types.AgentConfig, whose model field is `model_name`; Java is
    # the only config that names it `model`. Renaming the stored field would
    # require a coordinated edit of the non-config call sites
    # (commit0/cli_java.py, agent/agents_java.py, agent/run_agent_java.py) and
    # the existing .commit0.java construction path, so instead we expose
    # `model_name` as a read/write alias. This lets any code (present or future)
    # treat JavaAgentConfig like the shared schema via `.model_name` without
    # breaking the established `.model` accessor.
    @property
    def model_name(self) -> str:
        return self.model

    @model_name.setter
    def model_name(self, value: str) -> None:
        self.model = value

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError(
                f"model must be a non-empty string, got: {self.model!r}"
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
            "max_test_output_length",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, int) or val < 0:
                raise ValueError(
                    f"{field_name} must be a non-negative integer, got: {val!r}"
                )
