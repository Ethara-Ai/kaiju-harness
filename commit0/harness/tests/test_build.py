from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

MODULE = "commit0.harness.build"


def _repo_example(repo: str, instance_id: str = "inst/1") -> dict:
    return {
        "repo": f"org/{repo}",
        "instance_id": instance_id,
        "base_commit": "aaa",
        "reference_commit": "bbb",
        "setup": {},
        "test": {},
        "src_dir": "src",
    }


def _swe_example(instance_id: str) -> dict:
    return {
        "instance_id": instance_id,
        "repo": "owner/swe-repo",
        "base_commit": "aaa",
        "reference_commit": "bbb",
        "setup": {},
        "test": {},
        "src_dir": "src",
    }


def _simple_example(instance_id: str) -> dict:
    return {
        "instance_id": instance_id,
        "prompt": "p",
        "canonical_solution": "s",
        "test": "t",
    }


def _run_main(
    dataset_name: str,
    dataset: list,
    split: str = "all",
    dataset_split: str = "test",
    num_workers: int = 1,
    verbose: int = 1,
    build_return: tuple = (["img"], []),
    split_dict: dict | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    spec_sentinel = MagicMock(name="spec")
    mock_client = MagicMock(name="docker_client")

    with (
        patch(
            f"{MODULE}.load_dataset_from_config", return_value=list(dataset)
        ) as m_load,
        patch(f"{MODULE}.make_spec", return_value=spec_sentinel) as m_spec,
        patch(f"{MODULE}.docker"),
        patch(f"{MODULE}.docker_client", return_value=mock_client) as m_docker_client,
        patch(f"{MODULE}.build_repo_images", return_value=build_return) as m_build,
        patch(f"{MODULE}.run_health_checks", return_value=[]),
        patch(f"{MODULE}.sys") as m_sys,
    ):
        if split_dict is not None:
            with patch(f"{MODULE}.SPLIT", split_dict):
                from commit0.harness.build import main

                main(dataset_name, dataset_split, split, num_workers, verbose)
        else:
            from commit0.harness.build import main

            main(dataset_name, dataset_split, split, num_workers, verbose)

        # Position 3 is the docker_client mock — build now uses the context-aware
        # docker_client() (B10), not docker.from_env().
        return m_load, m_spec, m_docker_client, m_build, m_sys


class TestDatasetTypeDetection:
    @pytest.mark.parametrize(
        "dataset_name, expected_type",
        [
            ("swe_bench_lite", "swebench"),
            ("humaneval_python", "simple"),
            ("mbpp_dataset", "simple"),
            ("bigcodebench_v2", "simple"),
            ("codecontests_train", "simple"),
            ("my_custom_dataset", "commit0"),
        ],
        ids=[
            "test_dataset_type_swe",
            "test_dataset_type_humaneval",
            "test_dataset_type_mbpp",
            "test_dataset_type_bigcodebench",
            "test_dataset_type_codecontests",
            "test_dataset_type_commit0",
        ],
    )
    def test_dataset_type_detection(
        self, dataset_name: str, expected_type: str
    ) -> None:
        example = _repo_example("myrepo")
        _, _, _, m_build, _ = _run_main(dataset_name, [example])

        assert m_build.call_count == 1
        actual_type = m_build.call_args[0][2]
        assert actual_type == expected_type


class TestSplitFiltering:
    def test_swe_split_all_includes_everything(self) -> None:
        examples = [_swe_example("swe/1"), _swe_example("swe/2")]
        _, m_spec, _, _, _ = _run_main("swe_bench", examples, split="all")
        assert m_spec.call_count == 2

    def test_swe_split_filters_by_instance_id(self) -> None:
        examples = [_swe_example("swe/match"), _swe_example("swe/other")]
        _, m_spec, _, _, _ = _run_main("swe_bench", examples, split="match")
        assert m_spec.call_count == 1
        assert m_spec.call_args_list[0][0][0]["instance_id"] == "swe/match"

    def test_commit0_split_all_includes_everything(self) -> None:
        examples = [_repo_example("repoA"), _repo_example("repoB")]
        _, m_spec, _, _, _ = _run_main("my_dataset", examples, split="all")
        assert m_spec.call_count == 2

    def test_commit0_split_by_known_split_name(self) -> None:
        examples = [
            _repo_example("joblib"),
            _repo_example("scrapy"),
        ]
        custom_split = {"lite": ["joblib"]}
        _, m_spec, _, _, _ = _run_main(
            "my_dataset", examples, split="lite", split_dict=custom_split
        )
        assert m_spec.call_count == 1
        assert m_spec.call_args_list[0][0][0]["repo"] == "org/joblib"

    def test_commit0_split_unknown_name_filters_by_normalized_match(self) -> None:
        examples = [_repo_example("anyrepo")]
        custom_split = {"lite": ["other"]}
        _, m_spec, _, _, _ = _run_main(
            "my_dataset", examples, split="nonexistent_split", split_dict=custom_split
        )
        assert m_spec.call_count == 0

    def test_commit0_split_unknown_name_matches_with_normalization(self) -> None:
        examples = [_repo_example("my-repo")]
        custom_split = {"lite": ["other"]}
        _, m_spec, _, _, _ = _run_main(
            "my_dataset", examples, split="my_repo", split_dict=custom_split
        )
        assert m_spec.call_count == 1

    def test_simple_split_filters_by_instance_id(self) -> None:
        examples = [_simple_example("humaneval/1"), _simple_example("humaneval/2")]
        _, m_spec, _, _, _ = _run_main("humaneval_py", examples, split="humaneval/1")
        assert m_spec.call_count == 1
        assert m_spec.call_args_list[0][0][0]["instance_id"] == "humaneval/1"


class TestBuildExecution:
    def test_successful_build_no_exit(self) -> None:
        examples = [_repo_example("r")]
        _, _, _, _, m_sys = _run_main(
            "my_dataset", examples, build_return=(["img1"], [])
        )
        m_sys.exit.assert_not_called()

    def test_failed_build_calls_sys_exit_1(self) -> None:
        examples = [_repo_example("r")]
        _, _, _, _, m_sys = _run_main(
            "my_dataset", examples, build_return=([], ["img1"])
        )
        m_sys.exit.assert_called_once_with(1)

    def test_docker_from_env_called(self) -> None:
        examples = [_repo_example("r")]
        _, _, m_docker_client, _, _ = _run_main("my_dataset", examples)
        m_docker_client.assert_called_once()

    def test_make_spec_called_for_each_example(self) -> None:
        examples = [_repo_example("a"), _repo_example("b"), _repo_example("c")]
        _, m_spec, _, _, _ = _run_main("my_dataset", examples, split="all")
        assert m_spec.call_count == 3
        for c in m_spec.call_args_list:
            assert c[1]["absolute"] is True


class TestEdgeCases:
    def test_empty_dataset_no_specs_built(self) -> None:
        _, m_spec, _, m_build, _ = _run_main("my_dataset", [])
        m_spec.assert_not_called()
        assert m_build.call_args[0][1] == []

    def test_dataset_name_lowercased(self) -> None:
        examples = [_swe_example("SWE/1")]
        _, _, _, m_build, _ = _run_main("SWE_Bench_Lite", examples, split="all")
        actual_type = m_build.call_args[0][2]
        assert actual_type == "swebench"
