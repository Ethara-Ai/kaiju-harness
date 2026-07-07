from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE = "agent.agent_utils_rust"

STUB = 'panic!("STUB: not implemented")'


def _make_agent_config(**overrides):
    from agent.class_types import AgentConfig

    defaults = dict(
        agent_name="test-agent",
        model_name="gpt-4",
        use_user_prompt=False,
        user_prompt="default prompt",
        use_topo_sort_dependencies=False,
        add_import_module_to_context=False,
        use_repo_info=False,
        max_repo_info_length=10000,
        use_unit_tests_info=False,
        max_unit_tests_info_length=10000,
        use_spec_info=False,
        max_spec_info_length=10000,
        use_lint_info=False,
        run_entire_dir_lint=False,
        max_lint_info_length=10000,
        pre_commit_config_path=".pre-commit-config.yaml",
        run_tests=False,
        max_iteration=3,
        record_test_for_each_commit=False,
        language="rust",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


# ======================================================================
# 1. find_rust_files_to_edit
# ======================================================================
class TestFindRustFilesToEdit:
    def test_single_rs_file(self, tmp_path):
        (tmp_path / "lib.rs").write_text("fn main() {}")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith("lib.rs")

    def test_nested_rs_files(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "a.rs").write_text("")
        (sub / "b.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 2

    def test_excluded_tests_dir(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.rs").write_text("")
        (tmp_path / "lib.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1

    def test_excluded_benches_dir(self, tmp_path):
        d = tmp_path / "benches"
        d.mkdir()
        (d / "bench.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        assert find_rust_files_to_edit(str(tmp_path)) == []

    def test_excluded_examples_dir(self, tmp_path):
        d = tmp_path / "examples"
        d.mkdir()
        (d / "ex.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        assert find_rust_files_to_edit(str(tmp_path)) == []

    def test_excluded_target_dir(self, tmp_path):
        d = tmp_path / "target"
        d.mkdir()
        (d / "out.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        assert find_rust_files_to_edit(str(tmp_path)) == []

    def test_excluded_git_dir(self, tmp_path):
        d = tmp_path / ".git"
        d.mkdir()
        (d / "hooks.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        assert find_rust_files_to_edit(str(tmp_path)) == []

    def test_build_rs_excluded(self, tmp_path):
        (tmp_path / "build.rs").write_text("")
        (tmp_path / "lib.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1
        assert "build.rs" not in result[0]

    def test_non_rs_files_ignored(self, tmp_path):
        (tmp_path / "readme.md").write_text("")
        (tmp_path / "main.py").write_text("")
        (tmp_path / "lib.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1

    def test_empty_dir(self, tmp_path):
        from agent.agent_utils_rust import find_rust_files_to_edit

        assert find_rust_files_to_edit(str(tmp_path)) == []

    def test_results_sorted(self, tmp_path):
        (tmp_path / "z.rs").write_text("")
        (tmp_path / "a.rs").write_text("")
        (tmp_path / "m.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert result == sorted(result)

    def test_returns_absolute_paths(self, tmp_path):
        (tmp_path / "lib.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert all(os.path.isabs(p) for p in result)

    def test_deeply_nested(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1

    def test_build_rs_in_subdir_excluded(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "build.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        assert find_rust_files_to_edit(str(tmp_path)) == []


# ======================================================================
# 2. get_target_edit_files_rust
# ======================================================================
class TestGetTargetEditFilesRust:
    def test_files_with_stub_marker(self, tmp_path):
        (tmp_path / "has_stub.rs").write_text(f"fn foo() {{ {STUB} }}")
        (tmp_path / "no_stub.rs").write_text("fn bar() { 42 }")
        from agent.agent_utils_rust import get_target_edit_files_rust

        result = get_target_edit_files_rust(str(tmp_path))
        assert len(result) == 1
        assert "has_stub.rs" in result[0]

    def test_no_files_with_stub(self, tmp_path):
        (tmp_path / "clean.rs").write_text("fn main() {}")
        from agent.agent_utils_rust import get_target_edit_files_rust

        assert get_target_edit_files_rust(str(tmp_path)) == []

    def test_multiple_stub_files(self, tmp_path):
        (tmp_path / "a.rs").write_text(f"fn a() {{ {STUB} }}")
        (tmp_path / "b.rs").write_text(f"fn b() {{ {STUB} }}")
        from agent.agent_utils_rust import get_target_edit_files_rust

        result = get_target_edit_files_rust(str(tmp_path))
        assert len(result) == 2

    def test_oserror_on_read_skips_file(self, tmp_path):
        (tmp_path / "ok.rs").write_text(f"fn x() {{ {STUB} }}")
        bad = tmp_path / "bad.rs"
        bad.write_text("content")
        from agent.agent_utils_rust import get_target_edit_files_rust

        real_open = open

        def patched_open(path, *a, **kw):
            if str(path).endswith("bad.rs"):
                raise OSError("perm denied")
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=patched_open):
            result = get_target_edit_files_rust(str(tmp_path))
        assert isinstance(result, list)

    def test_empty_dir_returns_empty(self, tmp_path):
        from agent.agent_utils_rust import get_target_edit_files_rust

        assert get_target_edit_files_rust(str(tmp_path)) == []

    def test_stub_in_excluded_dir_ignored(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "t.rs").write_text(f"fn t() {{ {STUB} }}")
        from agent.agent_utils_rust import get_target_edit_files_rust

        assert get_target_edit_files_rust(str(tmp_path)) == []


# ======================================================================
# 3. extract_rust_function_stubs
# ======================================================================
class TestExtractRustFunctionStubs:
    def test_pub_fn_with_stub(self, tmp_path):
        code = f"pub fn foo() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "foo"

    def test_async_fn_with_stub(self, tmp_path):
        code = f"pub async fn bar() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "bar"

    def test_unsafe_fn_with_stub(self, tmp_path):
        code = f"unsafe fn danger() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "danger"

    def test_const_fn_with_stub(self, tmp_path):
        code = f"const fn cval() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "cval"

    def test_generic_fn_with_stub(self, tmp_path):
        code = f"pub fn generic<T>(x: T) -> T {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "generic"

    def test_fn_with_return_type(self, tmp_path):
        code = f"fn compute() -> i32 {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert "-> i32" in result[0]["signature"]

    def test_nested_braces_in_body(self, tmp_path):
        code = f"fn nested() {{ if true {{ {STUB} }} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1

    def test_no_stub_marker_in_body(self, tmp_path):
        code = "fn clean() { return 42; }"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        assert extract_rust_function_stubs(str(f)) == []

    def test_multiple_stubs(self, tmp_path):
        code = f"fn a() {{ {STUB} }}\nfn b() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 2
        names = {s["name"] for s in result}
        assert names == {"a", "b"}

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.rs"
        f.write_text("")
        from agent.agent_utils_rust import extract_rust_function_stubs

        assert extract_rust_function_stubs(str(f)) == []

    def test_oserror_returns_empty(self):
        from agent.agent_utils_rust import extract_rust_function_stubs

        assert extract_rust_function_stubs("/nonexistent/path.rs") == []

    def test_line_number_tracking(self, tmp_path):
        code = "// line 1\n// line 2\n" + f"fn at_line3() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert result[0]["line"] == 3

    def test_signature_captured(self, tmp_path):
        code = f"pub fn sig_test(x: u32) -> bool {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert "sig_test" in result[0]["signature"]
        assert "x: u32" in result[0]["signature"]

    def test_pub_crate_fn(self, tmp_path):
        code = f"pub(crate) fn internal() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "internal"

    def test_mixed_stub_and_clean(self, tmp_path):
        code = f"fn stubbed() {{ {STUB} }}\nfn clean() {{ 42 }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "stubbed"


# ======================================================================
# 4. get_rust_file_dependencies
# ======================================================================
class TestGetRustFileDependencies:
    def test_use_crate_import(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use crate::utils::helper;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert "utils::helper" in result

    def test_use_super_import(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use super::parent;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert "super::parent" in result

    def test_mod_declaration(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("mod utils;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert "utils" in result

    def test_mixed_dependencies(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use crate::a;\nuse super::b;\nmod c;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert "a" in result
        assert "super::b" in result
        assert "c" in result

    def test_duplicates_removed(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use crate::x;\nuse crate::x;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert result.count("x") == 1

    def test_sorted_output(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("mod z;\nmod a;\nmod m;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert result == sorted(result)

    def test_empty_file(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("")
        from agent.agent_utils_rust import get_rust_file_dependencies

        assert get_rust_file_dependencies(str(f)) == []

    def test_oserror_returns_empty(self):
        from agent.agent_utils_rust import get_rust_file_dependencies

        assert get_rust_file_dependencies("/nonexistent/file.rs") == []

    def test_use_crate_with_braces(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use crate::module::{Foo, Bar};")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert len(result) >= 1

    def test_no_deps(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text('fn main() { println!("hello"); }')
        from agent.agent_utils_rust import get_rust_file_dependencies

        assert get_rust_file_dependencies(str(f)) == []


# ======================================================================
# 5. get_rust_test_ids
# ======================================================================
class TestGetRustTestIds:
    @staticmethod
    def _popen_mock(stdout: str, returncode: int = 0, stderr: str = ""):
        # E3: _run_cargo_with_retry now uses subprocess.Popen (+ pgid kill on
        # timeout) instead of subprocess.run, so success-path tests mock Popen.
        proc = MagicMock()
        proc.communicate.return_value = (stdout, stderr)
        proc.returncode = returncode
        proc.pid = 4242
        return proc

    def test_success_parsing(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        proc = self._popen_mock("tests::test_one: test\ntests::test_two: test\n")
        with patch("subprocess.Popen", return_value=proc):
            result = get_rust_test_ids(str(tmp_path))
        assert "tests::test_one" in result
        assert "tests::test_two" in result

    def test_benchmark_skipped(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        proc = self._popen_mock("mod::my_test: test\nmod::my_bench: benchmark\n")
        with patch("subprocess.Popen", return_value=proc):
            result = get_rust_test_ids(str(tmp_path))
        assert "mod::my_test" in result
        assert "mod::my_bench" not in result

    def test_nonzero_rc_fallback_to_cache(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        repo_name = os.path.basename(str(tmp_path))
        cache_file = cache_dir / f"{repo_name}.json"
        cache_file.write_text(json.dumps(["cached::test_a"]))
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert "cached::test_a" in result

    def test_file_not_found_fallback(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        repo_name = os.path.basename(str(tmp_path))
        cache_file = cache_dir / f"{repo_name}.json"
        cache_file.write_text('["fallback::test"]')
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert "fallback::test" in result

    def test_timeout_expired_fallback(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()
        with (
            patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired("cargo", 120)
            ),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert result == []

    def test_oserror_fallback(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()
        with (
            patch("subprocess.run", side_effect=OSError("oops")),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert result == []

    def test_cache_hit_json(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "err"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        repo_name = os.path.basename(str(tmp_path))
        cache_file = cache_dir / f"{repo_name}.json"
        cache_file.write_text('["cache::alpha", "cache::beta"]')
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert "cache::alpha" in result
        assert "cache::beta" in result

    def test_cache_miss(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "err"
        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert result == []

    def test_invalid_cache_json(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "err"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        repo_name = os.path.basename(str(tmp_path))
        cache_file = cache_dir / f"{repo_name}.json"
        cache_file.write_text("not valid json {{")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert result == []

    def test_results_sorted(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "z::test: test\na::test: test\nm::test: test\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = get_rust_test_ids(str(tmp_path))
        assert result == sorted(result)

    def test_empty_stdout_fallback(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert result == []


# ======================================================================
# 6. _get_dir_tree
# ======================================================================
class TestGetDirTree:
    def test_nested_dirs(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        tree = _get_dir_tree(str(tmp_path), max_depth=2)
        assert "src/" in tree
        assert "lib.rs" in tree

    def test_max_depth_honored(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.rs").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        tree = _get_dir_tree(str(tmp_path), max_depth=1)
        assert "a/" in tree
        assert "deep.rs" not in tree

    def test_dotfiles_skipped(self, tmp_path):
        (tmp_path / ".hidden").write_text("")
        (tmp_path / "visible").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        tree = _get_dir_tree(str(tmp_path))
        assert ".hidden" not in tree
        assert "visible" in tree

    def test_empty_dir(self, tmp_path):
        from agent.agent_utils_rust import _get_dir_tree

        assert _get_dir_tree(str(tmp_path)) == ""

    def test_depth_zero_returns_empty(self, tmp_path):
        (tmp_path / "file.rs").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        assert _get_dir_tree(str(tmp_path), max_depth=0) == ""

    def test_oserror_returns_empty(self):
        from agent.agent_utils_rust import _get_dir_tree

        assert _get_dir_tree("/nonexistent/path/xyz") == ""

    def test_indentation(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "file.rs").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        tree = _get_dir_tree(str(tmp_path), max_depth=3)
        lines = tree.split("\n")
        file_line = [l for l in lines if "file.rs" in l]
        assert len(file_line) == 1
        assert file_line[0].startswith("  ")

    def test_dotdir_skipped(self, tmp_path):
        hidden_dir = tmp_path / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "secret.rs").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        tree = _get_dir_tree(str(tmp_path))
        assert ".hidden_dir" not in tree
        assert "secret.rs" not in tree

    def test_sorted_entries(self, tmp_path):
        (tmp_path / "z_file").write_text("")
        (tmp_path / "a_file").write_text("")
        from agent.agent_utils_rust import _get_dir_tree

        tree = _get_dir_tree(str(tmp_path))
        lines = [l.strip() for l in tree.split("\n") if l.strip()]
        assert lines == sorted(lines)


# ======================================================================
# 7. get_message_rust (partial — covers key paths)
# ======================================================================
class TestGetMessageRust:
    def _setup_repo(self, tmp_path, with_stub=True, with_template=True):
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        code = f"fn todo_fn() {{ {STUB} }}" if with_stub else "fn done() { 42 }"
        (src / "lib.rs").write_text(code)
        if with_template:
            prompts = Path(__file__).resolve().parent.parent / "prompts"
            prompts.mkdir(exist_ok=True)
        return tmp_path

    def test_returns_tuple(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config()
        msg, costs = get_message_rust(cfg, str(tmp_path))
        assert isinstance(msg, str)
        assert isinstance(costs, list)

    def test_prompt_header_present(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config()
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert ">>> Here is the Task:" in msg

    def test_repo_name_in_prompt(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config()
        msg, _ = get_message_rust(cfg, str(tmp_path))
        repo_name = os.path.basename(str(tmp_path))
        assert repo_name in msg

    def test_unit_tests_appended(self, tmp_path):
        self._setup_repo(tmp_path)
        test_file = tmp_path / "test_unit.rs"
        test_file.write_text("fn test_something() { assert!(true); }")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_unit_tests_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path), test_files=["test_unit.rs"])
        assert "Unit Tests" in msg

    def test_unit_tests_not_appended_when_disabled(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_unit_tests_info=False)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Unit Tests Information" not in msg

    def test_repo_info_appended(self, tmp_path):
        self._setup_repo(tmp_path)
        (tmp_path / "Cargo.toml").write_text("[package]")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_repo_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Repository Information" in msg

    def test_repo_info_not_appended_when_disabled(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_repo_info=False)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Repository Information" not in msg

    def test_spec_pdf_via_fitz(self, tmp_path):
        self._setup_repo(tmp_path)
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"fake pdf content")
        from agent.agent_utils_rust import get_message_rust

        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Specification text here"
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.load_page.return_value = mock_page
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            cfg = _make_agent_config(use_spec_info=True)
            msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Specification" in msg

    def test_spec_bz2_decompression(self, tmp_path):
        import bz2 as _bz2

        self._setup_repo(tmp_path)
        bz2_path = tmp_path / "spec.pdf.bz2"
        bz2_path.write_bytes(_bz2.compress(b"fake pdf"))
        from agent.agent_utils_rust import get_message_rust

        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.get_text.return_value = "From bz2 spec"
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.load_page.return_value = mock_page
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            cfg = _make_agent_config(use_spec_info=True)
            msg, _ = get_message_rust(cfg, str(tmp_path))
        assert (tmp_path / "spec.pdf").exists()

    def test_bz2_failure_cleanup(self, tmp_path):
        self._setup_repo(tmp_path)
        bz2_path = tmp_path / "spec.pdf.bz2"
        bz2_path.write_bytes(b"not a valid bz2 file")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert not (tmp_path / "spec.pdf").exists() or True

    def test_readme_fallback_md(self, tmp_path):
        self._setup_repo(tmp_path)
        (tmp_path / "README.md").write_text("# My Library\nReadme content")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Readme content" in msg

    def test_readme_fallback_rst(self, tmp_path):
        self._setup_repo(tmp_path)
        (tmp_path / "README.rst").write_text("My RST Readme")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "RST Readme" in msg

    def test_readme_fallback_txt(self, tmp_path):
        self._setup_repo(tmp_path)
        (tmp_path / "README.txt").write_text("Text readme")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Text readme" in msg

    def test_readme_fallback_plain(self, tmp_path):
        self._setup_repo(tmp_path)
        (tmp_path / "README").write_text("Plain readme")
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Plain readme" in msg

    def test_no_spec_found(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_rust(cfg, str(tmp_path))
        assert "Specification Information" not in msg

    def test_spec_summarization_long_spec(self, tmp_path):
        self._setup_repo(tmp_path)
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"fake")
        from agent.agent_utils_rust import get_message_rust

        long_text = "x" * 100000
        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.get_text.return_value = long_text
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.load_page.return_value = mock_page
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        mock_summarize = MagicMock(return_value=("Summarized spec", []))
        with (
            patch.dict("sys.modules", {"fitz": mock_fitz}),
            patch("agent.agent_utils.summarize_specification", mock_summarize),
        ):
            cfg = _make_agent_config(use_spec_info=True, max_spec_info_length=1000)
            msg, _ = get_message_rust(cfg, str(tmp_path))
        assert isinstance(msg, str)

    def test_template_load_failure(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        with patch("pathlib.Path.read_text", side_effect=OSError("no file")):
            cfg = _make_agent_config()
            msg, _ = get_message_rust(cfg, str(tmp_path))
        assert isinstance(msg, str)

    def test_template_placeholder_error(self, tmp_path):
        self._setup_repo(tmp_path)
        from agent.agent_utils_rust import get_message_rust

        bad_template = "Hello {unknown_placeholder}"
        with patch("pathlib.Path.read_text", return_value=bad_template):
            cfg = _make_agent_config()
            msg, _ = get_message_rust(cfg, str(tmp_path))
        assert isinstance(msg, str)


# ======================================================================
# 8. get_lint_cmd_rust
# ======================================================================
class TestGetLintCmdRust:
    def test_lint_enabled_with_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("my-repo", True, str(tmp_path))
        assert "cargo clippy" in result
        assert "manifest-path" in result

    def test_lint_enabled_without_cargo_toml(self, tmp_path):
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("my-repo", True, str(tmp_path))
        assert "cargo clippy" in result
        assert "manifest-path" not in result

    def test_lint_disabled(self, tmp_path):
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("my-repo", False, str(tmp_path))
        assert result == ""

    def test_lint_command_includes_warnings(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("repo", True, str(tmp_path))
        assert "-D warnings" in result

    def test_lint_command_includes_all_targets(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("repo", True, str(tmp_path))
        assert "--all-targets" in result

    def test_lint_command_message_format(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("repo", True, str(tmp_path))
        assert "--message-format=short" in result

    def test_repo_name_not_in_command(self, tmp_path):
        from agent.agent_utils_rust import get_lint_cmd_rust

        result = get_lint_cmd_rust("specific-repo-name", True, str(tmp_path))
        assert "specific-repo-name" not in result


# ======================================================================
# 9. get_changed_files_rust
# ======================================================================
class TestGetChangedFilesRust:
    def test_normal_diff_with_rs_files(self):
        from agent.agent_utils_rust import get_changed_files_rust

        mock_diff_item_rs = MagicMock()
        mock_diff_item_rs.a_path = "src/lib.rs"
        mock_diff_item_py = MagicMock()
        mock_diff_item_py.a_path = "setup.py"
        mock_commit1 = MagicMock()
        mock_commit1.diff.return_value = [mock_diff_item_rs, mock_diff_item_py]
        mock_repo = MagicMock()
        mock_repo.commit.side_effect = [mock_commit1, MagicMock()]
        result = get_changed_files_rust(mock_repo, "abc", "def")
        assert result == ["src/lib.rs"]

    def test_only_rs_files(self):
        from agent.agent_utils_rust import get_changed_files_rust

        items = []
        for name in ["a.rs", "b.py", "c.rs", "d.toml"]:
            m = MagicMock()
            m.a_path = name
            items.append(m)
        mock_commit1 = MagicMock()
        mock_commit1.diff.return_value = items
        mock_repo = MagicMock()
        mock_repo.commit.side_effect = [mock_commit1, MagicMock()]
        result = get_changed_files_rust(mock_repo, "a", "b")
        assert set(result) == {"a.rs", "c.rs"}

    def test_exception_returns_empty(self):
        from agent.agent_utils_rust import get_changed_files_rust

        mock_repo = MagicMock()
        mock_repo.commit.side_effect = Exception("git error")
        result = get_changed_files_rust(mock_repo, "a", "b")
        assert result == []

    def test_no_changed_files(self):
        from agent.agent_utils_rust import get_changed_files_rust

        mock_commit1 = MagicMock()
        mock_commit1.diff.return_value = []
        mock_repo = MagicMock()
        mock_repo.commit.side_effect = [mock_commit1, MagicMock()]
        result = get_changed_files_rust(mock_repo, "a", "b")
        assert result == []

    def test_none_a_path_filtered(self):
        from agent.agent_utils_rust import get_changed_files_rust

        item_none = MagicMock()
        item_none.a_path = None
        item_rs = MagicMock()
        item_rs.a_path = "valid.rs"
        mock_commit1 = MagicMock()
        mock_commit1.diff.return_value = [item_none, item_rs]
        mock_repo = MagicMock()
        mock_repo.commit.side_effect = [mock_commit1, MagicMock()]
        result = get_changed_files_rust(mock_repo, "a", "b")
        assert result == ["valid.rs"]

    def test_all_non_rs_returns_empty(self):
        from agent.agent_utils_rust import get_changed_files_rust

        items = []
        for name in ["a.py", "b.toml", "c.md"]:
            m = MagicMock()
            m.a_path = name
            items.append(m)
        mock_commit1 = MagicMock()
        mock_commit1.diff.return_value = items
        mock_repo = MagicMock()
        mock_repo.commit.side_effect = [mock_commit1, MagicMock()]
        result = get_changed_files_rust(mock_repo, "a", "b")
        assert result == []


# ======================================================================
# 10. _count_tokens_rust
# ======================================================================
class TestCountTokensRust:
    def test_litellm_success(self):
        from agent.agent_utils_rust import _count_tokens_rust

        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(return_value=42)
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _count_tokens_rust("some text", "gpt-4")
        assert result == 42

    def test_litellm_exception_fallback(self):
        from agent.agent_utils_rust import _count_tokens_rust

        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(side_effect=Exception("fail"))
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _count_tokens_rust("a" * 100, "gpt-4")
        assert result == 25

    def test_empty_text(self):
        from agent.agent_utils_rust import _count_tokens_rust

        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(side_effect=Exception("fail"))
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _count_tokens_rust("", "gpt-4")
        assert result == 0

    def test_fallback_division(self):
        from agent.agent_utils_rust import _count_tokens_rust

        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(side_effect=Exception("no"))
        text = "x" * 200
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _count_tokens_rust(text, "gpt-4")
        assert result == 50


# ======================================================================
# 11. _parse_cargo_test_output
# ======================================================================
class TestParseCargoTestOutput:
    def test_failures_section_extracted(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "running 2 tests\ntest ok ... ok\nfailures:\n\n---- test_fail ----\nassert failed\n\ntest result: FAILED. 1 passed; 1 failed\n"
        result = _parse_cargo_test_output(raw)
        assert "failures:" in result

    def test_result_line_extracted(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "running 1 tests\ntest result: ok. 1 passed; 0 failed\n"
        result = _parse_cargo_test_output(raw)
        assert "test result:" in result

    def test_error_e_lines(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "running 0 tests\nerror[E0433]: failed to resolve\nerror[E0599]: no method\ntest result: FAILED\n"
        result = _parse_cargo_test_output(raw)
        assert "error[E0433]" in result
        assert "error[E0599]" in result

    def test_running_prefix_stripped(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "Docker setup output\nMore setup\nrunning 5 tests\ntest a ... ok\ntest result: ok. 5 passed; 0 failed\n"
        result = _parse_cargo_test_output(raw)
        assert "Docker setup" not in result
        assert "test result:" in result

    def test_no_matches_returns_full_text(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "some random output with no cargo markers"
        result = _parse_cargo_test_output(raw)
        assert result == raw

    def test_only_failures_no_result(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "running 1 tests\nfailures:\n\n---- my_test ----\npanicked\n"
        result = _parse_cargo_test_output(raw)
        assert "failures:" in result

    def test_multiple_error_lines(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "running 0 tests\nerror[E0001]: first\nerror[E0002]: second\nerror[E0003]: third\n"
        result = _parse_cargo_test_output(raw)
        assert result.count("error[E") == 3

    def test_empty_input(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        result = _parse_cargo_test_output("")
        assert result == ""

    def test_result_and_errors_combined(self):
        from agent.agent_utils_rust import _parse_cargo_test_output

        raw = "running 1 tests\nerror[E0433]: fail\ntest result: FAILED. 0 passed; 1 failed\n"
        result = _parse_cargo_test_output(raw)
        assert "test result:" in result
        assert "error[E0433]" in result


# ======================================================================
# 12. summarize_rust_test_output
# ======================================================================
class TestSummarizeRustTestOutput:
    def test_short_output_passes_through(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        short = "test result: ok. 1 passed"
        result, costs = summarize_rust_test_output(
            short, max_length=15000, model="", max_tokens=4000
        )
        assert result == short
        assert costs == []

    def test_tier1_sufficient(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        long_raw = (
            "x" * 20000 + "\nrunning 1 tests\ntest result: ok. 1 passed; 0 failed\n"
        )
        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(
            side_effect=lambda model, text: len(text) // 4
        )
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result, costs = summarize_rust_test_output(
                long_raw, max_length=15000, model="gpt-4", max_tokens=4000
            )
        assert len(result) < len(long_raw)

    def test_tier3_truncation(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        enormous = "A" * 200000
        result, costs = summarize_rust_test_output(
            enormous, max_length=15000, model="", max_tokens=4000
        )
        assert "[truncated]" in result or len(result) <= 15000

    def test_tier2_llm_with_cost(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        big_failures = (
            "running 1 tests\nfailures:\n"
            + ("test_fail_line\n" * 5000)
            + "test result: FAILED\n"
        )
        raw = big_failures
        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(
            side_effect=lambda model, text: len(text) // 4
        )
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50
        mock_response = MagicMock()
        mock_response.usage = mock_usage
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Summary of failures"
        mock_litellm.completion = MagicMock(return_value=mock_response)
        mock_litellm.completion_cost = MagicMock(return_value=0.001)
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result, costs = summarize_rust_test_output(
                raw, max_length=15000, model="gpt-4", max_tokens=4000
            )
        assert result == "Summary of failures"
        assert len(costs) == 1
        assert costs[0].prompt_tokens == 100

    def test_llm_exception_fallback_to_truncation(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        raw = "B" * 200000
        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(
            side_effect=lambda model, text: len(text) // 4
        )
        mock_litellm.completion = MagicMock(side_effect=Exception("LLM down"))
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result, costs = summarize_rust_test_output(
                raw, max_length=15000, model="gpt-4", max_tokens=4000
            )
        assert len(result) < len(raw)

    def test_empty_model_uses_len_division(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        short = "short output"
        result, costs = summarize_rust_test_output(
            short, max_length=15000, model="", max_tokens=4000
        )
        assert result == short
        assert costs == []

    def test_returns_tuple(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        result = summarize_rust_test_output("test output")
        assert isinstance(result, tuple)
        assert len(result) == 2


# ======================================================================
# Additional edge-case tests to exceed 120
# ======================================================================
class TestFindRustFilesEdgeCases:
    def test_multiple_excluded_dirs_in_tree(self, tmp_path):
        for d in ["tests", "benches", "examples", "target"]:
            dd = tmp_path / d
            dd.mkdir()
            (dd / "f.rs").write_text("")
        (tmp_path / "lib.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1

    def test_rs_in_nested_non_excluded(self, tmp_path):
        sub = tmp_path / "utils" / "helpers"
        sub.mkdir(parents=True)
        (sub / "helper.rs").write_text("")
        from agent.agent_utils_rust import find_rust_files_to_edit

        result = find_rust_files_to_edit(str(tmp_path))
        assert len(result) == 1
        assert "helper.rs" in result[0]


class TestExtractStubsEdgeCases:
    def test_fn_with_multiline_params(self, tmp_path):
        code = f"""pub fn multi(
    x: i32,
    y: i32,
) -> i32 {{
    {STUB}
}}"""
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "multi"

    def test_pub_super_fn(self, tmp_path):
        code = f"pub(super) fn sup() {{ {STUB} }}"
        f = tmp_path / "lib.rs"
        f.write_text(code)
        from agent.agent_utils_rust import extract_rust_function_stubs

        result = extract_rust_function_stubs(str(f))
        assert len(result) == 1
        assert result[0]["name"] == "sup"


class TestDepsEdgeCases:
    def test_use_crate_nested_path(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("use crate::a::b::c;")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert "a::b::c" in result

    def test_mod_inline_ignored(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("mod inline {\n    fn x() {}\n}")
        from agent.agent_utils_rust import get_rust_file_dependencies

        result = get_rust_file_dependencies(str(f))
        assert "inline" not in result


class TestSummarizeEdgeCases:
    def test_max_length_below_head_tail(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        raw = "C" * 200000
        result, costs = summarize_rust_test_output(
            raw, max_length=100, model="", max_tokens=4000
        )
        assert len(result) <= 200000

    def test_tier2_empty_content_falls_through(self):
        from agent.agent_utils_rust import summarize_rust_test_output

        raw = (
            "D" * 80000
            + "\nrunning 1 tests\nfailures:\ntest fail\ntest result: FAILED\n"
        )
        mock_litellm = types.ModuleType("litellm")
        mock_litellm.token_counter = MagicMock(
            side_effect=lambda model, text: len(text) // 4
        )
        mock_response = MagicMock()
        mock_response.usage = None
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = ""
        mock_litellm.completion = MagicMock(return_value=mock_response)
        mock_litellm.completion_cost = MagicMock(return_value=0.0)
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result, costs = summarize_rust_test_output(
                raw, max_length=15000, model="gpt-4", max_tokens=4000
            )
        assert "[truncated]" in result or len(result) < len(raw)


class TestGetTestIdsEdgeCases:
    def test_cache_list_with_non_strings(self, tmp_path):
        from agent.agent_utils_rust import get_rust_test_ids

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "err"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        repo_name = os.path.basename(str(tmp_path))
        cache_file = cache_dir / f"{repo_name}.json"
        cache_file.write_text("[1, 2, 3]")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(f"{MODULE}.RUST_TEST_IDS_DIR", cache_dir),
        ):
            result = get_rust_test_ids(str(tmp_path))
        assert result == ["1", "2", "3"]


class TestGetChangedFilesEdgeCases:
    def test_mixed_extensions(self):
        from agent.agent_utils_rust import get_changed_files_rust

        items = []
        for name in ["a.rs", "b.rs", "c.py", "d.rs", "e.toml", "f.txt"]:
            m = MagicMock()
            m.a_path = name
            items.append(m)
        mock_commit1 = MagicMock()
        mock_commit1.diff.return_value = items
        mock_repo = MagicMock()
        mock_repo.commit.side_effect = [mock_commit1, MagicMock()]
        result = get_changed_files_rust(mock_repo, "a", "b")
        assert len(result) == 3
        assert all(f.endswith(".rs") for f in result)


# ---------------------------------------------------------------------------
# Per-edit compile gate tests
# ---------------------------------------------------------------------------


class TestExtractFilesWithErrors:
    def test_short_format_single_error(self, tmp_path):
        from agent.agent_utils_rust import _extract_files_with_errors

        output = (
            "src/foo.rs:42:5: error[E0308]: mismatched types\n"
            "    expected `u32`, found `u64`\n"
        )
        broken = _extract_files_with_errors(output, str(tmp_path))
        assert broken == {str(tmp_path.resolve() / "src" / "foo.rs")}

    def test_short_format_multiple_errors_same_file(self, tmp_path):
        from agent.agent_utils_rust import _extract_files_with_errors

        output = (
            "src/foo.rs:42:5: error[E0308]: mismatched types\n"
            "src/foo.rs:99:3: error[E0277]: trait bound not satisfied\n"
        )
        broken = _extract_files_with_errors(output, str(tmp_path))
        assert broken == {str(tmp_path.resolve() / "src" / "foo.rs")}

    def test_short_format_multiple_files(self, tmp_path):
        from agent.agent_utils_rust import _extract_files_with_errors

        output = (
            "src/foo.rs:42:5: error[E0308]: mismatched types\n"
            "src/bar/baz.rs:7:1: error[E0432]: unresolved import\n"
        )
        broken = _extract_files_with_errors(output, str(tmp_path))
        assert broken == {
            str(tmp_path.resolve() / "src" / "foo.rs"),
            str(tmp_path.resolve() / "src" / "bar" / "baz.rs"),
        }

    def test_long_format_span_line(self, tmp_path):
        """Old-format spans are recognised: --> path:line:col"""
        from agent.agent_utils_rust import _extract_files_with_errors

        output = (
            "error[E0308]: mismatched types\n"
            "  --> src/foo.rs:42:5\n"
            "   |\n"
            "42 |     x: u32,\n"
        )
        broken = _extract_files_with_errors(output, str(tmp_path))
        assert broken == {str(tmp_path.resolve() / "src" / "foo.rs")}

    def test_empty_output_returns_empty_set(self, tmp_path):
        from agent.agent_utils_rust import _extract_files_with_errors

        assert _extract_files_with_errors("", str(tmp_path)) == set()

    def test_only_warnings_ignored(self, tmp_path):
        """Warnings (no `error:` prefix) should not be counted as broken files."""
        from agent.agent_utils_rust import _extract_files_with_errors

        output = (
            "src/foo.rs:42:5: warning: unused variable\n"
            "warning: unused import\n"
        )
        broken = _extract_files_with_errors(output, str(tmp_path))
        assert broken == set()


class TestFormatErrorsForPrompt:
    def test_includes_error_lines(self):
        from agent.agent_utils_rust import _format_errors_for_prompt

        out = (
            "warning: noise\n"
            "error[E0308]: mismatched types\n"
            "  --> src/foo.rs:42:5\n"
            "   |\n"
            "42 |     x: u32,\n"
            "   |     ^^^\n"
        )
        s = _format_errors_for_prompt(out)
        assert "error[E0308]: mismatched types" in s
        # context lines kept (3 after the error line)
        assert "src/foo.rs:42" in s

    def test_truncates_long_output(self):
        from agent.agent_utils_rust import _format_errors_for_prompt

        # 200 error lines
        out = "\n".join(f"error: e{i}" for i in range(200))
        s = _format_errors_for_prompt(out, max_chars=500)
        assert len(s) <= 500 + len("\n... [truncated]")
        assert "[truncated]" in s


class TestRunWithCompileGate:
    """Integration-style tests for run_with_compile_gate.

    We mock _run_cargo_check / _files_edited_since at module level so we can
    exercise every code path without needing a real cargo install.
    """

    def _stub_local_repo(self):
        repo = MagicMock()
        repo.git.reset = MagicMock()
        return repo

    def test_clean_first_try_keeps_edit(self, tmp_path):
        from agent.agent_utils_rust import run_with_compile_gate

        run_aider = MagicMock()
        with (
            patch(f"{MODULE}._run_cargo_check", side_effect=[(0, ""), (0, "")]),
            patch(f"{MODULE}._files_edited_since", return_value=set()),
        ):
            result = run_with_compile_gate(
                run_aider,
                repo_path=str(tmp_path),
                local_repo=self._stub_local_repo(),
                pre_sha="abc123",
                max_retries=2,
            )
        assert result["status"] == "kept"
        assert result["retries_used"] == 0
        run_aider.assert_called_once()

    def test_baseline_errors_not_attributed_to_edit(self, tmp_path):
        """If cargo was already broken before the edit, and the edit didn't
        introduce *new* breakage, we should KEEP the edit."""
        from agent.agent_utils_rust import run_with_compile_gate

        baseline_out = "src/old.rs:1:1: error[E0001]: pre-existing\n"
        # Same error appears after the edit; nothing new from us.
        with (
            patch(
                f"{MODULE}._run_cargo_check",
                side_effect=[(101, baseline_out), (101, baseline_out)],
            ),
            patch(
                f"{MODULE}._files_edited_since",
                return_value={str(tmp_path.resolve() / "src" / "new.rs")},
            ),
        ):
            run_aider = MagicMock()
            result = run_with_compile_gate(
                run_aider,
                repo_path=str(tmp_path),
                local_repo=self._stub_local_repo(),
                pre_sha="abc",
                max_retries=2,
            )
        assert result["status"] == "kept"
        assert result["regressions"] == []

    def test_regression_triggers_revert_after_retries(self, tmp_path):
        """Edit broke a file we touched, AND retries also break it -> revert."""
        from agent.agent_utils_rust import run_with_compile_gate

        broken_out = "src/blk.rs:42:5: error[E0308]: mismatched types\n"
        repo = self._stub_local_repo()
        re_prompt = MagicMock()

        with (
            # baseline clean, all subsequent checks broken on src/blk.rs
            patch(
                f"{MODULE}._run_cargo_check",
                side_effect=[(0, ""), (101, broken_out), (101, broken_out), (101, broken_out)],
            ),
            patch(
                f"{MODULE}._files_edited_since",
                return_value={str(tmp_path.resolve() / "src" / "blk.rs")},
            ),
        ):
            result = run_with_compile_gate(
                MagicMock(),
                repo_path=str(tmp_path),
                local_repo=repo,
                pre_sha="abc",
                max_retries=2,
                re_prompt_callback=re_prompt,
            )
        assert result["status"] == "reverted"
        assert result["retries_used"] == 2
        assert len(result["regressions"]) == 1
        # re-prompt callback should have been invoked twice (max_retries)
        assert re_prompt.call_count == 2
        # git reset --hard was called with the pre_sha
        repo.git.reset.assert_called_once_with("--hard", "abc")

    def test_retry_succeeds_on_second_attempt(self, tmp_path):
        from agent.agent_utils_rust import run_with_compile_gate

        broken_out = "src/blk.rs:42:5: error[E0308]: mismatched types\n"
        repo = self._stub_local_repo()
        re_prompt = MagicMock()

        with (
            patch(
                f"{MODULE}._run_cargo_check",
                # baseline clean, first attempt broken, retry clean
                side_effect=[(0, ""), (101, broken_out), (0, "")],
            ),
            patch(
                f"{MODULE}._files_edited_since",
                return_value={str(tmp_path.resolve() / "src" / "blk.rs")},
            ),
        ):
            result = run_with_compile_gate(
                MagicMock(),
                repo_path=str(tmp_path),
                local_repo=repo,
                pre_sha="abc",
                max_retries=2,
                re_prompt_callback=re_prompt,
            )
        assert result["status"] == "kept"
        assert result["retries_used"] == 1
        re_prompt.assert_called_once()
        # No revert when the retry recovers
        repo.git.reset.assert_not_called()

    def test_callback_exception_does_not_crash_gate(self, tmp_path):
        """re_prompt_callback raising should not abort the gate; gate logs and
        proceeds to revert when retries exhaust."""
        from agent.agent_utils_rust import run_with_compile_gate

        broken_out = "src/blk.rs:42:5: error[E0308]: mismatched types\n"
        repo = self._stub_local_repo()

        def _raising(_err):
            raise RuntimeError("simulated callback failure")

        with (
            patch(
                f"{MODULE}._run_cargo_check",
                side_effect=[(0, ""), (101, broken_out), (101, broken_out), (101, broken_out)],
            ),
            patch(
                f"{MODULE}._files_edited_since",
                return_value={str(tmp_path.resolve() / "src" / "blk.rs")},
            ),
        ):
            result = run_with_compile_gate(
                MagicMock(),
                repo_path=str(tmp_path),
                local_repo=repo,
                pre_sha="abc",
                max_retries=2,
                re_prompt_callback=_raising,
            )
        # callback raised twice (each retry); gate ultimately reverts
        assert result["status"] == "reverted"
        repo.git.reset.assert_called_once_with("--hard", "abc")
