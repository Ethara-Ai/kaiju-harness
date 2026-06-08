#!/usr/bin/env python3
"""Live integration tests for summarize_specification() against real Bedrock.

Requires env vars:
  AWS_BEARER_TOKEN_BEDROCK
  AWS_DEFAULT_REGION (default: ap-south-1)
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path
import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.agent_utils import summarize_specification, _chunk_text


pytestmark = pytest.mark.skipif(
    not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"),
    reason="live test — requires AWS_BEARER_TOKEN_BEDROCK",
)
# ── Config ────────────────────────────────────────────────────────────────────
MODEL = os.environ.get(
    "SPEC_SUMMARY_MODEL",
    os.environ.get("BEDROCK_TEST_ARN", ""),
)
BUDGET = 10_000  # default max_char_length


# ── Synthetic spec generator ──────────────────────────────────────────────────
def _make_spec(target_chars: int) -> str:
    """Generate a realistic synthetic Python library specification."""
    module_block = """
## Module `beartype.claw`

### `beartype_this_package(conf: BeartypeConf = BeartypeConf()) -> None`
Instrument the **caller's package** with runtime type-checking.
Must be called from the package `__init__.py` *before* any sibling imports.

Parameters:
  - `conf` (`BeartypeConf`): Configuration dataclass controlling type-check depth,
    violation reporting strategy, and decorator wrapping mode. Defaults to
    `BeartypeConf()` which enables O(1) random-sample checking.

Raises:
  - `BeartypeClawException`: If called outside a package `__init__.py`.
  - `BeartypeConfException`: If `conf` is not a `BeartypeConf` instance.

Example:
```python
from beartype.claw import beartype_this_package
beartype_this_package()
```

### `beartype_package(package_name: str, conf: BeartypeConf = BeartypeConf()) -> None`
Instrument an *arbitrary* installed package by name.

Parameters:
  - `package_name` (`str`): Fully qualified package name (e.g. `"numpy"`).
  - `conf` (`BeartypeConf`): See `beartype_this_package()`.

Raises:
  - `BeartypeClawException`: If the package is not importable.
  - `ModuleNotFoundError`: If the package does not exist.

### Constants
  - `BEARTYPE_CLAW_VERSION: str` — Semantic version of the claw subsystem.
  - `BEARTYPE_SUPPORTED_HOOKS: frozenset[str]` — Set of supported import hook names.

---

## Module `beartype.roar`

Exception hierarchy for all beartype runtime violations:

```
BeartypeException (base)
├── BeartypeCallHintViolation      # arg/return type mismatch at call time
├── BeartypeDecorHintException     # unsupported type hint at decoration time
├── BeartypeClawException          # import hook configuration error
├── BeartypeConfException          # invalid BeartypeConf parameter
└── BeartypeDoorHintViolation      # beartype.door.die_if_unbearable() violation
```

### `class BeartypeCallHintViolation(BeartypeException)`
Raised when a `@beartype`-decorated callable receives or returns a value
violating its type hints. The exception message includes:
- the fully qualified callable name
- parameter name and position
- expected type(s) vs actual type
- the actual value (repr, truncated to 200 chars)

### Edge cases
- Generic aliases (`list[int]`, `dict[str, Any]`) are checked element-wise
  using O(1) random sampling by default.
- `None` return annotations are checked (`-> None` means the function must
  not return a value other than `None`).
- `@beartype` is idempotent — double-decorating is a no-op.

"""
    # Repeat to reach target size
    repeats = max(1, target_chars // len(module_block))
    spec = module_block * repeats
    return spec[:target_chars]


# ── Test runners ──────────────────────────────────────────────────────────────


def test_single_pass(size: int = 25_000, budget: int = BUDGET):
    """Test single-pass summarization (spec < 300K)."""
    spec = _make_spec(size)
    print(f"\n{'=' * 70}")
    print(f"TEST: Single-pass | input={len(spec):,} chars | budget={budget:,}")
    print(f"{'=' * 70}")

    t0 = time.time()
    result, _costs = summarize_specification(
        spec_text=spec,
        model=MODEL,
        max_tokens=4000,
        max_char_length=budget,
        timeout=120,
    )
    elapsed = time.time() - t0

    print(f"  Output:  {len(result):,} chars")
    print(f"  Ratio:   {len(spec) / len(result):.1f}x compression")
    print(
        f"  Budget:  {'✅ UNDER' if len(result) <= budget else '❌ OVER'} ({len(result):,} / {budget:,})"
    )
    print(f"  Time:    {elapsed:.1f}s")
    print(f"  Preview: {result[:200]}...")

    assert len(result) <= budget * 1.1, f"Over budget: {len(result)} > {budget}"
    assert len(result) > 100, f"Suspiciously short: {len(result)} chars"
    return result


def test_chunked(size: int = 700_000, budget: int = BUDGET):
    """Test chunked path (spec > 300K, triggers split + parallel)."""
    spec = _make_spec(size)
    chunks = _chunk_text(spec, 300_000)
    print(f"\n{'=' * 70}")
    print(
        f"TEST: Chunked ({len(chunks)} chunks) | input={len(spec):,} chars | budget={budget:,}"
    )
    print(f"{'=' * 70}")

    t0 = time.time()
    result, _costs = summarize_specification(
        spec_text=spec,
        model=MODEL,
        max_tokens=4000,
        max_char_length=budget,
        timeout=120,
    )
    elapsed = time.time() - t0

    print(f"  Output:  {len(result):,} chars")
    print(f"  Ratio:   {len(spec) / len(result):.1f}x compression")
    print(
        f"  Budget:  {'✅ UNDER' if len(result) <= budget else '❌ OVER'} ({len(result):,} / {budget:,})"
    )
    print(f"  Time:    {elapsed:.1f}s ({elapsed / len(chunks):.1f}s/chunk)")
    print(f"  Preview: {result[:200]}...")

    assert len(result) > 100, f"Suspiciously short: {len(result)} chars"
    return result


def test_caching():
    """Test that caching works — second call should be instant."""
    spec = _make_spec(20_000)

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / ".spec_summary_cache.json"

        print(f"\n{'=' * 70}")
        print(f"TEST: Caching | input={len(spec):,} chars")
        print(f"{'=' * 70}")

        # First call — cache miss
        t0 = time.time()
        result1, _costs1 = summarize_specification(
            spec_text=spec,
            model=MODEL,
            max_tokens=4000,
            max_char_length=BUDGET,
            timeout=120,
            cache_path=cache_path,
        )
        elapsed1 = time.time() - t0
        print(f"  Call 1:  {len(result1):,} chars in {elapsed1:.1f}s (cache miss)")

        assert cache_path.exists(), "Cache file not created!"
        cached = json.loads(cache_path.read_text())
        print(
            f"  Cache:   ✅ written ({len(cached['summary']):,} chars, hash={cached['hash'][:12]}...)"
        )

        # Second call — cache hit (should be instant)
        t1 = time.time()
        result2, _costs2 = summarize_specification(
            spec_text=spec,
            model=MODEL,
            max_tokens=4000,
            max_char_length=BUDGET,
            timeout=120,
            cache_path=cache_path,
        )
        elapsed1 = time.time() - t0
        print(f"  Call 1:  {len(result1):,} chars in {elapsed1:.1f}s (cache miss)")

        assert cache_path.exists(), "Cache file not created!"
        cached = json.loads(cache_path.read_text())
        print(
            f"  Cache:   ✅ written ({len(cached['summary']):,} chars, hash={cached['hash'][:12]}...)"
        )

        # Second call — cache hit (should be instant)
        t1 = time.time()
        result2, _costs2 = summarize_specification(
            spec_text=spec,
            model=MODEL,
            max_tokens=4000,
            max_char_length=BUDGET,
            timeout=120,
            cache_path=cache_path,
        )
        elapsed2 = time.time() - t1
        print(f"  Call 2:  {len(result2):,} chars in {elapsed2:.4f}s (cache hit)")

        assert result1 == result2, "Cache returned different result!"
        assert elapsed2 < 0.1, f"Cache hit too slow: {elapsed2:.2f}s"
        print("  Match:   ✅ identical output")
        print(f"  Speedup: {elapsed1 / max(elapsed2, 0.001):.0f}x")

    return result1


def test_tight_budget(size: int = 25_000, budget: int = 3_000):
    """Test with tight budget constraint."""
    return test_single_pass(size=size, budget=budget)


def test_large_budget(size: int = 25_000, budget: int = 20_000):
    """Test with generous budget (should preserve more detail)."""
    return test_single_pass(size=size, budget=budget)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Verify credentials
    bearer = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")

    if not bearer:
        print("ERROR: Set AWS_BEARER_TOKEN_BEDROCK")
        sys.exit(1)

    print(f"Model:  {MODEL}")
    print(f"Region: {region}")
    print(f"Auth:   Bearer token ({len(bearer)} chars)")

    results = {}
    tests = [
        ("single_pass_25K", lambda: test_single_pass(25_000)),
        ("tight_budget_3K", lambda: test_tight_budget(25_000, 3_000)),
        ("large_budget_20K", lambda: test_large_budget(25_000, 20_000)),
        ("chunked_700K", lambda: test_chunked(700_000)),
        ("caching", test_caching),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            result = fn()
            results[name] = {"status": "PASS", "length": len(result)}
            passed += 1
        except Exception as e:
            results[name] = {"status": "FAIL", "error": str(e)}
            failed += 1
            import traceback

            traceback.print_exc()

    # Summary
    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 70}")
    for name, r in results.items():
        status = "✅" if r["status"] == "PASS" else "❌"
        detail = f"{r['length']:,} chars" if "length" in r else r.get("error", "")
        print(f"  {status} {name:25s} {detail}")

    sys.exit(1 if failed else 0)
