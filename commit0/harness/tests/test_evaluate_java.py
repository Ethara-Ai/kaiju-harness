from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.constants import TestStatus
from commit0.harness.java_test_parser import JavaTestResult

MODULE = "commit0.harness.evaluate_java"


def _instance(repo: str = "org/myrepo") -> dict:
    return {
        "repo": repo,
        "instance_id": repo,
        "base_commit": "aaa" + "0" * 37,
        "reference_commit": "bbb" + "0" * 37,
        "setup": {},
        "test": {},
        "src_dir": "src",
    }


class TestJavaToTestStatus:
    def test_passed_maps(self) -> None:
        from commit0.harness.evaluate_java import _java_to_test_status

        assert _java_to_test_status(JavaTestResult.PASSED) is TestStatus.PASSED

    def test_failed_maps(self) -> None:
        from commit0.harness.evaluate_java import _java_to_test_status

        assert _java_to_test_status(JavaTestResult.FAILED) is TestStatus.FAILED

    def test_error_maps_to_error(self) -> None:
        from commit0.harness.evaluate_java import _java_to_test_status

        assert _java_to_test_status(JavaTestResult.ERROR) is TestStatus.FAILED

    def test_skipped_maps(self) -> None:
        from commit0.harness.evaluate_java import _java_to_test_status

        assert _java_to_test_status(JavaTestResult.SKIPPED) is TestStatus.SKIPPED


class TestEvaluateJavaRepo:
    @patch(f"{MODULE}.parse_surefire_reports")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.setup_logger", return_value=MagicMock())
    @patch(f"{MODULE}.make_java_spec")
    def test_successful_with_reports(
        self,
        mock_spec: MagicMock,
        mock_setup_log: MagicMock,
        mock_docker_cls: MagicMock,
        mock_parse: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = MagicMock()
        spec.make_eval_script_list.return_value = ["echo test"]
        spec._get_report_dir.return_value = "surefire-reports"
        mock_spec.return_value = spec

        ctx = MagicMock()
        ctx.exec_run_with_timeout.return_value = ("output", False, 10.0)
        mock_docker_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_docker_cls.return_value.__exit__ = MagicMock(return_value=False)

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("diff content")

        exit_file = tmp_path / "test_exit_code.txt"
        exit_file.write_text("0")

        report_dir = tmp_path / "surefire-reports"
        report_dir.mkdir()

        mock_parse.return_value = {"com.Test#method": JavaTestResult.PASSED}

        from commit0.harness.evaluate_java import evaluate_java_repo

        result = evaluate_java_repo(
            instance=_instance(),
            patch_path=str(patch_file),
            log_dir=str(tmp_path),
        )
        assert "com.Test#method" in result
        assert result["com.Test#method"] is TestStatus.PASSED

    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.setup_logger", return_value=MagicMock())
    @patch(f"{MODULE}.make_java_spec")
    def test_compilation_failed_detection(
        self,
        mock_spec: MagicMock,
        mock_setup_log: MagicMock,
        mock_docker_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = MagicMock()
        spec.make_eval_script_list.return_value = []
        spec._get_report_dir.return_value = "surefire-reports"
        mock_spec.return_value = spec

        ctx = MagicMock()
        ctx.exec_run_with_timeout.return_value = ("output", False, 5.0)
        mock_docker_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_docker_cls.return_value.__exit__ = MagicMock(return_value=False)

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("diff")

        exit_file = tmp_path / "test_exit_code.txt"
        exit_file.write_text("COMPILATION_FAILED")

        from commit0.harness.evaluate_java import evaluate_java_repo

        result = evaluate_java_repo(
            instance=_instance(),
            patch_path=str(patch_file),
            log_dir=str(tmp_path),
        )
        assert result == {"COMPILATION": TestStatus.FAILED}

    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.setup_logger", return_value=MagicMock())
    @patch(f"{MODULE}.make_java_spec")
    def test_timeout_handling(
        self,
        mock_spec: MagicMock,
        mock_setup_log: MagicMock,
        mock_docker_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = MagicMock()
        spec.make_eval_script_list.return_value = []
        spec._get_report_dir.return_value = "surefire-reports"
        mock_spec.return_value = spec

        ctx = MagicMock()
        ctx.exec_run_with_timeout.return_value = ("", True, 600.0)
        mock_docker_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_docker_cls.return_value.__exit__ = MagicMock(return_value=False)

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("diff")

        from commit0.harness.evaluate_java import evaluate_java_repo

        result = evaluate_java_repo(
            instance=_instance(),
            patch_path=str(patch_file),
            log_dir=str(tmp_path),
        )
        assert result == {"TIMEOUT": TestStatus.ERROR}

    def test_patch_not_found(self, tmp_path: Path) -> None:
        from commit0.harness.evaluate_java import evaluate_java_repo

        with pytest.raises(FileNotFoundError, match="Patch file not found"):
            evaluate_java_repo(
                instance=_instance(),
                patch_path=str(tmp_path / "nonexistent.diff"),
                log_dir=str(tmp_path),
            )

    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.setup_logger", return_value=MagicMock())
    @patch(f"{MODULE}.make_java_spec")
    def test_no_reports_returns_sentinel(
        self,
        mock_spec: MagicMock,
        mock_setup_log: MagicMock,
        mock_docker_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = MagicMock()
        spec.make_eval_script_list.return_value = []
        spec._get_report_dir.return_value = "surefire-reports"
        mock_spec.return_value = spec

        ctx = MagicMock()
        ctx.exec_run_with_timeout.return_value = ("output", False, 5.0)
        mock_docker_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_docker_cls.return_value.__exit__ = MagicMock(return_value=False)

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("diff")

        from commit0.harness.evaluate_java import evaluate_java_repo

        result = evaluate_java_repo(
            instance=_instance(),
            patch_path=str(patch_file),
            log_dir=str(tmp_path),
        )
        assert result == {}


class TestEdgeCases:
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.setup_logger", return_value=MagicMock())
    @patch(f"{MODULE}.make_java_spec")
    def test_docker_error_propagates(
        self,
        mock_spec: MagicMock,
        mock_setup_log: MagicMock,
        mock_docker_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = MagicMock()
        spec.make_eval_script_list.return_value = []
        spec._get_report_dir.return_value = "surefire-reports"
        mock_spec.return_value = spec

        mock_docker_cls.return_value.__enter__ = MagicMock(
            side_effect=RuntimeError("docker boom")
        )
        mock_docker_cls.return_value.__exit__ = MagicMock(return_value=False)

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("diff")

        from commit0.harness.evaluate_java import evaluate_java_repo

        with pytest.raises(RuntimeError, match="docker boom"):
            evaluate_java_repo(
                instance=_instance(),
                patch_path=str(patch_file),
                log_dir=str(tmp_path),
            )

    @patch(f"{MODULE}.parse_surefire_reports")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.setup_logger", return_value=MagicMock())
    @patch(f"{MODULE}.make_java_spec")
    def test_empty_test_ids_still_runs(
        self,
        mock_spec: MagicMock,
        mock_setup_log: MagicMock,
        mock_docker_cls: MagicMock,
        mock_parse: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = MagicMock()
        spec.make_eval_script_list.return_value = ["echo run"]
        spec._get_report_dir.return_value = "surefire-reports"
        mock_spec.return_value = spec

        ctx = MagicMock()
        ctx.exec_run_with_timeout.return_value = ("output", False, 5.0)
        mock_docker_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_docker_cls.return_value.__exit__ = MagicMock(return_value=False)

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("diff")

        report_dir = tmp_path / "surefire-reports"
        report_dir.mkdir()
        mock_parse.return_value = {}

        from commit0.harness.evaluate_java import evaluate_java_repo

        result = evaluate_java_repo(
            instance=_instance(),
            patch_path=str(patch_file),
            test_ids=[],
            log_dir=str(tmp_path),
        )
        mock_spec.assert_called_once()
        assert result == {}
