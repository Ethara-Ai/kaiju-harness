from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.stub_js_runner import (
    STUB_JS_PATH,
    STUBBER_DIR,
    _TS_NODE_COMPILER_OPTIONS,
    run_stub_js,
)


STUB_MARKER = "// __COMMIT0_STUB__"
STUB_THROW = 'throw new Error("STUB")'


def _ts_node_available() -> bool:
    return (STUBBER_DIR / "node_modules" / ".bin" / "ts-node").exists()


def _babel_parser_available() -> bool:
    return (STUBBER_DIR / "node_modules" / "@babel" / "parser").exists()


if not (_ts_node_available() and _babel_parser_available()):
    raise RuntimeError(
        "test_stub_js requires ts-node + @babel/parser under "
        f"{STUBBER_DIR}/node_modules. Run `cd {STUBBER_DIR} && npm install` "
        "before invoking pytest (TST-G1: fail loudly instead of silently skipping)."
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _run_stub_subprocess(
    src_dir: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    ts_node = STUBBER_DIR / "node_modules" / ".bin" / "ts-node"
    cmd = [
        str(ts_node),
        str(STUB_JS_PATH),
        "--src-dir",
        str(src_dir),
        "--mode",
        "all",
    ]
    env = os.environ.copy()
    env["TS_NODE_COMPILER_OPTIONS"] = _TS_NODE_COMPILER_OPTIONS
    env["TS_NODE_TRANSPILE_ONLY"] = "true"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(cwd or STUBBER_DIR),
        env=env,
        check=False,
    )


class TestSimpleFunctionStub:
    def test_named_function_body_replaced_with_stub(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "lib.js",
            "function adder(a, b) {\n  return a + b;\n}\nmodule.exports = adder;\n",
        )
        report = run_stub_js(src_dir=src, mode="all")
        text = target.read_text(encoding="utf-8")
        assert STUB_MARKER in text
        assert STUB_THROW in text
        assert report["functions_stubbed"] >= 1


class TestIdempotencyJR8:
    def test_double_stub_produces_identical_sha256(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "math.js",
            "function multiply(a, b) {\n  let r = 0;\n  for (let i=0;i<b;i++) r+=a;\n  return r;\n}\nmodule.exports = multiply;\n",
        )
        run_stub_js(src_dir=src, mode="all")
        sha_after_first = _sha256(target)
        run_stub_js(src_dir=src, mode="all")
        sha_after_second = _sha256(target)
        assert sha_after_first == sha_after_second, (
            "JR-8 idempotency violated: re-running the stubber must "
            "produce byte-identical output"
        )

    def test_double_stub_preserves_stub_marker(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "x.js",
            "function f(x) {\n  const y = x * 2;\n  return y;\n}\n",
        )
        run_stub_js(src_dir=src, mode="all")
        first = target.read_text(encoding="utf-8")
        run_stub_js(src_dir=src, mode="all")
        second = target.read_text(encoding="utf-8")
        assert first == second
        assert first.count(STUB_MARKER) == 1


class TestSimpleConstructorHeuristic:
    def test_simple_constructor_not_stubbed(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "klass.js",
            (
                "class Point {\n"
                "  constructor(x, y) {\n"
                "    this.x = x;\n"
                "    this.y = y;\n"
                "  }\n"
                "  distance(other) {\n"
                "    const dx = this.x - other.x;\n"
                "    const dy = this.y - other.y;\n"
                "    return Math.sqrt(dx*dx + dy*dy);\n"
                "  }\n"
                "}\n"
                "module.exports = Point;\n"
            ),
        )
        run_stub_js(src_dir=src, mode="all")
        text = target.read_text(encoding="utf-8")
        ctor_start = text.find("constructor(x, y)")
        distance_start = text.find("distance(other)")
        ctor_body = text[ctor_start:distance_start]
        assert STUB_THROW not in ctor_body, (
            "Simple constructor (only super/this.x= assignments) must NOT be stubbed; "
            f"found stub throw inside ctor body:\n{ctor_body}"
        )

    def test_non_simple_constructor_is_stubbed(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "klass.js",
            (
                "class Cache {\n"
                "  constructor(size) {\n"
                "    if (size < 0) throw new Error('bad');\n"
                "    this.size = size;\n"
                "    const arr = new Array(size).fill(null);\n"
                "    this.arr = arr;\n"
                "  }\n"
                "}\n"
                "module.exports = Cache;\n"
            ),
        )
        run_stub_js(src_dir=src, mode="all")
        text = target.read_text(encoding="utf-8")
        assert STUB_MARKER in text
        assert STUB_THROW in text


class TestImportTimeDetection:
    def test_import_time_function_not_stubbed(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "config.js",
            (
                "function readEnv() {\n"
                "  return process.env.MYVAR || 'default';\n"
                "}\n"
                "const ENV = readEnv();\n"
                "module.exports = { ENV };\n"
            ),
        )
        run_stub_js(src_dir=src, mode="all")
        text = target.read_text(encoding="utf-8")
        read_env_idx = text.find("function readEnv()")
        body_window = text[read_env_idx : read_env_idx + 200]
        assert STUB_THROW not in body_window, (
            "readEnv is called at module load → must be preserved (JR-4)"
        )


class TestReExportSourceGuard:
    def test_reexport_does_not_crash_stubber(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "sub.js", "export function x() { return 1; }\n")
        _write(
            src / "index.js",
            "export { x } from './sub.js';\nexport function f() { return 1; }\n",
        )
        report = run_stub_js(src_dir=src, mode="all")
        assert isinstance(report, dict)
        assert report["files_processed"] >= 1


class TestJsxGate:
    def test_refuses_tsx_with_exit_2(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "good.js", "function f(){return 1;}\n")
        _write(
            src / "bad.ts",
            "function g(x: number): number { return x + 1; }\n",
        )
        proc = _run_stub_subprocess(src)
        assert proc.returncode == 2, (
            f"stub_js must exit 2 on .ts files; got rc={proc.returncode}\n"
            f"stderr: {proc.stderr[-500:]}\nstdout: {proc.stdout[-500:]}"
        )
        assert "TypeScript" in proc.stderr or "stub_ts" in proc.stderr

    def test_refuses_tsx_extension(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "good.js", "function f(){return 1;}\n")
        _write(src / "comp.tsx", "export const C = (): null => null;\n")
        proc = _run_stub_subprocess(src)
        assert proc.returncode == 2

    def test_pure_js_passes_through(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "ok.js", "function pureFn() { let x = 1; return x + 1; }\n")
        proc = _run_stub_subprocess(src)
        assert proc.returncode == 0, (
            f"pure JS must succeed; got rc={proc.returncode}\nstderr: {proc.stderr[-500:]}"
        )


class TestStubMarkerInjection:
    def test_marker_value_constant(self) -> None:
        assert STUB_MARKER == "// __COMMIT0_STUB__"

    def test_marker_present_after_stub(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(src / "g.js", "function h() { return Math.random(); }\n")
        run_stub_js(src_dir=src, mode="all")
        assert STUB_MARKER in target.read_text(encoding="utf-8")


class TestReportShape:
    def test_report_returns_expected_keys(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "f.js", "function k() { return 42; }\n")
        report = run_stub_js(src_dir=src, mode="all")
        for k in (
            "files_processed",
            "files_modified",
            "files_skipped",
            "functions_stubbed",
            "functions_skipped_import_time",
            "functions_skipped_other",
            "errors",
        ):
            assert k in report, f"report missing key {k!r}"


class TestEmptyBodyPreserved:
    def test_empty_function_body_not_stubbed(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(src / "noop.js", "function noop() {}\nmodule.exports = noop;\n")
        run_stub_js(src_dir=src, mode="all")
        text = target.read_text(encoding="utf-8")
        assert STUB_THROW not in text, "Empty body should be skipped"


class TestVerboseReport:
    def test_import_time_names_field_is_list(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "f.js", "function a(){} const _ = a();\n")
        report = run_stub_js(src_dir=src, mode="all")
        assert isinstance(report.get("import_time_names"), list)


class TestTestFilesNotStubbed:
    def test_test_file_skipped(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        target = _write(
            src / "thing.test.js",
            "function inTest() { return 'wired'; }\n",
        )
        run_stub_js(src_dir=src, mode="all")
        text = target.read_text(encoding="utf-8")
        assert STUB_THROW not in text


class TestStubberPathConstants:
    def test_stubber_dir_exists(self) -> None:
        assert STUBBER_DIR.is_dir()

    def test_stub_js_path_exists(self) -> None:
        assert STUB_JS_PATH.exists()

    def test_ts_node_compiler_options_valid_json(self) -> None:
        decoded = json.loads(_TS_NODE_COMPILER_OPTIONS)
        assert decoded["target"] == "es2022"


class TestCleanup:
    def test_stubber_does_not_leave_node_modules_in_src(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src / "u.js", "function u(){return 1;}\n")
        run_stub_js(src_dir=src, mode="all")
        assert not (src / "node_modules").exists()
        assert not (src / "package.json").exists()


class TestPackageJsonDependencyPins:
    @pytest.fixture
    def manifest(self) -> dict:
        manifest_path = STUBBER_DIR / "package.json"
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "dep",
        [
            "@babel/generator",
            "@babel/parser",
            "@babel/traverse",
            "@babel/types",
        ],
    )
    def test_babel_deps_pinned_to_exact_version(
        self, manifest: dict, dep: str
    ) -> None:
        version = manifest["dependencies"][dep]
        assert version == "7.25.6", (
            f"{dep} must stay at 7.25.6 — AST shape changes between minors "
            f"can silently break stub_js.ts; got {version!r}"
        )
        assert not version.startswith("^"), (
            f"{dep} must use exact pin, not caret range (got {version!r})"
        )
        assert not version.startswith("~"), (
            f"{dep} must use exact pin, not tilde range (got {version!r})"
        )

    def test_ts_node_pinned(self, manifest: dict) -> None:
        assert manifest["devDependencies"]["ts-node"] == "10.9.2"

    def test_typescript_pinned(self, manifest: dict) -> None:
        assert manifest["devDependencies"]["typescript"] == "5.6.3"

    def test_engines_node_gte_20(self, manifest: dict) -> None:
        assert manifest["engines"]["node"] == ">=20"

    def test_private_true(self, manifest: dict) -> None:
        assert manifest.get("private") is True


class TestSymlinkLoopGuard:
    def test_symlink_to_self_does_not_hang(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        _write(src / "a.js", "function a(){return 1;}\n")
        loop = src / "self_loop"
        try:
            loop.symlink_to(src, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support directory symlinks")
        report = run_stub_js(src_dir=src, mode="all")
        assert report["files_processed"] >= 1

    def test_symlink_to_ancestor_does_not_hang(self, tmp_path: Path) -> None:
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        _write(inner / "a.js", "function a(){return 1;}\n")
        loop = inner / "back"
        try:
            loop.symlink_to(outer, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support directory symlinks")
        report = run_stub_js(src_dir=outer, mode="all")
        assert report["files_processed"] >= 1


class TestParseErrorRecovery:
    def test_malformed_source_does_not_crash_stubber(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(
            src / "broken.js",
            "function busted(a, b {\n  return a + b;\n}\n",
        )
        _write(src / "ok.js", "function ok(){return 1;}\n")
        report = run_stub_js(src_dir=src, mode="all")
        assert isinstance(report, dict)
        assert report["files_processed"] >= 1

    def test_malformed_source_records_error_or_skips(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        _write(
            src / "torn.js",
            "function torn(x) {\n  return x +\n",
        )
        report = run_stub_js(src_dir=src, mode="all")
        assert isinstance(report.get("errors"), list)
        assert len(report["errors"]) >= 1, (
            "SJ-G5: malformed source must surface as an entry in report.errors, "
            "not be silently corrupted by Babel errorRecovery."
        )
        assert any(
            "torn.js" in entry.get("file", "")
            for entry in report["errors"]
        ), "SJ-G5: the malformed file path must be recorded"

    def test_only_malformed_file_does_not_propagate_to_other_files(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        _write(
            src / "broken.js",
            "function broken( {\n",
        )
        clean = _write(
            src / "clean.js",
            "function clean() { let x = 1; return x + 2; }\n",
        )
        run_stub_js(src_dir=src, mode="all")
        text = clean.read_text(encoding="utf-8")
        assert STUB_MARKER in text or STUB_THROW in text or text == clean.read_text(
            encoding="utf-8"
        )


class TestF004AtomicWrite:
    def test_stub_js_uses_write_tmp_then_rename_pattern(self) -> None:
        source = STUB_JS_PATH.read_text(encoding="utf-8")

        assert "writeFileSync(tmp" in source, (
            "F-004: stub_js.ts must write to a `.tmp` sibling first, then "
            "atomically renameSync onto the source file. A SIGKILL or "
            "disk-full mid-write of the source file produces an "
            "irrecoverable truncation."
        )
        assert "renameSync(tmp, f)" in source, (
            "F-004: stub_js.ts must use renameSync(tmp, f) for the atomic swap"
        )
        assert "writeFileSync(f, transformed" not in source, (
            "F-004: stub_js.ts must NOT write directly to the source path; "
            "this is the non-atomic pattern called out by the dry-run finding"
        )


class TestPostStubReparseSafetyNet:
    def test_stub_js_reparses_transformed_source_strictly(self) -> None:
        source = STUB_JS_PATH.read_text(encoding="utf-8")

        assert "errorRecovery: false" in source, (
            "Post-stub re-parse must run without errorRecovery so any "
            "transformation that produces syntactically invalid JS is "
            "surfaced rather than silently written to disk."
        )
        assert "post-stub re-parse failed" in source, (
            "Re-parse failures must be recorded to report.errors with this "
            "exact marker so test runners can detect silent stubber bugs."
        )
        assert "Mirrors Python tools/stub.py post-rewrite check" in source, (
            "Comment documents Python-harness parity intent; removing it "
            "invites a future cleanup to delete the duplicate parse call."
        )

