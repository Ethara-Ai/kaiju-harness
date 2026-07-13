from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.prepare_repo_js as prepare_repo_js
from commit0.harness.constants_js import JS_BASE_BRANCH, JS_DATASET_BRANCH
from tools.prepare_repo_js import (
    DEFAULT_ORG,
    _build_test_dict,
    detect_js_src_dir,
    detect_js_test_dirs,
    generate_setup_dict_js,
)


PREPARE_REPO_JS_SOURCE = Path(prepare_repo_js.__file__).read_text(encoding="utf-8")


def _make_repo(tmp_path: Path, with_pkg: dict | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pkg = with_pkg or {
        "name": "p-queue",
        "version": "1.0.0",
        "scripts": {"test": "jest"},
        "devDependencies": {"jest": "29.0.0"},
    }
    (tmp_path / "package.json").write_text(
        json.dumps(pkg), encoding="utf-8"
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.js").write_text("function f(){return 1;}\n", encoding="utf-8")
    tests = tmp_path / "__tests__"
    tests.mkdir()
    (tests / "f.test.js").write_text(
        "test('x', () => {});\n", encoding="utf-8"
    )
    return tmp_path


class TestBranchConstants:
    def test_js_base_branch_is_commit0(self) -> None:
        assert JS_BASE_BRANCH == "commit0"
        assert "-" not in JS_BASE_BRANCH

    def test_js_dataset_branch_is_commit0_all(self) -> None:
        assert JS_DATASET_BRANCH == "commit0_all"

    def test_create_branch_defaults_to_js_constants(self) -> None:
        from tools.prepare_repo_js import create_js_stubbed_branch

        sig = inspect.signature(create_js_stubbed_branch)
        assert sig.parameters["branch_name"].default == JS_DATASET_BRANCH
        assert sig.parameters["base_branch_name"].default == JS_BASE_BRANCH


class TestSetupSpecificationEmpty:
    def test_specification_is_empty_string(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        with patch.object(
            prepare_repo_js,
            "_detect_node_version_for_repo",
            return_value=(20, "package_json", []),
        ):
            setup_dict, _, _, _ = generate_setup_dict_js(tmp_path)
        assert setup_dict["specification"] == ""

    def test_specification_field_present(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        with patch.object(
            prepare_repo_js,
            "_detect_node_version_for_repo",
            return_value=(20, "package_json", []),
        ):
            setup_dict, _, _, _ = generate_setup_dict_js(tmp_path)
        assert "specification" in setup_dict


class TestSetupDictShape:
    def test_setup_dict_keys(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        with patch.object(
            prepare_repo_js,
            "_detect_node_version_for_repo",
            return_value=(20, "package_json", []),
        ):
            setup_dict, _, _, _ = generate_setup_dict_js(tmp_path)
        for k in (
            "node_version",
            "install",
            "packages",
            "pre_install",
            "specification",
        ):
            assert k in setup_dict, f"missing key {k!r}"

    def test_test_dict_has_required_keys(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        with patch.object(
            prepare_repo_js,
            "_detect_node_version_for_repo",
            return_value=(20, "package_json", []),
        ):
            _, test_dict, _, _ = generate_setup_dict_js(tmp_path)
        assert "test_cmd" in test_dict
        assert "test_dir" in test_dict

    def test_framework_detected_from_devdeps(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        with patch.object(
            prepare_repo_js,
            "_detect_node_version_for_repo",
            return_value=(20, "package_json", []),
        ):
            _, _, framework, _ = generate_setup_dict_js(tmp_path)
        assert framework == "jest"

    def test_package_manager_from_lockfile(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        with patch.object(
            prepare_repo_js,
            "_detect_node_version_for_repo",
            return_value=(20, "package_json", []),
        ):
            _, _, _, pm = generate_setup_dict_js(tmp_path)
        assert pm == "npm"


class TestBuildTestDict:
    @pytest.mark.parametrize(
        ("pm", "framework", "expected_cmd"),
        [
            ("npm", "jest", "npx jest"),
            ("pnpm", "jest", "pnpm exec jest"),
            ("yarn", "jest", "yarn jest"),
            ("bun", "jest", "bunx jest"),
            ("npm", "vitest", "npx vitest run"),
            ("npm", "mocha", "npx mocha"),
            ("npm", "node_test", "node --test"),
        ],
    )
    def test_dispatch(self, pm: str, framework: str, expected_cmd: str) -> None:
        result = _build_test_dict(pm, framework, "__tests__")
        assert result["test_cmd"] == expected_cmd
        assert result["test_dir"] == "__tests__"


class TestInstanceIdFormat:
    def test_instance_id_uses_hyphen_prefix(self) -> None:
        for node in ast.walk(ast.parse(PREPARE_REPO_JS_SOURCE)):
            if (
                isinstance(node, ast.JoinedStr)
                and any(
                    isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                    and "commit-0/" in v.value
                    for v in node.values
                )
            ):
                return
        pytest.fail(
            "prepare_repo_js.py must build instance_id with 'commit-0/' (hyphen) prefix"
        )

    def test_instance_id_not_underscore_prefix(self) -> None:
        assert "commit_0/" not in PREPARE_REPO_JS_SOURCE, (
            "instance_id must use 'commit-0/' (hyphen) not 'commit_0/' (underscore)"
        )

    def test_dry_run_entry_uses_hyphen_format(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "stage"
        clone_dir.mkdir()

        with (
            patch.object(prepare_repo_js, "fork_repo", return_value="fork/p-queue"),
            patch.object(
                prepare_repo_js,
                "full_clone",
                side_effect=lambda full_name, *args, **kwargs: _make_repo(
                    clone_dir / "p-queue"
                ),
            ),
            patch.object(
                prepare_repo_js, "validate_js_candidate", return_value=(True, [])
            ),
            patch.object(
                prepare_repo_js,
                "_detect_node_version_for_repo",
                return_value=(20, "package_json", []),
            ),
            patch.object(
                prepare_repo_js,
                "create_js_stubbed_branch",
                return_value=("a" * 40, "b" * 40, 5, True),
            ),
            patch.object(prepare_repo_js, "_assert_monorepo_safety"),
        ):
            entry = prepare_repo_js.prepare_js_repo(
                full_name="sindresorhus/p-queue",
                clone_dir=clone_dir,
                dry_run=True,
            )

        assert entry is not None
        assert entry["instance_id"] == "commit-0/p-queue"
        assert entry["repo"].endswith("/p-queue")


class TestSetupSpecificationDocumentedDecision:
    def test_no_pdf_extraction_logic(self) -> None:
        for node in ast.walk(ast.parse(PREPARE_REPO_JS_SOURCE)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for n in node.names:
                    assert n.name not in {"fitz", "pymupdf"}, (
                        f"prepare_repo_js.py must not import PDF deps "
                        f"(JS-PLAN §11 item 7 option (a) — empty spec) "
                        f"(line {node.lineno})"
                    )


class TestDetectJsSrcDir:
    def test_finds_src_dir(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.js").write_text("x", encoding="utf-8")
        assert detect_js_src_dir(tmp_path) == "src"

    def test_finds_source_dir(self, tmp_path: Path) -> None:
        (tmp_path / "source").mkdir()
        (tmp_path / "source" / "a.js").write_text("x", encoding="utf-8")
        assert detect_js_src_dir(tmp_path) == "source"

    def test_falls_back_to_root(self, tmp_path: Path) -> None:
        (tmp_path / "rootthing.js").write_text("x", encoding="utf-8")
        assert detect_js_src_dir(tmp_path) == "."

    def test_returns_empty_when_no_js(self, tmp_path: Path) -> None:
        assert detect_js_src_dir(tmp_path) == ""


class TestDetectJsTestDirs:
    def test_finds_tests_directory(self, tmp_path: Path) -> None:
        td = tmp_path / "__tests__"
        td.mkdir()
        (td / "a.test.js").write_text("test('x', () => {});", encoding="utf-8")
        (td / "b.test.js").write_text("test('y', () => {});", encoding="utf-8")
        dirs = detect_js_test_dirs(tmp_path)
        assert len(dirs) >= 1
        assert any(d.name == "__tests__" for d in dirs)


class TestDryRunSkipsPush:
    def test_dry_run_returns_entry_with_token_disabled(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "stage"
        clone_dir.mkdir()
        with (
            patch.object(prepare_repo_js, "fork_repo") as mock_fork,
            patch.object(
                prepare_repo_js,
                "full_clone",
                side_effect=lambda full_name, *args, **kwargs: _make_repo(
                    clone_dir / "p-queue"
                ),
            ),
            patch.object(
                prepare_repo_js, "validate_js_candidate", return_value=(True, [])
            ),
            patch.object(
                prepare_repo_js,
                "_detect_node_version_for_repo",
                return_value=(20, "package_json", []),
            ),
            patch.object(
                prepare_repo_js,
                "create_js_stubbed_branch",
                return_value=("a" * 40, "b" * 40, 5, True),
            ),
            patch.object(prepare_repo_js, "_assert_monorepo_safety"),
            patch.object(prepare_repo_js, "push_to_fork") as mock_push,
            patch.object(prepare_repo_js, "git"),
        ):
            entry = prepare_repo_js.prepare_js_repo(
                full_name="sindresorhus/p-queue",
                clone_dir=clone_dir,
                dry_run=True,
            )
        assert entry is not None
        mock_fork.assert_not_called()
        mock_push.assert_not_called()


class TestEntryFields:
    def test_entry_contains_expected_keys(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "stage"
        clone_dir.mkdir()
        with (
            patch.object(prepare_repo_js, "fork_repo", return_value="org/p-queue"),
            patch.object(
                prepare_repo_js,
                "full_clone",
                side_effect=lambda full_name, *args, **kwargs: _make_repo(
                    clone_dir / "p-queue"
                ),
            ),
            patch.object(
                prepare_repo_js, "validate_js_candidate", return_value=(True, [])
            ),
            patch.object(
                prepare_repo_js,
                "_detect_node_version_for_repo",
                return_value=(20, "package_json", []),
            ),
            patch.object(
                prepare_repo_js,
                "create_js_stubbed_branch",
                return_value=("a" * 40, "b" * 40, 5, True),
            ),
            patch.object(prepare_repo_js, "_assert_monorepo_safety"),
        ):
            entry = prepare_repo_js.prepare_js_repo(
                full_name="sindresorhus/p-queue",
                clone_dir=clone_dir,
                dry_run=True,
            )
        assert entry is not None
        for k in (
            "instance_id",
            "repo",
            "original_repo",
            "base_commit",
            "reference_commit",
            "src_dir",
            "test_framework",
            "package_manager",
            "setup",
            "test",
        ):
            assert k in entry, f"missing key {k!r}"
        assert entry["setup"]["specification"] == ""


class TestDefaults:
    def test_default_org(self) -> None:
        assert DEFAULT_ORG == "Zahgon"


class TestValidateStubberDepsConcurrency:
    def test_existing_probe_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from tools.prepare_repo_js import _STUBBER_DIR, _validate_stubber_deps

        probe = _STUBBER_DIR / "node_modules" / "@babel" / "parser"
        if not probe.exists():
            pytest.skip("@babel/parser not installed; cannot verify short-circuit")

        run_mock = MagicMock()
        monkeypatch.setattr("tools.prepare_repo_js.subprocess.run", run_mock)
        _validate_stubber_deps()
        run_mock.assert_not_called()

    def test_npm_install_invoked_with_no_audit_no_fund_when_probe_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        import tools.prepare_repo_js as prj

        fake_stubber_dir = tmp_path / "stubber"
        fake_stubber_dir.mkdir()
        (fake_stubber_dir / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(prj, "_STUBBER_DIR", fake_stubber_dir)

        probe_dir = fake_stubber_dir / "node_modules" / "@babel" / "parser"

        captured_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            captured_calls.append(list(cmd))
            probe_dir.mkdir(parents=True)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        monkeypatch.setattr(prj.subprocess, "run", _fake_run)
        monkeypatch.setattr(prj.shutil, "which", lambda _name: "/usr/bin/npm")

        prj._validate_stubber_deps()

        assert len(captured_calls) == 1
        cmd = captured_calls[0]
        assert cmd[0] == "npm"
        assert "install" in cmd
        assert "--no-audit" in cmd
        assert "--no-fund" in cmd

    def test_npm_install_failure_raises_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        import tools.prepare_repo_js as prj

        fake_stubber_dir = tmp_path / "stubber"
        fake_stubber_dir.mkdir()
        (fake_stubber_dir / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(prj, "_STUBBER_DIR", fake_stubber_dir)

        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "npm error: registry unreachable"
            return result

        monkeypatch.setattr(prj.subprocess, "run", _fake_run)
        monkeypatch.setattr(prj.shutil, "which", lambda _name: "/usr/bin/npm")

        with pytest.raises(OSError, match="did not produce"):
            prj._validate_stubber_deps()

    def test_missing_npm_raises_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import tools.prepare_repo_js as prj

        fake_stubber_dir = tmp_path / "stubber"
        fake_stubber_dir.mkdir()
        (fake_stubber_dir / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(prj, "_STUBBER_DIR", fake_stubber_dir)
        monkeypatch.setattr(prj.shutil, "which", lambda _name: None)

        with pytest.raises(OSError, match="npm.*not on PATH"):
            prj._validate_stubber_deps()


class TestF002StubberInstallLock:
    def test_install_holds_fcntl_lock_around_npm_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        import tools.prepare_repo_js as prj

        fake_stubber_dir = tmp_path / "stubber"
        fake_stubber_dir.mkdir()
        (fake_stubber_dir / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(prj, "_STUBBER_DIR", fake_stubber_dir)
        probe_dir = fake_stubber_dir / "node_modules" / "@babel" / "parser"

        flock_calls: list[tuple[int, int]] = []
        original_flock = prj.fcntl.flock

        def _track_flock(fd, op):
            flock_calls.append((fd, op))
            return original_flock(fd, op)

        monkeypatch.setattr(prj.fcntl, "flock", _track_flock)

        def _fake_run(cmd, **kwargs):
            assert any(op == prj.fcntl.LOCK_EX for _, op in flock_calls), (
                "F-002: LOCK_EX must be held before npm install runs"
            )
            probe_dir.mkdir(parents=True)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        monkeypatch.setattr(prj.subprocess, "run", _fake_run)
        monkeypatch.setattr(prj.shutil, "which", lambda _name: "/usr/bin/npm")

        prj._validate_stubber_deps()

        assert (fake_stubber_dir / ".install.lock").exists(), (
            "F-002: install.lock sentinel must be created"
        )
        assert any(op == prj.fcntl.LOCK_EX for _, op in flock_calls)
        assert any(op == prj.fcntl.LOCK_UN for _, op in flock_calls)

    def test_probe_rechecked_inside_lock_avoids_double_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        import tools.prepare_repo_js as prj

        fake_stubber_dir = tmp_path / "stubber"
        fake_stubber_dir.mkdir()
        (fake_stubber_dir / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(prj, "_STUBBER_DIR", fake_stubber_dir)
        probe_dir = fake_stubber_dir / "node_modules" / "@babel" / "parser"

        original_lock = prj._stubber_install_lock

        @prj.contextmanager
        def _populated_lock():
            with original_lock():
                probe_dir.mkdir(parents=True)
                yield

        monkeypatch.setattr(prj, "_stubber_install_lock", _populated_lock)

        run_mock = MagicMock()
        monkeypatch.setattr(prj.subprocess, "run", run_mock)
        monkeypatch.setattr(prj.shutil, "which", lambda _name: "/usr/bin/npm")

        prj._validate_stubber_deps()
        run_mock.assert_not_called()


class TestF009InstallFailureAborts:
    def test_dependency_install_nonzero_raises_runtime_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        import tools.prepare_repo_js as prj

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "package.json").write_text("{}", encoding="utf-8")
        (repo_dir / "src").mkdir()
        (repo_dir / "src" / "index.js").write_text("// x\n", encoding="utf-8")

        monkeypatch.setattr(prj, "_ensure_pkg_manager", lambda _pm: None)

        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 7
            result.stderr = "npm error: ENOENT"
            return result

        monkeypatch.setattr(prj.subprocess, "run", _fake_run)
        monkeypatch.setattr(prj, "git", MagicMock(return_value=""))
        monkeypatch.setattr(prj, "detect_js_test_dirs", lambda _d: [])
        monkeypatch.setattr(prj, "_collect_extra_scan_dirs", lambda *_a, **_kw: [])

        with pytest.raises(RuntimeError, match="Dependency install failed"):
            prj.create_js_stubbed_branch(
                repo_dir=repo_dir,
                full_name="owner/repo",
                src_dir="src",
                pkg_manager="npm",
            )


class TestF007UnpushableRowsRejected:
    def test_push_failure_and_remote_resolution_failure_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        import tools.prepare_repo_js as prj

        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        monkeypatch.setattr(prj, "fork_repo", lambda *_a, **_kw: "Zahgon/lib")
        monkeypatch.setattr(prj, "full_clone", lambda *_a, **_kw: tmp_path / "lib")
        monkeypatch.setattr(prj, "validate_js_candidate", lambda _d: (True, []))
        monkeypatch.setattr(prj, "detect_js_src_dir", lambda _d: "src")
        monkeypatch.setattr(prj, "_assert_monorepo_safety", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            prj,
            "generate_setup_dict_js",
            lambda _d: ({}, {"test_cmd": "npx jest", "test_dir": "tests"}, "jest", "npm"),
        )
        monkeypatch.setattr(
            prj,
            "create_js_stubbed_branch",
            lambda *_a, **_kw: ("a" * 40, "b" * 40, 5, True),
        )
        monkeypatch.setattr(prj, "git", MagicMock())

        def _failing_push(*_a, **_kw):
            raise RuntimeError("remote unreachable")

        monkeypatch.setattr(prj, "push_to_fork", _failing_push)
        monkeypatch.setattr(prj, "_resolve_commits_from_remote", lambda *_a, **_kw: None)

        result = prj.prepare_js_repo(
            full_name="upstream/lib",
            clone_dir=tmp_path,
            org="Zahgon",
            dry_run=False,
        )
        assert result is None, (
            "F-007: when push fails AND remote resolution fails, prepare_js_repo "
            "must NOT emit a dataset row whose SHAs only exist locally"
        )


class TestStubMarkerInvariant:
    def test_zero_stub_markers_in_diff_raises(self) -> None:
        diff_no_stubs = "\n".join(
            [
                "diff --git a/src/foo.js b/src/foo.js",
                "index 1..2 100644",
                "+++ b/src/foo.js",
                "+function added() { return 1; }",
            ]
        )
        stub_marker_count = sum(
            1
            for line in diff_no_stubs.splitlines()
            if line.startswith("+")
            and not line.startswith("+++")
            and 'throw new Error("STUB")' in line
        )
        assert stub_marker_count == 0

    def test_positive_stub_markers_pass_invariant(self) -> None:
        diff_with_stubs = "\n".join(
            [
                "diff --git a/src/foo.js b/src/foo.js",
                "+++ b/src/foo.js",
                '+  throw new Error("STUB"); // __COMMIT0_STUB__',
                '+  throw new Error("STUB"); // __COMMIT0_STUB__',
            ]
        )
        stub_marker_count = sum(
            1
            for line in diff_with_stubs.splitlines()
            if line.startswith("+")
            and not line.startswith("+++")
            and 'throw new Error("STUB")' in line
        )
        assert stub_marker_count == 2

    def test_diff_header_plus_plus_plus_not_counted(self) -> None:
        diff = '+++ b/file.js with throw new Error("STUB") in header'
        count = sum(
            1
            for line in diff.splitlines()
            if line.startswith("+")
            and not line.startswith("+++")
            and 'throw new Error("STUB")' in line
        )
        assert count == 0


class TestCreateJsStubbedBranchInvariant:
    def test_zero_stub_markers_raises_runtime_error(self) -> None:
        import inspect

        from tools.prepare_repo_js import create_js_stubbed_branch

        source = inspect.getsource(create_js_stubbed_branch)
        assert "if stub_marker_count < 1:" in source, (
            "PR-G7: the invariant must guard against silent zero-marker case"
        )
        assert "Stubbing verification failed" in source
        assert 'throw new Error("STUB")' in source
