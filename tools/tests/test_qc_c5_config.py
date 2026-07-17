"""QC cluster C5 (config) regression + parity tests.

Pins the fixes for:
  * C5-001 — AgentConfig default drift across per-language config CLIs.
  * C5-002 — JavaAgentConfig field-name incompatibility (`model` vs `model_name`).
  * C5-003 — RepoInstance schema drift (`original_repo` / `language` / runtime
             version) missing from the C++/Java create_dataset validators.
  * C5-004 — Java/C++ dataset writers: UUID `id` + fail-loud on zero valid.
  * C5-005 — commit-SHA length (>=7) + hex-shape validation for C++/Java.

Deliberately source-text / AST / lightweight-import based so it needs no Docker,
network, or the heavy aider/import_deps stack that agent.config_c pulls in.
"""
from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# C5-001 / C5-002 — config default + field parity
# ---------------------------------------------------------------------------

# Scalar defaults that are language-SHARED and must never drift apart.
SHARED_SCALAR_DEFAULTS = [
    "model_name",
    "max_iteration",
    "cache_prompts",
    "max_test_output_length",
    "spec_summary_max_tokens",
    "max_repo_info_length",
    "max_unit_tests_info_length",
    "max_spec_info_length",
    "max_lint_info_length",
]

# Flags that are INTENTIONALLY per-language. Pinned to an explicit table so an
# accidental change fails CI while the documented deviations stay put. All three
# non-python CLIs (c/go/js) share the same values here; python (agent/cli.py)
# legitimately differs and is out of this cluster's ownership.
INTENTIONAL_PER_LANG_FLAGS = {
    "topo_sort_dependencies": False,
    "add_import_module_to_context": False,
    "run_entire_dir_lint": True,
    "pre_commit_config_path": "",
}

CLI_CONFIG_LANGS = ["c", "go", "js"]


def _extract_option_defaults(path: Path, fnname: str = "config") -> dict:
    """AST-extract the literal default of each typer.Option/Argument parameter
    of the `config` command in a per-language config CLI module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == fnname
    )
    args = fn.args.args
    defaults = fn.args.defaults
    offset = len(args) - len(defaults)
    out: dict = {}
    for i, dflt in enumerate(defaults):
        name = args[offset + i].arg
        value = None
        if isinstance(dflt, ast.Call) and dflt.args and isinstance(
            dflt.args[0], ast.Constant
        ):
            value = dflt.args[0].value
        elif isinstance(dflt, ast.Constant):
            value = dflt.value
        out[name] = value
    return out


@pytest.fixture(scope="module")
def cli_defaults() -> dict:
    return {
        lang: _extract_option_defaults(REPO_ROOT / "agent" / f"config_{lang}.py")
        for lang in CLI_CONFIG_LANGS
    }


@pytest.mark.parametrize("field", SHARED_SCALAR_DEFAULTS)
def test_shared_scalar_defaults_identical_across_clis(cli_defaults, field):
    values = {lang: cli_defaults[lang].get(field) for lang in CLI_CONFIG_LANGS}
    assert values["c"] is not None, (
        f"{field} default not found in config_c.py (AST drift?)"
    )
    distinct = set(values.values())
    assert len(distinct) == 1, (
        f"C5-001: shared default '{field}' drifted across config CLIs: {values}"
    )


@pytest.mark.parametrize("field,expected", sorted(INTENTIONAL_PER_LANG_FLAGS.items()))
def test_intentional_per_language_flags_match_table(cli_defaults, field, expected):
    for lang in CLI_CONFIG_LANGS:
        assert cli_defaults[lang].get(field) == expected, (
            f"C5-001: {lang} flag '{field}' = {cli_defaults[lang].get(field)!r}, "
            f"expected {expected!r} (intentional per-language table)"
        )


def test_model_name_default_matches_canonical():
    # The shared model id must match the canonical python CLI (agent/cli.py),
    # source-scanned to avoid importing the heavy aider stack.
    cli_src = (REPO_ROOT / "agent" / "cli.py").read_text(encoding="utf-8")
    assert "claude-3-5-sonnet-20240620" in cli_src
    for lang in CLI_CONFIG_LANGS:
        d = _extract_option_defaults(REPO_ROOT / "agent" / f"config_{lang}.py")
        assert d["model_name"] == "claude-3-5-sonnet-20240620"


def test_java_config_exposes_model_name_alias():
    # C5-002: JavaAgentConfig is the only config naming the field `model`; it
    # must still expose the canonical `model_name` accessor (read + write).
    from agent.config_java import JavaAgentConfig

    cfg = JavaAgentConfig()
    assert hasattr(cfg, "model_name")
    assert cfg.model_name == cfg.model
    cfg.model_name = "some-model-x"
    assert cfg.model == "some-model-x"
    assert cfg.model_name == "some-model-x"
    # Backward-compat: `model` kwarg + attribute still work for existing callers.
    cfg2 = JavaAgentConfig(model="claude-3")
    assert cfg2.model == "claude-3"
    assert cfg2.model_name == "claude-3"


# ---------------------------------------------------------------------------
# Dataset validators (C5-003 / C5-004 / C5-005)
# ---------------------------------------------------------------------------


def _valid_cpp_entry() -> dict:
    return {
        "instance_id": "fmtlib__fmt",
        "repo": "fork/fmt",
        "original_repo": "fmtlib/fmt",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "reference_commit": "abcdef0123456789abcdef0123456789abcdef01",
        "setup": {"build_system": "cmake", "cpp_standard": "17"},
        "test": {"test_cmd": "ctest --test-dir build"},
        "src_dir": "src",
        "language": "cpp",
    }


def _valid_java_entry() -> dict:
    return {
        "instance_id": "stleary__JSON-java",
        "repo": "fork/JSON-java",
        "original_repo": "stleary/JSON-java",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "reference_commit": "abcdef0123456789abcdef0123456789abcdef01",
        "setup": {"build_system": "maven", "java_version": "17"},
        "test": {},
        "src_dir": "src/main/java",
        "language": "java",
    }


def _validator(lang: str):
    if lang == "cpp":
        from tools.create_dataset_cpp import validate_cpp_entry

        return validate_cpp_entry
    from tools.create_dataset_java import validate_java_entry

    return validate_java_entry


def _valid_entry(lang: str) -> dict:
    return _valid_cpp_entry() if lang == "cpp" else _valid_java_entry()


LANGS = ["cpp", "java"]


@pytest.mark.parametrize("lang", LANGS)
def test_valid_entry_passes(lang):
    assert _validator(lang)(_valid_entry(lang)) == [], (
        f"{lang}: a fully-populated entry must validate clean"
    )


@pytest.mark.parametrize("lang", LANGS)
def test_original_repo_required(lang):
    # C5-003: original_repo missing must be rejected (was silently accepted).
    entry = {k: v for k, v in _valid_entry(lang).items() if k != "original_repo"}
    issues = _validator(lang)(entry)
    assert any("original_repo" in i for i in issues), (
        f"{lang}: missing original_repo not rejected: {issues}"
    )


@pytest.mark.parametrize("lang", LANGS)
def test_wrong_language_rejected(lang):
    # C5-003: a wrong `language` value is the concrete silent-corruption risk.
    entry = dict(_valid_entry(lang), language="python")
    issues = _validator(lang)(entry)
    assert any("language" in i for i in issues), (
        f"{lang}: wrong language not rejected: {issues}"
    )


@pytest.mark.parametrize("lang", LANGS)
def test_unsupported_runtime_version_rejected(lang):
    # C5-003: runtime-version value check (java_version / cpp_standard).
    entry = _valid_entry(lang)
    if lang == "cpp":
        entry = dict(entry, setup=dict(entry["setup"], cpp_standard="99"))
        needle = "C++ standard"
    else:
        entry = dict(entry, setup=dict(entry["setup"], java_version="99"))
        needle = "Java version"
    issues = _validator(lang)(entry)
    assert any(needle in i for i in issues), (
        f"{lang}: unsupported runtime version not rejected: {issues}"
    )


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("field", ["base_commit", "reference_commit"])
def test_short_commit_sha_rejected(lang, field):
    # C5-005
    entry = dict(_valid_entry(lang), **{field: "abc"})
    issues = _validator(lang)(entry)
    assert any(field in i and "too short" in i for i in issues), (
        f"{lang}/{field}: short SHA not rejected: {issues}"
    )


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("field", ["base_commit", "reference_commit"])
def test_non_hex_commit_sha_rejected(lang, field):
    # C5-005: 7-char but non-hex must fail the shape regex (parity with python/rust).
    entry = dict(_valid_entry(lang), **{field: "zzzzzzz"})
    issues = _validator(lang)(entry)
    assert any(field in i and "hex" in i for i in issues), (
        f"{lang}/{field}: non-hex SHA not rejected: {issues}"
    )


def _writer(lang: str):
    if lang == "cpp":
        from tools.create_dataset_cpp import create_cpp_dataset

        return create_cpp_dataset
    from tools.create_dataset_java import create_java_dataset

    return create_java_dataset


@pytest.mark.parametrize("lang", LANGS)
def test_zero_valid_entries_fails_loud(lang, tmp_path):
    # C5-004: must NOT silently write an empty dataset; must fail non-zero.
    out = tmp_path / f"{lang}_dataset.json"
    with pytest.raises(SystemExit) as exc:
        _writer(lang)([{"repo": "garbage"}], str(out))
    assert exc.value.code == 1
    assert not out.exists(), f"{lang}: empty dataset file must not be written"


def test_java_writer_assigns_uuid_id(tmp_path):
    # C5-004: Java writer previously never assigned a UUID id.
    from tools.create_dataset_java import create_java_dataset

    out = tmp_path / "java_dataset.json"
    entry = _valid_java_entry()
    entry.pop("id", None)
    create_java_dataset([entry], str(out))
    written = json.loads(out.read_text())
    assert written and written[0].get("id"), "Java entry must carry a UUID id"
    # Deterministic-ish shape check: uuid4 str is 36 chars with 4 dashes.
    assert len(written[0]["id"]) == 36 and written[0]["id"].count("-") == 4


def test_java_writer_preserves_existing_id(tmp_path):
    from tools.create_dataset_java import create_java_dataset

    out = tmp_path / "java_dataset.json"
    entry = dict(_valid_java_entry(), id="preexisting-id-123")
    create_java_dataset([entry], str(out))
    written = json.loads(out.read_text())
    assert written[0]["id"] == "preexisting-id-123"


def test_real_java_dataset_entry_validates_clean():
    # Regression anchor: the checked-in Java dataset must still pass the newly
    # strengthened validator (guards against over-strict schema changes).
    real = REPO_ROOT / "JSON-java_dataset.json"
    if not real.exists():
        pytest.skip("JSON-java_dataset.json not present")
    from tools.create_dataset_java import validate_java_entry

    data = json.loads(real.read_text())
    entries = data if isinstance(data, list) else [data]
    for e in entries:
        assert validate_java_entry(e) == [], (
            f"real java entry {e.get('repo')} failed validation: "
            f"{validate_java_entry(e)}"
        )
