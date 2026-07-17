"""QC cluster C3_runners parity/regression tests.

Source-text / AST assertions (no Docker, no network) pinning the C3 runner and
monkey-patch fixes and the cross-language invariants they restore:

  * C3-002: canonical run_agent.py marks .done BEFORE the per-module write.
  * C3-005: all 8 runners carry the idempotent post-loop output.json backstop.
  * C3-001: C++ batch pool has per-worker error isolation (no bare ar.get()).
  * C3-004: C/Go/Rust/C++ per-repo workers return (repo_name, ok), not None.
  * C3-006: Go/C/JS/Java drifted patch copies carry summarize_start (P6),
            register_active_coder, and (Go/C/JS) FinishReasonLength/patched_send
            (P7) + _last_response_id/trailing-chunk drain (P2 ext); cpp/rust/ts
            inherit the canonical patcher.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parent.parent

# (module filename, per-repo worker function name) for the 4 C3-004 languages.
_TUPLE_WORKERS = {
    "run_agent_c.py": "run_agent_for_repo",
    "run_agent_go.py": "run_agent_for_repo",
    "run_rust_agent.py": "run_rust_agent_for_repo",
    "run_cpp_agent.py": "run_cpp_agent_for_repo",
}

_ALL_RUNNERS = [
    "run_agent.py",
    "run_agent_c.py",
    "run_agent_go.py",
    "run_agent_js.py",
    "run_agent_ts.py",
    "run_agent_java.py",
    "run_rust_agent.py",
    "run_cpp_agent.py",
]


def _read(name: str) -> str:
    return (_AGENT_DIR / name).read_text()


# ---------------------------------------------------------------------------
# C3-002 — mark .done BEFORE per-module write in the canonical Python runner
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("log_dir", ["test_log_dir", "lint_log_dir", "file_log_dir"])
def test_python_runner_marks_done_before_write(log_dir: str):
    src = _read("run_agent.py")
    mark = src.find(f"_mark_module_done({log_dir})")
    write = src.find(f"module_log_dir={log_dir}")
    assert mark != -1, f"_mark_module_done({log_dir}) not found"
    assert write != -1, f"per-module write for {log_dir} not found"
    assert mark < write, (
        f"run_agent.py must call _mark_module_done({log_dir}) BEFORE writing "
        f"that module's output.json (C3-002) — a crash between write and mark "
        f"leaves output.json without .done, so resume re-runs & double-counts."
    )


# ---------------------------------------------------------------------------
# C3-005 — idempotent post-loop backstop present in ALL runners
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runner", _ALL_RUNNERS)
def test_runner_has_output_json_backstop(runner: str):
    src = _read(runner)
    assert 'output.json").exists()' in src, (
        f"{runner} lost its idempotent output.json backstop (C3-005): every "
        f"runner must recover output.json for a turn-bearing module that lacks "
        f"one, skipping any module that already has it."
    )


def test_python_runner_backstop_is_idempotent_and_guarded():
    src = _read("run_agent.py")
    assert "IDEMPOTENT BACKSTOP" in src
    assert "if thinking_capture is not None:" in src
    # The existence check must `continue` (skip) — never rewrite / double-count.
    assert 'if (module_log_dir / "output.json").exists():' in src
    assert "modules_seen" in src


# ---------------------------------------------------------------------------
# C3-004 — per-repo workers return a (repo_name, ok) tuple
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runner,fn", list(_TUPLE_WORKERS.items()))
def test_worker_returns_repo_name_ok_tuple(runner: str, fn: str):
    src = _read(runner)
    tree = ast.parse(src)
    func = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == fn
        ),
        None,
    )
    assert func is not None, f"{runner}: worker {fn} not found"

    returns = [n for n in ast.walk(func) if isinstance(n, ast.Return)]
    tuple_returns = [
        r
        for r in returns
        if isinstance(r.value, ast.Tuple) and len(r.value.elts) == 2
    ]
    assert tuple_returns, (
        f"{runner}:{fn} must return a 2-tuple (repo_name, ok) for isolation "
        f"(C3-004), not None — otherwise a failing worker cannot be attributed."
    )
    # First element of each tuple return is the repo_name variable.
    for r in tuple_returns:
        first = r.value.elts[0]
        assert isinstance(first, ast.Name) and first.id == "repo_name", (
            f"{runner}:{fn} tuple return must lead with repo_name (C3-004)."
        )
    # The isolating wrapper must express both the success and failure branch.
    assert "return repo_name, True" in src and "return repo_name, False" in src, (
        f"{runner}:{fn} must return (repo_name, True) on success and "
        f"(repo_name, False) on isolated failure."
    )


# ---------------------------------------------------------------------------
# C3-001 — C++ batch pool per-worker isolation (no bare ar.get())
# ---------------------------------------------------------------------------

def test_cpp_pool_has_worker_isolation():
    src = _read("run_cpp_agent.py")
    # The bare, batch-aborting collection loop must be gone.
    assert "for ar in async_results:\n                    ar.get()" not in src, (
        "run_cpp_agent.py still has the bare ar.get() loop that aborts the whole "
        "batch on the first worker failure (C3-001)."
    )
    # ... replaced by the resilient collector, used AND defined.
    assert src.count("_collect_cpp_worker_results") >= 2, (
        "run_cpp_agent.py must route worker results through "
        "_collect_cpp_worker_results (define + call)."
    )
    # A total wipeout is still surfaced as a systemic error.
    assert 'summary["failed"] == len(async_results)' in src


# ---------------------------------------------------------------------------
# C3-006 — monkey-patch parity across the drifted sibling copies
# ---------------------------------------------------------------------------

# Tokens proving each patch was ported. P6 + register_active_coder go to all 4
# drifted copies; P7 + P2-ext go to go/c/js (Java already had them).
_P6_TOKENS = ("patched_summarize_start", "copy_context", "register_active_coder")
_P7_P2_TOKENS = (
    "FinishReasonLength",
    "patched_send",
    "_last_response_id",
    "for trailing",
)


@pytest.mark.parametrize("mod", ["agents_go.py", "agents_c.py", "agents_js.py"])
def test_drifted_copies_have_full_patch_set(mod: str):
    src = _read(mod)
    for tok in _P6_TOKENS + _P7_P2_TOKENS:
        assert tok in src, f"{mod} missing patch token {tok!r} (C3-006)."


def test_java_copy_has_p6_and_preexisting_patches():
    src = _read("agents_java.py")
    for tok in _P6_TOKENS:
        assert tok in src, f"agents_java.py missing P6 token {tok!r} (C3-006)."
    # Java already carried P7 + P2 ext; assert they are still present.
    for tok in _P7_P2_TOKENS:
        assert tok in src, f"agents_java.py lost pre-existing token {tok!r}."


@pytest.mark.parametrize("mod", ["agents_cpp.py", "agents_rust.py", "agents_ts.py"])
def test_inheriting_copies_import_canonical_patcher(mod: str):
    src = _read(mod)
    assert "_apply_thinking_capture_patches" in src
    assert "from agent.agents import" in src, (
        f"{mod} must inherit the canonical _apply_thinking_capture_patches "
        f"(C3-006), not keep a drifted local copy."
    )
    # No local re-definition of the patcher.
    tree = ast.parse(src)
    local_defs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_apply_thinking_capture_patches"
    ]
    assert not local_defs, f"{mod} must not redefine _apply_thinking_capture_patches."


def test_js_preserves_turn_counter_ref_deviation():
    """JS deliberately keeps the shared-list turn counter (documented deviation)."""
    src = _read("agents_js.py")
    assert "_turn_counter_ref" in src, (
        "agents_js.py must keep its intentional _turn_counter_ref deviation "
        "(pinned by test_agents_js.py::TestPatchedCloneSharesTurnCounter)."
    )


def test_drifted_copies_pass_llm_response_id():
    """_last_response_id is only meaningful if fed to add_assistant_turn."""
    for mod in ("agents_go.py", "agents_c.py", "agents_js.py"):
        src = _read(mod)
        assert "llm_response_id=coder._last_response_id" in src, (
            f"{mod} tracks _last_response_id but never passes it to "
            f"add_assistant_turn — the P2 extension would be inert."
        )
