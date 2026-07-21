"""Tests for the PyPI dependency pre-flight (tools/pypi_preflight.py).

Network is mocked so these are deterministic and offline-safe. The real live
integration is exercised manually against PyPI; here we pin the CONTRACT:
non-existent names are dropped, impossible pins are warned-but-kept, and every
ambiguous case (network error, unparsable spec, editable/VCS install) is left
untouched (fail-open).
"""
import tools.pypi_preflight as pf


def _stub_pypi(mapping):
    """Return a fake _query_pypi_versions using *mapping*: canon-name -> result.

    Result is a set of versions (exists), None (404), or pf._UNKNOWN (network).
    """
    def _q(name, timeout):
        key = name.lower().replace("_", "-")
        return mapping.get(key, frozenset({"1.0.0"}))
    return _q


def test_drops_nonexistent_package(monkeypatch):
    # data-dsl 404s -> dropped; flow-codegen also 404s -> dropped; requests exists
    monkeypatch.setattr(
        pf, "_query_pypi_versions",
        _stub_pypi({"data-dsl": None, "flow-codegen": None}),
    )
    kept, rep = pf.check_pip_packages(["requests>=2", "data-dsl", "flow-codegen"])
    assert "data-dsl" not in kept
    assert "flow-codegen" not in kept
    assert "requests>=2" in kept
    assert ("data-dsl", "not found on public PyPI") in rep.dropped
    assert ("flow-codegen", "not found on public PyPI") in rep.dropped


def test_warns_but_keeps_impossible_pin(monkeypatch):
    monkeypatch.setattr(
        pf, "_query_pypi_versions",
        _stub_pypi({"huggingface-hub": frozenset({"0.35.0", "0.36.2"})}),
    )
    kept, rep = pf.check_pip_packages(["huggingface-hub>=1.5.0,<2.0"])
    assert "huggingface-hub>=1.5.0,<2.0" in kept  # kept, not auto-modified
    assert rep.warned and "no published version satisfies" in rep.warned[0][1]
    assert "0.36.2" in rep.warned[0][1]  # newest reported for the human


def test_satisfiable_pin_is_silent(monkeypatch):
    monkeypatch.setattr(
        pf, "_query_pypi_versions",
        _stub_pypi({"requests": frozenset({"2.31.0", "2.32.3"})}),
    )
    kept, rep = pf.check_pip_packages(["requests>=2.28"])
    assert kept == ["requests>=2.28"]
    assert rep.clean


def test_network_error_is_fail_open(monkeypatch):
    monkeypatch.setattr(
        pf, "_query_pypi_versions", _stub_pypi({"flaky": pf._UNKNOWN})
    )
    kept, rep = pf.check_pip_packages(["flaky==9.9"])
    assert kept == ["flaky==9.9"]  # kept despite being unverified
    assert rep.skipped_network == 1
    assert not rep.dropped and not rep.warned


def test_unvalidatable_specs_left_untouched(monkeypatch):
    # editable/local/VCS/URL specs must never be dropped or queried
    called = []
    monkeypatch.setattr(
        pf, "_query_pypi_versions", lambda n, t: called.append(n) or None
    )
    specs = ["-e .", ".", "/abs/path", "git+https://x/y", "pkg @ https://x/y.whl"]
    kept, rep = pf.check_pip_packages(specs)
    assert kept == specs
    assert rep.clean
    assert called == []  # no PyPI query for any of them


def test_env_off_switch_is_noop(monkeypatch):
    monkeypatch.setenv("KAIJU_SKIP_PYPI_PREFLIGHT", "1")
    # even a 404 stub must not drop when disabled
    monkeypatch.setattr(pf, "_query_pypi_versions", _stub_pypi({"data-dsl": None}))
    kept, rep = pf.check_pip_packages(["data-dsl"])
    assert kept == ["data-dsl"]
    assert rep.clean


def test_empty_input():
    kept, rep = pf.check_pip_packages([])
    assert kept == [] and rep.clean


def test_query_excludes_fully_yanked_versions(monkeypatch):
    """A version whose files are all yanked must not count as installable."""
    import json

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({
                "releases": {
                    "1.0": [{"filename": "a", "yanked": True}],   # dead
                    "1.1": [],                                     # no files
                    "1.2": [{"filename": "b", "yanked": False}],  # live
                }
            }).encode()

    monkeypatch.setattr(pf.urllib.request, "urlopen", lambda *a, **k: _Resp())
    versions = pf._query_pypi_versions("pkg", timeout=1.0)
    assert versions == frozenset({"1.2"})
