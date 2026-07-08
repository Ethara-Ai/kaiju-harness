# Commit0 Rust Pipeline Runbook

**Updated**: 2026-06-25 (see latest changes below). Earlier additions (2026-06-24): blind-lint/blind-tests/names-only-tests, anti-leakage levers (`strip_aux_docs`, `strip_non_stubs`, `inject_test_files_readonly`), pass@k via `--num-samples`, `--skip-to-stage` resumption, and the multi-tier watchdog (`--inactivity-timeout`, `--max-wall-time`, `--stage-timeout`).

**Update 2026-06-25** — fixes for issues caught while running RGB-WG/rgb-core via the Claude Code OAuth bridge:
- **Dataset filename**: prep tool emits `<crate>_dataset.json` (no `-rs` suffix unless upstream repo name already has `-rs`, e.g. `discord/itsdangerous-rs`). Older docs/examples showing `<crate>-rs_dataset.json` were misleading.
- **`base_commit` now includes spec PDF**: `tools/prepare_repo_rust.py` previously committed the docs.rs spec PDF AFTER capturing `base_commit`, so the agent branched from a commit without the spec and silently fell back to README.md. Fixed at `tools/prepare_repo_rust.py:497` — `base_commit` is now updated after the spec commit, matching the README-fallback path.
- **Docker daemon preflight**: `run_pipeline_rust.sh` now runs `docker info` during preflight. If the daemon is unreachable (Docker Desktop not running), the pipeline fails fast instead of silently no-opping eval cycles and wasting LLM budget.
- **`get-tests` auto-fallback to `reference_commit`**: when `cargo test --list` fails on the stubbed `base_commit` (typical when upstream's `#![deny(unused_imports, unused_variables)]` policy catches stub-induced unused items), `commit0/cli_rust.py get-tests` now reads the dataset, checks out `reference_commit`, retries, and restores HEAD. Test IDs are identical between commits since the stubber preserves `#[test]` / `#[cfg(test)]` blocks.
- **`anthropic/claude-opus-4-8` model**: added to `scripts/generate_aider_config.sh` with `use_temperature: false` (Opus 4.8 rejects the `temperature` parameter). Other Claude models (4-7, sonnet-4-6, haiku-4-5) were already wired. Use with `--use-claude-code --model anthropic/claude-opus-4-8`.

Production guide for preparing custom Rust crates, building Docker environments, and running the **3-stage AI coding pipeline**. The Rust side of `kaiju-harness` mirrors the Python pipeline structure but uses Rust-native tooling: `cargo`, `syn`/`prettyplease` AST stubbing via the native `ruststubber` binary, and Docker-based eval via `commit0.repo.<crate>` images.

All commands assume you're in the project root and using `.venv/bin/python`.

---

## TL;DR — Run the Pipeline

```bash
# 1. One-time machine setup (per-host)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"
rustup default stable
rustup component add rustfmt clippy
.venv/bin/python -m pip install playwright PyMuPDF PyPDF2 beautifulsoup4
.venv/bin/python -m playwright install chromium
cd tools/ruststubber && cargo build --release && cd ../..
gh auth login   # or set GITHUB_TOKEN in .env

# 2. Per-repo prep (forks, stubs, scrapes spec, pushes branch, emits dataset)
.venv/bin/python -m tools.prepare_repo_rust \
    --repo OWNER/REPO \
    --crate CRATE_NAME \
    --src-dir src \
    --test-cmd "cargo test -p CRATE_NAME" \
    --org YOUR_GITHUB_ACCOUNT \
    --clone-dir ./repos_staging \
    --output CRATE_entries.json \
    --rust-version stable \
    --strip-docs           # optional: increase difficulty

# 3. Clone fork into working dir + collect test IDs
#    NOTE: the dataset file and the get-tests arg key off the REPO name (repo.split('/')[-1]),
#    NOT the crate name. See "Complete Copy-Paste Runbook" below for the worked example.
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./REPO_dataset.json --dataset-split test \
    --commit0-config-file .commit0_rust.yaml
.venv/bin/python commit0/cli_rust.py get-tests REPO_NAME   # repo name, e.g. rust-signals (not the crate)

# 4. Run 3-stage pipeline (Stage 1 draft → eval → Stage 2 lint → eval → Stage 3 test → eval)
set -a && source .env && set +a
bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    2>&1 | tee logs/MODEL_CRATE_full.log
```

For **harder benchmarks** (research mode), add the anti-leakage flags:

```bash
bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    --strip-aux-docs \
    --strip-non-stubs \
    --no-test-files-readonly \
    --blind-lint \
    --names-only-tests \
    --num-samples 3 \
    2>&1 | tee logs/MODEL_CRATE_hard.log
```

---

## Complete Copy-Paste Runbook — Claude Code Subscription (worked example: `Pauan/rust-signals`)

A fully self-contained sequence that a human **or an LLM** can execute top-to-bottom. The Claude Code
subscription (via the local OAuth bridge) is used **ONLY** for the final agent/trajectory run in Step 6 —
every prep step (1–4) uses GitHub + cargo + Docker with **zero LLM calls**. Substitute your own
`OWNER/REPO` / `CRATE` / fork-org for other crates.

**Worked values used below**

| Thing | Value | Note |
|---|---|---|
| upstream repo | `Pauan/rust-signals` | passed to `--repo` |
| crate | `futures-signals` | the package name in `Cargo.toml`; passed to `--crate` and `-p` |
| fork org | `Aman-Yadav-Ethara-AI` | passed to `--org` |
| **dataset file** | `rust-signals_dataset.json` | named after the **repo** (`rust-signals`), **NOT** the crate |
| **test-IDs key / `get-tests` arg** | `rust-signals` | the **repo last-path component**, **NOT** the crate — must match `repo.split('/')[-1]` or eval reports 0/0 |
| edition | `2018` | auto-detected from `Cargo.toml`; pass `--edition` to be explicit |

> ⚠️ **Two naming gotchas** that bite every new crate:
> 1. The dataset and the `get-tests` argument key off the **repo name**, not the crate name (e.g. repo
>    `rust-signals` → `rust-signals_dataset.json` and `get-tests rust-signals`, even though the crate is
>    `futures-signals`). Mismatch → the eval aggregator can't find the test-ID inventory and reports `0/0`.
> 2. `tools.prepare_repo_rust` **strips doc comments by default** (harder benchmark). Pass `--keep-docs`
>    to preserve them.

### Step 0 — Prerequisites (one-time per machine)

```bash
# Rust toolchain + components
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env" && rustup default stable && rustup component add rustfmt clippy
# Native AST stubber
cd tools/ruststubber && cargo build --release && cd ../..
# Spec-scrape deps
.venv/bin/python -m pip install playwright PyMuPDF PyPDF2 beautifulsoup4 && .venv/bin/python -m playwright install chromium
# GitHub auth (env GITHUB_TOKEN or `gh auth login` keyring fallback)
gh auth login
# Claude Code login (provides the bridge's OAuth token) — verify:
security find-generic-password -s "Claude Code-credentials" -w | head -c 16 ; echo
# Docker Desktop installed and runnable (macOS: started in Step 4)
```

### Step 1 — Prepare the repo (fork → stub → scrape docs.rs spec → push → dataset). No LLM.

```bash
set -a && source .env && set +a
.venv/bin/python -m tools.prepare_repo_rust \
    --repo Pauan/rust-signals \
    --crate futures-signals \
    --src-dir src \
    --test-cmd "cargo test -p futures-signals" \
    --org Aman-Yadav-Ethara-AI \
    --clone-dir ./repos_staging \
    --output futures-signals_entries.json \
    --rust-version stable \
    --edition 2018
# Add --keep-docs to preserve doc comments (easier benchmark). Default STRIPS them.
# Emits: rust-signals_dataset.json + .commit0_rust.yaml; forks to Aman-Yadav-Ethara-AI/rust-signals
#        and pushes the commit0_all branch (base = stubbed code incl. spec.pdf.bz2).
```

### Step 2 — Clone the fork into the working dir. No LLM.

```bash
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./rust-signals_dataset.json --dataset-split test \
    --commit0-config-file .commit0_rust.yaml
# Result: repos/rust-signals/ on the commit0 branch at base_commit.
```

### Step 3 — Collect test IDs (argument = **repo name**). No LLM.

```bash
.venv/bin/python commit0/cli_rust.py get-tests rust-signals
# Writes commit0/data/rust_test_ids/rust-signals.{json,bz2}. Auto-falls back to reference_commit
# if `cargo test --list` won't compile on the stubbed base (expected for deny(unused) crates).
```

### Step 4 — Build Docker images (base + per-crate). No LLM. Requires Docker running.

```bash
# macOS: start Docker Desktop and wait until the daemon answers.
docker info >/dev/null 2>&1 || { open -a Docker; until docker info >/dev/null 2>&1; do sleep 3; done; }
.venv/bin/python commit0/cli_rust.py build --commit0-config-file .commit0_rust.yaml --num-workers 2
# Produces commit0.base.rust:latest (~3.6 GB, reused across crates) and
# commit0.repo.<crate-token>.<hash>:v0 for this crate.
```

### Step 5 — Start the Claude Code bridge (the ONLY subscription touchpoint).

```bash
eval "$(scripts/claude_code_bridge.sh start | grep ^export)"   # exports ANTHROPIC_API_BASE=http://127.0.0.1:8765
curl -s http://127.0.0.1:8765/healthz | jq .                   # {"ok": true, "token_prefix": "sk-ant-oat01-..."}
```

### Step 6 — Run the full 3-stage pipeline + eval (the trajectory run). Uses Claude Code.

```bash
bash run_pipeline_rust.sh \
    --use-claude-code \
    --model opus48cc \
    --dataset ./rust-signals_dataset.json \
    --repo-split all \
    --max-iteration 1 \
    --per-edit-compile-gate \
    --quality-watchdog \
    2>&1 | tee logs/opus48cc_rust-signals.log
```

| Flag | Why |
|---|---|
| `--use-claude-code` | route `anthropic/*` through the local bridge (subscription) |
| `--model opus48cc` | preset → `anthropic/claude-opus-4-8` via the bridge |
| `--max-iteration 1` | aider iterations per file per stage (raise for more refinement) |
| `--per-edit-compile-gate` | `cargo check` after each edit; revert + re-prompt on regression |
| `--quality-watchdog` | kill the agent if compile-error count keeps rising |

Optional hardening (omit for the easiest tier): `--no-spec-info` (keep the bridge strictly to coding turns —
skips the spec-summarization LLM call), or anti-leakage flags `--strip-aux-docs --no-test-files-readonly
--blind-lint --names-only-tests`.

### Step 7 — Read results & trajectory.

```bash
jq . logs/pipeline_rust_claude-opus-4.8_rust-signals_dataset_results.json
# Trajectories: logs/agent/rust-signals_dataset/claude-opus-4.8/run_1/stage{1,2,3}/<module>/  (trajectory.md, output.json, aider.log)
scripts/claude_code_bridge.sh stop   # shut the bridge down when finished
```

---

## Pipeline Architecture

```
                       COMMIT0 RUST DATASET PIPELINE
                       =============================

    PREPARATION (ad-hoc per crate)                EXECUTION (run_pipeline_rust.sh)
    ====================================          ============================================

    +---------------------+
    | tools/prepare_repo_ |  Forks upstream, clones, AST-stubs source,
    | rust.py             |  scrapes docs.rs spec PDF, pushes commit0_all branch,
    |                     |  emits CRATE-rs_dataset.json + .commit0_rust.yaml.
    +-----+----+----------+
          |    |
          |    | invokes
          |    v
          |  +-------------------------------+
          |  | tools/ruststubber (binary)    |  syn::Fold-based stubber:
          |  |                               |    - replaces fn bodies with panic!("STUB:...")
          |  |  --strip-docs (optional)      |    - preserves fn main(), #[test], #[cfg(test)]
          |  |  --in-place / --output-dir    |    - cross-file scan of #[cfg(test)] mod chain
          |  |                               |    - uses `cargo metadata` for crate roots
          |  +-------------------------------+
          v
    +-----+--------------+
    | commit0 setup_rust |  Clones fork from your GitHub account into
    |                    |  repos/<crate>/, checks out commit0 branch
    +-----+--------------+  at base_commit (stubbed code).
          |
          v
    +-----+--------------+
    | get-tests          |  Runs cargo test --list inside repos/<crate>,
    |                    |  saves both <crate>.json + <crate>.bz2 in
    +-----+--------------+  commit0/data/rust_test_ids/
          |
          v

    +---------------------------------------------------------------+
    | run_pipeline_rust.sh   3-stage orchestrator (pass@k support)  |
    |                                                                |
    |  +---------------------------+                                 |
    |  | preflight                  |  Validates jq, bc, cargo,       |
    |  |                            |  rustc, docker; probes model    |
    |  |                            |  API; auto-resolves Docker.app  |
    |  +-------------+--------------+  on macOS.                      |
    |                |                                                |
    |                v                                                |
    |  +---------------------------+                                 |
    |  | ensure_spec_docs_rust     |  Provisions spec.pdf.bz2 per    |
    |  |                            |  repo (scrape or cache hit)     |
    |  +-------------+--------------+                                 |
    |                |                                                |
    |                v                                                |
    |  +---------------------------+                                 |
    |  | run_build_once            |  Idempotent. Builds Docker      |
    |  | (pre-eval guard)          |  base + per-crate images via    |
    |  +-------------+-------------+  commit0/cli_rust.py build.     |
    |                |                                                |
    |                v   (loop: for each sample 1..NUM_SAMPLES)       |
    |  +---------------------------+                                 |
    |  | STAGE 1: Draft            |  agent runs locally; no test    |
    |  | run_tests=false           |  feedback; sees source+spec.    |
    |  | use_unit_tests_info=true  |                                 |
    |  | use_spec_info=true        |                                 |
    |  +-------------+-------------+                                 |
    |                |                                                |
    |                v                                                |
    |        [ commit0 evaluate ]   Runs cargo test in Docker.       |
    |                |                                                |
    |                v                                                |
    |  +---------------------------+                                 |
    |  | STAGE 2: Lint Refine      |  cargo clippy feedback (full    |
    |  | use_lint_info=true        |  or blind based on flag).       |
    |  | run_tests=false           |                                 |
    |  +-------------+-------------+                                 |
    |                |                                                |
    |                v                                                |
    |        [ commit0 evaluate ]                                    |
    |                |                                                |
    |                v                                                |
    |  +---------------------------+                                 |
    |  | STAGE 3: Test Refine      |  cargo test feedback per file   |
    |  | run_tests=true            |  (full / names-only / blind).   |
    |  | use_lint_info=true        |  Most impactful stage.          |
    |  +-------------+-------------+                                 |
    |                |                                                |
    |                v                                                |
    |        [ commit0 evaluate ]   Final pass rate.                 |
    +---------------------------------------------------------------+
                |
                v
    +-----------+-----------+
    | output/<repo>/<model>/results.json     Per-stage pass rates, costs, timings
    | logs/pipeline_*_results.json           Multi-sample pass@k aggregate
    +------------------------+
```

---

## End-to-End Execution Flow (Step-by-Step)

The pipeline (`run_pipeline_rust.sh`) executes the following phases:

### Phase 0 — Argument Parsing & Resolution (lines 1–278)

1. Source `.env` so credentials are exported.
2. Run `scripts/generate_aider_config.sh` to materialise `.aider.model.settings.yml` from your provider env vars.
3. Parse all CLI flags (model, dataset, anti-leakage levers, watchdog timeouts).
4. Source `commit0/harness/resolve_model.sh`; call `resolve_model` to map preset → full model ID + `MODEL_SHORT`.
5. **Bedrock priority**: if `MODEL_NAME == bedrock/*` and `AWS_BEARER_TOKEN_BEDROCK` is set, unset IAM credentials so litellm cannot fall back to SigV4 signing.
6. Resolve dataset: filename pattern, full path, or known split name; extract `REPO_SPLIT`.
7. Build branch name `<MODEL_SHORT>-<DATASET_SHORT>-rust` and per-sample log directories.

### Phase 1 — Preflight (lines 320–447, `preflight()`)

- Validates `jq`, `bc`, `timeout`, `cargo`, `rustc`, `docker` on PATH (auto-resolves `/Applications/Docker.app/Contents/Resources/bin/` on macOS).
- Verifies API credentials per provider (Vertex / Bedrock / OpenAI).
- Probes model with a tiny request (`PROBE_TIMEOUT=30`s by default).
- Confirms dataset JSON parses and `REPO_SPLIT` matches at least one entry.

### Phase 2 — Spec Provisioning (lines 483–606)

- `ensure_spec_docs_rust()` walks every repo in the dataset and confirms `spec.pdf.bz2` exists. Tries (in order): repo working dir → `specs/<repo>/spec.pdf.bz2` cache → live scrape from `setup.specification` URL.
- `verify_spec_docs_rust()` is fatal if any repo is missing the spec AND `USE_SPEC_INFO=true`.

### Phase 3 — Docker Build (lines 987–1019, `run_build_once()`)

- Gated by `_pipeline_build_done` flag (idempotent).
- Calls `commit0/cli_rust.py build` which loads dataset, dedupes by image hash, delegates per-crate to `commit0/harness/docker_build_rust.py`.
- Produces `commit0.base.rust:latest` (~3.6 GB) and one `commit0.repo.<crate>.<hash>:v0` (~3.6 GB) per unique crate.

### Phase 4 — Per-Sample 3-Stage Loop (`run_single_sample()` lines 1568–1702)

For each sample `1..NUM_SAMPLES`:

#### Stage 1 — Draft (lines 1242–1294)

| Setting | Value |
|---|---|
| `run_tests` | `false` |
| `use_lint_info` | `false` |
| `use_spec_info` | `true` (default) |
| `use_unit_tests_info` | `true` (default) |

Agent receives system prompt + function stubs + full source of stubbed files + spec PDF text (if available). No test or lint feedback. Aider iterates **up to `max_iteration` times per file**.

#### Stage 2 — Lint Refine (lines 1296–1349)

| Setting | Value |
|---|---|
| `run_tests` | `false` |
| `use_lint_info` | `true` |
| `run_entire_dir_lint` | `true` |
| `blind_lint` | depends on `--blind-lint` flag |

Agent reads `cargo clippy --all-targets --all-features -- -D warnings` output. If `--blind-lint` is set, agent only sees `"build clean"` or `"build failed: N compile errors"` (no error positions/messages).

#### Stage 3 — Test Refine (lines 1351–1409)

| Setting | Value |
|---|---|
| `run_tests` | `true` |
| `use_lint_info` | `true` (unless `--no-stage3-lint`) |
| `blind_tests` | depends on `--blind-tests` flag |
| `names_only_tests` | depends on `--names-only-tests` flag |

Agent runs `cargo test` and refines based on output. Three feedback levels (highest information first):

1. **Full output** (default): traceback, assertion messages, test names — everything the developer sees.
2. **Names-only** (`--names-only-tests`): `5/47 tests failed:\n- test_foo\n- test_bar` — no tracebacks.
3. **Blind** (`--blind-tests`): only the summary line `test result: ok. 42 passed; 5 failed;` — no test names at all.

After each stage, `commit0/cli_rust.py evaluate --branch $BRANCH_NAME --timeout 300` is invoked. Results merged into `RESULTS_JSON`.

### Phase 5 — Multi-Sample Aggregation (lines 1715–1767, `print_pass_at_k_summary`)

If `--num-samples > 1`:
- All samples write independent results files (`logs/pipeline_<RUN_ID>_sample<N>_results.json`).
- Final summary prints per-sample table and `best_s3_rate` (best Stage-3 pass rate across samples).

### Phase 6 — Cleanup (`cleanup()`, EXIT trap, lines 1532–1557)

- Kills lingering agent processes if the watchdog tripped.
- Restores auxiliary docs if `--strip-aux-docs` was used.
- Deletes per-sample config files on success (preserved on failure for debugging).

---

## Method A: Step-By-Step (Recommended for First Run)

### Step 1: Machine setup (one-time)

```bash
# Rust toolchain (any modern stable works; getifs and similar crates need >=1.85)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"
rustup default stable
rustup component add rustfmt clippy
cargo install cargo-nextest --locked   # optional, used by Stage 3 if available

# Build the AST stubber
cd tools/ruststubber && cargo build --release && cd ../..

# Spec-scrape Python deps (used by prepare_repo_rust.py)
.venv/bin/python -m pip install playwright PyMuPDF PyPDF2 beautifulsoup4
.venv/bin/python -m playwright install chromium

# GitHub auth (the prep tool falls back to gh keyring if .env GITHUB_TOKEN is stale)
gh auth login

# Verify Docker is reachable. On macOS the CLI sometimes lives outside the default PATH:
docker --version
# If "command not found", check /Applications/Docker.app/Contents/Resources/bin/docker
# The pipeline auto-resolves this at preflight; for ad-hoc commands, symlink:
ln -sf /Applications/Docker.app/Contents/Resources/bin/docker ~/.local/bin/docker
export PATH="$HOME/.local/bin:$PATH"
```

### Step 2: Prepare the repo

```bash
.venv/bin/python -m tools.prepare_repo_rust \
    --repo OWNER/REPO \
    --crate CRATE_NAME \
    --src-dir src \
    --test-cmd "cargo test -p CRATE_NAME" \
    --org YOUR_GITHUB_ACCOUNT \
    --clone-dir ./repos_staging \
    --output CRATE_entries.json \
    --rust-version stable

# Optional flags:
#   --strip-docs         strip every #[doc] attribute (harder benchmark)
#   --skip-spec          skip docs.rs scrape (uses README fallback)
#   --rust-version 1.85  pin a specific toolchain version in metadata
#   --dry-run            no fork, no push (debugging stub output)
```

What this produces:

| Artifact | Path | Purpose |
|---|---|---|
| Entries JSON | `CRATE_entries.json` | Raw entry dict (commit SHAs, metadata) |
| Dataset JSON | `CRATE-rs_dataset.json` | Final dataset array (consumed by setup/eval) |
| Commit0 config | `.commit0_rust.yaml` | Stateful config (dataset path, base_dir, split) |
| Spec PDF | `repos_staging/CRATE-rs/spec.pdf.bz2` | docs.rs scrape, also at `specs/spec.pdf.bz2` |
| Fork | `YOUR_ORG/REPO-rs` branch `commit0_all` | base + reference + spec commits pushed to GitHub |

### Step 3: Verify the entries JSON before continuing

```bash
cat CRATE_entries.json | python -m json.tool | grep -E '"src_dir"|"rust_version"|"edition"|"test_cmd"|"specification"'
```

Common pitfalls to check:

| Field | What to verify |
|---|---|
| `src_dir` | Matches actual source directory (single-crate: `src`; workspace member: `member/src`) |
| `setup.rust_version` | Matches what `rustc --version` reports on your machine, OR set `RUST_VERSION_STRICT=false` |
| `setup.edition` | Matches Cargo.toml `[package].edition` (auto-detected) |
| `test.test_cmd` | Should run only your crate's tests (`cargo test -p CRATE_NAME`); blanket `cargo test` may compile the whole workspace |
| `test.test_dir` | Should be `tests` for standard layout (older entries may say crate name) |
| `setup.specification` | Defaults to `https://docs.rs/CRATE`; check that docs.rs has a published version |

### Step 4: commit0 setup (clone fork into working dir)

```bash
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./CRATE-rs_dataset.json \
    --dataset-split test \
    --commit0-config-file .commit0_rust.yaml
```

Output: `repos/<crate>-rs/` on `commit0` branch pointing at the stubbed base_commit, with `.gitignore` entries (`target/`, `.aider*`, `logs/`) added.

### Step 5: Collect test IDs

```bash
# Argument is the REPO name (repo.split('/')[-1]), e.g. `rust-signals` — NOT the crate name.
.venv/bin/python commit0/cli_rust.py get-tests REPO_NAME
```

Runs `cargo test --list` inside `repos/<repo_name>/`, writes both:
- `commit0/data/rust_test_ids/CRATE_NAME.json` (human-readable; agent uses this preferentially)
- `commit0/data/rust_test_ids/CRATE_NAME.bz2` (compressed; canonical cache format)

A warning is shown if cargo fails (e.g., dependency MSRV mismatch) — fix the underlying issue before continuing.

### Step 6: Configure the model

```bash
# Vertex Gemini (easiest if you have a GCP project)
echo "VERTEX_AI_API_KEY=your_key" >> .env
echo "VERTEXAI_LOCATION=global" >> .env

# OR Vertex Claude (requires service account JSON)
echo "GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json" >> .env
echo "VERTEXAI_LOCATION=global" >> .env

# OR Bedrock (Claude Opus 4.6, Kimi K2.5, GLM 5, MiniMax M2.5)
echo "AWS_BEARER_TOKEN_BEDROCK=..." >> .env
echo "AWS_DEFAULT_REGION=us-east-1" >> .env
```

### Step 7: Run the pipeline

```bash
set -a && source .env && set +a

bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    2>&1 | tee logs/gemini25_CRATE_full.log

# Stage 1 only (just the trajectory):
bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    --skip-to-stage 1 \
    2>&1 | tee logs/gemini25_CRATE_stage1.log

# Resume an interrupted run from Stage 2:
bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    --branch gemini25-CRATE-rust \
    --skip-to-stage 2 \
    2>&1 | tee logs/gemini25_CRATE_resume.log

# pass@3 study (only works with single-stage runs):
bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --num-samples 3 \
    2>&1 | tee logs/gemini25_CRATE_pass3.log
```

Output lands in `output/<repo>/<model>/stage{1,2,3}_*/` with `trajectory.md`, `output.json`, and `aider.log` per module.

---

## Anti-Leakage System (CRITICAL for Research Validity)

The Rust pipeline ships with multiple **anti-leakage levers** to control what the agent can observe during code generation. These are essential for reproducible benchmark results.

### The 6 Anti-Leakage Flags

All flags default to **disabled** (most lenient setting — easiest benchmark). Enable them to harden the evaluation.

| Flag | CLI | Stage Affected | What It Hides |
|---|---|---|---|
| `strip_aux_docs` | `--strip-aux-docs` | All stages | README, CHANGELOG, HISTORY, AUTHORS, NOTICE files |
| `strip_non_stubs` | `--strip-non-stubs` | All stages | Source files that don't contain `panic!("STUB:...")` at base_commit |
| `inject_test_files_readonly` | `--no-test-files-readonly` | All stages | Test file bodies (assertions/expected values) — agent cannot read tests |
| `blind_lint` | `--blind-lint` | Stage 2 | Detailed clippy output → only "build clean" / "build failed: N errors" |
| `blind_tests` | `--blind-tests` | Stage 3 | Per-test failures → only summary line "test result: ok. 42 passed; 5 failed" |
| `names_only_tests` | `--names-only-tests` | Stage 3 | Tracebacks → only failed test names + counts |

> All flags are defined in `agent/class_types.py:37-44` and wired through `run_pipeline_rust.sh` lines 36–44, 95–102. They flow to `agent/run_rust_agent.py` (lines 179–228 for the blind shell commands).

### Information-Visibility Matrix

```
            +-----------------+-----------------+-----------------+
            | Stage 1 (Draft) | Stage 2 (Lint)  | Stage 3 (Test)  |
+-----------+-----------------+-----------------+-----------------+
| Spec PDF  | ✓ (default)     | ✓               | ✓               |
| Stub fns  | ✓ always        | ✓ always        | ✓ always        |
| Other src | ✓ (unless       | ✓ (unless       | ✓ (unless       |
|           |   --strip-non-  |   --strip-non-  |   --strip-non-  |
|           |   stubs)        |   stubs)        |   stubs)        |
| Test fns  | ✓ (unless       | ✓ (unless       | ✓ (unless       |
| (bodies)  |   --no-test-    |   --no-test-    |   --no-test-    |
|           |   files-readonly|   files-readonly|   files-readonly|
| Docs      | ✓ (unless       | ✓ (unless       | ✓ (unless       |
| (READMEs) |   --strip-aux-  |   --strip-aux-  |   --strip-aux-  |
|           |   docs)         |   docs)         |   docs)         |
| Lint out  | ✗ (not run)     | ✓ full          | ✓ full          |
|           |                 | (or summary if  | (unless --no-   |
|           |                 |   --blind-lint) |   stage3-lint)  |
| Test out  | ✗ (not run)     | ✗ (not run)     | ✓ full / names- |
|           |                 |                 |   only / blind  |
+-----------+-----------------+-----------------+-----------------+
```

### Blind Mode Shell Commands

When `--blind-lint` is set, the pipeline replaces the lint command with this shell wrapper (`agent/run_rust_agent.py:179-187`):

```bash
_out=$(cargo clippy --all-targets --all-features -- -D warnings 2>&1)
_rc=$?
if [ $_rc -eq 0 ]; then echo "build clean"
else _n=$(printf "%s" "$_out" | grep -cE "^error\[E[0-9]+\]" 2>/dev/null)
     [ -z "$_n" ] && _n=0
     printf "build failed: %s compile errors\n" "$_n"
fi
exit $_rc
```

When `--blind-tests` is set (`agent/run_rust_agent.py:189-198`):

```bash
_out=$(cargo test --all-features 2>&1)
_rc=$?
_summary=$(printf "%s" "$_out" | grep -E "^test result:" | tail -1)
if [ -n "$_summary" ]; then printf "%s\n" "$_summary"
else _n=$(printf "%s" "$_out" | grep -cE "^error\[E[0-9]+\]" 2>/dev/null)
     [ -z "$_n" ] && _n=0
     printf "compilation failed: %s errors\n" "$_n"
fi
exit $_rc
```

When `--names-only-tests` is set (`agent/run_rust_agent.py:210-223`):

```bash
_out=$(cargo test --all-features 2>&1)
_rc=$?
if [ $_rc -eq 0 ]; then printf "tests pass\n"
else
  _failed=$(printf "%s" "$_out" | sed -nE "s/^test (.+) \.\.\. FAILED$/- \1/p")
  _n_failed=$(printf "%s" "$_failed" | grep -cE "^- " 2>/dev/null); _n_failed=${_n_failed:-0}
  _n_passed=$(printf "%s" "$_out" | grep -oE "[0-9]+ passed" | head -1 | cut -d" " -f1); _n_passed=${_n_passed:-0}
  _total=$((_n_failed + _n_passed))
  if [ -n "$_failed" ]; then printf "%s/%s tests failed:\n%s\n" "$_n_failed" "$_total" "$_failed"
  elif [ "$_n_passed" -gt 0 ]; then printf "tests pass (non-zero rc): %s passed, rc=%s\n" "$_n_passed" "$_rc"
  else printf "tests failed (no per-test names parsed): rc=%s\n" "$_rc"
  fi
fi
exit $_rc
```

### Difficulty Tiers

| Tier | Flags | Description | Expected pass rate vs default |
|---|---|---|---|
| **Easiest (default)** | None | Full visibility, docs intact, full test/lint feedback | 100% baseline |
| **Moderate** | `--blind-lint` `--names-only-tests` | Hides line numbers + tracebacks but agent still knows what's failing | -10 to -20% |
| **Hard** | `--strip-aux-docs` `--no-test-files-readonly` `--blind-tests` | No docs, no test bodies, no test output detail | -40 to -60% |
| **Maximum** | All flags + `--strip-non-stubs` + prep `--strip-docs` | Maximum information hiding | -60 to -80% |

---

## Prompt System

### System Prompt File

`agent/prompts/rust_system_prompt.md` — Rust-specific system prompt with three placeholders:

- `{repo_name}` — Repository name
- `{function_list}` — Bulleted list of stub functions (format: `- \`<rel_path>\` line <N>: \`<SIGNATURE>\``)
- `{file_context}` — Full source code of files containing stubs

### Prompt Construction (`agent/run_rust_agent.py:82-176`)

1. **Discover target files** via `get_target_edit_files_rust()` — finds files containing `panic!("STUB: not implemented")` at base_commit (line 317).
2. **Extract stubs** with `extract_rust_function_stubs()` — regex-based signature extraction (line 22, 99–102).
3. **Build function list**: `- \`<rel_path>\` line <LINE>: \`<SIGNATURE>\`` for each stub.
4. **Concatenate target file source** into `file_context` (lines 106–116).
5. **Load template** from `rust_system_prompt.md` (line 119).
6. **Render** `template.format(repo_name=..., function_list=..., file_context=...)` (lines 124–128).
7. **Append spec PDF text** if `use_spec_info=true` (lines 134–157). If output > `spec_summary_max_tokens` (default 4000), run an LLM summarization pass first.
8. Return `(formatted_message, spec_costs)`.

### System Prompt Injection (`agent/agents_rust.py:116-140`)

Base prompt is augmented with stage-specific tail text depending on `inject_test_files_readonly`:
- **True (default)**: Tells agent test files are read-only reference material; focus on implementation.
- **False**: Spec-driven instructions only; explicitly notes test access is unavailable.

### What Is *Not* Used

- **No few-shot examples**: Prompt contains zero in-context demos.
- **No RAG/retrieval**: Only the spec PDF and (optionally) README are injected. No vector search, no dynamic doc retrieval.
- **No conversation pruning**: Full chat history retained across iterations.

---

## Dataset JSON Schema

```json
[
  {
    "instance_id": "commit-0/itsdangerous",
    "repo": "Aman-Yadav-Ethara-AI/itsdangerous-rs",
    "original_repo": "discord/itsdangerous-rs",
    "base_commit": "f85caa4fe082d415cff354496a41a8b0d65cf810",
    "reference_commit": "43b5469d8400562810c6ad86b8d3cc66e61d0612",
    "setup": {
      "rust_version": "stable",
      "edition": "2018",
      "packages": "pkg-config libssl-dev",
      "pre_install": [],
      "install": "cargo fetch",
      "specification": "https://docs.rs/itsdangerous",
      "version_source": "Cargo.toml[package.rust-version]",
      "version_conflicts": []
    },
    "test": {
      "test_cmd": "cargo test -p itsdangerous",
      "test_dir": "tests"
    },
    "src_dir": "src",
    "language": "rust"
  }
]
```

### Field reference

| Field | Type | Description |
|---|---|---|
| `instance_id` | str | Unique ID `commit-0/<crate_name>` |
| `repo` | str | Fork location: `<your_org>/<repo_name>` |
| `original_repo` | str | Upstream repo: `<owner>/<repo_name>` |
| `base_commit` | str | SHA of the stubbed commit |
| `reference_commit` | str | SHA of the original working code |
| `setup.rust_version` | str | rustc version (or "stable") |
| `setup.edition` | str | Cargo.toml `[package].edition` |
| `setup.packages` | str | Apt packages installed in Docker image |
| `setup.pre_install` | list[str] | Shell commands run before install |
| `setup.install` | str | Install command (typically `cargo fetch`) |
| `setup.specification` | str | docs.rs URL for spec PDF scraping |
| `setup.version_source` | str | Where rust_version came from |
| `setup.version_conflicts` | list[str] | Detected conflicts between rust-version sources |
| `test.test_cmd` | str | Test runner (typically `cargo test -p <crate>`) |
| `test.test_dir` | str | `tests` for standard layout, workspace-member dir for workspaces |
| `src_dir` | str | Source dir relative to repo root |
| `language` | str | `"rust"` |

---

## Configuration Reference

### `.commit0_rust.yaml` (auto-generated by prep tool)

```yaml
# commit0 Rust config for <crate>
dataset_name: ./<crate>-rs_dataset.json
dataset_split: test
repo_split: all
base_dir: repos

# Repo details (comments preserved):
# upstream: <owner>/<repo>
# fork: <your_org>/<repo>
# crate: <crate>
# language: rust
# test_cmd: cargo test -p <crate>
# src_dir: src
```

### `.agent.yaml` Schema (`AgentConfig` in `agent/class_types.py`)

All anti-leakage flags have safe defaults. New fields MUST have defaults to maintain backward compatibility.

| Field | Type | Default | Purpose |
|---|---|---|---|
| `agent_name` | str | (required) | Currently only `"aider"` supported |
| `model_name` | str | (required) | Full litellm model ID (e.g., `vertex_ai/gemini-2.5-pro`) |
| `model_short` | str | `""` | Client-safe short name (e.g., `"opus4.6"`) for log paths |
| `use_user_prompt` | bool | (required) | Use custom prompt instead of default template |
| `user_prompt` | str | (required) | Override prompt text (if above true) |
| `use_topo_sort_dependencies` | bool | (required) | Process files in topological dependency order |
| `add_import_module_to_context` | bool | (required) | Auto-include imported modules in context |
| `use_repo_info` | bool | (required) | Include repo overview in prompt |
| `max_repo_info_length` | int | (required) | Char cap for repo info |
| `use_unit_tests_info` | bool | (required) | Inject inline-test bodies in prompt (Stage 1 only) |
| `max_unit_tests_info_length` | int | (required) | Char cap for unit tests info |
| `use_spec_info` | bool | (required) | Inject spec PDF text |
| `max_spec_info_length` | int | (required) | Char cap for spec info |
| `use_lint_info` | bool | (required) | Show lint output to agent |
| `run_entire_dir_lint` | bool | (required) | Lint the whole directory vs single file |
| `max_lint_info_length` | int | (required) | Char cap for lint output |
| `pre_commit_config_path` | str | (required) | Path to `.pre-commit-config.yaml` |
| `run_tests` | bool | (required) | Run tests as agent feedback (Stage 3) |
| `max_iteration` | int | (required) | Max aider iterations per file per stage |
| `record_test_for_each_commit` | bool | (required) | Save per-commit test results |
| `cache_prompts` | bool | `True` | Enable litellm prompt caching |
| `spec_summary_max_tokens` | int | `4000` | LLM-summarize spec if longer |
| `max_test_output_length` | int | `15000` | Char cap for test output passed to agent |
| `capture_thinking` | bool | `False` | Persist reasoning tokens (extended thinking models) |
| `trajectory_md` | bool | `True` | Write `trajectory.md` per module |
| `output_jsonl` | bool | `False` | Write `output.jsonl` per module |
| `repo_map_tokens` | int | `1024` | Aider auto repo-map budget; 0 disables |
| `strip_aux_docs` | bool | `False` | **Anti-leakage**: hide README/CHANGELOG/etc from agent |
| `blind_lint` | bool | `False` | **Anti-leakage**: Stage 2 sees only "build failed: N errors" |
| `blind_tests` | bool | `False` | **Anti-leakage**: Stage 3 sees only summary line |
| `names_only_tests` | bool | `False` | **Anti-leakage**: Stage 3 sees only failed test names + counts |
| `strip_non_stubs` | bool | `False` | **Anti-leakage**: hide non-stubbed source files |
| `inject_test_files_readonly` | bool | `True` | If False, test bodies NOT injected as read-only aider context |

### `.env` (user-managed)

```bash
# GitHub
GITHUB_TOKEN=ghp_...   # optional; gh keyring is the fallback

# Vertex AI (any one path is sufficient for Gemini)
VERTEX_AI_API_KEY=...
VERTEXAI_LOCATION=global

# For Vertex Claude or any model
GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json

# Bedrock
AWS_BEARER_TOKEN_BEDROCK=...
AWS_DEFAULT_REGION=us-east-1
# OR
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...

# Pipeline-side tunables (all optional)
RUST_VERSION=1.85.0                      # pin toolchain in dataset metadata
RUST_VERSION_STRICT=false                # fail preflight on rustc mismatch
WATCHDOG_MTIME_FALLBACK_MIN_SECS=3600    # min wall-time cap when mtime probe broken (macOS)
SPEC_DEPS_AUTO_INSTALL=true              # auto-pip-install spec scrape deps
CARGO_NEXTEST_VERSION=0.9.96             # baked into Docker image
PROBE_TIMEOUT=30                         # model API probe timeout (sec)
```

### Model presets

| Preset | Provider | Model | Auth needed |
|---|---|---|---|
| `gemini25` | Vertex AI | `vertex_ai/gemini-2.5-pro` | `VERTEX_AI_API_KEY` |
| `gemini25flash` | Vertex AI | `vertex_ai/gemini-2.5-flash` | `VERTEX_AI_API_KEY` |
| `gemini31` (`gemini`) | Vertex AI | `vertex_ai/gemini-3.1-pro-preview` | `VERTEX_AI_API_KEY` |
| `opus47v` | Vertex AI | `vertex_ai/claude-opus-4-7` | `GOOGLE_APPLICATION_CREDENTIALS` |
| `opus48v` | Vertex AI | `vertex_ai/claude-opus-4-8` | `GOOGLE_APPLICATION_CREDENTIALS` |
| `opus` | Bedrock | `bedrock/global.anthropic.claude-opus-4-6-v1` | AWS Bedrock |
| `kimi` | Bedrock | Kimi K2.5 (ARN) | AWS Bedrock |
| `glm5` | Bedrock | GLM 5 (ARN) | AWS Bedrock |
| `minimax` | Bedrock | MiniMax M2.5 (ARN) | AWS Bedrock |
| `gpt54` | OpenAI | `openai/gpt-5.4` | `OPENAI_API_KEY` |
| `gpt55` | OpenAI | `openai/gpt-5.5-2026-04-23` | `OPENAI_API_KEY` |
| `claude-sonnet-4-6` | Bedrock | `bedrock/anthropic.claude-sonnet-4.6` | AWS Bedrock |
| (custom) | Any | Pass full model string directly | Provider-dependent |

### Complete `run_pipeline_rust.sh` CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--model` | (required) | Preset or full model string |
| `--dataset` | (required) | Path to dataset JSON or known name |
| `--branch` | auto | Override auto-generated branch name |
| `--repo-split` | derived | Override repo_split (required for custom dataset paths) |
| `--max-iteration` | `3` | Agent iterations per file per stage |
| `--stage-timeout` | `0` | Hard stage timeout in seconds (0=disabled, skipped if agent active) |
| `--inactivity-timeout` | `900` | Kill agent if no log activity for N seconds |
| `--max-wall-time` | `86400` | Absolute per-stage wall-time cap (24h default; 0=disable) |
| `--eval-timeout` | `3600` | Evaluation timeout in seconds |
| `--backend` | `local` | `local` or `modal` (note: "local" still uses Docker) |
| `--num-samples` | `1` | Number of independent samples for pass@k |
| `--skip-to-stage` | (off) | Resume from stage 1/2/3 (reuses prior branch state) |
| `--max-test-output-length` | `15000` | Max chars of test output passed to agent |
| `--max-parallel-repos` | `1` | Parallel repo workers (keep at 1 unless sandbox is robust) |
| **--- ANTI-LEAKAGE FLAGS ---** | | |
| `--no-spec-info` | enabled | Disable spec PDF injection |
| `--no-unit-tests-info` | enabled | Disable inline-test injection into prompt (Stage 1 only) |
| `--no-repo-map` | enabled (1024) | Disable aider's internal repo-map |
| `--strip-aux-docs` | off | Hide README/CHANGELOG/HISTORY/etc. from agent's view |
| `--strip-non-stubs` | off | Hide non-stubbed source files from agent context |
| `--no-test-files-readonly` | inject | Do not inject test files as read-only reference |
| `--blind-lint` | off | Stage 2 sees only "build failed: N errors" |
| `--blind-tests` | off | Stage 3 sees only summary line, no per-test failures |
| `--names-only-tests` | off | Stage 3 shows only failed test names, not tracebacks |
| `--no-stage3-lint` | enabled | Disable lint in Stage 3 (for ablation experiments) |

---

## Multi-Tier Watchdog

The pipeline ships with a 3-tier process supervisor (`run_pipeline_rust.sh:782-914`, `watchdog_run()`):

| Tier | Default | Behavior |
|---|---|---|
| **Absolute wall-time** (`--max-wall-time`) | 86400s (24h) | Unconditional kill — prevents unbounded spend |
| **Stage hard-timeout** (`--stage-timeout`) | 0 (disabled) | Per-stage cap; skipped if agent shows activity (log writes) |
| **Inactivity timeout** (`--inactivity-timeout`) | 900s (15min) | Kill if no log activity for N seconds |

### macOS Fallback

macOS lacks `/proc/self/status`, so the `get_mtime` probe returns 0. The pipeline auto-halves `--max-wall-time` with a floor at `WATCHDOG_MTIME_FALLBACK_MIN_SECS` (default 3600s = 1h) when this fails. Increase via env var for longer runs.

### Per-Sample Retry

If a single sample fails inside the `run_single_sample` loop (lines 1568–1702), the pipeline logs a warning and **continues with the next sample** — partial pass@k success is allowed. Only complete failure across all samples returns nonzero.

---

## File Reference

### `tools/prepare_repo_rust.py` (771 lines)

| Function | What it does |
|---|---|
| `prepare_rust_repo(...)` | Top-level: forks, clones, stubs, scrapes spec, pushes branch, emits dataset entry |
| `stub_source_dir(repo_dir, src_dir, strip_docs=False)` | Invokes the native ruststubber binary; parses summary output |
| `scrape_spec(crate, repo_dir)` | Calls `scrape_rust_pdf.scrape_rust_spec` to render docs.rs into a compressed PDF; falls back to README scrape |
| `_ensure_spec_scrape_deps(auto_install=True)` | Auto-installs Playwright + PyMuPDF + PyPDF2 + beautifulsoup4 + chromium |
| `create_dataset_entry(...)` | Builds the `RustRepoInstance` dict written to the dataset JSON |
| `get_default_branch(repo_dir)` | Resolves upstream's default branch (master/main fallback) |
| CLI flags | `--repo --crate --src-dir --test-cmd --org --clone-dir --output --rust-version --edition --packages --skip-spec --strip-docs --dry-run` |

### `tools/ruststubber/` (Rust binary)

| File | What it does |
|---|---|
| `src/main.rs` | CLI (`--input-dir --output-dir --in-place --strip-docs`), WalkDir traversal, cross-file `cfg(test)` scan, `cargo metadata` integration |
| `src/stubber.rs` | `StubFolder` (`syn::fold::Fold` impl), 22 fold overrides covering every item kind that can hold doc attributes, `StubOptions { strip_docs }` |
| `src/lib.rs` | Public API: `stub_file`, `stub_file_with_options`, `stub_source`, `stub_source_with_options` |

### `commit0/cli_rust.py` (Rust-specific commands)

| Command | What it does |
|---|---|
| `setup REPO_SPLIT --dataset-name <path>` | Clones fork into `repos/<crate>/`, checks out commit0 branch at base_commit |
| `build` | Builds `commit0.base.rust:latest` + per-crate `commit0.repo.<crate>.<hash>:v0` images |
| `get-tests REPO_NAME [--base-dir repos]` | Calls `agent.agent_utils_rust.get_rust_test_ids`, writes both `<crate>.json` + `.bz2` |
| `test REPO [TEST_IDS]` | Runs `cargo test` directly (no Docker; for debugging) |
| `evaluate --branch BRANCH ...` | Runs eval harness: applies the agent's patch, runs `cargo test` in Docker, aggregates pass rates |
| `lint [FILES]` | `cargo clippy` + `cargo fmt --check` on the working tree |
| `save REPO_OR_SPLIT --org ORG` | Pushes agent-modified branches back to GitHub forks |
| `health-check` | Reports rustc/cargo/clippy/rustfmt/nextest versions |

### `commit0/harness/` (Rust-specific harness)

| File | What it does |
|---|---|
| `constants_rust.py` | `RUST_VERSION` (env-overridable, semver-validated, default `1.84.0`), `CARGO_NEXTEST_VERSION` (default `0.9.96`), `RUST_STUB_MARKER`, `RUST_BASE_BRANCH = "commit0"`, `RUST_GITIGNORE_ENTRIES`, `RUST_SPLIT`, `RUN_RUST_TESTS_LOG_DIR`, `RUST_TEST_IDS_DIR`, `DOCKERFILES_RUST_DIR`, `RustRepoInstance` model |
| `setup_rust.py` | Clones dataset fork into `repos/<crate>/`, creates `commit0` branch at `base_commit`, appends gitignore entries |
| `build_rust.py` | Reads dataset, deduplicates by image hash, delegates per-crate builds to `docker_build_rust.build_repo_images` |
| `docker_build_rust.py` | Builds base + per-crate images via buildx; auto-detects MITM proxy CA |
| `spec_rust.py` | Generates eval-shell scripts: `git apply --check` → `--3way` → `-C0` fallback chain; reverts test paths |
| `evaluate_rust.py` | Runs `run_rust_tests` per repo, aggregates results, parses JSON-line nextest AND text-mode cargo output |
| `run_rust_tests.py` | Constructs patch from local branch diff, invokes Docker eval, collects `test_output.txt` + `cargo_test_exit_code.txt` |
| `rust_test_parser.py` | JSON-line nextest event parser (`type=test event=ok/failed/ignored/timeout`) → `RustTestResult` |
| `patch_utils_rust.py` | `generate_rust_patch(repo_dir, base, target, *, strict=False)` → filters `target/` artefacts; `InvalidRustPatchError` on strict-mode failure |
| `health_check_rust.py` | Reports rustc/cargo/clippy/rustfmt/cargo-nextest install status |

### `agent/agent_utils_rust.py` (656+ lines)

| Function / Variable | What it does |
|---|---|
| `get_rust_test_ids(repo_path)` | Runs `cargo test --list` via `_run_cargo_with_retry`; falls back to `.json` then `.bz2` cache |
| `_run_cargo_with_retry(args, cwd, ...)` | Exponential-backoff retry with jitter; classifies stderr against `_TRANSIENT_CARGO_ERRORS` (49 markers) and `_PERMANENT_CARGO_ERRORS` (19 markers); deny-list precedence |
| `find_rust_files_to_edit(src_dir)` | Walks `src/` collecting `.rs` files, excludes `tests/benches/examples/target/.git`, skips `build.rs` |
| `get_target_edit_files_rust(src_dir)` | Subset of above that contain `RUST_STUB_MARKER` |
| `extract_rust_function_stubs(file)` | Regex-based extraction of fn signatures + line numbers |
| `get_message_rust(...)` | Builds agent system prompt: template + function list + dependency content + spec PDF text |
| `summarize_rust_test_output(raw, max_length, model, ...)` | 3-tier: deterministic regex extract → LLM summarize → tail-truncate |

### `agent/run_rust_agent.py` (~660 lines)

| Function | What it does |
|---|---|
| `run_rust_agent(branch, override, backend, ...)` | Main entry: multiprocessing pool over Rust repos, calls `run_rust_agent_for_repo` per |
| `run_rust_agent_for_repo(...)` | Per-repo work: opens git, ensures `commit0` branch at base_commit, instantiates `RustAiderAgents`, iterates files in test/lint/draft modes |
| `get_rust_message(agent_config, repo_path, target_files)` | Builds agent prompt (stage-aware) |
| `get_rust_lint_cmd(repo_path)` | Returns `bash -c 'cargo clippy --all-targets --all-features -- -D warnings' --` (bash wrapper prevents aider from appending filenames) |
| `_make_blind_lint_cmd(repo_path)` | Wraps clippy with `_BLIND_LINT_SHELL` so only error counts surface |
| `_make_blind_test_cmd(repo_path)` | Wraps `cargo test` with `_BLIND_TEST_SHELL` so only summary line surfaces |
| `_make_names_only_test_cmd(repo_path)` | Wraps `cargo test` with `_NAMES_ONLY_TEST_SHELL` so only failed test names surface |

### `agent/agents_rust.py`

| Function | What it does |
|---|---|
| `RustAiderAgents.run(...)` | Aider subclass; wires `lint_cmds={"rust": ...}`, sets `filename_to_lang` for `.rs` |
| System prompt tail adjustment | Modifies system prompt based on `inject_test_files_readonly` (lines 116–140) |
| Thinking capture integration | Hooks into aider's send-chat flow to persist reasoning tokens when `capture_thinking=True` |

### `run_pipeline_rust.sh` (~1800 lines)

| Function | What it does |
|---|---|
| `preflight()` | Validates required CLIs, auto-resolves Docker.app on macOS, probes toolchain versions (semver-aware), checks API creds, model API probe |
| `resolve_model()` (sourced) | Maps presets to full model IDs, sets MODEL_SHORT |
| `resolve_dataset()` | Resolves dataset name or path; supports `<name>`, full path, or known split name |
| `ensure_spec_docs_rust()` | Pre-pipeline: ensures each repo has `spec.pdf.bz2` (repo dir → specs/ cache → scrape). Tracks failures via `[ENSURE_SPECS_SUMMARY]` |
| `verify_spec_docs_rust()` | Post-provision: fatal if any repo missing spec and `USE_SPEC_INFO=true` |
| `run_build_once()` | Idempotent Docker-image build before first eval. Gated on `_pipeline_build_done`. Calls `commit0/cli_rust.py build` |
| `run_agent()` | Launches `agent.cli_rust` under `watchdog_run` |
| `watchdog_run()` | Activity-based timeout with three tiers: absolute_max → hard_timeout when inactive → inactivity_timeout. Auto-halves on macOS mtime probe failure |
| `run_evaluate()` | Runs `commit0/cli_rust.py evaluate` with timeout |
| `stage_1_draft()` / `stage_2_lint_refine()` / `stage_3_test_refine()` | Stage orchestrators (write_agent_config → run_agent → run_evaluate) |
| `run_single_sample(idx)` | Executes full 3-stage pipeline for one sample (pass@k loop) |
| `print_summary_table()` | Per-sample human-readable summary |
| `print_pass_at_k_summary()` | Aggregate across all samples, computes `best_s3_rate` and `best_s3_sample` |
| `cleanup()` (EXIT trap) | Kill lingering agents; restore aux docs; delete per-sample configs on success |

---

## Output Artifacts

| Phase | Artifact | Path | Purpose |
|---|---|---|---|
| Config | Agent config | `.agent_<RUN_ID>.yaml` | Model, max_iter, anti-leakage flags |
| Config | Commit0 config | `.commit0_<RUN_ID>.yaml` | Dataset, base_dir, repo_split, language |
| Logs | Agent logs | `logs/agent/<DATASET>/<MODEL>/run_<N>/stage<N>/aider.log` | Agent ↔ LLM conversation |
| Logs | Test output | `logs/agent/<DATASET>/<MODEL>/run_<N>/stage<N>/test_output.txt` | Cargo/nextest raw output |
| Results | Pipeline JSON | `logs/pipeline_<RUN_ID>_results.json` | Aggregated stage metrics (pass rate, costs, times) |
| Results | Per-sample JSON | `logs/pipeline_<RUN_ID>_sample<N>_results.json` | Single-sample metrics (when --num-samples > 1) |
| Trajectory | Module output | `logs/agent/<DATASET>/<MODEL>/run_<N>/stage<N>/<module>/output.json` | Per-module LLM turns, metrics, costs |
| Trajectory | Trajectory MD | `logs/agent/<DATASET>/<MODEL>/run_<N>/stage<N>/trajectory.md` | Human-readable turn-by-turn conversation |
| Eval | Per-repo results | `output/<repo>/<model>/results.json` | Per-stage pass rates, costs, timings |

---

## Troubleshooting

### Docker installed but `command not found`

**Symptom**: pipeline preflight reports `docker: command not found` despite Docker Desktop being installed.

**Cause**: Docker Desktop on macOS sometimes leaves `/usr/local/bin/docker` as a symlink to `/Volumes/Docker/...` (a dismounted DMG path).

**Fix (automatic)**: `preflight()` auto-resolves `/Applications/Docker.app/Contents/Resources/bin/` if `docker` isn't on PATH.

**Fix (manual)**: `ln -sf /Applications/Docker.app/Contents/Resources/bin/docker ~/.local/bin/docker && export PATH="$HOME/.local/bin:$PATH"`

### Eval fails with HTTP 404 "image not found on Docker Hub"

**Cause**: previous pipeline runs didn't build the Docker image before invoking eval.

**Fix (automatic in current code)**: `run_build_once()` runs before the first `run_evaluate` call. If you're using older `run_pipeline_rust.sh`, manually:

```bash
.venv/bin/python commit0/cli_rust.py build --commit0-config-file .commit0_rust.yaml --num-workers 2
```

### Eval reports `0/0` passed but tests obviously passed

**Cause**: hash-key mismatch — evaluate_rust hashed `test_dir` while run_rust_tests hashed `test_ids`, so the aggregator read from the wrong log directory.

**Fix**: this was patched in `evaluate_rust.py:211-217` to hash the same `test_ids = ""` value used by the writer. If you ever see this again, check that both hash inputs match.

### "No Rust repos matched repo_split='all'"

**Cause**: `RUST_SPLIT` constant is empty (the default for ad-hoc local datasets) AND old eval code required it to be populated.

**Fix**: patched in `evaluate_rust.py:169+` — when `RUST_SPLIT` is empty, the eval now falls back to evaluating every entry in the dataset.

### `cargo test --list` fails with "requires Rust X.Y" mid-prep

**Cause**: getifs-style problem — the crate's declared MSRV is satisfied, but a transitive dependency requires a newer rustc.

**Fix**: `rustup install stable && rustup default stable`. The retry-with-backoff helper in `agent_utils_rust._run_cargo_with_retry` will surface the underlying error after exhausting retries.

### Spec scrape failed: "scrape_rust_pdf requires: pip install playwright PyMuPDF PyPDF2 beautifulsoup4"

**Cause**: spec scrape deps not installed.

**Fix (automatic in current code)**: `_ensure_spec_scrape_deps()` in `prepare_repo_rust.py` auto-installs them. Set `SPEC_DEPS_AUTO_INSTALL=false` to opt out.

**Fix (manual)**:
```bash
.venv/bin/python -m pip install playwright PyMuPDF PyPDF2 beautifulsoup4
.venv/bin/python -m playwright install chromium
```

### Stubber wrongly stubs `src/tests/*.rs`

**Cause**: prior stubber walked each file in isolation; missed that `src/lib.rs` had `#[cfg(test)] mod tests;` gating sub-files.

**Fix**: patched in `tools/ruststubber/src/main.rs` — two-tier detection (path heuristic + cross-file scan). 41 tests verify universal coverage across `lib.rs`/`main.rs`/`bin/*.rs`, custom `#[path]` attrs, `cfg_attr` indirection, and workspace members.

### `.env` `GITHUB_TOKEN` returns HTTP 401

**Cause**: token expired.

**Fix (automatic in current code)**: `tools/_git_auth.get_github_token()` falls back to `gh auth token` (keyring). To refresh `.env`:

```bash
gh auth token > /tmp/token && sed -i.bak "s/^GITHUB_TOKEN=.*/GITHUB_TOKEN=$(cat /tmp/token)/" .env
```

### Watchdog killed agent prematurely on macOS

**Cause**: macOS doesn't have `/proc/self/status`, so `get_mtime` probe returns 0 and the watchdog's inactivity detection becomes non-functional.

**Fix (automatic in current code)**: watchdog auto-halves `absolute_max` with floor at `WATCHDOG_MTIME_FALLBACK_MIN_SECS` (default 3600s = 1 hour). Increase the env var for longer-running benchmarks.

### `--num-samples` and `--skip-to-stage` together

**Error**: `Error: --skip-to-stage and --num-samples > 1 cannot be used together.`

**Why**: Each sample writes its own results file, so `--skip-to-stage` cannot determine which sample's prior results to resume from.

**Fix**: Run each sample individually with `--skip-to-stage`, e.g.:

```bash
for i in 1 2 3; do
  bash run_pipeline_rust.sh --model gemini25 --dataset CRATE-rs --branch run-$i --skip-to-stage 3
done
```

### Cost extraction returns 0 across all stages

**Cause**: `output.json` `metrics.total_cost` field missing or zero, AND `aider.log` regex fallback didn't match.

**Fix**: check the aider log format — providers like Vertex may use a different cost field. Look for `tokens sent, tokens received, cost: $X.XX` in `aider.log` to confirm the regex still matches. File-level metric override pattern: see `extract_all_stage_costs` in `run_pipeline_rust.sh`.

### Bedrock model with IAM credentials clashing with `AWS_BEARER_TOKEN_BEDROCK`

**Symptom**: 403 from Bedrock despite valid bearer token in `.env`.

**Cause**: litellm/boto3 prefers IAM credentials if present, which may lack `bedrock:InvokeModel` permissions.

**Fix (automatic)**: `run_pipeline_rust.sh:185-189` unsets IAM env vars when `MODEL_NAME=bedrock/*` AND `AWS_BEARER_TOKEN_BEDROCK` is set, and points `AWS_SHARED_CREDENTIALS_FILE=/dev/null`.

---

## Common Gotchas Checklist

Before running the pipeline, verify each item:

- [ ] `src_dir` matches actual source directory (single-crate: `src`; workspace member: `<member>/src`)
- [ ] Rust toolchain matches dataset's `rust_version` field (or set `RUST_VERSION_STRICT=false`)
- [ ] `test_cmd` runs only your crate's tests (`cargo test -p CRATE`); blanket `cargo test` may compile the whole workspace
- [ ] Test IDs file is non-empty: `bzcat commit0/data/rust_test_ids/CRATE.bz2 | wc -l`
- [ ] Stubbed code still parses + compiles: `cargo check --tests` in `repos_staging/CRATE-rs/` should report only missing fn-body errors
- [ ] `gh auth status` shows an active authenticated account, OR `.env`'s `GITHUB_TOKEN` validates
- [ ] `.commit0_rust.yaml` exists and points to the right dataset JSON
- [ ] No stale Docker images from previous prep runs (`docker images | grep commit0.repo.CRATE` — remove with `docker rmi` if dataset changed)
- [ ] `spec.pdf.bz2` exists in repo dir if `--no-spec-info` not used (check: `ls repos/CRATE-rs/spec.pdf.bz2`)
- [ ] Enough disk space for Docker images (Rust base ~3.6 GB, per-crate ~3.6 GB)
- [ ] Docker daemon is running (`docker info` succeeds)
- [ ] Vertex/Bedrock/OpenAI credentials in `.env` match the model preset chosen
- [ ] `--strip-docs` flag set during prep if you want a harder benchmark variant
- [ ] No leftover orphan branches on the fork (`gh repo edit --delete-branch BRANCH`)
- [ ] **Anti-leakage flags chosen deliberately** for research integrity — document which flags were enabled in your results

---

## Reference Example: `discord/itsdangerous-rs`

### Repo profile

| Property | Value |
|---|---|
| Repo | `discord/itsdangerous-rs` |
| Crate | `itsdangerous` (note: not `itsdangerous-rs`) |
| Description | Rust port of the Python itsdangerous library |
| Layout | Standard single-crate (`src/` only, no integration `tests/`) |
| Source files | 13 (`.rs`) |
| Tests | 17 (11 unit + 6 doc-tests) |
| Runtime deps | `hmac 0.7`, `sha-1 0.8`, `base64 0.10`, `generic-array 0.12`, `typenum 1.10` |
| Build backend | Cargo |
| Edition | `2018` |
| Default branch | `master` |
| Fork | `Aman-Yadav-Ethara-AI/itsdangerous-rs` |

### Commands run

```bash
# 1. Prep (no strip-docs first to baseline)
.venv/bin/python -m tools.prepare_repo_rust \
    --repo discord/itsdangerous-rs \
    --crate itsdangerous \
    --src-dir src \
    --test-cmd "cargo test -p itsdangerous" \
    --org Aman-Yadav-Ethara-AI \
    --clone-dir ./repos_staging \
    --output itsdangerous_rs_entries.json \
    --rust-version stable

# 2. Setup
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./itsdangerous-rs_dataset.json \
    --dataset-split test \
    --commit0-config-file .commit0_rust.yaml

# 3. Test ID collection
.venv/bin/python commit0/cli_rust.py get-tests itsdangerous-rs
# Output: Collected 17 test IDs → itsdangerous-rs.json + itsdangerous-rs.bz2

# 4. Run pipeline — baseline (easiest mode)
bash run_pipeline_rust.sh \
    --model claude-sonnet-4-6 \
    --dataset ./itsdangerous-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    --skip-to-stage 1 \
    2>&1 | tee logs/sonnet46_itsdangerous-rs_stage1.log

# 5. Run pipeline — hardened (anti-leakage flags)
bash run_pipeline_rust.sh \
    --model claude-sonnet-4-6 \
    --dataset ./itsdangerous-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    --strip-aux-docs \
    --no-test-files-readonly \
    --blind-lint \
    --names-only-tests \
    --num-samples 3 \
    2>&1 | tee logs/sonnet46_itsdangerous-rs_hard.log
```

### Results (Stage 1 only, no `--strip-docs`)

| Stage | Pass Rate | Passed/Total | Stage Cost | Time |
|---|---|---|---|---|
| Stage 1 (Draft) | **100%** | **17/17** | ~$0.40 | ~12 min |

Note: this was the BASELINE with all docs preserved (including a working API usage example in the file-level `//!`).

### Output file locations

| Artifact | Path |
|---|---|
| Entries JSON | `itsdangerous_rs_entries.json` |
| Dataset JSON | `itsdangerous-rs_dataset.json` |
| Results JSON | `output/Aman-Yadav-Ethara-AI_itsdangerous-rs/<model>/results.json` |
| Pipeline log | `logs/<model>_itsdangerous-rs_<stage>.log` |
| Stage logs | `output/Aman-Yadav-Ethara-AI_itsdangerous-rs/<model>/stage{1,2,3}_*/` |
| Test IDs | `commit0/data/rust_test_ids/itsdangerous-rs.{json,bz2}` |
| Stubbed source (prep workspace) | `repos_staging/itsdangerous-rs/` |
| Working clone (eval workspace) | `repos/itsdangerous-rs/` |
| Commit0 config | `.commit0_rust.yaml` |
| Docker base image | `commit0.base.rust:latest` (~3.6 GB) |
| Docker repo image | `commit0.repo.itsdangerous.<hash>:v0` (~3.6 GB) |

---

## Differences From The Python Pipeline

| Concern | Python | Rust |
|---|---|---|
| Stubber | `tools/stub.py` (Python AST-based) | `tools/ruststubber` (native binary, syn-based) |
| Stubbing modes | `all`, `docstring`, `combined` (default `combined`) | one mode, body-only; `--strip-docs` to remove `#[doc]` |
| Default difficulty | combined mode — VERY aggressive | all-with-docs — EASIEST possible |
| Spec scrape | Playwright via `tools/scrape_pdf.py` | Playwright via `scrape_rust_pdf.py` |
| Test runner | `pytest --collect-only` for IDs; `pytest` in Docker | `cargo test --list` for IDs; `cargo test` in Docker |
| Lint feedback | `ruff` | `cargo clippy` |
| Pre-eval check | None (Docker built upfront) | `run_build_once` in pipeline (ensures image before eval) |
| Toolchain pinning | `python` version in dataset | `rust_version` (env-overridable, semver-validated) |
| Curated splits | `SPLIT` dict populated by HF dataset loaders | `RUST_SPLIT` empty by default (eval auto-falls-back to all entries) |
| Workspace support | One Cargo.toml = one crate | Workspaces handled via `cargo metadata` integration in stubber |
| Anti-leakage flags | All 6 flags wired across all 7 languages (commit 78be512) | All 6 flags wired; same defaults |
| Blind feedback modes | `--blind-tests`, `--names-only-tests`, `--blind-lint` | Same flags, identical semantics |

---

## Pipeline Self-Healing Behaviour

The Rust pipeline now auto-handles these previously-manual situations:

| Scenario | What the code does now |
|---|---|
| `.env` GITHUB_TOKEN expired/missing | `tools/_git_auth.get_github_token()` falls back to `gh auth token` (keyring) |
| Docker installed but `docker` not on PATH | Auto-resolves `/Applications/Docker.app/Contents/Resources/bin/` at preflight |
| Docker image not pre-built before first eval | `run_build_once()` in `stage_1_draft()` builds it (idempotent) |
| `repo_split="all"` with empty `RUST_SPLIT` | Eval evaluates every entry in the dataset |
| Hash-key mismatch between writer/aggregator | Both use `test_ids=""` → same hash → same log dir |
| Spec scrape deps missing | `_ensure_spec_scrape_deps()` auto-pip-installs Playwright + PDF tooling |
| Spec scrape file named `<crate>.pdf.bz2` | Renamed to canonical `spec.pdf.bz2` (agent's loader expects this) |
| `cli_rust.py get-tests` was NotImplemented | Wired to `get_rust_test_ids()` + writes both `.json` + `.bz2` |
| `test_dir` defaulted to `<crate>` | Defaults to `"tests"` (standard Rust convention) |
| Stubber walked `src/tests/*` as production code | Cross-file scan from `lib.rs`/`main.rs` + path heuristic |
| Stubber missed `#[cfg(not(test))]` (substring match) | Proper Meta evaluator: `not(test)` correctly returns false |
| Watchdog inactivity timeout broke on macOS | Auto-halved `absolute_max` with env-configurable floor |
| `RUST_VERSION` mismatch in `.env` | Semver-validated; `RUST_VERSION_STRICT=true` to fail-fast, else warn-only |
| Cargo network transient failures | `_run_cargo_with_retry` with 3 attempts, exponential backoff + jitter |
| Doc attrs leaking context | `--strip-docs` in ruststubber + prep tool; 22 fold overrides cover every syn item kind |
| IAM creds clobbering Bedrock bearer token | Auto-unset IAM env vars when `MODEL_NAME=bedrock/*` AND bearer token present |
| Sample failure in pass@k loop | Logged + skipped; pipeline continues with next sample, returns partial success |
| Spec text exceeds `spec_summary_max_tokens` | LLM-summarized before injection (cost recorded under spec_costs) |

All of these were either silent failures or required manual workarounds in earlier iterations of the pipeline; they're now first-class auto-handled paths with clear logging when they fire.

---

## Quick Recipes

### Run a fresh baseline (easiest mode)

```bash
bash run_pipeline_rust.sh --model gemini25 --dataset ./CRATE-rs_dataset.json --repo-split all
```

### Run hardened benchmark (research mode)

```bash
bash run_pipeline_rust.sh --model gemini25 --dataset ./CRATE-rs_dataset.json --repo-split all \
    --strip-aux-docs --strip-non-stubs --no-test-files-readonly \
    --blind-lint --blind-tests --num-samples 3
```

### Resume from Stage 3 after a Stage 2 failure

```bash
bash run_pipeline_rust.sh --model gemini25 --dataset ./CRATE-rs_dataset.json \
    --branch gemini25-CRATE-rust --skip-to-stage 3
```

### Pass@k study (3 samples, fail fast on watchdog)

```bash
bash run_pipeline_rust.sh --model gemini25 --dataset ./CRATE-rs_dataset.json --repo-split all \
    --num-samples 3 --inactivity-timeout 600 --max-wall-time 14400
```

### Stage 1 only with extended thinking capture

Edit `.agent.yaml` after first run to add `capture_thinking: true`, then:

```bash
bash run_pipeline_rust.sh --model opus47v --dataset ./CRATE-rs_dataset.json --repo-split all --skip-to-stage 1
```

### Single repo from a multi-repo dataset

```bash
bash run_pipeline_rust.sh --model gemini25 --dataset ./multi_dataset.json --repo-split <REPO_NAME_ONLY>
```

---

## Glossary

- **Anti-leakage**: Mechanisms that hide information from the agent during code generation to ensure benchmark results reflect genuine reasoning, not memorization.
- **Blind mode**: Wrapping a feedback command so the agent only sees aggregate counts, not detailed output.
- **commit0 branch**: Auto-created branch at `base_commit` (stubbed code) where the agent makes edits.
- **commit0_all branch**: Branch on the fork that contains base + reference + spec commits in sequence.
- **pass@k**: Probability that at least one of k independent samples passes all tests. Computed in `print_pass_at_k_summary()`.
- **Reference commit**: The original working code, used only by evaluation, never shown to the agent.
- **Spec PDF**: `docs.rs` rendered to PDF and bzip2'd. Injected into Stage 1 prompt (and Stage 2/3 if no compression needed).
- **Stub marker**: The string `panic!("STUB: not implemented")` — `RUST_STUB_MARKER` in `constants_rust.py`. Identifies which files/functions need implementation.
- **Watchdog**: Multi-tier supervisor that kills the agent if it hangs (inactivity, stage timeout, or wall-time).

---

## From Scratch to Claude Code Subscription Run

This section walks the entire pipeline from a clean machine through a successful Stage-3 run using your **Claude Code subscription** (not a Vertex/Bedrock key, not a pay-per-token API). It assumes nothing exists yet.

### Prerequisites

- macOS (these instructions are macOS-tested; Linux paths are similar — Keychain steps don't apply)
- Anthropic Pro/Max subscription with the `claude` CLI installed
- GitHub account (you'll fork repos into it)
- Docker Desktop installed
- Python 3.13+

### Step 1: Clone + install

```bash
git clone <this-repo-url> kaiju-harness
cd kaiju-harness

# Install the .venv (using uv or pip; project uses uv)
uv sync                          # creates .venv with all deps
# OR: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Rust toolchain (stubber needs cargo, edition 2024 repos need rustc >= 1.85)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"
rustup default stable
rustup component add rustfmt clippy

# Build the AST stubber
cd tools/ruststubber && cargo build --release && cd ../..

# Playwright (for docs.rs spec scraping)
.venv/bin/python -m pip install playwright PyMuPDF PyPDF2 beautifulsoup4
.venv/bin/python -m playwright install chromium

# GitHub auth
gh auth login
```

### Step 2: Sign into the Claude CLI

```bash
claude login
# Follow OAuth flow; tokens land in macOS Keychain under service "Claude Code-credentials"
```

Verify:

```bash
security find-generic-password -s "Claude Code-credentials" -w | head -c 50 ; echo
# Should print 'sk-ant-oat01-...' (your access token prefix)
```

### Step 3: Configure `.env`

```bash
cat > .env <<'EOF'
GITHUB_TOKEN=ghp_yourtoken_here
EOF
```

**Do NOT set `GOOGLE_APPLICATION_CREDENTIALS`** — that activates Vertex routing for `vertex_ai/*` models. Pure Claude Code OAuth needs no GCP setup.

### Step 4: Start the Claude Code bridge

```bash
scripts/claude_code_bridge.sh start
eval "$(scripts/claude_code_bridge.sh start | grep ^export)"   # exports ANTHROPIC_API_BASE=http://127.0.0.1:8765 into shell
```

Verify it's up:

```bash
curl -s http://127.0.0.1:8765/healthz | jq .
# Expected: {"ok": true, "token_prefix": "sk-ant-oat01-..."}
```

### Step 5: (Optional) Configure multi-account failover

Set this up if you have 2+ Claude subscriptions and want automatic failover when one hits its 5-hour limit.

```bash
# Interactive: switch CLI accounts and capture acct2 into a second keychain entry
scripts/setup_multi_account.sh add-from-cli

# Activate the pool
scripts/setup_multi_account.sh enable

# Restart bridge to pick up KAIJU_CC_ACCOUNT_POOL
scripts/claude_code_bridge.sh stop
scripts/claude_code_bridge.sh start

# Verify
curl -s http://127.0.0.1:8765/quota | jq .
# Expected: {"multi_account": true, "accounts": [...], "next_reset_at_unix": null}
```

See [docs/MULTI_ACCOUNT_SETUP.md](docs/MULTI_ACCOUNT_SETUP.md) for the full runbook.

### Step 6: Prepare a Rust repo

```bash
.venv/bin/python -m tools.prepare_repo_rust \
    --repo discord/itsdangerous-rs \
    --crate itsdangerous \
    --src-dir src \
    --test-cmd "cargo test -p itsdangerous" \
    --org YOUR_GITHUB_ACCOUNT \
    --clone-dir ./repos_staging \
    --output itsdangerous-rs_entries.json \
    --rust-version stable
```

Produces: fork on GitHub, `itsdangerous-rs_dataset.json`, `.commit0_rust.yaml`, stubbed source pushed to `commit0_all` branch.

### Step 7: commit0 setup + test ID collection

```bash
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./itsdangerous-rs_dataset.json --dataset-split test \
    --commit0-config-file .commit0_rust.yaml

.venv/bin/python commit0/cli_rust.py get-tests itsdangerous
# Output: Collected N test IDs → commit0/data/rust_test_ids/itsdangerous.{json,bz2}
```

### Step 8: Run the pipeline against Claude Code

```bash
bash run_pipeline_rust.sh \
    --use-claude-code \
    --model opus48cc \
    --dataset ./itsdangerous-rs_dataset.json \
    --repo-split all \
    --max-iteration 1 \
    --per-edit-compile-gate \
    --quality-watchdog \
    2>&1 | tee logs/opus48cc_itsdangerous-rs.log
```

Breakdown of flags:

| Flag | Purpose |
|---|---|
| `--use-claude-code` | Route `anthropic/*` models through the local bridge instead of direct API |
| `--model opus48cc` | Preset for `anthropic/claude-opus-4-8` via Claude Code OAuth |
| `--per-edit-compile-gate` | After each aider edit, run `cargo check`; revert + re-prompt on regression |
| `--quality-watchdog` | Sidecar that kills the agent if compile-error count is monotonically rising |

The pipeline will:

1. **Preflight** — validate tools, probe model API via bridge
2. **Ensure spec docs** — scrape docs.rs if needed
3. **Build Docker image** — one-time per pipeline run
4. **Stage 1** — agent writes initial implementations
5. **Stage 1 eval** — `cargo test` in Docker, results saved
6. **Stage 2** — agent refines using clippy feedback
7. **Stage 2 eval** — re-test
8. **Stage 2→3 gate** — if tree doesn't compile, Stage 3 is SKIPPED with `SKIPPED_STAGE_2_BROKE_TREE` status
9. **Stage 3** — agent refines using test feedback
10. **Stage 3 eval** — final pass rate
11. **Summary table + results JSON**

### Step 9: Monitor (in another terminal)

```bash
# Pipeline-level summary log
tail -f logs/opus48cc_itsdangerous-rs.log

# Per-module agent activity
tail -f logs/agent/itsdangerous-rs_dataset/claude-opus-4.8/run_1/stage1_draft/agent_run.log

# Bridge: account quota + rate-limit state
watch -n 30 'curl -s http://127.0.0.1:8765/quota | jq .'

# Quality watchdog (if enabled, separate log per stage)
tail -f logs/agent/itsdangerous-rs_dataset/claude-opus-4.8/run_1/stage*/quality_watchdog.log
```

### Step 10: Read results

```bash
jq . logs/pipeline_rust_claude-opus-4.8_itsdangerous-rs_dataset_results.json
```

Look for:

- `stage1.num_passed / stage1.num_tests` — actual pass rate (denominator from `.bz2` inventory)
- `stage1.cost_usd` — **$0.00 actual money** on Claude Code subscription (flat-rate)
- `stage3.status: "SKIPPED_STAGE_2_BROKE_TREE"` — if Stage 2 left tree broken

### Stopping / restarting

```bash
scripts/claude_code_bridge.sh status   # check it's up
scripts/claude_code_bridge.sh stop     # graceful shutdown
scripts/claude_code_bridge.sh start    # restart
```

---

## Universal Parameters Reference

Every parameter the Rust pipeline accepts, grouped by category. Apply identically to all of `run_pipeline_{rust,go,c,cpp,java,ts,js}.sh` unless noted Rust-only.

### Required pipeline flags

| Flag | Description |
|---|---|
| `--model <preset\|model_id>` | Model preset (e.g. `opus48cc`) or full litellm model ID (e.g. `anthropic/claude-opus-4-8`) |
| `--dataset <name\|path>` | Dataset short-name (`<name>_dataset.json` in repo root) or absolute/relative path to JSON |

### Model presets

Defined in `commit0/harness/resolve_model.sh`.

| Preset | Resolves to | Channel |
|---|---|---|
| `opus48cc` | `anthropic/claude-opus-4-8` | Claude Code bridge (subscription) |
| `opus48v` | `vertex_ai/claude-opus-4-8` | Vertex AI (GCP) |
| `opus47v` | `vertex_ai/claude-opus-4-7` | Vertex AI |
| `sonnet46v` | `vertex_ai/claude-sonnet-4-6` | Vertex AI |
| `opus`, `opus47` | Bedrock Claude Opus 4.6 / 4.7 | AWS Bedrock |
| `kimi`, `glm5`, `minimax` | Bedrock Kimi K2.5 / GLM 5 / MiniMax M2.5 | AWS Bedrock |
| `nova-premier`, `nova-lite` | Bedrock Nova Premier / Nova-2 Lite | AWS Bedrock |
| `gemini`, `gemini25pro`, `gemini25flash` | `vertex_ai/gemini-{3.1-pro-preview,2.5-pro,2.5-flash}` | Vertex AI |
| `gpt54`, `gpt55` | OpenAI GPT-5.4 / GPT-5.5 | OpenAI direct |
| any other string | Passed through to litellm as-is | depends on prefix |

### Pipeline-level flags

| Flag | Default | Description |
|---|---|---|
| `--branch <name>` | auto | Override auto-generated branch name |
| `--repo-split <name>` | derived | Override repo_split (required for custom dataset paths) |
| `--max-iteration <n>` | `1` | Agent iterations per file per stage |
| `--num-samples <n>` | `1` | Independent samples for pass@k (Stage 1 only when >1) |
| `--skip-to-stage <1\|2\|3>` | (off) | Resume from stage N |
| `--backend <local\|modal>` | `local` | Eval backend |
| `--max-parallel-repos <n>` | `1` | Parallel repo workers |
| `--use-claude-code` | (off) | Route `anthropic/*` via local bridge |

### Watchdog & timing flags

| Flag | Default | Description |
|---|---|---|
| `--inactivity-timeout <s>` | `900` | Kill agent if no log activity for N seconds |
| `--stage-timeout <s>` | `0` | Hard per-stage timeout (0 = disabled) |
| `--max-wall-time <s>` | `86400` | Absolute per-stage wall cap (0 = disable) |
| `--eval-timeout <s>` | `3600` | Per-eval timeout |
| `--max-test-output-length <n>` | `15000` | Max chars of test output the agent sees |

### Quality-gate flags (new)

| Flag | Default | Description |
|---|---|---|
| `--per-edit-compile-gate` | off | After each aider edit, run `cargo check`; revert + re-prompt on regression (Rust) |
| `--compile-gate-max-retries <n>` | `2` | Re-prompts before reverting a module |
| `--no-stage3-skip-if-broken` | gate is ON | Disable the Stage 2→3 compile gate (always run Stage 3) |
| `--quality-watchdog` | off | Sidecar that kills agent on rising compile-error trend |
| `--quality-watchdog-interval <s>` | `90` | Seconds between samples |
| `--quality-watchdog-rising <n>` | `3` | Consecutive rising samples to trigger kill |
| `--quality-watchdog-min-delta <n>` | `5` | Minimum error increase per sample to count as rising |

### Anti-leakage flags

| Flag | Default | Description |
|---|---|---|
| `--no-spec-info` | enabled | Disable spec PDF injection |
| `--no-unit-tests-info` | enabled (Stage 1) | Disable inline-test injection |
| `--no-repo-map` | enabled (1024) | Disable aider's repo-map |
| `--strip-aux-docs` | off | Hide README/CHANGELOG/etc. |
| `--blind-lint` | off | Stage 2 sees only "build failed: N errors" |
| `--blind-tests` | off | Stage 3 sees only summary line |
| `--strip-non-stubs` | off | Hide non-stubbed source files |
| `--names-only-tests` | off | Stage 3 shows only failed test names |
| `--no-test-files-readonly` | inject | Don't inject test files as read-only reference |
| `--no-stage3-lint` | enabled | Disable lint in Stage 3 |

### Environment variables

#### Credentials (place in `.env`)

| Var | Purpose | Required for |
|---|---|---|
| `GITHUB_TOKEN` | GitHub auth for fork/push | All runs |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON | `vertex_ai/*` models |
| `VERTEXAI_LOCATION` | `global` recommended | `vertex_ai/*` models |
| `VERTEX_PROJECT` | GCP project ID | `vertex_ai/*` models |
| `VERTEX_AI_API_KEY` | Gemini Studio API key (alternative to ADC, Gemini-only) | `vertex_ai/gemini-*` |
| `AWS_BEARER_TOKEN_BEDROCK` | Bedrock bearer token | `bedrock/*` models |
| `AWS_DEFAULT_REGION` | e.g. `us-east-1` | `bedrock/*` models |
| `OPENAI_API_KEY` | OpenAI API key | `gpt5*` |
| `ANTHROPIC_API_KEY` | Direct Anthropic API | `anthropic/*` without bridge |

#### Claude Code bridge tuning

| Var | Default | Purpose |
|---|---|---|
| `KAIJU_CC_ACCOUNT_POOL` | (unset) | Multi-account spec: `keychain:foo:keychain:bar:file:/path` |
| `KAIJU_CC_MAX_PAUSE_SEC` | `21600` (6h) | Max sleep on rate-limit hit before giving up |
| `KAIJU_CC_TRANSIENT_BACKOFF` | `5,10,20` | Transient-error retry backoff (Fix #5) |
| `KAIJU_BRIDGE_REQUEST_TIMEOUT` | `600` | Total httpx timeout (seconds) |
| `KAIJU_BRIDGE_READ_TIMEOUT` | `180` | Per-chunk read timeout (Fix #2) |
| `KAIJU_BRIDGE_CONNECT_TIMEOUT` | `30` | TCP connect timeout |

#### Quality watchdog overrides (alternative to CLI flags)

| Var | Default | Purpose |
|---|---|---|
| `KAIJU_QW_INTERVAL` | `90` | Sampling interval |
| `KAIJU_QW_RISING` | `3` | Consecutive rising samples |
| `KAIJU_QW_MIN_DELTA` | `5` | Min error increase per sample |

#### Test execution

| Var | Default | Purpose |
|---|---|---|
| `KAIJU_TEST_TIMEOUT` | `600` | Per-suite timeout for agent's cargo test invocation |
| `EVAL_TEST_TIMEOUT` | `600` | Per-suite timeout in eval.sh |

#### macOS-specific

| Var | Default | Purpose |
|---|---|---|
| `WATCHDOG_MTIME_FALLBACK_MIN_SECS` | `3600` | Floor for absolute_max when mtime probe broken |
| `SPEC_DEPS_AUTO_INSTALL` | `true` | Auto-pip-install spec scrape deps |

---

## Resilience & Quality Features (Fixes #2–5)

Reference for the four resilience features added on top of the basic 3-stage pipeline.

### Fix #2 — Bridge read timeout (default ON, 180s)

**Problem**: Opus 4.8 + extended thinking + ~96k context routinely takes 90–150s per turn. The default httpx read-phase timeout (5s with `httpx.Timeout(total)`) caused `MidStreamFallbackError` storms.

**Fix**: `_bridge_timeout()` in `agent/claude_code/bridge.py` builds `httpx.Timeout(total=600, connect=30, read=180)` — read phase gets its own 180s slot while total request can still take 10 min.

**Tuning**:

```bash
export KAIJU_BRIDGE_READ_TIMEOUT=240        # if you see read timeouts mid-stream
export KAIJU_BRIDGE_CONNECT_TIMEOUT=10      # if connect is reliably fast
export KAIJU_BRIDGE_REQUEST_TIMEOUT=900     # if entire request can be long
```

Restart bridge to apply.

### Fix #3 — Stage 2 → Stage 3 gate (default ON)

**Problem**: When Stage 2 leaves the tree uncompilable, running `cargo test` in Stage 3 just produces the same compile errors at much higher cost (Opus tokens + Docker startup + test compilation).

**Fix**: Between Stage 2 and Stage 3, the pipeline runs `cargo check --tests --message-format=short` (`check_tree_compiles` helper in `run_pipeline_rust.sh`). If it fails, Stage 3 is skipped and `RESULTS_JSON.stage3` is:

```json
{
  "name": "Test refine",
  "status": "SKIPPED_STAGE_2_BROKE_TREE",
  "compile_errors_after_stage2": 43,
  "elapsed_s": 0,
  "eval_time_s": 0,
  "cost_usd_incremental": 0.0,
  "num_passed": 0,
  "num_tests": 0,
  "pass_rate": 0.0
}
```

**Disable** with `--no-stage3-skip-if-broken` to force Stage 3 to run regardless (matches the old behavior).

**Side effects**:
- Adds 5–30 seconds between stages (one cargo check on incremental build)
- Logs `[GATE] cargo check ...` lines to the pipeline log
- Writes diagnostic to `${LOG_BASE}/stage_gate_cargo_check.log`

### Fix #4 — Quality-aware watchdog (default OFF)

**Problem**: The existing `watchdog_run` only kills on log silence. An agent can be "alive" (writing logs) while introducing more compile errors with every edit — the inactivity watchdog won't catch it. virtio Stage 2 went from 4 → 43 errors over 3 hours before the inactivity watchdog finally triggered on a separate stall.

**Fix**: Sidecar Python process (`agent/claude_code/quality_watchdog.py`) that:

1. Samples `cargo check --tests --message-format=short` every `--interval` seconds (default 90)
2. Tracks an in-memory history of compile-error counts
3. Kills the agent if the last `--consecutive-rising` deltas (default 3) are each ≥ `--min-delta` (default 5)
4. Exits 0 if agent disappears naturally, 1 if it killed the agent

**Enable**:

```bash
bash run_pipeline_rust.sh ... --quality-watchdog
```

Or tune the heuristic:

```bash
bash run_pipeline_rust.sh ... --quality-watchdog \
    --quality-watchdog-interval 60 \
    --quality-watchdog-rising 4 \
    --quality-watchdog-min-delta 3
```

**Default OFF rationale**: The Stage 2→3 gate (Fix #3) handles the most common case ("Stage 2 broke things"). The quality watchdog is more aggressive — it kills *mid-stage*, which can throw away salvageable work. Use when you've seen genuine regression cascades and want to bound budget loss.

**Logs**: each stage writes `${stage_log_dir}/quality_watchdog.log` with sample-by-sample history.

### Fix #5 — Transient network-error retry (default ON, 5s/10s/20s)

**Problem**: `httpx.ReadTimeout`, `httpcore.ReadTimeout`, mid-stream aborts, connection resets — these were re-raised immediately. The pipeline saw them as fatal agent errors. On a real run we counted 30+ such errors over 3 hours.

**Fix**: `_is_transient_network_error()` in `agent/claude_code/recovery.py` detects transient errors by exception class name (covers `httpx`, `httpcore`, `litellm`) OR message substring. `run_with_recovery()` now has two retry tracks:

- **Transient**: 5s → 10s → 20s exponential backoff, 3 attempts
- **Rate-limit**: separate slower track that waits for `/quota` reset (unchanged)

Rate-limit errors are NOT classified as transient — they go down their own track to avoid burning the retry budget too fast.

**Tuning**:

```bash
export KAIJU_CC_TRANSIENT_BACKOFF="2,4,8,16,32"   # 5 attempts, max 32s
```

**Effect on watchdog**: the recovery loop calls `_sleep_with_heartbeat()` between attempts, touching `agent_run.log` so the inactivity watchdog doesn't fire mid-retry.

### Resilience knobs summary

| Failure mode | Detected by | Recovery |
|---|---|---|
| Read timeout (Opus 4.8 thinking long) | `httpx.ReadTimeout` | Fix #5: 5s/10s/20s retry |
| Connection reset / mid-stream abort | Exception class + msg | Fix #5: 5s/10s/20s retry |
| Rate limit hit | `RateLimitError`/`subscription_cap` | Wait for `/quota` reset |
| Account exhausted (5h cap) | 429 + multi-account pool | Fail over to next account |
| Stage 2 broke the tree | `cargo check` post-Stage 2 | Fix #3: skip Stage 3 |
| Compile errors trending up | Periodic `cargo check` | Fix #4: kill agent (opt-in) |
| Single edit broke a previously-compiling file | `cargo check` per edit | Per-edit gate: revert + re-prompt |
| Hung cargo test | `KAIJU_TEST_TIMEOUT` wrapper | SIGTERM at 600s, SIGKILL at 610s |
| Agent process stuck silent | Log mtime watchdog | SIGTERM at INACTIVITY_TIMEOUT (900s) |
| Stage running too long | `STAGE_TIMEOUT` / `MAX_WALL_TIME` | Hard kill |

