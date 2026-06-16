# Commit0 Rust Pipeline Runbook

Production guide for preparing custom Rust crates, building Docker environments, and running the 3-stage AI coding pipeline. The Rust side of `kaiju-harness` mirrors the Python pipeline structure but uses Rust-native tooling: `cargo`, `syn`/`prettyplease` AST stubbing via the native `ruststubber` binary, and Docker-based eval via `commit0.repo.<crate>` images.

All commands assume you're in the project root and using `.venv/bin/python`.

---

## Quick Start

```bash
# 1. One-time machine setup (per-host)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"
rustup default stable
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
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./CRATE-rs_dataset.json --dataset-split test \
    --commit0-config-file .commit0_rust.yaml
.venv/bin/python commit0/cli_rust.py get-tests CRATE

# 4. Run 3-stage pipeline (Stage 1 draft → eval → Stage 2 lint → eval → Stage 3 test → eval)
set -a && source .env && set +a
bash run_pipeline_rust.sh \
    --model gemini25 \
    --dataset ./CRATE-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    2>&1 | tee logs/MODEL_CRATE_full.log
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
          |  |                               |    - uses `cargo metadata` for crate roots when
          |  |                               |      Cargo.toml is reachable; falls back to
          |  |                               |      walking lib.rs/main.rs/bin/*.rs
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
    |  | run_build_once            |  Idempotent. Builds Docker      |
    |  | (NEW: pre-eval guard)     |  base + per-crate images via    |
    |  +-------------+-------------+  commit0/cli_rust.py build.     |
    |                |                                                |
    |                v                                                |
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
    |  | STAGE 2: Lint Refine      |  cargo clippy feedback.         |
    |  | use_lint_info=true        |                                 |
    |  | run_tests=false           |                                 |
    |  +-------------+-------------+                                 |
    |                |                                                |
    |                v                                                |
    |        [ commit0 evaluate ]                                    |
    |                |                                                |
    |                v                                                |
    |  +---------------------------+                                 |
    |  | STAGE 3: Test Refine      |  cargo test feedback per file.  |
    |  | run_tests=true            |  Most impactful stage.          |
    |  | use_lint_info=true        |                                 |
    |  +-------------+-------------+                                 |
    |                |                                                |
    |                v                                                |
    |        [ commit0 evaluate ]   Final pass rate.                 |
    +---------------------------------------------------------------+
                |
                v
    +-----------+-----------+
    | output/<repo>/<model>/results.json     Per-stage pass rates, costs, timings |
    +------------------------+
```

---

## Method A: Step-By-Step (Recommended For First Run)

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
| `setup.rust_version` | Matches what `rustc --version` reports on your machine, OR set RUST_VERSION_STRICT=false to ignore mismatch |
| `setup.edition` | Matches Cargo.toml `[package].edition` (auto-detected; rarely needs editing) |
| `test.test_cmd` | Should run only your crate's tests (`cargo test -p CRATE_NAME`); blanket `cargo test` may compile the whole workspace |
| `test.test_dir` | Should be `tests` for standard layout (fixed by recent prep-tool update; older entries may say crate name) |
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
.venv/bin/python commit0/cli_rust.py get-tests CRATE_NAME
```

This runs `cargo test --list` inside `repos/<crate>-rs/`, then writes both formats:
- `commit0/data/rust_test_ids/CRATE_NAME.json` (human-readable; agent uses this preferentially)
- `commit0/data/rust_test_ids/CRATE_NAME.bz2` (compressed; canonical cache format)

A warning is shown if the cargo command fails (e.g., dependency MSRV mismatch) — fix the underlying issue before continuing.

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
```

The pipeline:
1. **Preflight**: validates `jq`, `bc`, `timeout`, `cargo`, `rustc`, `docker` on PATH (auto-resolves Docker.app on macOS if missing); checks API credentials per provider; probes the model API.
2. **`run_build_once`**: idempotently builds `commit0.base.rust:latest` + `commit0.repo.<crate>.<hash>:v0` Docker images.
3. **Stage 1 (Draft)**: agent edits files locally with cargo; eval runs in Docker.
4. **Stage 2 (Lint refine)**: clippy feedback in agent context; eval again.
5. **Stage 3 (Test refine)**: cargo test feedback per file; final eval.

Output lands in `output/<repo>/<model>/stage{1,2,3}_*/` with `trajectory.md` and `output.json` per module.

---

## Method B: Increasing Benchmark Difficulty

The default Rust pipeline is the easiest possible variant — function bodies stubbed, everything else preserved (including doc comments, type definitions, full docs.rs spec). To raise difficulty:

| Flag | What it does | Expected pass-rate drop |
|---|---|---|
| `--strip-docs` (in prep) | Strips every `#[doc]` attribute (covers `///` outer, `//!` inner, and explicit `#[doc(...)]`). 22 fold overrides cover all syn item kinds. | ~40-55% |
| `--skip-spec` (in prep) | Uses README fallback instead of full docs.rs PDF | ~10-15% additional |
| `--max-iteration 1` (in pipeline) | One attempt per file per stage (no retries) | ~10-20% additional |
| `--no-stage3-lint` (in pipeline) | Disable lint info in Stage 3 (lower-quality test refine) | ~5% additional |
| Lower stage count (`--skip-to-stage 1` with no later stages) | One-shot benchmark, no test feedback | ~30-50% (huge swing) |

Combining `--strip-docs` + `--skip-spec` + `--max-iteration 1` is roughly equivalent to the Python pipeline's "combined" mode in terms of difficulty.

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

## Configuration

### `.commit0_rust.yaml` (auto-generated by prep tool)

```yaml
# commit0 Rust config for <crate>
dataset_name: ./<crate>-rs_dataset.json
dataset_split: test
repo_split: all
base_dir: repos

# Repo details
# upstream: <owner>/<repo>
# fork: <your_org>/<repo>
# crate: <crate>
# language: rust
# test_cmd: cargo test -p <crate>
# src_dir: src
```

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
RUST_VERSION=1.85.0                  # pin toolchain in dataset metadata
RUST_VERSION_STRICT=false            # fail preflight on rustc mismatch
WATCHDOG_MTIME_FALLBACK_MIN_SECS=3600  # min wall-time cap when mtime probe broken
SPEC_DEPS_AUTO_INSTALL=true          # auto-pip-install spec scrape deps
CARGO_NEXTEST_VERSION=0.9.96         # baked into Docker image
PROBE_TIMEOUT=30                     # model API probe timeout (sec)
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

### `run_pipeline_rust.sh` CLI options

| Flag | Default | Description |
|---|---|---|
| `--model` | (required) | Preset or full model string |
| `--dataset` | (required) | Path to dataset JSON |
| `--branch` | auto | Override auto-generated branch name |
| `--repo-split` | `all` | Repo split to evaluate |
| `--max-iteration` | `3` | Agent iterations per file per stage |
| `--stage-timeout` | `0` | Hard stage timeout in seconds (0=disabled) |
| `--inactivity-timeout` | `900` | Kill agent if no log activity for N seconds |
| `--max-wall-time` | `86400` | Absolute per-stage wall-time cap |
| `--eval-timeout` | `3600` | Evaluation timeout in seconds |
| `--backend` | `local` | `local` or `modal` (note: "local" still uses Docker) |
| `--no-stage3-lint` | enabled | Disable lint in Stage 3 |
| `--no-spec-info` | enabled | Disable spec PDF injection |
| `--num-samples` | `1` | Number of independent samples (pass@k) |
| `--skip-to-stage` | start at 1 | Resume from a specific stage (1, 2, or 3) |
| `--max-test-output-length` | `15000` | Max chars of test output passed to agent |
| `--max-parallel-repos` | `1` | Parallel repo workers (keep at 1 unless sandbox is robust) |

---

## File Reference

### `tools/prepare_repo_rust.py` (771 lines)

| Function | What it does |
|---|---|
| `prepare_rust_repo(...)` | Top-level: forks, clones, stubs, scrapes spec, pushes branch, emits dataset entry |
| `stub_source_dir(repo_dir, src_dir, strip_docs=False)` | Invokes the native ruststubber binary; parses its summary output to count stubbed files |
| `scrape_spec(crate, repo_dir)` | Calls `scrape_rust_pdf.scrape_rust_spec` to render docs.rs into a compressed PDF; falls back to README scrape if docs.rs is unavailable |
| `_ensure_spec_scrape_deps(auto_install=True)` | Auto-installs Playwright + PyMuPDF + PyPDF2 + beautifulsoup4 + chromium if missing |
| `create_dataset_entry(...)` | Builds the `RustRepoInstance` dict written to the dataset JSON |
| `get_default_branch(repo_dir)` | Resolves upstream's default branch (master/main fallback chain) |
| CLI flags | `--repo --crate --src-dir --test-cmd --org --clone-dir --output --rust-version --edition --packages --skip-spec --strip-docs --dry-run` |

### `tools/ruststubber/` (Rust binary)

| File | What it does |
|---|---|
| `src/main.rs` | CLI (`--input-dir --output-dir --in-place --strip-docs`), WalkDir traversal, two-tier test-module detection (path heuristic + cross-file `cfg(test)` scan from lib.rs/main.rs), `cargo metadata` integration for accurate crate-root discovery |
| `src/stubber.rs` | `StubFolder` (impl `syn::fold::Fold`), 22 fold overrides covering every item kind that can hold doc attributes (fn, struct, enum, union, trait, impl, mod, const, static, type, foreign, variant, field, plus their members), `StubOptions { strip_docs }` config, fn-body replacement with `panic!("STUB: not implemented")` |
| `src/lib.rs` | Public API: `stub_file`, `stub_file_with_options`, `stub_source`, `stub_source_with_options`, `StubFolder`, `StubOptions` |

### `commit0/cli_rust.py` (CLI wrapper for Rust-specific commands)

| Command | What it does |
|---|---|
| `setup REPO_SPLIT --dataset-name <path>` | Clones fork into `repos/<crate>/`, checks out commit0 branch at base_commit |
| `build` | Builds `commit0.base.rust:latest` then per-crate `commit0.repo.<crate>.<hash>:v0` images via `docker_build_rust` |
| `get-tests REPO_NAME [--base-dir repos]` | Calls `agent.agent_utils_rust.get_rust_test_ids`, writes both `<crate>.json` and `<crate>.bz2` to `commit0/data/rust_test_ids/` |
| `test REPO [TEST_IDS]` | Runs `cargo test` directly (no Docker; for debugging) |
| `evaluate --branch BRANCH ...` | Runs the eval harness: applies the agent's patch, runs `cargo test` in Docker, aggregates pass rates |
| `lint [FILES]` | `cargo clippy` + `cargo fmt --check` on the working tree |
| `save REPO_OR_SPLIT --org ORG` | Pushes agent-modified branches back to GitHub forks |
| `health-check` | Reports rustc/cargo/clippy/rustfmt/nextest versions |

### `commit0/harness/` (Rust-specific harness)

| File | What it does |
|---|---|
| `constants_rust.py` | `RUST_VERSION` (env-overridable, semver-validated, default `1.84.0`), `CARGO_NEXTEST_VERSION` (env-overridable, default `0.9.96`), `RUST_STUB_MARKER`, `RUST_BASE_BRANCH = "commit0"`, `RUST_GITIGNORE_ENTRIES`, `RUST_SPLIT` (empty by default; populated by loaders), `RUN_RUST_TESTS_LOG_DIR`, `RUST_TEST_IDS_DIR`, `DOCKERFILES_RUST_DIR`, `RustRepoInstance` model |
| `setup_rust.py` | Clones the dataset fork into `repos/<crate>/`, creates `commit0` branch at `base_commit`, appends gitignore entries |
| `build_rust.py` | Reads dataset, deduplicates by image hash, delegates per-crate builds to `docker_build_rust.build_repo_images` |
| `docker_build_rust.py` | Builds the base + per-crate images via buildx; uses Docker `--build-context oci-layout://` for multi-arch when applicable; auto-detects MITM proxy CA |
| `spec_rust.py` | Generates eval-shell scripts: `git apply --check` precheck → `--3way` merge fallback → `-C0` zero-context fallback; reverts test paths via `git checkout`; captures all stderr for postmortem |
| `evaluate_rust.py` | Runs `run_rust_tests` per repo, aggregates results, parses both JSON-line nextest output AND text-mode cargo output (handles `test result: ok. N passed; M failed;`) |
| `run_rust_tests.py` | Constructs patch from local branch diff, invokes Docker eval, collects `test_output.txt` + `cargo_test_exit_code.txt` |
| `rust_test_parser.py` | JSON-line nextest event parser (`type=test event=ok/failed/ignored/timeout`) → `RustTestResult` |
| `patch_utils_rust.py` | `generate_rust_patch(repo_dir, base, target, *, strict=False)` → filters `target/` artefacts; `InvalidRustPatchError` on strict-mode validation failure |
| `health_check_rust.py` | Reports rustc/cargo/clippy/rustfmt/cargo-nextest install status |

### `agent/agent_utils_rust.py` (656+ lines)

| Function / Variable | What it does |
|---|---|
| `get_rust_test_ids(repo_path)` | Runs `cargo test --list` via `_run_cargo_with_retry`; falls back to `.json` then `.bz2` cache in `RUST_TEST_IDS_DIR` |
| `_run_cargo_with_retry(args, cwd, ...)` | Exponential-backoff retry with jitter; classifies stderr against `_TRANSIENT_CARGO_ERRORS` (49 markers: network/DNS/TLS/HTTP-5xx/IO/cargo-lock/crates.io) and `_PERMANENT_CARGO_ERRORS` (19 markers: disk-full/auth/parse-errors); deny-list precedence |
| `find_rust_files_to_edit(src_dir)` | Walks `src/` collecting `.rs` files, excludes `tests/benches/examples/target/.git`, skips `build.rs` |
| `get_target_edit_files_rust(src_dir)` | Subset of above that contain `RUST_STUB_MARKER` |
| `extract_rust_function_stubs(file)` | Regex-based extraction of fn signatures + line numbers for files with stub markers |
| `get_message_rust(...)` | Builds the agent system prompt: template + function list + dependency content + spec PDF text (with summarization) |
| `summarize_rust_test_output(raw, max_length, model, ...)` | 3-tier: deterministic regex extract → LLM summarize → tail-truncate |

### `agent/run_rust_agent.py` (~660 lines)

| Function | What it does |
|---|---|
| `run_rust_agent(branch, override, backend, ...)` | Main entry: multiprocessing pool over Rust repos in dataset, calls `run_rust_agent_for_repo` per |
| `run_rust_agent_for_repo(...)` | Per-repo work: opens git, ensures `commit0` branch at base_commit, instantiates `RustAiderAgents`, iterates files in test/lint/draft modes |
| `get_rust_message(agent_config, repo_path, target_files)` | Builds the agent prompt (similar to `get_message_rust` but stage-aware) |
| `get_rust_lint_cmd(repo_path)` | Returns `bash -c 'cargo clippy --all-targets --all-features -- -D warnings' --` (the bash wrapper prevents aider from appending filenames to clippy) |

### `agent/agents_rust.py`

| Function | What it does |
|---|---|
| `RustAiderAgents.run(...)` | Subclass of aider runner; wires `lint_cmds={"rust": ...}`, sets `filename_to_lang` mapping for `.rs` |

### `run_pipeline_rust.sh` (~1660 lines)

| Function | What it does |
|---|---|
| `preflight()` | Validates required CLIs, **auto-resolves Docker.app on macOS**, probes toolchain versions (semver-aware), checks API credentials per provider, model API probe |
| `resolve_model()` (sourced from `commit0/harness/resolve_model.sh`) | Maps presets to full model IDs, sets MODEL_SHORT |
| `resolve_dataset()` | Resolves dataset name or path; supports `<name>` (looks for `<name>_dataset.json`), full path, or known split name |
| `ensure_spec_docs_rust()` | Pre-pipeline: ensures each repo has `spec.pdf.bz2` (checks repo dir → specs/ cache → scrape from URL). Tracks failures via `[ENSURE_SPECS_SUMMARY]` marker |
| `verify_spec_docs_rust()` | Post-provision: fatal if any repo missing spec and `USE_SPEC_INFO=true` |
| `run_build_once()` | Idempotent Docker-image build before first eval. Gated on `_pipeline_build_done` flag. Calls `commit0/cli_rust.py build` |
| `run_agent()` | Launches `agent.cli_rust` under `watchdog_run` |
| `watchdog_run()` | Activity-based timeout with three-tier killing: absolute_max wall-cap → hard_timeout when inactive → inactivity_timeout. **Auto-halves absolute_max with env-configurable floor when mtime probe fails** |
| `run_evaluate()` | Runs `commit0/cli_rust.py evaluate` with timeout |
| `stage_1_draft()` / `stage_2_lint()` / `stage_3_test()` | Stage orchestrators (write_agent_config → run_agent → run_evaluate) |
| `run_single_sample(idx)` | Executes full 3-stage pipeline for one sample (pass@k loop) |

---

## Troubleshooting

### Docker installed but `command not found`

**Symptom**: pipeline preflight reports `docker: command not found` despite Docker Desktop being installed.

**Cause**: Docker Desktop on macOS sometimes leaves `/usr/local/bin/docker` as a symlink to `/Volumes/Docker/...` (a dismounted DMG path).

**Fix (automatic)**: the pipeline's `preflight()` auto-resolves `/Applications/Docker.app/Contents/Resources/bin/` if `docker` isn't on PATH.

**Fix (manual)**: `ln -sf /Applications/Docker.app/Contents/Resources/bin/docker ~/.local/bin/docker && export PATH="$HOME/.local/bin:$PATH"`

### Eval fails with HTTP 404 "image not found on Docker Hub"

**Cause**: previous pipeline runs didn't build the Docker image before invoking eval.

**Fix (automatic in current code)**: `run_build_once()` runs before the first `run_evaluate` call. If you're using older `run_pipeline_rust.sh`, manually run:
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

**Fix (automatic in current code)**: `tools/_git_auth.get_github_token()` falls back to `gh auth token` (keyring) on env-token validation failure. To refresh `.env`:
```bash
gh auth token > /tmp/token && sed -i.bak "s/^GITHUB_TOKEN=.*/GITHUB_TOKEN=$(cat /tmp/token)/" .env
```

### Watchdog killed agent prematurely on macOS

**Cause**: macOS doesn't have `/proc/self/status`, so `get_mtime` probe returns 0 and the watchdog's inactivity detection becomes non-functional.

**Fix (automatic in current code)**: watchdog auto-halves `absolute_max` with floor at `WATCHDOG_MTIME_FALLBACK_MIN_SECS` (default 3600s = 1 hour) when the mtime probe fails. Increase the env var for longer-running benchmarks.

### Cost extraction returns 0 across all stages

**Cause**: `output.json` `metrics.total_cost` field missing or zero, AND `aider.log` regex fallback didn't match.

**Fix**: check the aider log format — providers like Vertex may use a different cost field. Look for `tokens sent, tokens received, cost: $X.XX` in `aider.log` to confirm the regex still matches. File-level metric override pattern: see `extract_all_stage_costs` in `run_pipeline_rust.sh`.

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
- [ ] `--strip-docs` flag set if you want the harder benchmark variant
- [ ] No leftover orphan branches on the fork from previous runs (clean up via `gh repo edit --delete-branch BRANCH`)

---

## Reference Example: `discord/itsdangerous-rs`

This section documents a complete run, including corrections we encountered.

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

# Output: 13 files stubbed (0 errors), spec scraped from docs.rs (37 pages, 1.2 MB),
# fork pushed to Aman-Yadav-Ethara-AI/itsdangerous-rs branch=commit0_all

# 2. Setup
.venv/bin/python commit0/cli_rust.py setup all \
    --dataset-name ./itsdangerous-rs_dataset.json \
    --dataset-split test \
    --commit0-config-file .commit0_rust.yaml

# 3. Test ID collection
.venv/bin/python commit0/cli_rust.py get-tests itsdangerous-rs
# Output: Collected 17 test IDs → itsdangerous-rs.json + itsdangerous-rs.bz2

# 4. Run pipeline
bash run_pipeline_rust.sh \
    --model claude-sonnet-4-6 \
    --dataset ./itsdangerous-rs_dataset.json \
    --repo-split all \
    --max-iteration 3 \
    --skip-to-stage 1 \
    2>&1 | tee logs/sonnet46_itsdangerous-rs_stage1.log
```

### Results (Stage 1 only, no `--strip-docs`)

| Stage | Pass Rate | Passed/Total | Stage Cost | Time |
|---|---|---|---|---|
| Stage 1 (Draft) | **100%** | **17/17** | ~$0.40 | ~12 min |

Note: this was the BASELINE with all docs preserved (including a working API usage example in the file-level `//!`).

### Harder variant with `--strip-docs`

Re-run prep with `--strip-docs`. Expected drop to 40-60% on sonnet 4.6 (no longer has the literal usage example to imitate).

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

---

## Quick Reference: Pipeline Self-Healing Behaviour

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
| Watchdog inactivity timeout broke on macOS | Auto-halved `absolute_max` with env-configurable floor (`WATCHDOG_MTIME_FALLBACK_MIN_SECS`) |
| `RUST_VERSION` mismatch in `.env` | Semver-validated; `RUST_VERSION_STRICT=true` to fail-fast, else warn-only |
| Cargo network transient failures | `_run_cargo_with_retry` with 3 attempts, exponential backoff + jitter; permanent error deny-list |
| Doc attrs leaking context | `--strip-docs` in ruststubber + prep tool; 22 fold overrides cover every syn item kind |

All of these were either silent failures or required manual workarounds in earlier iterations of the pipeline; they're now first-class auto-handled paths with clear logging when they fire.
