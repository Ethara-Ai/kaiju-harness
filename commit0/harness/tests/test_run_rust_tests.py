from __future__ import annotations

import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

MODULE = "commit0.harness.run_rust_tests"


def _example(repo="Rust-commit0/taffy", base_commit="aaa111", ref_commit="bbb222"):
    return {
        "repo": repo,
        "base_commit": base_commit,
        "reference_commit": ref_commit,
        "instance_id": repo,
        "setup": {"install": "cargo build"},
        "test": {"test_cmd": "cargo test"},
        "src_dir": "src",
    }


def _spec():
    s = MagicMock()
    s.eval_script = "#!/bin/bash\ncargo test {test_ids}"
    return s


def _repo(branch_name="main", hexsha="cafe1234", has_branch=True):
    r = MagicMock()
    if has_branch:
        r.branches.__contains__ = lambda self, b: b == branch_name
    else:
        r.branches.__contains__ = lambda self, b: False
    c = MagicMock()
    c.hexsha = hexsha
    r.commit.return_value = c
    r.remotes = []
    return r


def _ctx_ok():
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.return_value = ("output", False, 10.0)
    return ctx


def _apply_patches(
    stack,
    tmp_path,
    dataset=None,
    spec_val=None,
    repo_val=None,
    ctx_val=None,
    hash_val="hashval123",
):
    ex = _example()
    if dataset is None:
        dataset = [ex]
    if spec_val is None:
        spec_val = _spec()
    if repo_val is None:
        repo_val = _repo()
    if ctx_val is None:
        ctx_val = _ctx_ok()

    stack.enter_context(
        patch(f"{MODULE}.load_dataset_from_config", return_value=list(dataset))
    )
    stack.enter_context(patch(f"{MODULE}.make_rust_spec", return_value=spec_val))
    stack.enter_context(patch(f"{MODULE}.get_hash_string", return_value=hash_val))
    stack.enter_context(
        patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
    )
    stack.enter_context(patch(f"{MODULE}.setup_logger", return_value=MagicMock()))
    stack.enter_context(patch(f"{MODULE}.close_logger"))
    stack.enter_context(patch(f"{MODULE}.RUN_RUST_TESTS_LOG_DIR", tmp_path))
    stack.enter_context(patch(f"{MODULE}.git.Repo", return_value=repo_val))
    stack.enter_context(patch(f"{MODULE}.Docker", return_value=ctx_val))
    stack.enter_context(patch(f"{MODULE}.Modal", return_value=ctx_val))
    stack.enter_context(patch(f"{MODULE}.E2B", return_value=ctx_val))
    return spec_val, repo_val, ctx_val


def _prep_log_dir(
    tmp_path,
    repo_name="taffy",
    branch="reference",
    hash_val="hashval123",
    exit_code="0",
):
    log_dir = tmp_path / repo_name / branch / hash_val
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "cargo_test_exit_code.txt").write_text(exit_code)
    (log_dir / "test_output.txt").write_text("all tests passed")
    return log_dir


def _call_main(
    repo_or_repo_dir="taffy",
    branch="reference",
    test_ids="test_a",
    backend="local",
    timeout=300,
    num_cpus=1,
    rebuild_image=False,
    verbose=0,
):
    from commit0.harness.run_rust_tests import main

    main(
        dataset_name="ds",
        dataset_split="test",
        base_dir="/base",
        repo_or_repo_dir=repo_or_repo_dir,
        branch=branch,
        test_ids=test_ids,
        backend=backend,
        timeout=timeout,
        num_cpus=num_cpus,
        rebuild_image=rebuild_image,
        verbose=verbose,
    )


def test_no_matching_repo_raises(tmp_path):
    ex = _example(repo="Rust-commit0/other")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        with pytest.raises(ValueError, match="No matching Rust repo found"):
            _call_main(repo_or_repo_dir="taffy")


def test_empty_dataset_raises(tmp_path):
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[])
        with pytest.raises(ValueError, match="No matching Rust repo found"):
            _call_main()


def test_match_by_basename(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="taffy")
        assert exc.value.code == 0


def test_match_by_endswith(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="/some/path/taffy")
        assert exc.value.code == 0


def test_match_with_trailing_slash(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="taffy/")
        assert exc.value.code == 0


def test_match_with_trailing_slash_long_path(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="/repos/taffy/")
        assert exc.value.code == 0


def test_no_match_different_repo_name(tmp_path):
    ex = _example(repo="Rust-commit0/bon")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        with pytest.raises(ValueError, match="No matching Rust repo found"):
            _call_main(repo_or_repo_dir="taffy")


def test_match_among_multiple_datasets(tmp_path):
    _prep_log_dir(tmp_path)
    ds = [_example(repo="Rust-commit0/bon"), _example(repo="Rust-commit0/taffy")]
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=ds)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="taffy")
        assert exc.value.code == 0


def test_match_first_entry_in_multiple(tmp_path):
    _prep_log_dir(tmp_path, repo_name="bon")
    ds = [_example(repo="Rust-commit0/bon"), _example(repo="Rust-commit0/taffy")]
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=ds)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="bon")
        assert exc.value.code == 0


def test_basename_contains_repo_name(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main(repo_or_repo_dir="my-taffy-fork")
        assert exc.value.code == 0


def test_log_dir_created(tmp_path):
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        log_dir = tmp_path / "taffy" / "reference" / "hashval123"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "cargo_test_exit_code.txt").write_text("0")
        (log_dir / "test_output.txt").write_text("ok")
        with pytest.raises(SystemExit):
            _call_main()
        assert log_dir.exists()


def test_log_dir_uses_hashed_test_ids(tmp_path):
    with ExitStack() as s:
        _apply_patches(s, tmp_path, hash_val="customhash")
        log_dir = tmp_path / "taffy" / "reference" / "customhash"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "cargo_test_exit_code.txt").write_text("0")
        (log_dir / "test_output.txt").write_text("ok")
        with pytest.raises(SystemExit):
            _call_main()
        assert log_dir.exists()


def test_log_dir_uses_branch_name(tmp_path):
    repo_mock = _repo(branch_name="dev", hexsha="deadbeef")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        log_dir = tmp_path / "taffy" / "dev" / "hashval123"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "cargo_test_exit_code.txt").write_text("0")
        (log_dir / "test_output.txt").write_text("ok")
        with pytest.raises(SystemExit):
            _call_main(branch="dev")
        assert log_dir.exists()


def test_git_repo_loaded_from_repo_or_repo_dir(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        spec, repo_val, ctx = _apply_patches(s, tmp_path)
        git_repo_mock = s.enter_context(
            patch(f"{MODULE}.git.Repo", return_value=repo_val)
        )
        with pytest.raises(SystemExit):
            _call_main(repo_or_repo_dir="taffy")
        git_repo_mock.assert_called_with("taffy")


def test_git_repo_fallback_to_base_dir(tmp_path):
    import git as real_git

    _prep_log_dir(tmp_path)
    repo_mock = _repo()
    side_effects = [real_git.exc.NoSuchPathError("nope"), repo_mock]
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        s.enter_context(patch(f"{MODULE}.git.Repo", side_effect=side_effects))
        with pytest.raises(SystemExit):
            _call_main(repo_or_repo_dir="taffy")


def test_git_repo_fallback_invalid_repo(tmp_path):
    import git as real_git

    _prep_log_dir(tmp_path)
    repo_mock = _repo()
    side_effects = [real_git.exc.InvalidGitRepositoryError("bad"), repo_mock]
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        s.enter_context(patch(f"{MODULE}.git.Repo", side_effect=side_effects))
        with pytest.raises(SystemExit):
            _call_main(repo_or_repo_dir="taffy")


def test_git_repo_both_fail_raises(tmp_path):
    import git as real_git

    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(
            patch(
                f"{MODULE}.git.Repo",
                side_effect=[
                    real_git.exc.NoSuchPathError("nope"),
                    real_git.exc.NoSuchPathError("nope2"),
                ],
            )
        )
        with pytest.raises(Exception, match="are not git directories"):
            _call_main(repo_or_repo_dir="taffy")


def test_branch_reference_uses_reference_commit(tmp_path):
    _prep_log_dir(tmp_path)
    ex = _example(ref_commit="ref_sha_abc")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        gen_patch = s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
        )
        with pytest.raises(SystemExit):
            _call_main(branch="reference")
        gen_patch.assert_called_once()
        assert gen_patch.call_args[0][2] == "ref_sha_abc"


def test_branch_local_uses_hexsha(tmp_path):
    repo_mock = _repo(branch_name="dev", hexsha="localsha999")
    _prep_log_dir(tmp_path, branch="dev")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        gen_patch = s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
        )
        with pytest.raises(SystemExit):
            _call_main(branch="dev")
        assert gen_patch.call_args[0][2] == "localsha999"


def test_branch_remote_found(tmp_path):
    repo_mock = _repo(has_branch=False)
    ref_mock = MagicMock()
    ref_mock.remote_head = "feature"
    ref_mock.name = "origin/feature"
    remote = MagicMock()
    remote.refs = [ref_mock]
    repo_mock.remotes = [remote]
    commit_obj = MagicMock()
    commit_obj.hexsha = "remote_sha_456"
    repo_mock.commit.return_value = commit_obj
    _prep_log_dir(tmp_path, branch="feature")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        gen_patch = s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
        )
        with pytest.raises(SystemExit):
            _call_main(branch="feature")
        assert gen_patch.call_args[0][2] == "remote_sha_456"


def test_branch_remote_fetch_called(tmp_path):
    repo_mock = _repo(has_branch=False)
    ref_mock = MagicMock()
    ref_mock.remote_head = "feat"
    ref_mock.name = "origin/feat"
    remote = MagicMock()
    remote.refs = [ref_mock]
    repo_mock.remotes = [remote]
    _prep_log_dir(tmp_path, branch="feat")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        with pytest.raises(SystemExit):
            _call_main(branch="feat")
        remote.fetch.assert_called_once()


def test_branch_not_found_raises(tmp_path):
    repo_mock = _repo(has_branch=False)
    repo_mock.remotes = []
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        with pytest.raises(Exception, match="does not exist locally or remotely"):
            _call_main(branch="nonexistent")


def test_branch_remote_second_remote(tmp_path):
    repo_mock = _repo(has_branch=False)
    ref_no_match = MagicMock()
    ref_no_match.remote_head = "other"
    remote1 = MagicMock()
    remote1.refs = [ref_no_match]
    ref_match = MagicMock()
    ref_match.remote_head = "target"
    ref_match.name = "upstream/target"
    remote2 = MagicMock()
    remote2.refs = [ref_match]
    repo_mock.remotes = [remote1, remote2]
    commit_obj = MagicMock()
    commit_obj.hexsha = "sha_from_remote2"
    repo_mock.commit.return_value = commit_obj
    _prep_log_dir(tmp_path, branch="target")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        gen_patch = s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
        )
        with pytest.raises(SystemExit):
            _call_main(branch="target")
        assert gen_patch.call_args[0][2] == "sha_from_remote2"


def test_branch_remote_no_match_in_refs(tmp_path):
    repo_mock = _repo(has_branch=False)
    ref_mock = MagicMock()
    ref_mock.remote_head = "wrong"
    remote = MagicMock()
    remote.refs = [ref_mock]
    repo_mock.remotes = [remote]
    with ExitStack() as s:
        _apply_patches(s, tmp_path, repo_val=repo_mock)
        with pytest.raises(Exception, match="does not exist locally or remotely"):
            _call_main(branch="wanted")


def test_patch_file_written(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main()
        patch_file = tmp_path / "taffy" / "reference" / "hashval123" / "patch.diff"
        assert patch_file.exists()


def test_eval_file_written(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main()
        eval_file = tmp_path / "taffy" / "reference" / "hashval123" / "eval.sh"
        assert eval_file.exists()


def test_eval_script_formatted_with_test_ids(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main(test_ids="my_test_id")
        eval_file = tmp_path / "taffy" / "reference" / "hashval123" / "eval.sh"
        content = eval_file.read_text()
        assert "my_test_id" in content


def test_patch_file_contains_diff(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main()
        patch_file = tmp_path / "taffy" / "reference" / "hashval123" / "patch.diff"
        assert patch_file.read_text() == "diff"


def test_backend_local_uses_docker(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local")
        docker_mock.assert_called_once()


def test_backend_modal_uses_modal(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        modal_mock = s.enter_context(patch(f"{MODULE}.Modal", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="modal")
        modal_mock.assert_called_once()


def test_backend_e2b_uses_e2b(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        e2b_mock = s.enter_context(patch(f"{MODULE}.E2B", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="e2b")
        e2b_mock.assert_called_once()


def test_backend_case_insensitive_LOCAL(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="LOCAL")
        docker_mock.assert_called_once()


def test_backend_case_insensitive_Local(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="Local")
        docker_mock.assert_called_once()


def test_backend_invalid_raises(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(ValueError):
            _call_main(backend="kubernetes")


def test_backend_invalid_empty_raises(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(ValueError):
            _call_main(backend="")


@pytest.mark.parametrize("backend", ["local", "modal"])
def test_absolute_true_for_non_e2b(tmp_path, backend):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        spec_val, _, _ = _apply_patches(s, tmp_path)
        make_spec_mock = s.enter_context(
            patch(f"{MODULE}.make_rust_spec", return_value=spec_val)
        )
        with pytest.raises(SystemExit):
            _call_main(backend=backend)
        assert make_spec_mock.call_args[0][1] is True


def test_absolute_false_for_e2b(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        spec_val, _, _ = _apply_patches(s, tmp_path)
        make_spec_mock = s.enter_context(
            patch(f"{MODULE}.make_rust_spec", return_value=spec_val)
        )
        with pytest.raises(SystemExit):
            _call_main(backend="e2b")
        assert make_spec_mock.call_args[0][1] is False


@pytest.mark.parametrize("backend", ["local", "modal"])
def test_eval_command_non_e2b(tmp_path, backend):
    _prep_log_dir(tmp_path)
    ctx = _ctx_ok()
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(SystemExit):
            _call_main(backend=backend)
        ctx.exec_run_with_timeout.assert_called_once_with("/bin/bash /eval.sh")


def test_eval_command_e2b(tmp_path):
    _prep_log_dir(tmp_path)
    ctx = _ctx_ok()
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(SystemExit):
            _call_main(backend="e2b")
        ctx.exec_run_with_timeout.assert_called_once_with("/bin/bash eval.sh")


def test_files_to_copy_eval_script_absolute(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local")
        call_kwargs = docker_mock.call_args
        files_to_copy = call_kwargs[0][5] if len(call_kwargs[0]) > 5 else call_kwargs[1].get("files_to_copy")
        if files_to_copy is None:
            files_to_copy = call_kwargs[0][5]
        assert files_to_copy.eval_script["dest"] == Path("/eval.sh")
        assert files_to_copy.patch["dest"] == Path("/patch.diff")


def test_files_to_copy_eval_script_relative_e2b(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        e2b_mock = s.enter_context(patch(f"{MODULE}.E2B", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="e2b")
        call_kwargs = e2b_mock.call_args
        files_to_copy = call_kwargs[0][5] if len(call_kwargs[0]) > 5 else call_kwargs[1].get("files_to_copy")
        if files_to_copy is None:
            files_to_copy = call_kwargs[0][5]
        assert files_to_copy.eval_script["dest"] == Path("eval.sh")
        assert files_to_copy.patch["dest"] == Path("patch.diff")


def test_files_to_collect(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local")
        call_kwargs = docker_mock.call_args
        files_to_collect = call_kwargs[0][6] if len(call_kwargs[0]) > 6 else call_kwargs[1].get("files_to_collect")
        if files_to_collect is None:
            files_to_collect = call_kwargs[0][6]
        assert "cargo_test_exit_code.txt" in files_to_collect
        assert "test_output.txt" in files_to_collect


def test_timeout_raises_evaluation_error(tmp_path):
    from commit0.harness.utils import EvaluationError
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.return_value = ("output", True, 10.0)
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(EvaluationError):
            _call_main()


def test_timeout_message_includes_seconds(tmp_path):
    from commit0.harness.utils import EvaluationError
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.return_value = ("output", True, 10.0)
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(EvaluationError, match="timed out"):
            _call_main(timeout=999)


def test_exit_code_zero(tmp_path):
    _prep_log_dir(tmp_path, exit_code="0")
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main()
        assert exc.value.code == 0


def test_exit_code_nonzero(tmp_path):
    _prep_log_dir(tmp_path, exit_code="1")
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main()
        assert exc.value.code == 1


def test_exit_code_101(tmp_path):
    _prep_log_dir(tmp_path, exit_code="101")
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main()
        assert exc.value.code == 101


def test_close_logger_called(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        close_mock = s.enter_context(patch(f"{MODULE}.close_logger"))
        with pytest.raises(SystemExit):
            _call_main()
        close_mock.assert_called_once()


def test_verbose_zero_no_print(tmp_path, capsys):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main(verbose=0)
        captured = capsys.readouterr()
        assert "all tests passed" not in captured.out


def test_verbose_one_prints_output(tmp_path, capsys):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main(verbose=1)
        captured = capsys.readouterr()
        assert "all tests passed" in captured.out


def test_verbose_two_prints_output(tmp_path, capsys):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main(verbose=2)
        captured = capsys.readouterr()
        assert "all tests passed" in captured.out


def test_evaluation_error_reraised(tmp_path):
    from commit0.harness.utils import EvaluationError
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.return_value = ("output", True, 10.0)
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(EvaluationError, match="Error running Rust tests"):
            _call_main()


def test_generic_exception_wrapped_in_runtime_error(tmp_path):
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.side_effect = RuntimeError("boom")
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(RuntimeError, match="General error"):
            _call_main()


def test_generic_exception_includes_traceback(tmp_path):
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.side_effect = TypeError("bad type")
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(RuntimeError, match="bad type"):
            _call_main()


def test_generate_patch_called_with_base_commit(tmp_path):
    ex = _example(base_commit="base_abc")
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        gen_patch = s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
        )
        with pytest.raises(SystemExit):
            _call_main()
        assert gen_patch.call_args[0][1] == "base_abc"


def test_setup_logger_called(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        logger_mock = s.enter_context(
            patch(f"{MODULE}.setup_logger", return_value=MagicMock())
        )
        with pytest.raises(SystemExit):
            _call_main()
        logger_mock.assert_called_once()
        assert logger_mock.call_args[0][0] == "taffy"


def test_setup_logger_verbose_passed(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        logger_mock = s.enter_context(
            patch(f"{MODULE}.setup_logger", return_value=MagicMock())
        )
        with pytest.raises(SystemExit):
            _call_main(verbose=2)
        assert logger_mock.call_args[1]["verbose"] == 2 or logger_mock.call_args[0][-1] == 2


def test_execution_context_receives_spec(tmp_path):
    _prep_log_dir(tmp_path)
    spec_val = _spec()
    with ExitStack() as s:
        _apply_patches(s, tmp_path, spec_val=spec_val)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local")
        assert docker_mock.call_args[0][0] is spec_val


def test_execution_context_receives_timeout(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local", timeout=999)
        assert docker_mock.call_args[0][2] == 999


def test_execution_context_receives_num_cpus(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local", num_cpus=4)
        assert docker_mock.call_args[0][3] == 4


def test_execution_context_receives_rebuild_image(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local", rebuild_image=True)
        assert docker_mock.call_args[0][7] is True


def test_make_rust_spec_called_with_example(tmp_path):
    ex = _example(repo="Rust-commit0/taffy")
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        spec_mock = s.enter_context(
            patch(f"{MODULE}.make_rust_spec", return_value=_spec())
        )
        with pytest.raises(SystemExit):
            _call_main()
        call_args = spec_mock.call_args[0]
        assert call_args[0]["repo"] == "Rust-commit0/taffy"


def test_get_hash_string_called_with_test_ids(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        hash_mock = s.enter_context(
            patch(f"{MODULE}.get_hash_string", return_value="hashval123")
        )
        with pytest.raises(SystemExit):
            _call_main(test_ids="test_foo::bar")
        hash_mock.assert_called_once_with("test_foo::bar")


def test_repo_name_extracted_from_slash(tmp_path):
    ex = _example(repo="org/myrepo")
    _prep_log_dir(tmp_path, repo_name="myrepo")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        with pytest.raises(SystemExit):
            _call_main(repo_or_repo_dir="myrepo")


def test_repo_name_with_deep_path(tmp_path):
    ex = _example(repo="a/b/c/mylib")
    _prep_log_dir(tmp_path, repo_name="mylib")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        with pytest.raises(SystemExit):
            _call_main(repo_or_repo_dir="mylib")


@pytest.mark.parametrize("exit_code", ["0", "1", "2", "42", "127"])
def test_exit_code_parametrized(tmp_path, exit_code):
    _prep_log_dir(tmp_path, exit_code=exit_code)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main()
        assert exc.value.code == int(exit_code)


@pytest.mark.parametrize("backend", ["local", "modal", "e2b"])
def test_backend_routing_parametrized(tmp_path, backend):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main(backend=backend)


@pytest.mark.parametrize("backend", ["LOCAL", "MODAL", "E2B", "Local", "Modal", "e2B"])
def test_backend_case_variants(tmp_path, backend):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit):
            _call_main(backend=backend)


def test_patch_diff_encoding(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="unicode \u00e9\u00e0\u00fc")
        )
        with pytest.raises(SystemExit):
            _call_main()
        patch_file = tmp_path / "taffy" / "reference" / "hashval123" / "patch.diff"
        assert patch_file.exists()


def test_load_dataset_called_with_args(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        load_mock = s.enter_context(
            patch(f"{MODULE}.load_dataset_from_config", return_value=list([_example()]))
        )
        with pytest.raises(SystemExit):
            _call_main()
        load_mock.assert_called_once_with("ds", split="test")


def test_multiple_trailing_slash_only_one_stripped(tmp_path):
    _prep_log_dir(tmp_path)
    ex = _example(repo="Rust-commit0/taffy")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        with pytest.raises(ValueError, match="No matching Rust repo found"):
            _call_main(repo_or_repo_dir="taffy//")


def test_context_manager_entered(tmp_path):
    _prep_log_dir(tmp_path)
    ctx = _ctx_ok()
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(SystemExit):
            _call_main()
        ctx.__enter__.assert_called_once()


def test_context_manager_exited(tmp_path):
    _prep_log_dir(tmp_path)
    ctx = _ctx_ok()
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(SystemExit):
            _call_main()
        ctx.__exit__.assert_called_once()


def test_output_logged(tmp_path):
    _prep_log_dir(tmp_path)
    logger = MagicMock()
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(patch(f"{MODULE}.setup_logger", return_value=logger))
        with pytest.raises(SystemExit):
            _call_main()
        logger.info.assert_any_call("output")


def test_branch_reference_exact_string(tmp_path):
    _prep_log_dir(tmp_path)
    ex = _example(ref_commit="exact_ref_sha")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        gen_patch = s.enter_context(
            patch(f"{MODULE}.generate_patch_between_commits", return_value="diff")
        )
        with pytest.raises(SystemExit):
            _call_main(branch="reference")
        assert gen_patch.call_args[0][2] == "exact_ref_sha"


def test_no_repo_name_resolved_impossible_path(tmp_path):
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[])
        with pytest.raises(ValueError):
            _call_main()


def test_spec_none_after_iteration(tmp_path):
    ex = _example(repo="Rust-commit0/other")
    with ExitStack() as s:
        _apply_patches(s, tmp_path, dataset=[ex])
        with pytest.raises(ValueError, match="No matching Rust repo found"):
            _call_main(repo_or_repo_dir="nope")


def test_eval_script_format_called(tmp_path):
    _prep_log_dir(tmp_path)
    spec_val = MagicMock()
    spec_val.eval_script = "run {test_ids} please"
    with ExitStack() as s:
        _apply_patches(s, tmp_path, spec_val=spec_val)
        with pytest.raises(SystemExit):
            _call_main(test_ids="test_xyz")
        eval_file = tmp_path / "taffy" / "reference" / "hashval123" / "eval.sh"
        content = eval_file.read_text()
        assert "test_xyz" in content
        assert "{test_ids}" not in content


def test_git_repo_loaded_log_message(tmp_path):
    _prep_log_dir(tmp_path)
    logger = MagicMock()
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(patch(f"{MODULE}.setup_logger", return_value=logger))
        with pytest.raises(SystemExit):
            _call_main(repo_or_repo_dir="taffy")
        logger.info.assert_any_call("Loaded git repo from taffy")


def test_execution_context_log_dir_passed(tmp_path):
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        docker_mock = s.enter_context(patch(f"{MODULE}.Docker", return_value=_ctx_ok()))
        with pytest.raises(SystemExit):
            _call_main(backend="local")
        log_dir_arg = docker_mock.call_args[0][4]
        assert isinstance(log_dir_arg, Path)


def test_backend_modal_log_message(tmp_path):
    _prep_log_dir(tmp_path)
    logger = MagicMock()
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(patch(f"{MODULE}.setup_logger", return_value=logger))
        with pytest.raises(SystemExit):
            _call_main(backend="modal")
        logger.info.assert_any_call("Running on Modal")


def test_backend_local_log_message(tmp_path):
    _prep_log_dir(tmp_path)
    logger = MagicMock()
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(patch(f"{MODULE}.setup_logger", return_value=logger))
        with pytest.raises(SystemExit):
            _call_main(backend="local")
        logger.info.assert_any_call("Running locally")


def test_backend_e2b_log_message(tmp_path):
    _prep_log_dir(tmp_path)
    logger = MagicMock()
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        s.enter_context(patch(f"{MODULE}.setup_logger", return_value=logger))
        with pytest.raises(SystemExit):
            _call_main(backend="e2b")
        logger.info.assert_any_call("Running on E2B")


def test_generic_exception_preserves_cause(tmp_path):
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    original = TypeError("original cause")
    ctx.exec_run_with_timeout.side_effect = original
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(RuntimeError) as exc:
            _call_main()
        assert exc.value.__cause__ is original


def test_evaluation_error_preserves_cause(tmp_path):
    from commit0.harness.utils import EvaluationError
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.exec_run_with_timeout.return_value = ("output", True, 10.0)
    _prep_log_dir(tmp_path)
    with ExitStack() as s:
        _apply_patches(s, tmp_path, ctx_val=ctx)
        with pytest.raises(EvaluationError) as exc:
            _call_main()
        assert exc.value.__cause__ is not None


def test_exit_code_file_read_stripped(tmp_path):
    log_dir = tmp_path / "taffy" / "reference" / "hashval123"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "cargo_test_exit_code.txt").write_text("  42  \n")
    (log_dir / "test_output.txt").write_text("ok")
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main()
        assert exc.value.code == 42


def test_sys_exit_called_with_int(tmp_path):
    _prep_log_dir(tmp_path, exit_code="7")
    with ExitStack() as s:
        _apply_patches(s, tmp_path)
        with pytest.raises(SystemExit) as exc:
            _call_main()
        assert isinstance(exc.value.code, int)
