from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

MODULE = "commit0.harness.build_ts"


def _ts_example(repo: str = "myrepo", instance_id: str = "inst/1") -> dict:
    return {
        "repo": f"org/{repo}",
        "instance_id": instance_id,
        "base_commit": "aaa",
        "reference_commit": "bbb",
        "setup": {"node": "20", "install": "npm install"},
        "test": {"test_cmd": "npx jest"},
        "src_dir": "src",
    }


def _run_main(
    dataset: list,
    split: str = "all",
    dataset_name: str = "ts_dataset.json",
    dataset_split: str = "test",
    num_workers: int = 1,
    verbose: int = 1,
    build_return: tuple = (["img"], []),
    health_return: list | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    if health_return is None:
        health_return = []

    mock_client = MagicMock(name="docker_client")
    spec_sentinel = MagicMock(name="spec")
    spec_sentinel.repo_image_key = "commit0.repo.test.abc:v0"
    spec_sentinel._get_setup_dict.return_value = {"node": "20", "packages": []}

    with (
        patch(
            f"{MODULE}.load_dataset_from_config", return_value=iter(dataset)
        ) as m_load,
        patch(f"{MODULE}.make_ts_spec", return_value=spec_sentinel) as m_spec,
        patch("docker.from_env", return_value=mock_client),
        patch(f"{MODULE}.build_repo_images", return_value=build_return) as m_build,
        patch(f"{MODULE}.run_ts_health_checks", return_value=health_return) as m_health,
        patch(f"{MODULE}.sys") as m_sys,
    ):
        from commit0.harness.build_ts import main

        main(dataset_name, dataset_split, split, num_workers, verbose)
        return m_load, m_spec, m_build, m_health, m_sys


class TestFilterBySplit:
    def test_all_includes_everything(self) -> None:
        examples = [_ts_example("repoA"), _ts_example("repoB")]
        _, m_spec, _, _, _ = _run_main(examples, split="all")
        assert m_spec.call_count == 2

    def test_all_ts_includes_everything(self) -> None:
        examples = [_ts_example("repoA"), _ts_example("repoB")]
        _, m_spec, _, _, _ = _run_main(examples, split="all_ts")
        assert m_spec.call_count == 2

    def test_unknown_split_matches_by_normalization(self) -> None:
        examples = [_ts_example("my-repo")]
        _, m_spec, _, _, _ = _run_main(examples, split="my_repo")
        assert m_spec.call_count == 1

    def test_unknown_split_no_match_skips(self) -> None:
        examples = [_ts_example("other-repo")]
        _, m_spec, _, _, _ = _run_main(examples, split="nonexistent")
        assert m_spec.call_count == 0

    def test_empty_split_repos_means_all(self) -> None:
        examples = [_ts_example("anything")]
        with patch(f"{MODULE}.TS_SPLIT", {"custom": []}):
            _, m_spec, _, _, _ = _run_main(examples, split="custom")
            assert m_spec.call_count == 1


class TestBuildExecution:
    def test_successful_build_no_exit(self) -> None:
        examples = [_ts_example()]
        _, _, _, _, m_sys = _run_main(examples, build_return=(["img1"], []))
        m_sys.exit.assert_not_called()

    def test_failed_build_calls_sys_exit_1(self) -> None:
        examples = [_ts_example()]
        _, _, _, _, m_sys = _run_main(examples, build_return=([], ["img1"]))
        m_sys.exit.assert_called_once_with(1)

    def test_docker_from_env_called(self) -> None:
        examples = [_ts_example()]
        with (
            patch(f"{MODULE}.load_dataset_from_config", return_value=iter(examples)),
            patch(f"{MODULE}.make_ts_spec", return_value=MagicMock()),
            patch("docker.from_env", return_value=MagicMock()) as m_docker,
            patch(f"{MODULE}.build_repo_images", return_value=(["img"], [])),
            patch(f"{MODULE}.run_ts_health_checks", return_value=[]),
            patch(f"{MODULE}.sys"),
        ):
            from commit0.harness.build_ts import main

            main("ds", "test", "all", 1, 1)
            m_docker.assert_called_once()

    def test_build_repo_images_called_with_specs(self) -> None:
        examples = [_ts_example("a"), _ts_example("b")]
        _, _, m_build, _, _ = _run_main(examples)
        assert m_build.call_count == 1
        specs_arg = m_build.call_args[0][1]
        assert len(specs_arg) == 2


class TestHealthChecks:
    def test_health_checks_run_for_successful_images(self) -> None:
        examples = [_ts_example()]
        _, _, _, m_health, _ = _run_main(
            examples,
            build_return=(["commit0.repo.test.abc:v0"], []),
            health_return=[(True, "node_modules", "42 packages")],
        )
        assert m_health.call_count == 1

    def test_health_checks_skipped_for_failed_images(self) -> None:
        spec_sentinel = MagicMock(name="spec")
        spec_sentinel.repo_image_key = "commit0.repo.test.abc:v0"
        spec_sentinel._get_setup_dict.return_value = {"node": "20"}
        with (
            patch(
                f"{MODULE}.load_dataset_from_config", return_value=iter([_ts_example()])
            ),
            patch(f"{MODULE}.make_ts_spec", return_value=spec_sentinel),
            patch("docker.from_env", return_value=MagicMock()),
            patch(
                f"{MODULE}.build_repo_images",
                return_value=([], ["commit0.repo.test.abc:v0"]),
            ),
            patch(f"{MODULE}.run_ts_health_checks") as m_health,
            patch(f"{MODULE}.sys"),
        ):
            from commit0.harness.build_ts import main

            main("ds", "test", "all", 1, 1)
            m_health.assert_not_called()


class TestEdgeCases:
    def test_empty_dataset(self) -> None:
        _, m_spec, m_build, _, m_sys = _run_main([])
        m_spec.assert_not_called()
        m_build.assert_not_called()
        m_sys.exit.assert_not_called()

    def test_make_ts_spec_called_with_absolute_true(self) -> None:
        examples = [_ts_example()]
        _, m_spec, _, _, _ = _run_main(examples)
        assert m_spec.call_args[1]["absolute"] is True


class TestFilterBySplitAllTsWithSplitRepos:
    """all_ts split with non-empty split_repos list still returns True."""

    def test_all_ts_split_with_nonempty_ts_split(self) -> None:
        with patch(f"{MODULE}.TS_SPLIT", {"all_ts": ["repoA", "repoB"]}):
            examples = [_ts_example("repoA"), _ts_example("repoC")]
            _, m_spec, _, _, _ = _run_main(examples, split="all_ts")
            assert m_spec.call_count == 2

    def test_filter_by_split_direct_all_ts(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        assert _filter_by_split({"repo": "org/whatever"}, "all_ts") is True

    def test_filter_by_split_direct_all(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        assert _filter_by_split({"repo": "org/whatever"}, "all") is True


class TestFilterBySplitNamedSplitRepos:
    """_filter_by_split with specific named split_repos (line 33)."""

    def test_named_split_includes_matching_repo(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        with patch(f"{MODULE}.TS_SPLIT", {"my_split": ["repoA", "repoB"]}):
            assert _filter_by_split({"repo": "org/repoA"}, "my_split") is True

    def test_named_split_excludes_non_matching_repo(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        with patch(f"{MODULE}.TS_SPLIT", {"my_split": ["repoA", "repoB"]}):
            assert _filter_by_split({"repo": "org/repoC"}, "my_split") is False

    def test_named_split_empty_list_means_all(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        with patch(f"{MODULE}.TS_SPLIT", {"empty_split": []}):
            assert _filter_by_split({"repo": "org/anything"}, "empty_split") is True

    def test_filter_non_dict_example_with_repo_attr(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        class FakeInstance:
            repo = "org/repoA"

        with patch(f"{MODULE}.TS_SPLIT", {"s": ["repoA"]}):
            assert _filter_by_split(FakeInstance(), "s") is True

    def test_filter_non_dict_example_not_matching(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        class FakeInstance:
            repo = "org/repoX"

        with patch(f"{MODULE}.TS_SPLIT", {"s": ["repoA"]}):
            assert _filter_by_split(FakeInstance(), "s") is False

    def test_fallthrough_normalization_with_non_dict(self) -> None:
        from commit0.harness.build_ts import _filter_by_split

        class FakeInstance:
            repo = "org/my-repo"

        assert _filter_by_split(FakeInstance(), "my_repo") is True


class TestClientClose:
    """Cover line 80/84: client.close() in finally block."""

    def test_client_close_called_on_success(self) -> None:
        mock_client = MagicMock(name="docker_client")
        spec_sentinel = MagicMock(name="spec")
        spec_sentinel.repo_image_key = "commit0.repo.test.abc:v0"
        spec_sentinel._get_setup_dict.return_value = {"node": "20", "packages": []}

        with (
            patch(
                f"{MODULE}.load_dataset_from_config", return_value=iter([_ts_example()])
            ),
            patch(f"{MODULE}.make_ts_spec", return_value=spec_sentinel),
            patch("docker.from_env", return_value=mock_client),
            patch(f"{MODULE}.build_repo_images", return_value=(["img"], [])),
            patch(f"{MODULE}.run_ts_health_checks", return_value=[]),
            patch(f"{MODULE}.sys"),
        ):
            from commit0.harness.build_ts import main

            main("ds", "test", "all", 1, 1)

        mock_client.close.assert_called_once()

    def test_client_close_called_on_build_failure(self) -> None:
        mock_client = MagicMock(name="docker_client")
        spec_sentinel = MagicMock(name="spec")
        spec_sentinel.repo_image_key = "commit0.repo.test.abc:v0"
        spec_sentinel._get_setup_dict.return_value = {"node": "20"}

        with (
            patch(
                f"{MODULE}.load_dataset_from_config", return_value=iter([_ts_example()])
            ),
            patch(f"{MODULE}.make_ts_spec", return_value=spec_sentinel),
            patch("docker.from_env", return_value=mock_client),
            patch(
                f"{MODULE}.build_repo_images",
                return_value=([], ["commit0.repo.test.abc:v0"]),
            ),
            patch(f"{MODULE}.run_ts_health_checks", return_value=[]),
            patch(f"{MODULE}.sys"),
        ):
            from commit0.harness.build_ts import main

            main("ds", "test", "all", 1, 1)

        mock_client.close.assert_called_once()

    def test_client_close_called_on_exception(self) -> None:
        mock_client = MagicMock(name="docker_client")
        spec_sentinel = MagicMock(name="spec")
        spec_sentinel.repo_image_key = "commit0.repo.test.abc:v0"
        spec_sentinel._get_setup_dict.return_value = {"node": "20"}

        with (
            patch(
                f"{MODULE}.load_dataset_from_config", return_value=iter([_ts_example()])
            ),
            patch(f"{MODULE}.make_ts_spec", return_value=spec_sentinel),
            patch("docker.from_env", return_value=mock_client),
            patch(f"{MODULE}.build_repo_images", side_effect=RuntimeError("boom")),
            patch(f"{MODULE}.run_ts_health_checks", return_value=[]),
            patch(f"{MODULE}.sys"),
        ):
            from commit0.harness.build_ts import main

            with pytest.raises(RuntimeError, match="boom"):
                main("ds", "test", "all", 1, 1)

        mock_client.close.assert_called_once()


class TestHealthCheckLogging:
    """Cover health check pass/fail logging branches."""

    def test_health_check_failure_logged(self) -> None:
        examples = [_ts_example()]
        _, _, _, m_health, _ = _run_main(
            examples,
            build_return=(["commit0.repo.test.abc:v0"], []),
            health_return=[(False, "node_check", "node not found")],
        )
        assert m_health.call_count == 1

    def test_health_check_mixed_results(self) -> None:
        examples = [_ts_example()]
        _, _, _, m_health, _ = _run_main(
            examples,
            build_return=(["commit0.repo.test.abc:v0"], []),
            health_return=[
                (True, "node_modules", "42 packages"),
                (False, "typescript", "tsc not found"),
            ],
        )
        assert m_health.call_count == 1
