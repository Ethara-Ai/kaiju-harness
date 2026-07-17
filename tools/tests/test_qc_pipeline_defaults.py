"""QC-C2-005 / C2-010: enforce the canonical per-pipeline default matrix.

The eight run_pipeline_*.sh drivers are copy-then-edit siblings; their scoring-
relevant defaults had *silently* drifted (the finding's core risk: divergent
defaults make per-language scores incomparable without anyone noticing). Rather
than blindly homogenize values that are LEGITIMATELY language-specific (C++
compiles are slow; Java needs an in-place backend; JS has no PDF spec), this test
pins every default to the canonical value AND records each intentional deviation
with a rationale. A NEW, undocumented drift now fails CI; an intentional change
must be added to the whitelist with a reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVERS = {
    "python": REPO_ROOT / "run_pipeline.sh",
    "c": REPO_ROOT / "run_pipeline_c.sh",
    "cpp": REPO_ROOT / "run_pipeline_cpp.sh",
    "go": REPO_ROOT / "run_pipeline_go.sh",
    "java": REPO_ROOT / "run_pipeline_java.sh",
    "js": REPO_ROOT / "run_pipeline_js.sh",
    "rust": REPO_ROOT / "run_pipeline_rust.sh",
    "ts": REPO_ROOT / "run_pipeline_ts.sh",
}

# Canonical default (from run_pipeline.sh, the reference driver).
CANONICAL = {
    "BACKEND": "local",
    "EVAL_TIMEOUT": "3600",
    "INACTIVITY_TIMEOUT": "900",
    "USE_SPEC_INFO": "true",
    "MAX_ITERATION": "3",
    "MAX_WALL_TIME": "86400",
}

# lang -> {var -> (allowed_value, rationale)}. Every entry is an AUDITED,
# intentional per-language deviation. Anything NOT here must equal CANONICAL.
INTENTIONAL_DEVIATIONS = {
    "cpp": {
        "EVAL_TIMEOUT": ("7200", "C++ compiles are slow; a 3600s eval cap would "
                                 "unfairly time out large translation units"),
        "INACTIVITY_TIMEOUT": ("1800", "slow C++ builds/link steps go quiet for "
                                       "long stretches without hanging"),
        # NOTE: cpp is the only driver that makes USE_SPEC_INFO overridable and
        # defaults it OFF. Flagged for live validation — if the C++ agent should
        # consume the scraped spec, flip this to true. Kept as-is (not blindly
        # changed) to avoid an unvalidated behavior change.
        "USE_SPEC_INFO": ("false", "cpp defaults spec-info OFF (overridable via "
                                   "env); PENDING live-validation confirmation"),
    },
    "java": {
        "BACKEND": ("local_inplace", "Java's build/image flow requires the "
                                     "in-place backend"),
        "INACTIVITY_TIMEOUT": ("1800", "Gradle/Maven builds go quiet for long "
                                       "stretches without hanging"),
    },
    "js": {
        "USE_SPEC_INFO": ("false", "JS has no PDF spec; the agent reads the "
                                   "README in-container (see QC-C1-007)"),
    },
}


def _default_value(src: str, var: str) -> str | None:
    """First `VAR=...` assignment, unwrapping `${VAR:-default}` and quotes."""
    m = re.search(rf'^{var}=(.+)$', src, re.M)
    if not m:
        return None
    raw = m.group(1).strip()
    # strip inline comments
    raw = raw.split("#")[0].strip()
    # unwrap ${VAR:-default}
    mo = re.match(rf'"?\$\{{{var}:-([^}}"]*)\}}"?', raw)
    if mo:
        return mo.group(1).strip()
    return raw.strip('"').strip("'")


@pytest.mark.parametrize("lang", sorted(DRIVERS))
@pytest.mark.parametrize("var", sorted(CANONICAL))
def test_default_matches_canonical_or_whitelisted(lang, var):
    src = DRIVERS[lang].read_text(encoding="utf-8")
    val = _default_value(src, var)
    if val is None:
        pytest.skip(f"{lang} does not declare {var}")
    dev = INTENTIONAL_DEVIATIONS.get(lang, {}).get(var)
    if dev is not None:
        allowed, _why = dev
        assert val == allowed, (
            f"{lang} {var}={val!r} but whitelisted deviation expects "
            f"{allowed!r}; update run_pipeline_{lang}.sh or the whitelist"
        )
        return
    assert val == CANONICAL[var], (
        f"{lang} {var}={val!r} drifted from canonical {CANONICAL[var]!r}. "
        f"If this is intentional, add it to INTENTIONAL_DEVIATIONS with a "
        f"rationale; otherwise fix run_pipeline_{lang}.sh."
    )
