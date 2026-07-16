import copy
import ast
import bz2
import hashlib
import json
import git
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional
import fitz
from import_deps import ModuleSet
from graphlib import TopologicalSorter, CycleError
import yaml

from agent.class_types import AgentConfig
from commit0.harness.utils import relativize
from agent.thinking_capture import SummarizerCost

logger = logging.getLogger(__name__)

PROMPT_HEADER = ">>> Here is the Task:\n"

MODULE_SCOPE_NOTE = (
    "\n\n## Scope — implement only what is provided\n"
    "The file(s) you must implement are ALREADY added to this chat. Every stubbed "
    "file is handled in a SEPARATE session, so do NOT ask to add, request, or wait "
    "for any other file, and do NOT try to edit files outside the ones provided — "
    "the ones you need are already here. Implement the stubbed functions in the "
    "provided file(s) now, in this turn."
)

REFERENCE_HEADER = "\n\n>>> Here is the Reference for you to finish the task:\n"
REPO_INFO_HEADER = "\n\n>>> Here is the Repository Information:\n"
UNIT_TESTS_INFO_HEADER = "\n\n>>> Here are the Unit Tests Information:\n"
LINT_INFO_HEADER = "\n\n>>> Here is the Lint Information:\n"
SPEC_INFO_HEADER = "\n\n>>> Here is the Specification Information:\n"
IMPORT_DEPENDENCIES_HEADER = "\n\n>>> Here are the Import Dependencies:\n"
# prefix components:
space = "    "
branch = "│   "
# pointers:
tee = "├── "
last = "└── "


def agent_test_timeout_sec() -> int:
    """Return the per-test timeout (seconds) the agent-side commit0 CLI must use.

    Reads ``KAIJU_AGENT_TEST_TIMEOUT_SEC`` env override. Falls back to 300s on
    unset/blank/non-numeric/non-positive. Previously hardcoded to 100s which was
    too short for framework-full-suite runs (ava/node:test cannot use per-test-ID
    selection and must run the entire suite; JS/TS eval scripts wrap with
    ``timeout ${EVAL_TEST_TIMEOUT:-900}`` internally, so a 100s outer cap
    starves output). Kept safely below the 900s inactivity watchdog so a slow
    (but progressing) test run is not misattributed as an agent hang.
    """
    raw = os.environ.get("KAIJU_AGENT_TEST_TIMEOUT_SEC", "").strip()
    if not raw:
        return 300
    try:
        val = int(raw)
    except ValueError:
        return 300
    return val if val > 0 else 300


def extract_function_stubs(file_path: Path) -> List[str]:
    """Extract function stubs from a Python file, including type hints.

    Uses AST parsing instead of regex to avoid catastrophic backtracking
    on complex type annotations (e.g. typing.Callable[..., typing.Any]).
    """
    if not file_path.exists():
        logger.warning("File not found, skipping stub extraction: %s", file_path)
        return []
    with open(file_path, "r") as file:
        content = file.read()

    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        logger.debug("SyntaxError parsing %s: %s", file_path, e)
        return []

    stubs = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        args = node.args
        processed_args = []
        for arg in args.args:
            arg_str = arg.arg
            if arg.annotation:
                arg_str += f": {ast.unparse(arg.annotation)}"
            processed_args.append(arg_str)
        for arg in args.kwonlyargs:
            arg_str = arg.arg
            if arg.annotation:
                arg_str += f": {ast.unparse(arg.annotation)}"
            processed_args.append(arg_str)
        if args.vararg:
            va = f"*{args.vararg.arg}"
            if args.vararg.annotation:
                va += f": {ast.unparse(args.vararg.annotation)}"
            processed_args.append(va)
        if args.kwarg:
            kw = f"**{args.kwarg.arg}"
            if args.kwarg.annotation:
                kw += f": {ast.unparse(args.kwarg.annotation)}"
            processed_args.append(kw)

        args_str = ", ".join(processed_args)
        return_annotation = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        stubs.append(f"def {name}({args_str}){return_annotation}: ...")

    return stubs


def get_dir_info(
    dir_path: Path,
    prefix: str = "",
    max_depth: int = 10,
    include_stubs: bool = False,
    current_depth: int = 0,
    ignore_dot_files: bool = True,
) -> str:
    """A recursive generator, given a directory Path object will yield a visual
    tree structure line by line with each line prefixed by the same characters.

    Args:
    ----
    dir_path (Path): The directory to traverse
    prefix (str): The prefix to use for the current level
    max_depth (int): The maximum depth to traverse (default: infinite)
    current_depth (int): The current depth of traversal (used internally)
    ignore_dot_files (bool): Whether to ignore files/directories starting with a dot (default: True)
    include_stubs (bool): Whether to include function stubs for Python files (default: True)

    """
    if current_depth >= max_depth:
        return ""

    contents = list(dir_path.iterdir())

    if ignore_dot_files:
        contents = [c for c in contents if not c.name.startswith(".")]

    tree_string = []
    # contents each get pointers that are ├── with a final └── :
    pointers = [tee] * (len(contents) - 1) + [last]
    for pointer, path in zip(pointers, contents):
        tree_string.append(prefix + pointer + path.name)
        if path.is_dir():
            extension = branch if pointer == tee else space
            tree_string.append(
                get_dir_info(
                    path,
                    prefix=prefix + extension,
                    max_depth=max_depth,
                    include_stubs=include_stubs,
                    current_depth=current_depth + 1,
                    ignore_dot_files=ignore_dot_files,
                )
            )
        elif include_stubs and path.suffix == ".py":
            stubs = extract_function_stubs(path)
            for stub in stubs:
                tree_string.append(prefix + space + space + stub)
    return "\n".join(filter(None, tree_string))


def get_file_info(file_path: Path, prefix: str = "") -> str:
    """Return the contents of a file with a given prefix."""
    if not file_path.exists():
        logger.warning("File not found, skipping: %s", file_path)
        return ""
    tree_string = [tee + file_path.name]
    stubs = extract_function_stubs(file_path)
    for stub in stubs:
        tree_string.append(prefix + space + space + stub)
    return "\n".join(filter(None, tree_string))


def collect_test_files(directory: str) -> list[str]:
    """Collect all the test files in the directory."""
    test_files = []
    subdirs = []

    # Walk through the directory
    for root, dirs, files in os.walk(directory):
        if root.endswith("/"):
            root = root[:-1]
        # Check if 'test' is part of the folder name
        if (
            "test" in os.path.basename(root).lower()
            or os.path.basename(root) in subdirs
        ):
            for file in files:
                # Process only Python files
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    test_files.append(file_path)
            for d in dirs:
                subdirs.append(d)

    return test_files


def collect_python_files(directory: str) -> list[str]:
    """List to store all the .py filenames"""
    python_files = []

    # Walk through the directory recursively
    for root, _, files in os.walk(directory):
        for file in files:
            # Check if the file ends with '.py'
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                python_files.append(file_path)

    return python_files


# Directories that should never be treated as source code by the agent.
# Only applied when src_dir is "." (repo-root traversal) to prevent
# malformed test data, examples, and build artifacts from leaking in.
EXCLUDED_DIRS: set[str] = {
    "testing",
    "examples",
    "example",
    "benchmarks",
    "benchmark",
    "docs",
    "doc",
    "documentation",
    ".git",
    ".github",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    ".env",
}


def _find_files_to_edit(base_dir: str, src_dir: str, test_dir: str) -> tuple[list[str], list[str]]:
    """Identify files to remove content by heuristics.
    We assume source code is under [lib]/[lib] or [lib]/src.
    We exclude test code. This function would not work
    if test code doesn't have its own directory.

    Args:
    ----
        base_dir (str): The path to local library.
        src_dir (str): The directory containing source code.
        test_dir (str): The directory containing test code.
            May be comma-separated for multiple test directories
            (e.g. "tests,testing").

    Returns:
    -------
        tuple[list[str], list[str]]: (editable_source_files, test_files).
        editable_source_files have tests subtracted; test_files is the sorted list
        of test files discovered under ``test_dir`` (one per comma-separated entry).

    """
    files = [
        os.path.normpath(f)
        for f in collect_python_files(os.path.join(base_dir, src_dir))
    ]

    test_dirs = [d.strip() for d in test_dir.split(",") if d.strip()]
    test_files: set[str] = set()
    for td in test_dirs:
        test_files.update(
            os.path.normpath(f) for f in collect_test_files(os.path.join(base_dir, td))
        )
    files = list(set(files) - test_files)

    if src_dir in (".", ""):
        base = Path(base_dir)
        files = [
            f
            for f in files
            if not any(
                part in EXCLUDED_DIRS for part in Path(f).relative_to(base).parts
            )
        ]

    # don't edit __init__ files
    files = [f for f in files if "__init__" not in f]
    # don't edit __main__ files
    files = [f for f in files if "__main__" not in f]
    # don't edit conftest.py files
    files = [f for f in files if "conftest.py" not in f]
    return files, sorted(test_files)


def ignore_cycles(graph: dict) -> list[str]:
    """Ignore the cycles in the graph."""
    graph = copy.deepcopy(graph)
    ts = TopologicalSorter(graph)
    try:
        return list(ts.static_order())
    except CycleError as e:
        logger.debug("Breaking dependency cycle: %s", e.args[1] if e.args else e)
        # You can either break the cycle by modifying the graph or handle it as needed.
        # For now, let's just remove the first node in the cycle and try again.
        cycle_nodes = e.args[1]
        node_to_remove = cycle_nodes[0]
        # print(f"Removing node {node_to_remove} to resolve cycle.")
        graph.pop(node_to_remove, None)
        return ignore_cycles(graph)


def topological_sort_based_on_dependencies(
    pkg_paths: list[str],
) -> tuple[list[str], dict]:
    """Topological sort based on dependencies."""
    module_set = ModuleSet([str(p) for p in pkg_paths])

    import_dependencies = {}
    for path in sorted(module_set.by_path.keys()):
        module_name = ".".join(module_set.by_path[path].fqn)
        mod = module_set.by_name[module_name]
        try:
            imports = module_set.get_imports(mod)
            import_dependencies[path] = set([str(x) for x in imports])
        except Exception as e:
            logger.warning("Failed to resolve imports for %s: %s", path, e)
            import_dependencies[path] = set()

    import_dependencies_files = ignore_cycles(import_dependencies)

    return import_dependencies_files, import_dependencies


def files_stubbed_at_commit(
    local_repo: git.Repo,
    candidate_files: list[str],
    base_commit: str,
    stub_marker: str,
) -> set[str]:
    """Return the subset of ``candidate_files`` whose blob at ``base_commit``
    contains ``stub_marker`` — i.e. the files that were stubbed BEFORE the agent
    (or any earlier stage) touched them.

    Stubbed-set membership is a fixed dataset property of the base commit, NOT a
    fact about the current working tree. This helper enables ``target_edit_files``
    to be derived deterministically across all 3 stages: stages 2/3 resume on top
    of stage 1's implementation without resetting, so a working-tree scan would
    return an empty list once stage 1 fills the stubs — crashing stage 2/3 with
    "No target-edit source files" and yielding 0 score across every module.

    Mirrors ``agent_utils_js.py:get_target_edit_files_js`` design; callers should
    fall back to a working-tree scan if this returns an empty set (indicating the
    base blob is inaccessible or path drift).

    ``candidate_files`` may be absolute or repo-relative paths; the returned set
    preserves the input path form.
    """
    target_dir = str(local_repo.working_dir)
    stubbed: set[str] = set()
    for file_path in candidate_files:
        rel_path = (
            os.path.relpath(file_path, target_dir)
            if os.path.isabs(file_path)
            else file_path
        )
        try:
            content = local_repo.git.show(f"{base_commit}:{rel_path}")
        except Exception:  # noqa: BLE001
            # File didn't exist at base_commit (added by branch), or git blob
            # unreadable — both mean "not a stub at base", skip.
            continue
        if stub_marker in content:
            stubbed.add(file_path)
    return stubbed



def get_target_edit_files(
    local_repo: git.Repo,
    src_dir: str,
    test_dir: str,
    branch: str,
    reference_commit: str,
    use_topo_sort_dependencies: bool = True,
) -> tuple[list[str], dict, list[str]]:
    """Find the files with functions with the pass statement."""
    target_dir = str(local_repo.working_dir)
    files, test_files_abs = _find_files_to_edit(target_dir, src_dir, test_dir)
    filtered_files = []
    for file_path in files:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as file:
            content = file.read()
            # Don't undo this or reintroduce this will cause massive failure in trajectories as it skip the fill with more than 1500 lines 
            # if len(content.splitlines()) > 1500: "Deleting "
            #     logger.debug("Skipping %s: exceeds 1500 line limit", file_path)
            #     continue
            if "    pass" in content:
                # Verify the file actually has stubs by checking it differs from
                # the reference commit. Files with only abstract method `pass` or
                # intentional no-ops will be identical and should be skipped.
                rel_path = os.path.relpath(file_path, target_dir)
                diff_output = local_repo.git.diff(reference_commit, "--", rel_path)
                if not diff_output:
                    continue
                filtered_files.append(file_path)
    # Change to reference commit to get the correct dependencies
    logger.debug("Checking out reference commit %s", reference_commit)
    local_repo.git.checkout(reference_commit)

    try:
        topological_sort_files, import_dependencies = (
            topological_sort_based_on_dependencies(filtered_files)
        )
        if len(topological_sort_files) != len(filtered_files):
            if len(topological_sort_files) < len(filtered_files):
                # Find the missing elements
                missing_files = set(filtered_files) - set(topological_sort_files)
                logger.info(
                    "Topological sort: %d files, %d files not in dependency graph — appending",
                    len(topological_sort_files),
                    len(missing_files),
                )
                # Add the missing files to the end of the list
                topological_sort_files = topological_sort_files + list(missing_files)
            else:
                raise ValueError(
                    "topological_sort_files should not be longer than filtered_files"
                )
        assert len(topological_sort_files) == len(filtered_files), (
            "all files should be included"
        )
    finally:
        local_repo.git.checkout(branch)

    # Remove the base_dir prefix
    topological_sort_files = [
        file.replace(target_dir, "").lstrip("/") for file in topological_sort_files
    ]

    # Remove the base_dir prefix from import dependencies
    import_dependencies_without_prefix = {}
    for key, value in import_dependencies.items():
        key_without_prefix = key.replace(target_dir, "").lstrip("/")
        value_without_prefix = [v.replace(target_dir, "").lstrip("/") for v in value]
        import_dependencies_without_prefix[key_without_prefix] = value_without_prefix
    if use_topo_sort_dependencies:
        test_files_rel = [
            f.replace(target_dir, "").lstrip("/") for f in test_files_abs
        ]
        return topological_sort_files, import_dependencies_without_prefix, test_files_rel
    else:
        filtered_files = [
            file.replace(target_dir, "").lstrip("/") for file in filtered_files
        ]
        test_files_rel = [
            f.replace(target_dir, "").lstrip("/") for f in test_files_abs
        ]
        return filtered_files, import_dependencies_without_prefix, test_files_rel


def get_target_edit_files_from_patch(
    local_repo: git.Repo, patch: str, use_topo_sort_dependencies: bool = True
) -> tuple[list[str], dict]:
    """Get the target files from the patch."""
    working_dir = str(local_repo.working_dir)
    target_files = set()
    for line in patch.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            file_path = line.split()[1]
            if file_path.startswith("a/"):
                file_path = file_path[2:]
            if file_path.startswith("b/"):
                file_path = file_path[2:]
            target_files.add(file_path)

    target_files_list = list(target_files)
    target_files_list = [
        os.path.join(working_dir, file_path) for file_path in target_files_list
    ]

    if use_topo_sort_dependencies:
        topological_sort_files, import_dependencies = (
            topological_sort_based_on_dependencies(target_files_list)
        )
        if len(topological_sort_files) != len(target_files_list):
            if len(topological_sort_files) < len(target_files_list):
                missing_files = set(target_files_list) - set(topological_sort_files)
                topological_sort_files = topological_sort_files + list(missing_files)
            else:
                raise ValueError(
                    "topological_sort_files should not be longer than target_files_list"
                )
        assert len(topological_sort_files) == len(target_files_list), (
            "all files should be included"
        )

        topological_sort_files = [
            file.replace(working_dir, "").lstrip("/") for file in topological_sort_files
        ]
        for key, value in import_dependencies.items():
            import_dependencies[key] = [
                v.replace(working_dir, "").lstrip("/") for v in value
            ]
        return topological_sort_files, import_dependencies
    else:
        target_files_list = [
            file.replace(working_dir, "").lstrip("/") for file in target_files_list
        ]
        return target_files_list, {}


def get_message(
    agent_config: AgentConfig,
    repo_path: str,
    test_files: list[str] | None = None,
) -> tuple[str, list[SummarizerCost]]:
    """Get the message to Aider. Returns (message, summarizer_costs)."""
    spec_costs: list[SummarizerCost] = []
    prompt = f"{PROMPT_HEADER}" + agent_config.user_prompt

    #    if agent_config.use_unit_tests_info and test_file:
    #         unit_tests_info = (
    #             f"\n{UNIT_TESTS_INFO_HEADER} "
    #             + get_file_info(
    #                 file_path=Path(os.path.join(repo_path, test_file)), prefix=""
    #             )[: agent_config.max_unit_tests_info_length]
    #         )
    if agent_config.use_unit_tests_info and test_files:
        unit_tests_info = f"\n{UNIT_TESTS_INFO_HEADER} "
        for test_file in test_files:
            unit_tests_info += get_file_info(
                file_path=Path(os.path.join(repo_path, test_file)), prefix=""
            )
        unit_tests_info = unit_tests_info[: agent_config.max_unit_tests_info_length]
    else:
        unit_tests_info = ""

    if agent_config.use_repo_info:
        repo_info = (
            f"\n{REPO_INFO_HEADER} "
            + get_dir_info(
                dir_path=Path(repo_path), prefix="", max_depth=2, include_stubs=False
            )[: agent_config.max_repo_info_length]
        )
    else:
        repo_info = ""

    if agent_config.use_spec_info:
        spec_pdf_path = Path(repo_path) / "spec.pdf"
        spec_bz2_path = Path(repo_path) / "spec.pdf.bz2"
        decompress_failed = False
        if spec_bz2_path.exists() and not spec_pdf_path.exists():
            try:
                with bz2.open(str(spec_bz2_path), "rb") as in_file:
                    with open(str(spec_pdf_path), "wb") as out_file:
                        out_file.write(in_file.read())
            except Exception as e:
                logger.warning(
                    "Failed to decompress spec file %s: %s", spec_bz2_path, e
                )
                # Clean up partial file to prevent reading corrupt data
                if spec_pdf_path.exists():
                    spec_pdf_path.unlink()
                decompress_failed = True
        if not decompress_failed and spec_pdf_path.exists():
            raw_spec = get_specification(specification_pdf_path=spec_pdf_path)
            if len(raw_spec) > int(agent_config.max_spec_info_length * 1.5):
                processed_spec, spec_costs = summarize_specification(
                    spec_text=raw_spec,
                    model=agent_config.model_name,
                    max_tokens=agent_config.spec_summary_max_tokens,
                    max_char_length=agent_config.max_spec_info_length,
                    cache_path=spec_pdf_path.parent / ".spec_summary_cache.json",
                    model_short=agent_config.model_short,
                )
            else:
                processed_spec = raw_spec
            spec_info = f"\n{SPEC_INFO_HEADER} " + processed_spec
        else:
            spec_info = ""
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = Path(repo_path) / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[: agent_config.max_spec_info_length]
                        spec_info = f"\n{SPEC_INFO_HEADER} " + readme_text
                        logger.info(
                            "Using %s as spec fallback for %s", readme_name, repo_path
                        )
                        break
                    except Exception as e:
                        logger.warning("Failed to read %s: %s", readme_path, e)
    else:
        spec_info = ""

    message_to_agent = prompt + repo_info + unit_tests_info + spec_info + MODULE_SCOPE_NOTE

    return message_to_agent, spec_costs


def update_message_with_dependencies(message: str, dependencies: list[str]) -> str:
    """Update the message with the dependencies."""
    if len(dependencies) == 0:
        return message
    import_dependencies_info = f"\n{IMPORT_DEPENDENCIES_HEADER}"
    for dependency in dependencies:
        try:
            with open(dependency, "r") as file:
                import_dependencies_info += (
                    f"\nHere is the content of the file {dependency}:\n{file.read()}"
                )
        except FileNotFoundError:
            logger.warning("Dependency file not found: %s", dependency)
    message += import_dependencies_info
    return message


def get_specification(specification_pdf_path: Path) -> str:
    """Get the reference for a given specification PDF path."""
    # TODO: after pdf_to_text is available, use it to extract the text from the PDF
    text = ""
    with fitz.open(specification_pdf_path) as document:
        for page_num in range(len(document)):
            page = document.load_page(page_num)  # loads the specified page
            text += page.get_text()  # type: ignore
    return text


def _count_tokens(text: str, model: str) -> int:
    """Count tokens using litellm's tokenizer for the given model."""
    try:
        import litellm

        return litellm.token_counter(model=model, text=text)
    except Exception:
        logger.warning(
            "litellm tokenizer unavailable for model '%s', "
            "falling back to len//4 approximation",
            model,
        )
        return len(text) // 4


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split text into chunks of approximately chunk_size characters.

    Tries to break at newline boundaries to avoid splitting mid-sentence.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Try to break at a newline within the last 20% of the chunk
        search_start = end - chunk_size // 5
        newline_pos = text.rfind("\n", search_start, end)
        if newline_pos > start:
            end = newline_pos + 1
        chunks.append(text[start:end])
        start = end
    return chunks


_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a technical documentation summarizer for an AI coding "
    "agent that must implement a Python library from its specification. "
    "Your summary will be the ONLY reference the agent receives.\n\n"
    "PRESERVE (mandatory, never drop):\n"
    "- Every public API signature: function/class/method names, "
    "parameter names, types, default values, return types.\n"
    "- Behavioral contracts: what each function/method does, "
    "preconditions, postconditions, invariants.\n"
    "- Error handling: exceptions raised, error conditions, "
    "what happens on invalid input, fallback behaviors.\n"
    "- Code examples and usage patterns that show HOW to call the API "
    "(keep them verbatim or minimally shortened).\n"
    "- Module/package structure, class hierarchy, inheritance, "
    "dependencies between components.\n"
    "- Constants, enums, config values, magic numbers with meaning.\n"
    "- Edge cases, boundary conditions, thread-safety notes, "
    "platform-specific behavior.\n\n"
    "OMIT (drop first when budget is tight):\n"
    "- Introductions, installation instructions, changelog, "
    "marketing text, contributor guidelines.\n"
    "- Verbose prose that restates what the API signature already shows.\n"
    "- Redundant examples (keep one per pattern, drop duplicates).\n\n"
    "PRIORITY (when budget forces cuts, drop in this order):\n"
    "1. Drop internal/private helpers before public API.\n"
    "2. Drop verbose descriptions before signatures.\n"
    "3. Drop duplicate examples before unique ones.\n"
    "4. Never drop: public API signatures, error conditions, "
    "code examples showing non-obvious usage.\n\n"
    "FORMAT: Be maximally dense. Use terse notation over full sentences. "
    "Group by module/class. Use code blocks for signatures."
)


_CONSOLIDATION_SYSTEM_PROMPT = (
    "You are combining multiple section summaries of a Python library "
    "specification into one cohesive summary. The sections may overlap.\n\n"
    "Rules:\n"
    "- Remove duplicate API signatures, keeping the most complete version.\n"
    "- Preserve ALL unique: public API signatures, error conditions, "
    "code examples, behavioral contracts, edge cases.\n"
    "- Merge related sections logically (group by module/class).\n"
    "- Use terse notation. No preamble or meta-commentary."
)


def _summarize_single(
    text: str,
    model: str,
    max_tokens: int,
    token_budget: int,
    litellm_module: object,
    system_prompt: Optional[str] = None,
    timeout: float = 120,
) -> tuple[Optional[str], SummarizerCost]:
    """Call LLM to summarize a single piece of text. Returns (summary, cost_info)."""
    prompt = system_prompt or _SUMMARIZER_SYSTEM_PROMPT
    response = litellm_module.completion(  # type: ignore[union-attr]
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    prompt
                    + "\n- Your summary MUST be under "
                    + str(token_budget)
                    + " tokens."
                ),
            },
            {
                "role": "user",
                "content": "Summarize this specification:\n\n" + text,
            },
        ],
        max_tokens=max_tokens,
        timeout=timeout,
        num_retries=3,
        retry_strategy="exponential_backoff_retry",
    )

    cost = SummarizerCost()
    usage = getattr(response, "usage", None)
    if usage:
        cost.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        cost.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    try:
        import litellm

        cost.cost = litellm.completion_cost(completion_response=response)
    except Exception:
        pass

    content = response.choices[0].message.content  # type: ignore[union-attr]
    if content:
        return content.strip(), cost
    return None, cost


def summarize_specification(
    spec_text: str,
    model: str,
    max_tokens: int = 4000,
    max_char_length: int = 10000,
    timeout: float = 120,
    cache_path: Optional[Path] = None,
    model_short: str = "",
) -> tuple[str, list[SummarizerCost]]:
    """Summarize specification text using an LLM instead of hard truncation.

    Returns (summary_text, list_of_costs) where costs tracks every LLM call made.
    For specs that fit within a single LLM context window, summarizes in one pass.
    For larger specs, splits into chunks, summarizes each in parallel, then consolidates.
    Falls back to truncation if any LLM call fails.
    """
    all_costs: list[SummarizerCost] = []

    max_token_length = _count_tokens(spec_text[:max_char_length], model)
    if max_token_length < 1:
        max_token_length = max_char_length // 4

    cache_key = hashlib.sha256(
        (spec_text + model + str(max_char_length)).encode()
    ).hexdigest()

    if cache_path is not None:
        try:
            if cache_path.exists():
                cached = json.loads(cache_path.read_text())
                if cached.get("hash") == cache_key:
                    logger.info("Spec summary cache hit (%s)", relativize(cache_path))
                    return cached["summary"], all_costs
        except Exception:
            logger.debug("Cache read failed, proceeding with summarization")

    import litellm

    original_len = len(spec_text)
    original_tokens = _count_tokens(spec_text, model)

    def _write_cache(summary: str) -> None:
        if cache_path is None:
            return
        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "hash": cache_key,
                        "model": model_short or model,
                        "max_char_length": max_char_length,
                        "summary": summary,
                    }
                )
            )
        except Exception:
            logger.debug("Cache write failed for %s", cache_path)

    # ~100K tokens per chunk, leaving room for system prompt + output
    chunk_max_tokens = 100_000
    # Convert to approximate chars for the char-based _chunk_text splitter
    chunk_max_chars = chunk_max_tokens * 4

    try:
        input_tokens = original_tokens
        if input_tokens <= chunk_max_tokens:
            summary, cost = _summarize_single(
                text=spec_text,
                model=model,
                max_tokens=max_tokens,
                token_budget=max_token_length,
                litellm_module=litellm,
                timeout=timeout,
            )
            all_costs.append(cost)
            if summary:
                logger.info(
                    "Spec summarized (single-pass): %d chars (%d tokens) -> %d chars (model=%s)",
                    original_len,
                    original_tokens,
                    len(summary),
                    model_short or model,
                )
                _write_cache(summary)
                return summary, all_costs
            logger.warning("Empty summary from %s, falling back to truncation", model)
            return spec_text[:max_char_length], all_costs

        chunks = _chunk_text(spec_text, chunk_max_chars)
        logger.info(
            "Spec too large for single pass (%d tokens), splitting into %d chunks",
            original_tokens,
            len(chunks),
        )

        per_chunk_token_budget = max_token_length // len(chunks)
        chunk_summaries: list[str] = []

        max_workers = min(len(chunks), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(
                    _summarize_single,
                    chunk,
                    model,
                    max_tokens,
                    per_chunk_token_budget,
                    litellm,
                    None,
                    timeout,
                ): i
                for i, chunk in enumerate(chunks)
            }
            results: dict[int, Optional[tuple[Optional[str], SummarizerCost]]] = {}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception:
                    logger.warning(
                        "Chunk %d/%d raised exception, skipping", idx + 1, len(chunks)
                    )
                    results[idx] = None

        for i in range(len(chunks)):
            r = results.get(i)
            if r is not None:
                text_result, chunk_cost = r
                all_costs.append(chunk_cost)
                if text_result:
                    chunk_summaries.append(text_result)
                else:
                    logger.warning(
                        "Chunk %d/%d returned empty, skipping", i + 1, len(chunks)
                    )
            else:
                logger.warning(
                    "Chunk %d/%d returned empty, skipping", i + 1, len(chunks)
                )

        if not chunk_summaries:
            logger.warning("All chunk summaries empty, falling back to truncation")
            return spec_text[:max_char_length], all_costs

        merged = "\n\n".join(chunk_summaries)
        merged_tokens = _count_tokens(merged, model)
        logger.info(
            "Consolidating %d chunk summaries (%d tokens total) into final summary",
            len(chunk_summaries),
            merged_tokens,
        )

        if merged_tokens <= max_token_length:
            logger.info(
                "Spec summarized (chunked, no consolidation needed): %d tokens -> %d tokens",
                original_tokens,
                merged_tokens,
            )
            _write_cache(merged)
            return merged, all_costs

        final, consolidation_cost = _summarize_single(
            text=merged,
            model=model,
            max_tokens=max_tokens,
            token_budget=max_token_length,
            litellm_module=litellm,
            system_prompt=_CONSOLIDATION_SYSTEM_PROMPT,
            timeout=timeout,
        )
        all_costs.append(consolidation_cost)
        if final:
            logger.info(
                "Spec summarized (chunked+consolidated): %d chars -> %d chars (model=%s)",
                original_len,
                len(final),
                model_short or model,
            )
            _write_cache(final)
            return final, all_costs

        logger.warning("Consolidation returned empty, using merged chunk summaries")
        return merged, all_costs

    except Exception as e:
        logger.warning(
            "Spec summarization failed (%s), falling back to truncation: %s", model, e
        )
        return spec_text[:max_char_length], all_costs


_TEST_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a test output summarizer for an AI coding agent. "
    "Your job is to compress pytest output while preserving ALL information "
    "needed to debug test failures.\n\n"
    "PRESERVE (mandatory, never drop):\n"
    "- EVERY failed test name and its full traceback.\n"
    "- Assertion messages with expected vs actual values.\n"
    "- Collection errors, import errors, fixture errors.\n"
    "- The short test summary info section.\n"
    "- The final status line (N failed, M passed, etc.).\n\n"
    "OMIT (drop first when budget is tight):\n"
    "- Docker/container setup output (pip install, container lifecycle).\n"
    "- Passing test details (just keep the count).\n"
    "- Duplicate traceback frames that appear in both full output and summary.\n"
    "- Warnings unless they indicate why tests fail.\n"
    "- Captured stdout/stderr from passing tests.\n\n"
    "FORMAT: Keep tracebacks as code blocks. Be maximally dense."
)


def _parse_pytest_output(raw: str) -> str:
    """Tier 1: Deterministic extraction of pytest failure info from raw output.

    Strips Docker preamble, extracts FAILURES section, ERRORS section,
    short test summary, and final status line.
    """
    lines = raw.split("\n")

    # Find the first pytest-like line (test session starts or collection)
    pytest_start = -1
    for i, line in enumerate(lines):
        if "test session starts" in line or line.startswith("collected "):
            pytest_start = i
            break

    # Strip Docker preamble
    if pytest_start > 0:
        lines = lines[pytest_start:]

    text = "\n".join(lines)

    sections: list[str] = []

    # Extract FAILURES section
    failures_match = re.search(
        r"(={3,} FAILURES ={3,}.*?)(?=={3,} (?:warnings|short test summary|ERRORS)|\Z)",
        text,
        re.DOTALL,
    )
    if failures_match:
        sections.append(failures_match.group(1).strip())

    # Extract ERRORS section (collection errors)
    errors_match = re.search(
        r"(={3,} ERRORS ={3,}.*?)(?=={3,} (?:warnings|short test summary|FAILURES)|\Z)",
        text,
        re.DOTALL,
    )
    if errors_match:
        sections.append(errors_match.group(1).strip())

    # Extract short test summary info
    summary_match = re.search(
        r"(={3,} short test summary info ={3,}.*?)(?=={3,}[^=]|\Z)",
        text,
        re.DOTALL,
    )
    if summary_match:
        sections.append(summary_match.group(1).strip())

    # Extract final status line (e.g., "= 2 failed, 48 passed in 12.34s =")
    status_match = re.search(r"(={3,}\s+[\d]+ .*? in [\d.]+s\s*={3,})", text)
    if status_match:
        sections.append(status_match.group(1).strip())

    if sections:
        return "\n\n".join(sections)

    # Parse failed — return text with preamble stripped
    return text


def summarize_test_output(
    raw_output: str,
    max_length: int = 15000,
    model: str = "",
    max_tokens: int = 4000,
) -> tuple[str, list[SummarizerCost]]:
    """Hybrid 3-tier test output summarization.

    Returns (summarized_text, list_of_costs).
    Tier 1: Deterministic pytest parsing (free, instant).
    Tier 2: LLM summarization if Tier 1 exceeds budget.
    Tier 3: Smart truncation fallback.
    """
    all_costs: list[SummarizerCost] = []

    max_token_length = (
        _count_tokens(raw_output[:max_length], model) if model else max_length // 4
    )
    if max_token_length < 1:
        max_token_length = max_length // 4

    raw_tokens = _count_tokens(raw_output, model) if model else len(raw_output) // 4
    if raw_tokens <= max_token_length:
        return raw_output, all_costs

    # Tier 1: Deterministic parse
    parsed = _parse_pytest_output(raw_output)
    parsed_tokens = _count_tokens(parsed, model) if model else len(parsed) // 4
    if parsed_tokens <= max_token_length:
        logger.info(
            "Test output summarized (Tier 1 parse): %d -> %d tokens",
            raw_tokens,
            parsed_tokens,
        )
        return parsed, all_costs

    # Tier 2: LLM summarization
    try:
        import litellm

        response = litellm.completion(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        _TEST_SUMMARIZER_SYSTEM_PROMPT
                        + "\n- Your summary MUST be under "
                        + str(max_token_length)
                        + " tokens."
                    ),
                },
                {
                    "role": "user",
                    "content": "Summarize this test output:\n\n" + parsed,
                },
            ],
            max_tokens=max_tokens,
        )

        cost = SummarizerCost()
        usage = getattr(response, "usage", None)
        if usage:
            cost.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            cost.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        try:
            cost.cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass
        all_costs.append(cost)

        content = response.choices[0].message.content  # type: ignore[union-attr]
        if content:
            result = content.strip()
            logger.info(
                "Test output summarized (Tier 2 LLM): %d -> %d chars (model=%s)",
                len(raw_output),
                len(result),
                model,
            )
            return result, all_costs
    except Exception:
        logger.warning(
            "LLM test summarization failed, falling back to truncation",
            exc_info=True,
        )

    # Tier 3: Smart truncation — first 2K + ... + last 2K
    head = 2000
    tail = 2000
    if max_length >= head + tail + 40:
        truncated = parsed[:head] + "\n\n... [truncated] ...\n\n" + parsed[-tail:]
        logger.info(
            "Test output summarized (Tier 3 truncation): %d -> %d chars",
            len(raw_output),
            len(truncated),
        )
        return truncated, all_costs
    return parsed[:max_length], all_costs


def create_branch(repo: git.Repo, branch: str, from_commit: str) -> None:
    """Create a new branch or switch to an existing branch.

    Parameters
    ----------
    repo : git.Repo
        The repository object.
    branch : str
        The name of the branch to create or switch to.
    from_commit : str
        from which commit to create the branch

    Returns
    -------
    None

    Raises
    ------
    RuntimeError
        If creating or switching to the branch fails.

    """
    try:
        # Check if the branch already exists
        logger.info(
            "Creating/switching to branch '%s' from commit %s", branch, from_commit
        )
        if branch in repo.heads:
            repo.git.checkout(branch)
        else:
            repo.git.checkout(from_commit)
            repo.git.checkout("-b", branch)
    except git.exc.GitCommandError as e:  # type: ignore
        raise RuntimeError(
            f"Failed to create or switch to branch '{branch}': {e}"
        ) from e


def get_changed_files_from_commits(
    repo: git.Repo, commit1: str, commit2: str
) -> list[str]:
    """Get the changed files from two commits."""
    try:
        # Get the commit objects
        commit1_obj = repo.commit(commit1)
        commit2_obj = repo.commit(commit2)

        # Get the diff between the two commits
        diff = commit1_obj.diff(commit2_obj)

        # Extract the changed file paths
        changed_files = [item.a_path for item in diff]

        # Check if each changed file is a Python file
        python_files = [file for file in changed_files if file.endswith(".py")]

        # Update the changed_files list to only include Python files
        changed_files = python_files

        return changed_files
    except Exception as e:
        logger.error(
            "Failed to get changed files between %s and %s: %s",
            commit1,
            commit2,
            e,
            exc_info=True,
        )
        return []


def args2string(agent_config: AgentConfig) -> str:
    """Converts specific fields from an `AgentConfig` object into a formatted string.

    Args:
    ----
        agent_config (AgentConfig): A dataclass object containing configuration
        options for an agent.

    Returns:
    -------
        str: A string representing the selected key-value pairs from the `AgentConfig`
        object, joined by double underscores.

    """
    arg_dict = asdict(agent_config)
    result_list = []
    keys_to_collect = ["model_name", "run_tests", "use_lint_info", "use_spec_info"]
    for key in keys_to_collect:
        value = arg_dict[key]
        if isinstance(value, bool):
            if value:
                value = "1"
            else:
                value = "0"
        result_list.append(f"{key}-{value}")
    concatenated_string = "__".join(result_list)
    return concatenated_string


def get_changed_files(repo: git.Repo) -> list[str]:
    """Get a list of files that were changed in the latest commit of the provided Git repository.

    Args:
    ----
        repo (git.Repo): An instance of GitPython's Repo object representing the Git repository.

    Returns:
    -------
        list[str]: A list of filenames (as strings) that were changed in the latest commit.

    """
    latest_commit = repo.head.commit
    # Get the list of files changed in the latest commit
    files_changed = latest_commit.stats.files
    files_changed = [str(one) for one in files_changed]
    return files_changed


def get_lint_cmd(repo_name: str, use_lint_info: bool, commit0_config_file: str) -> str:
    """Generate a linting command based on whether to include files.

    Args:
    ----
        repo_name (str): The name of the repository.
        use_lint_info (bool): A flag indicating whether to include changed files in the lint command.
        commit0_config_file (str): The path to the commit0 dot file.

    Returns:
    -------
        str: The generated linting command string. If `use_lint_info` is True, the command includes
             the list of changed files. If False, returns an empty string.

    """
    if commit0_config_file and not os.path.isabs(commit0_config_file):
        raise ValueError(
            f"get_lint_cmd requires an absolute commit0_config_file (got {commit0_config_file!r}). "
            "The lint subprocess runs inside repos/<repo>/ via DirContext; "
            "relative paths will not resolve correctly."
        )
    lint_cmd = f"{sys.executable} -m commit0 lint "
    if use_lint_info:
        lint_cmd += (
            repo_name + " --commit0-config-file " + commit0_config_file + " --files "
        )
    else:
        lint_cmd = ""
    return lint_cmd


def write_agent_config(agent_config_file: str, agent_config: dict) -> None:
    """Write the agent config to the file."""
    logger.info("Writing agent config to %s", agent_config_file)
    with open(agent_config_file, "w") as f:
        yaml.dump(agent_config, f)


def read_yaml_config(config_file: str) -> dict:
    """Read the yaml config from the file."""
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"The config file '{config_file}' does not exist.")
    with open(config_file, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file '{config_file}' is empty or invalid. "
            f"Expected a YAML mapping, got {type(data).__name__}."
        )
    logger.debug("Loaded YAML config from %s", config_file)
    return data


def load_agent_config(config_file: str) -> "AgentConfig":
    """Load and validate agent config from YAML file into AgentConfig."""
    import dataclasses

    config = read_yaml_config(config_file)

    valid_fields = {f.name for f in dataclasses.fields(AgentConfig)}
    unknown = set(config.keys()) - valid_fields
    if unknown:
        logger.warning(
            "Unknown keys in '%s' will be ignored: %s", config_file, sorted(unknown)
        )

    filtered = {k: v for k, v in config.items() if k in valid_fields}

    try:
        return AgentConfig(**filtered)
    except TypeError as e:
        raise TypeError(
            f"Failed to create AgentConfig from '{config_file}': {e}. "
            f"Required fields: {sorted(f.name for f in dataclasses.fields(AgentConfig) if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING)}"
        ) from e
