from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE = "commit0.harness.lint_java"


class TestJavaLintResult:
    def test_dataclass_fields(self) -> None:
        from commit0.harness.lint_java import JavaLintResult

        result = JavaLintResult(
            file="Foo.java",
            line=10,
            column=5,
            severity="error",
            message="Missing semicolon",
            rule="com.puppycrawl.tools.checkstyle.checks",
        )
        assert result.file == "Foo.java"
        assert result.line == 10
        assert result.column == 5
        assert result.severity == "error"
        assert result.message == "Missing semicolon"
        assert result.rule == "com.puppycrawl.tools.checkstyle.checks"


class TestParseCheckstyleXml:
    def test_valid_xml(self) -> None:
        from commit0.harness.lint_java import _parse_checkstyle_xml

        xml = (
            '<?xml version="1.0"?>'
            "<checkstyle>"
            '<file name="Foo.java">'
            '<error line="10" column="5" severity="error" '
            'message="Missing brace" source="rule.X"/>'
            "</file>"
            "</checkstyle>"
        )
        results = _parse_checkstyle_xml(xml)
        assert len(results) == 1
        assert results[0].file == "Foo.java"
        assert results[0].line == 10

    def test_multiple_files(self) -> None:
        from commit0.harness.lint_java import _parse_checkstyle_xml

        xml = (
            '<?xml version="1.0"?>'
            "<checkstyle>"
            '<file name="A.java">'
            '<error line="1" column="1" severity="warning" message="w" source="r1"/>'
            "</file>"
            '<file name="B.java">'
            '<error line="2" column="2" severity="error" message="e" source="r2"/>'
            '<error line="3" column="3" severity="info" message="i" source="r3"/>'
            "</file>"
            "</checkstyle>"
        )
        results = _parse_checkstyle_xml(xml)
        assert len(results) == 3
        files = {r.file for r in results}
        assert files == {"A.java", "B.java"}

    def test_empty_xml(self) -> None:
        from commit0.harness.lint_java import _parse_checkstyle_xml

        results = _parse_checkstyle_xml("")
        assert results == []

    def test_invalid_xml(self) -> None:
        from commit0.harness.lint_java import _parse_checkstyle_xml

        results = _parse_checkstyle_xml("<not>valid<xml")
        assert results == []


class TestEnsureCheckstyle:
    @patch(f"{MODULE}.urllib.request.urlretrieve")
    @patch(f"{MODULE}._get_checkstyle_sha256", return_value="")
    @patch(f"{MODULE}._get_checkstyle_url", return_value="https://example.com/cs.jar")
    def test_jar_exists_skips_download(
        self,
        mock_url: MagicMock,
        mock_sha: MagicMock,
        mock_retrieve: MagicMock,
        tmp_path: Path,
    ) -> None:
        jar_dir = tmp_path / ".commit0" / "tools"
        jar_dir.mkdir(parents=True)
        jar_path = jar_dir / "checkstyle-10.12.5.jar"
        jar_path.write_text("fake jar")

        with patch(f"{MODULE}.Path.home", return_value=tmp_path):
            from commit0.harness.lint_java import _ensure_checkstyle

            result = _ensure_checkstyle("10.12.5")
        assert result == jar_path
        mock_retrieve.assert_not_called()

    @patch(f"{MODULE}.urllib.request.urlretrieve")
    @patch(f"{MODULE}._sha256_file", return_value="abc123")
    @patch(f"{MODULE}._get_checkstyle_sha256", return_value="abc123")
    @patch(f"{MODULE}._get_checkstyle_url", return_value="https://example.com/cs.jar")
    def test_downloads_and_verifies_sha(
        self,
        mock_url: MagicMock,
        mock_sha: MagicMock,
        mock_sha_file: MagicMock,
        mock_retrieve: MagicMock,
        tmp_path: Path,
    ) -> None:
        def fake_retrieve(url: str, dest: Path) -> None:
            Path(dest).write_text("jar content")

        mock_retrieve.side_effect = fake_retrieve

        with patch(f"{MODULE}.Path.home", return_value=tmp_path):
            from commit0.harness.lint_java import _ensure_checkstyle

            result = _ensure_checkstyle("10.12.5")
        assert result.name == "checkstyle-10.12.5.jar"
        mock_retrieve.assert_called_once()


class TestLintJavaCheckstyle:
    @patch(f"{MODULE}._ensure_checkstyle")
    @patch(f"{MODULE}.subprocess.run")
    def test_runs_checkstyle_returns_results(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
    ) -> None:
        mock_ensure.return_value = Path("/tools/checkstyle.jar")
        xml_out = (
            '<?xml version="1.0"?>'
            "<checkstyle>"
            '<file name="X.java">'
            '<error line="1" column="1" severity="error" message="bad" source="r"/>'
            "</file>"
            "</checkstyle>"
        )
        mock_run.return_value = MagicMock(stdout=xml_out)

        from commit0.harness.lint_java import lint_java_checkstyle

        results = lint_java_checkstyle("/repo")
        assert len(results) == 1
        assert results[0].severity == "error"

    @patch(f"{MODULE}._ensure_checkstyle")
    @patch(f"{MODULE}.subprocess.run")
    def test_no_violations(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
    ) -> None:
        mock_ensure.return_value = Path("/tools/checkstyle.jar")
        mock_run.return_value = MagicMock(
            stdout='<?xml version="1.0"?><checkstyle></checkstyle>'
        )

        from commit0.harness.lint_java import lint_java_checkstyle

        results = lint_java_checkstyle("/repo")
        assert results == []


class TestLintJavaCompilation:
    @patch(f"{MODULE}.resolve_build_cmd", return_value="mvn")
    @patch(f"{MODULE}.subprocess.run")
    def test_maven_compilation(
        self, mock_run: MagicMock, mock_cmd: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        from commit0.harness.lint_java import lint_java_compilation

        result = lint_java_compilation("/repo", "maven")
        assert result is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "mvn"
        assert "compile" in cmd

    @patch(f"{MODULE}.resolve_build_cmd", return_value="gradle")
    @patch(f"{MODULE}.subprocess.run")
    def test_gradle_compilation(
        self, mock_run: MagicMock, mock_cmd: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        from commit0.harness.lint_java import lint_java_compilation

        result = lint_java_compilation("/repo", "gradle")
        assert result is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gradle"
        assert "compileJava" in cmd


class TestEdgeCases:
    @patch(f"{MODULE}.urllib.request.urlretrieve")
    @patch(f"{MODULE}._get_checkstyle_url", return_value="https://example.com/cs.jar")
    def test_sha_mismatch_redownloads(
        self,
        mock_url: MagicMock,
        mock_retrieve: MagicMock,
        tmp_path: Path,
    ) -> None:
        jar_dir = tmp_path / ".commit0" / "tools"
        jar_dir.mkdir(parents=True)
        jar_path = jar_dir / "checkstyle-10.12.5.jar"
        jar_path.write_text("bad jar content")

        def fake_retrieve(url: str, dest: Path) -> None:
            Path(dest).write_text("good jar")

        mock_retrieve.side_effect = fake_retrieve

        with (
            patch(f"{MODULE}.Path.home", return_value=tmp_path),
            patch(
                f"{MODULE}._get_checkstyle_sha256", return_value="expectedhash"
            ),
            patch(
                f"{MODULE}._sha256_file",
                side_effect=["wronghash", "expectedhash"],
            ),
        ):
            from commit0.harness.lint_java import _ensure_checkstyle

            result = _ensure_checkstyle("10.12.5")
        assert result.exists() is False or mock_retrieve.called is True

    def test_missing_attrs_in_xml_default(self) -> None:
        from commit0.harness.lint_java import _parse_checkstyle_xml

        xml = (
            '<?xml version="1.0"?>'
            "<checkstyle>"
            '<file name="F.java">'
            "<error/>"
            "</file>"
            "</checkstyle>"
        )
        results = _parse_checkstyle_xml(xml)
        assert len(results) == 1
        assert results[0].line == 0
        assert results[0].column == 0
        assert results[0].severity == "warning"
        assert results[0].message == ""

    @patch(f"{MODULE}.resolve_build_cmd", return_value="unknown-tool")
    @patch(f"{MODULE}.subprocess.run")
    def test_unknown_build_system_fallback(
        self, mock_run: MagicMock, mock_cmd: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        from commit0.harness.lint_java import lint_java_compilation

        result = lint_java_compilation("/repo", "ant")
        assert result is False
