from __future__ import annotations

from pathlib import Path


from agent.agent_utils_java import (
    _in_build_dir,
    collect_java_files,
    is_java_stubbed,
    count_java_stubs,
)


class TestInBuildDir:
    def test_target_dir(self) -> None:
        assert _in_build_dir(("target", "classes", "Foo.java")) is True

    def test_build_dir(self) -> None:
        assert _in_build_dir(("build", "classes", "Foo.java")) is True

    def test_gradle_dir(self) -> None:
        assert _in_build_dir((".gradle", "caches", "file.txt")) is True

    def test_not_in_build_dir(self) -> None:
        assert _in_build_dir(("src", "main", "java", "Foo.java")) is False

    def test_nested_build_dir(self) -> None:
        assert _in_build_dir(("module", "target", "Foo.java")) is True


class TestCollectJavaFiles:
    def test_finds_java_files(self, tmp_path: Path) -> None:
        java_dir = tmp_path / "myproject" / "src" / "main" / "java"
        java_dir.mkdir(parents=True)
        (java_dir / "Foo.java").write_text("class Foo {}")
        (java_dir / "Bar.java").write_text("class Bar {}")

        result = collect_java_files(str(tmp_path))
        assert len(result) == 2
        names = [Path(f).name for f in result]
        assert "Foo.java" in names
        assert "Bar.java" in names

    def test_skips_build_dirs(self, tmp_path: Path) -> None:
        build_dir = tmp_path / "target" / "classes"
        build_dir.mkdir(parents=True)
        (build_dir / "Foo.java").write_text("class Foo {}")

        result = collect_java_files(str(tmp_path))
        assert result == []

    def test_skips_test_dirs(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "module" / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        (test_dir / "FooTest.java").write_text("class FooTest {}")

        result = collect_java_files(str(tmp_path))
        assert result == []

    def test_empty_repo(self, tmp_path: Path) -> None:
        result = collect_java_files(str(tmp_path))
        assert result == []


class TestIsJavaStubbed:
    def test_stubbed_file(self, tmp_path: Path) -> None:
        f = tmp_path / "Foo.java"
        f.write_text(
            'throw new UnsupportedOperationException("STUB: not implemented");'
        )
        assert is_java_stubbed(str(f)) is True

    def test_unstubbed_file(self, tmp_path: Path) -> None:
        f = tmp_path / "Foo.java"
        f.write_text("class Foo { void bar() { return; } }")
        assert is_java_stubbed(str(f)) is False

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "Foo.java"
        f.write_text("")
        assert is_java_stubbed(str(f)) is False


class TestCountJavaStubs:
    def test_counts_correctly(self, tmp_path: Path) -> None:
        java_dir = tmp_path / "myproject" / "src" / "main" / "java"
        java_dir.mkdir(parents=True)
        (java_dir / "Foo.java").write_text(
            'void a() { throw new UnsupportedOperationException("STUB: not implemented"); }\n'
            'void b() { throw new UnsupportedOperationException("STUB: not implemented"); }'
        )
        (java_dir / "Bar.java").write_text("class Bar { void run() {} }")

        result = count_java_stubs(str(tmp_path))
        assert result["total_files"] == 2
        assert result["stubbed_files"] == 1
        assert result["total_stubs"] == 2
