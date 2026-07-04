from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable


def _run(args: list[str], env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(args, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30)


def _make_synthetic_entries(tmp_path: Path, uuid_str: str) -> Path:
    entries = [
        {
            "instance_id": "commit-0/synthetic",
            "id": uuid_str,
            "repo": "V1ibh1vsingh/synthetic",
            "original_repo": "test/synthetic",
            "base_commit": "0" * 40,
            "reference_commit": "1" * 40,
            "src_dir": "src",
            "language": "typescript",
            "test_framework": "vitest",
            "functions_stubbed": 0,
            "setup": {
                "node_version": "20",
                "install": "npm install",
                "packages": "",
                "pre_install": [],
                "specification": "https://example.com",
            },
            "test": {"test_cmd": "npx vitest run", "test_dir": "tests"},
        }
    ]
    entries_path = tmp_path / "entries.json"
    entries_path.write_text(json.dumps(entries, indent=2))
    return entries_path


def test_paths_module_layout_functions() -> None:
    with tempfile.TemporaryDirectory() as td:
        env = {"KAIJU_OUTPUTS_ROOT": td, "KAIJU_LOG_LAYOUT": "consolidated"}
        res = _run([PY, "-c",
            "from kaiju.paths import outputs_root, is_consolidated, datasets_dir, runs_dir, configs_dir, build_logs_dir, harbor_dir;"
            "u='abc-123';"
            "print(outputs_root());"
            "print(is_consolidated());"
            "print(datasets_dir(u));"
            "print(runs_dir(u));"
            "print(configs_dir(u));"
            "print(build_logs_dir(u));"
            "print(harbor_dir(u))"
        ], env)
        assert res.returncode == 0, res.stderr
        lines = res.stdout.strip().split("\n")
        assert lines[0] == td, f"outputs_root mismatch: {lines[0]!r} vs {td!r}"
        assert lines[1] == "True", f"is_consolidated should be True: {lines[1]!r}"
        assert lines[2].endswith("/abc-123/datasets")
        assert lines[3].endswith("/abc-123/runs")
        assert lines[4].endswith("/abc-123/configs")
        assert lines[5].endswith("/abc-123/build_logs")
        assert lines[6].endswith("/abc-123/harbor")


def test_flat_layout_preserves_legacy_paths() -> None:
    res = _run([PY, "-c",
        "from kaiju.paths import layout, is_consolidated; print(layout()); print(is_consolidated())"
    ], {"KAIJU_LOG_LAYOUT": "flat"})
    assert res.returncode == 0, res.stderr
    lines = res.stdout.strip().split("\n")
    assert lines[0] == "flat"
    assert lines[1] == "False"


def test_default_layout_is_consolidated() -> None:
    env = dict(os.environ)
    env.pop("KAIJU_LOG_LAYOUT", None)
    env.pop("KAIJU_OUTPUTS_ROOT", None)
    res = subprocess.run(
        [PY, "-c", "from kaiju.paths import layout; print(layout())"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "consolidated"


def test_create_dataset_ts_writes_to_consolidated_folder() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        uid = str(uuid.uuid4())
        entries_path = _make_synthetic_entries(tmp_path, uid)
        outputs = tmp_path / "outputs"
        env = {"KAIJU_OUTPUTS_ROOT": str(outputs), "KAIJU_LOG_LAYOUT": "consolidated"}
        res = _run([PY, "-m", "tools.create_dataset_ts", str(entries_path)], env)
        assert res.returncode == 0, res.stderr
        expected = outputs / uid / "datasets" / "dataset.json"
        assert expected.exists(), f"dataset.json missing at {expected}"
        loaded = json.loads(expected.read_text())
        assert loaded[0]["id"] == uid


def test_flat_mode_writes_to_explicit_output() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        entries_path = _make_synthetic_entries(tmp_path, str(uuid.uuid4()))
        target = tmp_path / "custom.json"
        res = _run(
            [PY, "-m", "tools.create_dataset_ts", str(entries_path), "--output", str(target)],
            {"KAIJU_LOG_LAYOUT": "flat"},
        )
        assert res.returncode == 0, res.stderr
        assert target.exists(), "flat mode should write to --output path"


def test_constants_docker_paths_respect_env() -> None:
    env_flat = {"KAIJU_LOG_LAYOUT": "flat"}
    env_flat.pop("KAIJU_EXPERIMENT_UUID", None)
    res = _run([PY, "-c",
        "from commit0.harness.constants import base_image_build_dir;"
        "print(base_image_build_dir())"
    ], env_flat)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "logs/build_images/base"

    with tempfile.TemporaryDirectory() as td:
        env_c = {
            "KAIJU_OUTPUTS_ROOT": td,
            "KAIJU_LOG_LAYOUT": "consolidated",
            "KAIJU_EXPERIMENT_UUID": "test-uuid",
        }
        res = _run([PY, "-c",
            "from commit0.harness.constants import base_image_build_dir;"
            "print(base_image_build_dir())"
        ], env_c)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == f"{td}/test-uuid/build_logs/base"


def test_bash_helper_sourceable() -> None:
    script = REPO / "scripts" / "_outputs_layout.sh"
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env.update({"BASE_DIR": td, "KAIJU_LOG_LAYOUT": "consolidated"})
        res = subprocess.run(
            ["bash", "-c", f'source "{script}" && datasets_dir def-999 && runs_dir def-999 && (is_consolidated && echo YES || echo NO)'],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        lines = res.stdout.strip().split("\n")
        assert lines[0].endswith("/def-999/datasets"), lines[0]
        assert lines[1].endswith("/def-999/runs"), lines[1]
        assert lines[2] == "YES", f"is_consolidated should be YES: {lines[2]!r}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  \u2713 {name}")
            except AssertionError as e:
                print(f"  \u2717 {name}: {e}")
                failures += 1
            except Exception as e:
                print(f"  ! {name}: {type(e).__name__}: {e}")
                failures += 1
    sys.exit(1 if failures else 0)
