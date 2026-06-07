# BUGS.md — Production-Readiness Audit Tickets

**Target:** commit0 (kaiju working tree) · **Commit:** `9649faab59b4e1274dfb626d84f06e2f9340d51d` (branch `test`, **dirty tree**) · **Audited:** 2026-06-07T08:29:11Z
**Source of truth:** `findings.json` · **Full analysis:** `REPORT.md`
**Ticket scope:** one ticket per finding with disposition **BLOCK** or **HOLD** (9 tickets). SHIP/INFO findings are not ticketed; verified-safe items are recorded in the [False Positives / Won't-Do](#false-positives--wont-do-do-not-reopen) table so they are not re-opened.

---

## Triage / Release-Note Summary Matrix

| Issue Key | Type | Priority | Severity | Disposition | Axis | CWE | Exploitable-by | One-line |
|--|--|:--:|:--:|:--:|--|--|--|--|
| BUG-T-001 | Bug | P1 | MEDIUM | 🔴 BLOCK | Testing | CWE-561 / CWE-1164 | N/A (correctness) | Duplicate test classes silently shadow earlier tests — lost coverage |
| BUG-S-001 | Vulnerability | P2 | MEDIUM | 🟡 HOLD | Security | CWE-611 / CWE-776 | Malicious target repo (untrusted XML) | XML parsed without defusedxml |
| BUG-S-002 | Vulnerability | P2 | MEDIUM | 🟡 HOLD | Security | CWE-22 / CWE-918 | Operator/repo-metadata-controlled URL | urlopen with unvalidated scheme |
| BUG-D-001 | Bug | P2 | MEDIUM | 🟡 HOLD | Dependencies | CWE-1357 / CWE-829 | Fork maintainer (supply chain) | Core deps pinned to moving git branches |
| BUG-D-002 | Task | P3 | LOW | 🟡 HOLD | Dependencies | N/A | N/A (assessment limitation) | CVE scan could not run in sandbox |
| BUG-H-001 | Task | P3 | LOW | 🟡 HOLD | Hygiene | CWE-1164 | N/A | 3.99 MB Mach-O binary committed |
| BUG-H-002 | Task | P3 | LOW | 🟡 HOLD | Hygiene | CWE-1164 | N/A | 23 MB GIF + .docx bloat (.git = 161 MB) |
| BUG-Q-001 | Tech-Debt | P3 | LOW | 🟡 HOLD | Quality | CWE-1164 | N/A | 9,612 lint violations + suppressed pyright |
| BUG-C-001 | Task | P3 | LOW | 🟡 HOLD | Config/CI | CWE-1164 | N/A | No coverage floor + ruff version skew |

**Counts:** 1 BLOCK + 8 HOLD = **9 tickets** (matches the 9 non-null `bug_ticket` values in findings.json).

---

## BUG-T-001 — Duplicate test-class names silently shadow earlier classes (lost coverage)

| Field | Value |
|--|--|
| **Issue Key** | BUG-T-001 |
| **Type** | Bug |
| **Priority** | P1 |
| **Severity** | MEDIUM |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🔴 BLOCK (release-blocking) |
| **Components** | commit0-harness, test-suite |
| **Labels** | testing, lost-coverage, ruff-F811, ci-gate |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | All (language-level Python behaviour) |
| **CWE** | CWE-561 (Dead Code), CWE-1164 (Irrelevant Code) |
| **Exploitable-by** | N/A — correctness/coverage defect, not an attack |
| **Instrument** | [INSTRUMENTED: ruff 0.6.4 — F811 ×2] |

**Summary:** Two test modules each define the same test-class name twice; Python keeps only the last definition, so the earlier class (and all its assertions) is never collected by pytest. The suite reports green while silently skipping real tests.

**Description (with code evidence):**
- `commit0/harness/tests/test_constants_rust.py:123` and `:458` — `class TestPathConstants` defined twice.
- `commit0/harness/tests/test_rust_test_parser.py:208` and `:550` — `class TestRustTestResultDataclass` defined twice.

In a single module, `class TestPathConstants: ...` at line 123 is rebound by the redefinition at line 458 before pytest collection runs. The line-123 class object is discarded; its test methods never execute. ruff confirms: `F811 redefinition of unused 'TestPathConstants' from line 123`.

**Steps to Reproduce:**
1. `uvx ruff@0.6.4 check commit0/harness/tests/test_constants_rust.py commit0/harness/tests/test_rust_test_parser.py --select F811`
2. Observe two `F811` redefinition reports.
3. (Confirmatory) Add a deliberately failing assertion to the **first** `TestPathConstants` (line 123) body and run pytest on that file — it passes, because the first class is never collected.

**Expected Result:** Every defined test class is collected and executed.

**Actual Result:** The earlier same-named class is overwritten; its tests never run. Coverage is silently lost.

**Impact:** This is a benchmark whose entire value is the correctness of its test harness. Silently-skipped tests produce false confidence in exactly the component that must be trusted. Release-blocking.

**Suggested Fix:** Rename the duplicates (e.g. `TestPathConstantsExtended`) or merge their methods into one class. Add `F811` to the merge-blocking ruff CI selection so recurrence is impossible.

**Acceptance Criteria:**
- [ ] `ruff check --select F811` reports zero violations in `commit0/harness/tests/`.
- [ ] Both formerly-shadowed classes' methods appear in `pytest --collect-only` output.
- [ ] CI fails on any new `F811`.

---

## BUG-S-001 — XML parsed without defusedxml on untrusted test-runner output (XXE / billion-laughs)

| Field | Value |
|--|--|
| **Issue Key** | BUG-S-001 |
| **Type** | Vulnerability |
| **Priority** | P2 |
| **Severity** | MEDIUM |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | commit0-harness (c/java parsers) |
| **Labels** | security, xxe, defusedxml, untrusted-input |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | Any host running the C/Java test harness against target repos |
| **CWE** | CWE-611 (XXE), CWE-776 (Entity Expansion) |
| **Exploitable-by** | A malicious target repository that emits crafted test-runner XML |
| **CVSS v3.1** | `CVSS:3.1/AV:L/AC:H/PR:L/UI:N/S:U/C:L/I:N/A:L` → **3.9** |
| **Instrument** | [INSTRUMENTED: bandit 1.7.9 — B314 ×4] |

**Summary:** Four parse sites use `xml.etree.ElementTree` on XML produced by executing untrusted target repos' test suites, without the hardened `defusedxml` wrapper.

**Description (with code evidence):**
- `commit0/harness/c_test_parser.py:58` — `ET.fromstring(...)`
- `commit0/harness/java_test_parser.py:57`
- `commit0/harness/constants_java.py:122`
- `commit0/harness/lint_java.py:168`

Per the audit trust model, output from the 57 target repos is attacker-controlled. A crafted JUnit/test XML with an external DTD (XXE) or nested-entity bomb is the classic risk.

**Steps to Reproduce:**
1. Construct a target repo whose test runner emits XML containing an external entity / billion-laughs payload.
2. Run the C/Java harness so `c_test_parser.py` / `java_test_parser.py` parses that output.
3. Observe entity resolution / resource amplification attempt.

**Expected Result:** Hardened parser rejects external entities and entity expansion.

**Actual Result:** Stdlib ElementTree is used directly. (Note: modern CPython ElementTree does not resolve external entities by default, which bounds exploitability to Medium — hence HOLD, not BLOCK.)

**Impact:** Local DoS / limited info exposure when running untrusted benchmark targets. Bounded but real given the explicit untrusted-input threat model.

**Suggested Fix:** Single root-cause fix — replace `xml.etree.ElementTree` with `defusedxml.ElementTree` across the 4 sites; add `defusedxml` to dependencies.

**Acceptance Criteria:**
- [ ] All 4 parse sites import from `defusedxml`.
- [ ] `bandit --select B314` reports zero in `commit0/harness/`.
- [ ] A billion-laughs fixture is rejected by the parser in a unit test.

---

## BUG-S-002 — urllib.urlopen with unvalidated scheme (file:// / SSRF surface)

| Field | Value |
|--|--|
| **Issue Key** | BUG-S-002 |
| **Type** | Vulnerability |
| **Priority** | P2 |
| **Severity** | MEDIUM |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | commit0-harness, tools/discover |
| **Labels** | security, ssrf, file-scheme, input-validation |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | Any host running discovery/lint tooling |
| **CWE** | CWE-22 (Path Traversal via file://), CWE-918 (SSRF) |
| **Exploitable-by** | Whoever controls the URL input (operator config / repo metadata) |
| **CVSS v3.1** | `CVSS:3.1/AV:L/AC:H/PR:H/UI:N/S:U/C:L/I:N/A:N` → **2.4** |
| **Instrument** | [INSTRUMENTED: bandit 1.7.9 — B310 ×4] |

**Summary:** `urllib`'s `urlopen` is called on URLs whose scheme is not constrained to http(s); a `file://` (or other) scheme could read local files.

**Description (with code evidence):**
- `commit0/harness/lint_java.py:134`
- `tools/discover.py:151`
- `tools/discover_c.py:39`
- `tools/discover_go.py:40`

**Steps to Reproduce:**
1. Supply a `file:///etc/passwd`-style URL through whichever config/metadata feeds these `urlopen` calls.
2. Observe local file read instead of a network fetch.

**Expected Result:** Only `http`/`https` schemes are accepted.

**Actual Result:** Scheme is unvalidated before `urlopen`.

**Impact:** Local file disclosure / limited SSRF. Privilege required is high (most inputs are operator-controlled), hence Medium/HOLD.

**Suggested Fix:** One shared helper that validates `urlparse(url).scheme in {"http","https"}` before fetching (or switch to `requests` with an explicit allowlist); route all 4 sites through it.

**Acceptance Criteria:**
- [ ] All 4 sites validate scheme via the shared helper.
- [ ] `bandit --select B310` reports zero in the affected files.
- [ ] A `file://` URL is rejected with a clear error in a unit test.

---

## BUG-D-001 — Core dependencies pinned to moving git branches (non-reproducible builds)

| Field | Value |
|--|--|
| **Issue Key** | BUG-D-001 |
| **Type** | Bug |
| **Priority** | P2 |
| **Severity** | MEDIUM |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | packaging, dependencies |
| **Labels** | reproducibility, supply-chain, pinning |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | All installs / CI |
| **CWE** | CWE-1357 (Reliance on Insufficiently Trustworthy Component), CWE-829 (Inclusion of Functionality from Untrusted Control Sphere) |
| **Exploitable-by** | The fork maintainer (Ethara-Ai) — supply-chain alteration without version bump |
| **Instrument** | [INSTRUMENTED: uv / grep — CMD-035/036] |

**Summary:** Two core runtime dependencies resolve from `branch = "main"` on third-party forks, so installs days apart can ship different code.

**Description (with code evidence):**
- `pyproject.toml:168` — `aider-chat = {git = "https://github.com/Ethara-Ai/aider.git", branch = "main"}`
- `pyproject.toml:169` — `litellm = {git = "https://github.com/Ethara-Ai/litellm.git", branch = "main"}`

`uv.lock` pins a commit today, but any lock refresh re-resolves the branch HEAD.

**Steps to Reproduce:**
1. `grep -n "branch" pyproject.toml` → see the two `branch = "main"` git deps.
2. Refresh the lock (`uv lock --upgrade`) after the fork advances → resolved commit changes with no version bump.

**Expected Result:** Dependencies resolve to immutable revisions.

**Actual Result:** Moving branch refs on third-party forks.

**Impact:** Benchmark results must be comparable across time; non-deterministic deps undermine that and add supply-chain risk.

**Suggested Fix:** Pin to immutable `rev = "<sha>"` (or a tag). Keep `uv.lock` authoritative in CI via `uv sync --frozen`. Document the fork-divergence rationale.

**Acceptance Criteria:**
- [ ] No `branch = ...` git dependencies remain in `pyproject.toml`.
- [ ] CI uses `uv sync --frozen` (fails if lock is stale).
- [ ] A README/CONTRIBUTING note explains the fork pins.

---

## BUG-D-002 — Dependency CVE scan could not be performed (assessment limitation)

| Field | Value |
|--|--|
| **Issue Key** | BUG-D-002 |
| **Type** | Task |
| **Priority** | P3 |
| **Severity** | LOW |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | ci, dependencies |
| **Labels** | assessment-limitation, cve-scan, ci-gate |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | Audit sandbox (uv-managed cpython-3.13 on Darwin arm64) |
| **CWE** | N/A |
| **Exploitable-by** | N/A — this is an assessment gap, not a product defect |
| **Instrument** | [TOOL-BLOCKED: pip-audit 2.7.3 (ensurepip SIGABRT); osv-scanner (not uvx-installable)] |

**Summary:** No CVE scanner could run in the audit sandbox, so the known-vulnerability status of the dependency tree is UNKNOWN. This is disclosed as an assessment limitation, not as evidence of a vulnerability.

**Description (with code evidence):**
- `pip-audit 2.7.3` aborted with `ensurepip` SIGABRT while building its resolver venv under uv-managed cpython-3.13 (both `uv export` and `--no-deps` paths).
- `osv-scanner` is a Go binary and is not installable via `uvx`/PyPI in this sandbox.

**Steps to Reproduce:**
1. `uvx pip-audit@2.7.3` → SIGABRT during resolver venv creation.
2. `uvx osv-scanner` → not available on PyPI.

**Expected Result:** A CVE scan produces a clean/known result.

**Actual Result:** Both scanners blocked; CVE status unknown.

**Impact:** Confidence on the Dependencies axis is lowered. Not itself a vulnerability.

**Suggested Fix:** Run `pip-audit` (or `osv-scanner` standalone binary) in CI on a clean runner; the crash is environment-specific. Make it a merge-blocking job.

**Acceptance Criteria:**
- [ ] A CVE-scan job runs green in CI and is merge-blocking.
- [ ] Audit re-run records an INSTRUMENTED (not TOOL-BLOCKED) result for dependency CVEs.

---

## BUG-H-001 — Compiled binary committed to git (tools/gostubber/gostubber, 3.99 MB Mach-O)

| Field | Value |
|--|--|
| **Issue Key** | BUG-H-001 |
| **Type** | Task |
| **Priority** | P3 |
| **Severity** | LOW |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | tools/gostubber, repo-hygiene |
| **Labels** | hygiene, binary-artifact, git-bloat |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | All (binary is arm64 Mach-O — non-portable) |
| **CWE** | CWE-1164 (Irrelevant Code) |
| **Exploitable-by** | N/A |
| **Instrument** | [INSTRUMENTED: git verify-pack/rev-list — CMD-016] |

**Summary:** A 3.99 MB platform-specific compiled binary (`tools/gostubber/gostubber`, Mach-O arm64) is tracked in source control even though its Go source (`go.mod`) is present.

**Description (with code evidence):**
- `git rev-list --objects` + `verify-pack` (CMD-016): `tools/gostubber/gostubber` = 3.99 MB, file type Mach-O arm64 executable.

**Steps to Reproduce:**
1. `git ls-files tools/gostubber/gostubber` → tracked.
2. `file tools/gostubber/gostubber` → Mach-O 64-bit arm64 executable.

**Expected Result:** Build artifacts are produced from source, not versioned.

**Actual Result:** A non-portable binary blob is committed and permanently bloats `.git`.

**Impact:** Permanent `.git` bloat, non-portable (arm64 only), unreviewable in PRs (supply-chain/trust hazard).

**Suggested Fix:** `git rm --cached tools/gostubber/gostubber`, add to `.gitignore`, build from source in CI/setup. Consider `git-filter-repo` to purge history if `.git` size matters.

**Acceptance Criteria:**
- [ ] Binary is untracked and gitignored.
- [ ] Setup/CI builds gostubber from source.
- [ ] No Mach-O/ELF/PE executables in `git ls-files`.

---

## BUG-H-002 — 23 MB animated GIF + .docx committed (repo bloat, .git = 161 MB)

| Field | Value |
|--|--|
| **Issue Key** | BUG-H-002 |
| **Type** | Task |
| **Priority** | P3 |
| **Severity** | LOW |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | docs, repo-hygiene |
| **Labels** | hygiene, git-bloat, large-files, git-lfs |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | All (affects every clone) |
| **CWE** | CWE-1164 (Irrelevant Code) |
| **Exploitable-by** | N/A |
| **Instrument** | [INSTRUMENTED: git — CMD-015/016] |

**Summary:** A 23.3 MB animated GIF and binary office docs are committed; total `.git` is 161 MB.

**Description (with code evidence):**
- `docs/commit0.gif` = 23.3 MB (dominates clone time and `.git` size forever).
- `COMMIT0_*.docx` = 260 KB (binary, non-diffable).
- `.git` total = 161 MB.

**Steps to Reproduce:**
1. `du -sh .git` → 161 MB.
2. `git rev-list --objects --all | git cat-file --batch-check ... | sort -k3 -n` → `docs/commit0.gif` at top.

**Expected Result:** Large media hosted externally or via Git LFS; docs in markdown.

**Actual Result:** Large binaries tracked in history.

**Impact:** Slow clones and permanent bloat for a benchmark people clone repeatedly.

**Suggested Fix:** Move large media to Git LFS or external hosting (link from README); convert the GIF to hosted MP4/webm; replace `.docx` with markdown. `check-added-large-files` is already configured (the GIF predates it) — keep a strict cap.

**Acceptance Criteria:**
- [ ] No single tracked file > 1 MB (or all large media under LFS).
- [ ] README links to externally-hosted demo media.
- [ ] `.docx` removed in favour of markdown.

---

## BUG-Q-001 — 9,612 residual lint violations + broadly suppressed pyright type-checking

| Field | Value |
|--|--|
| **Issue Key** | BUG-Q-001 |
| **Type** | Tech-Debt |
| **Priority** | P3 |
| **Severity** | LOW |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | commit0, agent, tools |
| **Labels** | quality, lint, type-checking, maintainability |
| **Affects Version** | 0.1.8 |
| **Fix Version** | backlog |
| **Environment** | All |
| **CWE** | CWE-1164 (Irrelevant Code) |
| **Exploitable-by** | N/A |
| **Instrument** | [INSTRUMENTED: ruff 0.6.4 — CMD-026] |

**Summary:** 9,612 ruff violations remain, and pyright is broadly dialed down to `none`, so real signals are buried and type regressions pass silently.

**Description (with code evidence):**
- ruff 0.6.4 (CMD-026): 9,612 violations — 3,417 ANN001, 3,019 D102, 2,052 ANN201, **201 F401 (unused imports)**, **68 F841 (unused vars)**, 60 E402.
- `pyproject.toml [tool.pyright]`: `reportUnknownMemberType/ParameterType/ArgumentType/VariableType/MissingTypeStubs/PrivateUsage = "none"`.

**Steps to Reproduce:**
1. `uvx ruff@0.6.4 check commit0 agent tools --statistics`
2. Inspect `[tool.pyright]` in `pyproject.toml`.

**Expected Result:** Low lint count; meaningful type-checking enforced.

**Actual Result:** High-volume style noise hides real F401/F841; pyright suppressed.

**Impact:** Erodes maintainability of a long-lived multi-language harness; real signals buried.

**Suggested Fix:** Auto-fix F401/F841 first; incrementally re-enable pyright strictness per-module; make `ruff check` merge-blocking so the count cannot grow.

**Acceptance Criteria:**
- [ ] F401 and F841 counts are zero.
- [ ] `ruff check` is merge-blocking in CI.
- [ ] A pyright strictness ratchet is documented and at least one module is strict.

---

## BUG-C-001 — No coverage floor enforced + ruff version skew between pre-commit and pyproject

| Field | Value |
|--|--|
| **Issue Key** | BUG-C-001 |
| **Type** | Task |
| **Priority** | P3 |
| **Severity** | LOW |
| **Status** | Open |
| **Resolution** | Unresolved |
| **Disposition** | 🟡 HOLD |
| **Components** | ci, config |
| **Labels** | ci, coverage, version-skew, pre-commit |
| **Affects Version** | 0.1.8 |
| **Fix Version** | 0.1.9 |
| **Environment** | CI + contributor pre-commit |
| **CWE** | CWE-1164 (Irrelevant Code) |
| **Exploitable-by** | N/A |
| **Instrument** | [ABSENCE + INSTRUMENTED: grep CMD-069 / manual CMD-022] |

**Summary:** 116 test files exist but CI enforces no coverage minimum, and the pre-commit ruff (v0.6.1) lags the project's required ruff (>=0.6.4), causing rule-behaviour drift.

**Description (with code evidence):**
- ABSENCE: no `--cov`/coverage configuration in `pyproject.toml` (CMD-069 grep for `cov` → empty).
- `.pre-commit-config.yaml` ruff rev `v0.6.1` vs `pyproject` `ruff>=0.6.4` (CMD-022).

**Steps to Reproduce:**
1. `grep -in cov pyproject.toml` → no coverage config.
2. Compare ruff rev in `.pre-commit-config.yaml` vs `pyproject.toml`.

**Expected Result:** Coverage floor enforced; lint tool versions aligned.

**Actual Result:** No coverage gate; ruff version skew between local hooks and project requirement.

**Impact:** Coverage can silently regress; contributors and CI may lint with different rule behaviour.

**Suggested Fix:** Add `pytest-cov` with a coverage floor in CI; bump pre-commit ruff rev to match `pyproject` (>=0.6.4).

**Acceptance Criteria:**
- [ ] CI fails below the agreed coverage threshold.
- [ ] pre-commit ruff rev matches `pyproject` requirement.

---

## False Positives / Won't-Do (DO NOT RE-OPEN)

These items were evaluated against actual tool output and source, and verified safe. Each cites the disproving evidence so future audits/scanners do not re-file them. (Source: findings.json S-004, S-003, Q-002.)

| Item | Flagged by | Why it is NOT a bug (disproving evidence) | Disposition |
|--|--|--|--|
| `tarfile.extractall` path traversal | bandit B202 (HIGH) | `commit0/harness/docker_utils.py:100-129` wraps extraction in `safe_extract()` with an `is_within_directory()` guard that raises *"Attempted Path Traversal in Tar File"* before `extractall`. bandit cannot see the guard loop. | SHIP (S-004) |
| MD5 usage (agents_java.py:268) | bandit B324 (HIGH) | MD5 used only to hash content into an 8-char filename, not for security. | SHIP (S-004) |
| MD5 usage (scrape_pdf.py:303) | bandit B324 (HIGH) | Same — filename hashing only, not a security context. | SHIP (S-004) |
| `docker` "undefined name" (validate_java.py:111) | ruff F821 | Line 111 is a **quoted** forward-ref annotation `"docker.DockerClient"` that Python never evaluates; `docker` is lazily imported at line 91 inside `_get_runner()`. No runtime `NameError`. | NIT/SHIP (Q-002) |
| Dead code: `__exit__` params, pytest fixtures | vulture 2.11 (≥80%) | `exc_type/exc_val/exc_tb` are mandatory context-manager dunder params; mock fixtures are used by pytest injection. Not dead. | SHIP (no finding) |
| Secrets in code/history | (proactive scan) | git-history scan (CMD-041) found no `BEGIN PRIVATE`/`AKIA`/`sk-`/`ghp_`/bearer matches; code uses `os.environ.get(...)`; only `.env.example` tracked. | SHIP (S-003) |

---

*Generated from `findings.json`. Tickets cover all 9 BLOCK/HOLD findings. For full evidence, instrument transcripts, and methodology see `REPORT.md` (Appendix A/B/C).*
