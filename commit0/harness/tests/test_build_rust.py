"""Exhaustive unit tests for commit0.harness.build_rust."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

MODULE = "commit0.harness.build_rust"

from commit0.harness.build_rust import _load_datasets, RUST_DATASET_GLOB


# ===== RUST_DATASET_GLOB constant =====
class TestRustDatasetGlob:
    def test_value(self):
        assert RUST_DATASET_GLOB == "*_rust_dataset.json"

    def test_is_string(self):
        assert isinstance(RUST_DATASET_GLOB, str)

    def test_ends_with_json(self):
        assert RUST_DATASET_GLOB.endswith(".json")


# ===== _load_datasets =====
class TestLoadDatasetsSingleFile:
    def test_loads_list_json(self, tmp_path):
        data = [{"repo": "Rust-commit0/taffy", "instance_id": "t1"}]
        f = tmp_path / "test_rust_dataset.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert len(result) == 1
        assert result[0]["repo"] == "Rust-commit0/taffy"

    def test_loads_dict_json_converts_to_list(self, tmp_path):
        data = {
            "taffy": {"repo": "Rust-commit0/taffy"},
            "bon": {"repo": "Rust-commit0/bon"},
        }
        f = tmp_path / "test_rust_dataset.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert len(result) == 2
        repos = [r["repo"] for r in result]
        assert "Rust-commit0/taffy" in repos
        assert "Rust-commit0/bon" in repos

    def test_empty_list(self, tmp_path):
        f = tmp_path / "test_rust_dataset.json"
        f.write_text("[]")
        result = _load_datasets(f)
        assert result == []

    def test_empty_dict(self, tmp_path):
        f = tmp_path / "test_rust_dataset.json"
        f.write_text("{}")
        result = _load_datasets(f)
        assert result == []

    def test_single_item_list(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(json.dumps([{"id": 1}]))
        result = _load_datasets(f)
        assert len(result) == 1

    def test_preserves_all_fields(self, tmp_path):
        data = [{"repo": "r", "instance_id": "i", "base_commit": "abc", "extra": True}]
        f = tmp_path / "d.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert result[0]["extra"] is True
        assert result[0]["base_commit"] == "abc"

    def test_large_dataset(self, tmp_path):
        data = [{"repo": f"org/repo{i}"} for i in range(100)]
        f = tmp_path / "big.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert len(result) == 100


class TestLoadDatasetsDirectory:
    def test_loads_from_directory(self, tmp_path):
        data1 = [{"repo": "org/a"}]
        data2 = [{"repo": "org/b"}]
        (tmp_path / "a_rust_dataset.json").write_text(json.dumps(data1))
        (tmp_path / "b_rust_dataset.json").write_text(json.dumps(data2))
        result = _load_datasets(tmp_path)
        assert len(result) == 2
        repos = [r["repo"] for r in result]
        assert "org/a" in repos
        assert "org/b" in repos

    def test_empty_directory_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _load_datasets(tmp_path)

    def test_ignores_non_matching_files(self, tmp_path):
        (tmp_path / "a_rust_dataset.json").write_text('[{"repo":"org/a"}]')
        (tmp_path / "other.json").write_text('[{"repo":"org/b"}]')
        result = _load_datasets(tmp_path)
        assert len(result) == 1
        assert result[0]["repo"] == "org/a"

    def test_merges_multiple_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"set{i}_rust_dataset.json").write_text(
                json.dumps([{"repo": f"org/repo{i}"}])
            )
        result = _load_datasets(tmp_path)
        assert len(result) == 5

    def test_dict_files_in_directory(self, tmp_path):
        (tmp_path / "x_rust_dataset.json").write_text(
            json.dumps({"k1": {"repo": "org/a"}, "k2": {"repo": "org/b"}})
        )
        result = _load_datasets(tmp_path)
        assert len(result) == 2

    def test_mixed_list_and_dict_files(self, tmp_path):
        (tmp_path / "a_rust_dataset.json").write_text(json.dumps([{"repo": "org/a"}]))
        (tmp_path / "b_rust_dataset.json").write_text(
            json.dumps({"k": {"repo": "org/b"}})
        )
        result = _load_datasets(tmp_path)
        assert len(result) == 2


class TestLoadDatasetsNonexistent:
    def test_nonexistent_path_exits(self):
        with pytest.raises(SystemExit):
            _load_datasets(Path("/nonexistent/path/to/file.json"))

    def test_nonexistent_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _load_datasets(tmp_path / "missing.json")


class TestLoadDatasetsEdgeCases:
    def test_nested_data_preserved(self, tmp_path):
        data = [{"repo": "r", "setup": {"pre_install": ["apt-get update"]}}]
        f = tmp_path / "d.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert result[0]["setup"]["pre_install"] == ["apt-get update"]

    def test_unicode_content(self, tmp_path):
        data = [{"repo": "org/repo", "desc": "unicode: \u00e9\u00e8\u00ea"}]
        f = tmp_path / "d.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert "\u00e9" in result[0]["desc"]

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        with pytest.raises(json.JSONDecodeError):
            _load_datasets(f)

    def test_sorted_file_order_in_directory(self, tmp_path):
        (tmp_path / "z_rust_dataset.json").write_text(json.dumps([{"order": 2}]))
        (tmp_path / "a_rust_dataset.json").write_text(json.dumps([{"order": 1}]))
        result = _load_datasets(tmp_path)
        assert result[0]["order"] == 1
        assert result[1]["order"] == 2


# ===== main =====
from commit0.harness.build_rust import main


class TestMain:
    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_success(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["img1"], [])
        main("/path/to/data.json")
        mock_docker.assert_called_once()
        mock_build.assert_called_once()

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_failed_builds_exit(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = ([], ["org/a"])
        with pytest.raises(SystemExit):
            main("/path/to/data.json")

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_passes_num_workers(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["img"], [])
        main("/path/to/data.json", num_workers=8)
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["max_workers"] == 8

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_passes_verbose(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["img"], [])
        main("/path/to/data.json", verbose=2)
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["verbose"] == 2

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_default_workers(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["img"], [])
        main("/path/to/data.json")
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["max_workers"] == 4

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_empty_dataset(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = []
        mock_build.return_value = ([], [])
        main("/path/to/data.json")
        mock_build.assert_called_once()

    @patch(f"{MODULE}.docker.from_env", side_effect=Exception("Docker not running"))
    @patch(f"{MODULE}._load_datasets")
    def test_docker_not_available(self, mock_load, mock_docker):
        mock_load.return_value = [{"repo": "org/a"}]
        with pytest.raises(Exception, match="Docker not running"):
            main("/path/to/data.json")

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_partial_failure(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}, {"repo": "org/b"}]
        mock_build.return_value = (["img_a"], ["org/b"])
        with pytest.raises(SystemExit):
            main("/path")


# ===== _load_datasets edge-cases =====
class TestLoadDatasetsEdgeCasesExtra:
    def test_dict_json_converted_to_list(self, tmp_path):
        data = {"a": {"repo": "org/a"}, "b": {"repo": "org/b"}}
        f = tmp_path / "test_rust_dataset.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert len(result) == 2

    def test_empty_list_json(self, tmp_path):
        f = tmp_path / "test_rust_dataset.json"
        f.write_text("[]")
        result = _load_datasets(f)
        assert result == []

    def test_empty_dict_json(self, tmp_path):
        f = tmp_path / "test_rust_dataset.json"
        f.write_text("{}")
        result = _load_datasets(f)
        assert result == []

    def test_directory_multiple_files_merged(self, tmp_path):
        d1 = [{"repo": "org/a"}]
        d2 = [{"repo": "org/b"}, {"repo": "org/c"}]
        (tmp_path / "a_rust_dataset.json").write_text(json.dumps(d1))
        (tmp_path / "b_rust_dataset.json").write_text(json.dumps(d2))
        result = _load_datasets(tmp_path)
        assert len(result) == 3

    def test_directory_no_matching_files_exits(self, tmp_path):
        (tmp_path / "not_matching.json").write_text("[]")
        with pytest.raises(SystemExit):
            _load_datasets(tmp_path)

    def test_nonexistent_path_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _load_datasets(tmp_path / "nope")

    def test_directory_sorted_glob_order(self, tmp_path):
        (tmp_path / "z_rust_dataset.json").write_text(json.dumps([{"repo": "z"}]))
        (tmp_path / "a_rust_dataset.json").write_text(json.dumps([{"repo": "a"}]))
        result = _load_datasets(tmp_path)
        assert result[0]["repo"] == "a"
        assert result[1]["repo"] == "z"

    def test_single_element_list(self, tmp_path):
        f = tmp_path / "test_rust_dataset.json"
        f.write_text(json.dumps([{"repo": "org/only"}]))
        result = _load_datasets(f)
        assert len(result) == 1
        assert result[0]["repo"] == "org/only"


# ===== main() edge-cases =====
class TestMainEdge:
    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_num_workers_passed(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["a"], [])
        from commit0.harness.build_rust import main

        main("/path/to/data.json", num_workers=8, verbose=2)
        _, kwargs = mock_build.call_args
        assert kwargs["max_workers"] == 8
        assert kwargs["verbose"] == 2

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_default_workers_is_four(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["a"], [])
        from commit0.harness.build_rust import main

        main("/path/to/data.json")
        _, kwargs = mock_build.call_args
        assert kwargs["max_workers"] == 4

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_all_success_no_exit(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["a"], [])
        from commit0.harness.build_rust import main

        main("/path")

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_client_from_env_called(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["a"], [])
        from commit0.harness.build_rust import main

        main("/path")
        mock_docker.assert_called_once()

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_client_passed_to_build(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["a"], [])
        fake_client = MagicMock()
        mock_docker.return_value = fake_client
        from commit0.harness.build_rust import main

        main("/path")
        assert mock_build.call_args[0][0] is fake_client

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_dataset_passed_to_build(self, mock_load, mock_docker, mock_build):
        instances = [{"repo": "org/a"}, {"repo": "org/b"}]
        mock_load.return_value = instances
        mock_build.return_value = (["a", "b"], [])
        from commit0.harness.build_rust import main

        main("/path")
        assert mock_build.call_args[0][1] is instances

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_logs_instance_count(self, mock_load, mock_docker, mock_build, caplog):
        import logging

        mock_load.return_value = [{"repo": "org/a"}, {"repo": "org/b"}]
        mock_build.return_value = (["a", "b"], [])
        from commit0.harness.build_rust import main

        with caplog.at_level(logging.INFO):
            main("/path")
        assert any("2" in r.message and "instance" in r.message for r in caplog.records)

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_logs_success_count(self, mock_load, mock_docker, mock_build, caplog):
        import logging

        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = (["img_a"], [])
        from commit0.harness.build_rust import main

        with caplog.at_level(logging.INFO):
            main("/path")
        assert any(
            "1" in r.message and "success" in r.message.lower() for r in caplog.records
        )

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_failure_logs_error(self, mock_load, mock_docker, mock_build, caplog):
        import logging

        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = ([], ["org/a"])
        from commit0.harness.build_rust import main

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit):
                main("/path")
        assert any(
            "Failed" in r.message or "failed" in r.message for r in caplog.records
        )

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_exit_code_one_on_failure(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = [{"repo": "org/a"}]
        mock_build.return_value = ([], ["org/a"])
        from commit0.harness.build_rust import main

        with pytest.raises(SystemExit) as exc:
            main("/path")
        assert exc.value.code == 1

    @patch(f"{MODULE}.build_rust_repo_images")
    @patch(f"{MODULE}.docker.from_env")
    @patch(f"{MODULE}._load_datasets")
    def test_path_object_created(self, mock_load, mock_docker, mock_build):
        mock_load.return_value = []
        mock_build.return_value = ([], [])
        from commit0.harness.build_rust import main

        main("/some/path.json")
        load_arg = mock_load.call_args[0][0]
        assert isinstance(load_arg, Path)
