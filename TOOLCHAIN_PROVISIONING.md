# Universal JS/TS Toolchain Provisioning — Architecture

**Status:** Design locked, implementation in progress.
**Owner:** kaiju-harness core.
**Last updated:** 2026-07-16.

Grounded in verified research: Metis pre-planning analysis + 3 explore agents (toolchain touchpoints, Node-version handling, tests/shared code). Oracle infra was unavailable at design time; the architect proceeded with the collected evidence.

---

## 1. Module Layout + Contracts

**One file:** `tools/_toolchain.py` (underscore-prefix matches `tools/_git_auth.py`, `tools/_versioning.py`, `tools/_repo_naming.py` convention for shared low-level utilities).

**Why `tools/` not `commit0/harness/` or `agent/`:**
- `tools/prepare_repo_*.py` is where PM provisioning is triggered on the host today.
- `commit0/harness/` is data + Docker specs; keeping toolchain out of it preserves layering.
- `agent/` is runtime; provisioning should be complete before agents run.
- The module is imported from `commit0/harness/spec_{ts,js}.py` (which shells out to `npm install -g pnpm` at run script emit time) and from `agent/container/agent_image.py:91` — both cross layers, so `tools/` sits at the bottom of the import graph.

**CLI entry:** `python -m tools.toolchain <subcommand>` (thin `tools/toolchain.py` re-export of `_toolchain.cli()`), so `_toolchain.py` stays importable-only.

### Public API (frozen dataclasses + free functions, no classes-with-state)

```python
# Enums
class Tool(str, Enum):
    NPM = "npm"; PNPM = "pnpm"; YARN = "yarn"; BUN = "bun"; NODE = "node"; COREPACK = "corepack"

class TrustMode(str, Enum):
    STRICT = "strict"; NORMAL = "normal"; PERMISSIVE = "permissive"

class InstallMethod(str, Enum):
    ALREADY_PRESENT = "already-present"
    NPM_GLOBAL = "npm-global"
    COREPACK_ACTIVATE = "corepack-activate"
    BUN_INSTALLER_SCRIPT = "bun-installer-script"
    VERSION_MANAGER = "version-manager"  # nvm/fnm/volta for Node

# Frozen dataclasses
@dataclass(frozen=True)
class ToolchainConfig:
    trust_mode: TrustMode
    no_auto_install: bool
    dry_run: bool
    offline: bool
    state_dir: Path
    log_path: Path | None
    allow_bun_install_script: bool
    allow_global_npm: bool
    node_switcher_preference: str  # "auto" | "fnm" | "nvm" | "volta" | "none"
    pin_node: str | None           # version override
    version_pins: dict[str, str]   # {"bun": "1.1.29", "pnpm": "9.15.0", ...}

@dataclass(frozen=True)
class ProvisionResult:
    tool: Tool
    version: str                    # resolved concrete version
    install_method: InstallMethod
    install_path: str               # `which` result post-install
    from_cache: bool                # true if already-present
    journal_id: str | None          # state file entry id, None for dry-run

@dataclass(frozen=True)
class NodeSwitchResult:
    resolved_version: str           # e.g. "18"
    switcher: str                   # "fnm" | "nvm" | "volta" | "none-controller"
    env_delta: dict[str, str]       # PATH-prefix + NVM_DIR etc. — merge into subprocess env
    source: str                     # detection source label

@dataclass(frozen=True)
class DoctorReport:
    config: ToolchainConfig
    tools: dict[Tool, str | None]                  # `which` per tool
    node_switcher: str | None                      # detected
    state_entries: list[dict]                      # from journal
    warnings: list[str]

# Free functions
def load_config(env: Mapping[str, str] | None = None) -> ToolchainConfig: ...
def ensure_pm(pm: str, *, version: str | None = None, config: ToolchainConfig | None = None) -> ProvisionResult: ...
def ensure_node(repo_root: Path, *, config: ToolchainConfig | None = None) -> NodeSwitchResult: ...
def build_env_for_repo(repo_root: Path, *, base_env: Mapping[str, str] | None = None,
                       config: ToolchainConfig | None = None) -> dict[str, str]: ...
def doctor(config: ToolchainConfig | None = None) -> DoctorReport: ...
def uninstall(tool: str, *, force: bool = False, config: ToolchainConfig | None = None) -> None: ...
def list_installed(config: ToolchainConfig | None = None) -> list[dict]: ...
```

### Error Taxonomy

Root: `class ToolchainError(RuntimeError)` — matches `GitAuthError(RuntimeError)` in `_git_auth.py:56`.

```
ToolchainError
├── ToolchainMissingError          — tool absent and cannot be auto-installed (no npm, no switcher, etc.)
├── ToolchainInstallFailedError    — install attempted, exited non-zero (carries stderr tail)
├── ToolchainTrustDeniedError      — action blocked by TrustMode (carries required mode)
├── ToolchainVersionMismatchError  — packageManager integrity hash mismatch, or version conflict
├── ToolchainConcurrencyError      — file-lock acquisition failed (timeout, deadlock)
├── ToolchainSwitchUnavailableError — Node switch required but no switcher on host
└── ToolchainAllowlistError        — pkg name outside {npm, pnpm, yarn, bun} (invariant I3 violation)
```

**Not** `OSError` / `EnvironmentError`: the pre-existing `_ensure_pkg_manager` used both inconsistently (Metis Trap #10 in `_git_auth.py`-style code review). A dedicated hierarchy lets callers distinguish "install failed" from "user forbade it" without brittle string checks. The pre-existing wrappers (`_ensure_pkg_manager` in `prepare_repo_ts.py` and `prepare_repo_js.py`) continue to raise `OSError` for backcompat via a translation layer, but new callers should catch `ToolchainError`.

---

## 2. Contracts + Invariants

Numbered; referenced by number throughout the codebase (docstrings, tests).

- **I1** — Node switching NEVER modifies parent `os.environ["PATH"]` or `os.environ["NVM_*"]`. All switching is via `env=` dict passed to `subprocess.run`. Enforced by unit test that verifies `os.environ` is unchanged after `build_env_for_repo`. (Addresses Metis Risk #1: aider/stubber/playwright breakage on old Node.)
- **I2** — `sudo` is NEVER invoked. If `npm install -g` needs root, catch `EACCES` from stderr, raise `ToolchainInstallFailedError` with the exact command the user should run manually. No hidden privilege escalation. (Addresses Metis Trap #10.)
- **I3** — Tool names must be in `SUPPORTED_TOOLS = {"npm", "pnpm", "yarn", "bun"}`. `packageManager` field with any other name raises `ToolchainAllowlistError`. (Addresses supply-chain typo-squat.)
- **I4** — When a repo's `packageManager` field contains an integrity hash (`yarn@4.5.0+sha224.abc...`) and it mismatches or corepack rejects it, we DO NOT fall back to `@latest`. Raise `ToolchainVersionMismatchError`. (Addresses Metis Risk #5.)
- **I5** — Every state-mutating operation is idempotent: `ensure_pm` on a version already present returns `ProvisionResult(from_cache=True)` in O(1) after `shutil.which` check. Re-running the entire pipeline is safe. No install writes happen before the file lock is held.
- **I6** — State journal writes are two-phase: `INTENT` record BEFORE the install subprocess, `SUCCESS` (or `FAILURE`) record AFTER. On startup, orphan `INTENT` records without a matching outcome are treated as "unknown"; verification (`shutil.which` + `--version`) either promotes to `SUCCESS` or triggers reinstall under lock.
- **I7** — Concurrency is serialized via `fcntl.flock` on `${state_dir}/toolchain.lock` (advisory, POSIX). Double-checked read after acquire: re-run `shutil.which` inside the critical section; if another process installed what we need, promote to cache-hit and return. Lock timeout: 300s (5 min), configurable via `KAIJU_TOOLCHAIN_LOCK_TIMEOUT`.
- **I8** — Failure loud, never silent. No `|| true`, no `except: pass`, no swallowed stderr. Every failure path carries a `stderr` tail (last 1000 chars) in the exception message. (Addresses Metis Risk #4.)
- **I9** — Two-Node model: kaiju's own tooling (aider, stubber, this module itself) always runs under the "controller Node" (whatever the parent process has). The "worker Node" (repo's install/test/build) is switched via subprocess env only. Never mixed. (Enforces I1.)
- **I10** — `dry_run` (`KAIJU_TOOLCHAIN_DRY_RUN=1`) makes every mutating operation print the exact command it would run and return a `ProvisionResult` with `journal_id=None`. Zero side effects.
- **I11** — `offline` (`KAIJU_TOOLCHAIN_OFFLINE=1`) blocks every network fetch (registry, corepack activate, bun installer). If the desired tool is not already present or in the state journal cache, raise `ToolchainMissingError` with a clear "offline mode; run once online to cache".

---

## 3. Trust Matrix

3 modes × 6 dimensions. Default is `normal`. `KAIJU_TOOLCHAIN_TRUST` env var.

| Dimension | strict | normal (default) | permissive |
|---|---|---|---|
| `curl \| bash` bun installer | ❌ | ✅ with pinned URL + SHA256 verify | ✅ URL pinned, SHA warning-only |
| `npm install -g <pm>` global | ❌ (require user-managed) | ✅ if prefix writable; loud fail if EACCES | ✅ + prints sudo hint |
| corepack self-install (`npm i -g corepack`) | ❌ | ✅ | ✅ |
| `packageManager` integrity-hash mismatch | fatal (`I4`) | fatal (`I4`) | WARN + fall back to `@latest` |
| `@latest` fallback when no `packageManager` field | fatal | ✅ WARN | ✅ silent |
| Version pin enforcement (`packageManager: yarn@X.Y.Z`) | fatal on drift | WARN on drift | best-effort |

**Rationale:**
- `strict` is for CI/audited environments where every install must be pre-approved.
- `normal` favors "just works" (matches user's stated intent) while keeping supply-chain risk bounded (SHA-verified bun, refuse hash mismatch).
- `permissive` unblocks debugging without compromising invariants I1–I3.

---

## 4. Node Version Switching

### Precedence chain (highest-priority first)

1. `KAIJU_PIN_NODE=<version>` env override — user's last word.
2. `package.json[packageManager]` — RFC-standardized field.
3. `package.json[engines.node]` — SpecifierSet via existing `tools/_versioning.py:normalize_semver_range`.
4. `package.json[volta.node]` — Volta's pin format (exact version).
5. `.nvmrc`.
6. `.node-version`.
7. `.tool-versions` (asdf-format; not currently parsed — added here).
8. `.github/workflows/**/*.yml[matrix.node-version]` (Tier B in `_versioning.py`).
9. `Dockerfile FROM node:X` (Tier C).
10. `DEFAULT_NODE_VERSION = 20` (constants).

Steps 2–9 already collected by `tools/node_version.py:collect_signals`. Steps 1, 7 added here.

### `lts/*` alias resolution

Node LTS names are historically stable; a small hardcoded map is faster and safer than shelling to `nvm ls-remote` (which needs network):

```python
_LTS_ALIASES = {
    "lts/*": "22",       # rolling; latest LTS as of 2026-01
    "lts/latest": "22",
    "lts/jod": "22",     # Node 22 LTS codename
    "lts/iron": "20",    # Node 20 LTS
    "lts/hydrogen": "18",# Node 18 LTS (EOL 2025-04)
    "lts/gallium": "16", # Node 16 LTS (EOL 2023)
    "lts/fermium": "14", # Node 14 LTS (EOL 2023-04)
}
```

Updated manually when a new LTS lands; drift-checked by a unit test that scrapes `SUPPORTED_NODE_VERSIONS` and verifies each alias resolves inside it.

### Switcher detection order

`_detect_switcher()` tries in order and returns the first hit:
1. **fnm** — `shutil.which("fnm")`. Fast Rust binary, no shell-hook dependency for one-shot invocation.
2. **volta** — `shutil.which("volta")`. Also binary-based, shims are fine for subprocess.
3. **nvm** — check `$NVM_DIR/nvm.sh` file existence. NVM is a shell function; can't `shutil.which("nvm")`.
4. **none-controller** — no switcher found; fall through to controller Node.

Override: `KAIJU_TOOLCHAIN_NODE_SWITCHER=fnm|nvm|volta|none|auto`.

### Subprocess-scoped switching (Invariant I1)

`build_env_for_repo(repo_root)` returns a fresh dict `env` where `PATH` has the target Node's `bin/` prepended. Parent `os.environ` is never touched.

**fnm example:**
```python
def _env_for_fnm(node_major: str, base_env: dict) -> dict:
    # fnm exec --using=<version> resolves the install path, but for a stable env
    # we call `fnm env --shell bash --version <ver>` and parse export lines.
    # Even simpler: use `fnm which <ver>` -> ${INSTALL_DIR}/bin/node.
    node_bin = _subprocess(["fnm", "which", node_major]).stdout.strip()
    if not node_bin:
        _subprocess(["fnm", "install", node_major], check=True)
        node_bin = _subprocess(["fnm", "which", node_major]).stdout.strip()
    bin_dir = str(Path(node_bin).parent)
    env = dict(base_env)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env
```

**nvm** requires sourcing `nvm.sh` in a subshell then echoing `PATH`. Wrapped as one helper `_env_for_nvm(node_major, base_env)`.

**volta** uses its shims; setting `VOLTA_HOME` and prepending `${VOLTA_HOME}/bin` is enough because shims read the target project's `package.json`.

### Fallback when NO switcher

If a repo requires Node ≠ controller Node AND no switcher is on the host:
- `TrustMode.strict` → `ToolchainSwitchUnavailableError` (fatal).
- `TrustMode.normal` → log WARN and proceed with controller Node. In the container path, the Dockerfile chose the right image, so this fallback is safe there.
- `TrustMode.permissive` → same as normal.

**Never silently proceed on wrong Node** in strict mode.

### Two-Node model justification

Kaiju's own controller runs Python 3.12 + aider + our stubber (needs Node ≥18 for ts-morph). Repos may need Node 14/16. The stubber runs BEFORE the repo's install/test, at project root, under controller Node — always safe. The repo's install/test runs inside `subprocess.run(..., env=build_env_for_repo(repo_dir))`, so the switched Node is scoped to that call. When the subprocess exits, controller Node is back. No mixed state.

---

## 5. Host vs Container Parity

**Provisioner runs in BOTH host and container, from the SAME Python module.** Single source of truth eliminates drift (Metis Risk #4).

### Container path (build time)

Replace the current `|| true`-swallowed lines in every `Dockerfile.node*`:

```dockerfile
# BEFORE (silent failures):
RUN corepack enable && (corepack prepare pnpm@latest --activate || true)
RUN npm install -g bun@latest || true

# AFTER (loud, unified):
COPY tools/_toolchain.py /opt/kaiju-toolchain/_toolchain.py
COPY tools/_versioning.py /opt/kaiju-toolchain/_versioning.py
COPY tools/node_version.py /opt/kaiju-toolchain/node_version.py
RUN python3 -m opt.kaiju-toolchain.provision_prewarm \
    --tools pnpm,yarn,bun \
    --trust normal
```

Where `provision_prewarm` is a tiny script invoking `ensure_pm("pnpm")`, `ensure_pm("yarn")`, `ensure_pm("bun")` under Container mode (state journal in `/opt/kaiju/toolchain-state.json`).

**Confirmed latent bug** (Metis Trap #11 verified): `Dockerfile.node14` calls `corepack enable` on Node 14, which predates bundled corepack (bundled since 16.13). Line silently fails; container ships without pnpm/yarn. The migration to `provision_prewarm` will catch this loudly. Fix path for Node 14 container: fall through to `npm install -g corepack@0.24.0` (last version supporting Node 14), then activate.

### Migration for the 6 Dockerfiles

- Dockerfile.node14: `npm i -g corepack@0.24.0 && python3 provision_prewarm ...` (special-case).
- Dockerfile.node16/18/20/22/24: `python3 provision_prewarm ...` (uses bundled corepack).
- Removal of `|| true` is atomic per Dockerfile — a failing build is caught in CI, not shipped to production.

### Host path (run time)

`tools/prepare_repo_ts.py:_ensure_pkg_manager` becomes a 3-line wrapper:

```python
def _ensure_pkg_manager(pkg_manager: str) -> None:
    from tools._toolchain import ensure_pm, ToolchainError
    try:
        ensure_pm(pkg_manager)
    except ToolchainError as e:
        raise OSError(str(e)) from e   # preserve backcompat
```

Same for `prepare_repo_js.py`. Callers unchanged.

---

## 6. State Journal

### Location

Precedence:
1. `KAIJU_TOOLCHAIN_STATE_DIR` env override.
2. `$XDG_STATE_HOME/kaiju/toolchain/` (Linux XDG spec).
3. `~/.kaiju/toolchain/` (fallback; matches Metis recommendation).

Files:
- `state.json` — the journal.
- `toolchain.lock` — advisory `fcntl.flock`.
- `provision.log` — append-only structured log if `KAIJU_TOOLCHAIN_LOG` is set.

### Schema (jsonschema-ish)

```json
{
  "version": 1,
  "controller": {
    "hostname": "MacBooks-MacBook-Pro-81.local",
    "os": "darwin-25.5.0",
    "python": "3.13.12",
    "created_at": "2026-07-16T12:34:56Z"
  },
  "entries": [
    {
      "id": "01HXY... (ULID)",
      "tool": "yarn",
      "version": "1.22.22",
      "install_method": "npm-global",
      "install_path": "/opt/homebrew/bin/yarn",
      "install_command": ["npm", "install", "-g", "yarn@1.22.22"],
      "phase": "success",              // "intent" | "success" | "failure"
      "started_at": "2026-07-16T12:34:56Z",
      "finished_at": "2026-07-16T12:35:12Z",
      "stderr_tail": ""                 // populated on failure
    }
  ]
}
```

### 2-phase write (Invariant I6)

```
1. acquire lock
2. re-check shutil.which(tool); if present at target version → cache hit, return
3. append INTENT entry, fsync, release-then-reacquire NOT needed (still holding lock)
4. subprocess.run(install_cmd)
5. append SUCCESS or FAILURE entry with returncode + stderr_tail
6. release lock
```

### Crash recovery

On `load_config()`, scan for entries with `phase == "intent"` and no matching `success`/`failure`. For each orphan:
- If `shutil.which(entry.tool)` returns a path AND `<tool> --version` matches `entry.version` → promote to SUCCESS.
- Else → mark as FAILURE with `stderr_tail="orphan-intent, could not verify install"`.

### Multi-version tracking

Node: multiple entries per major (14, 16, 18, 20, 22, 24) — one per version manager install.
pnpm/yarn/bun: single "current" entry per tool (corepack per-project shims are transparent; we don't track them individually).

---

## 7. Concurrency Model

`fcntl.flock(fd, LOCK_EX)` on `${state_dir}/toolchain.lock` (created 0644 on first open). Advisory, POSIX; works across processes but not across NFS (out of scope per Non-Goals).

### Critical section

Everything from "decide we need to install" through "state journal write" is inside the lock:

```python
def ensure_pm(pm: str, ...) -> ProvisionResult:
    if shutil.which(pm):                         # fast path, no lock
        return ProvisionResult(from_cache=True, ...)
    with _acquire_lock(config):                  # blocks up to timeout
        if shutil.which(pm):                     # double-checked read (I7)
            return ProvisionResult(from_cache=True, ...)
        _write_intent(entry)
        try:
            _do_install(pm, version, config)
            _write_success(entry)
        except Exception as e:
            _write_failure(entry, e)
            raise
    return ProvisionResult(from_cache=False, ...)
```

### Timeout + deadlock

`_acquire_lock` uses `LOCK_EX | LOCK_NB` in a loop with 100ms sleep, up to `KAIJU_TOOLCHAIN_LOCK_TIMEOUT` (default 300s). On timeout: `ToolchainConcurrencyError` naming the pid holding the lock (via `/proc/<pid>` or `ps` — best-effort).

### Container concurrency

Multiple slugs in one container → same lock file at `/opt/kaiju/toolchain/toolchain.lock`. `fcntl.flock` works across containers only if the lock file is on a shared bind mount (not our case), but within one container between multiple `python -m` invocations it works.

---

## 8. Env Var Surface

Full table. Loaded by `load_config(env=None)` which uses `os.environ` by default.

| Var | Values | Default | Purpose | Precedence |
|---|---|---|---|---|
| `KAIJU_NO_AUTO_INSTALL` | `1` / unset | unset | Skip all installs; if missing tool → raise | Overrides all install paths |
| `KAIJU_TOOLCHAIN_TRUST` | `strict` / `normal` / `permissive` | `normal` | Trust mode (§3) | Governs install methods |
| `KAIJU_TOOLCHAIN_STATE_DIR` | path | `$XDG_STATE_HOME/kaiju/toolchain` or `~/.kaiju/toolchain` | State journal + lock location | Overrides discovery |
| `KAIJU_TOOLCHAIN_DRY_RUN` | `1` / unset | unset | Print commands, don't run | Overrides all install/switch |
| `KAIJU_TOOLCHAIN_OFFLINE` | `1` / unset | unset | Block network fetches (I11) | Overrides trust matrix |
| `KAIJU_TOOLCHAIN_LOG` | path | unset | Append-only structured log | Additive |
| `KAIJU_TOOLCHAIN_NODE_SWITCHER` | `fnm` / `nvm` / `volta` / `none` / `auto` | `auto` | Force switcher | Overrides `_detect_switcher()` |
| `KAIJU_PIN_NODE` | `X` (major) or `X.Y.Z` | unset | Force Node version | Overrides all detection |
| `KAIJU_BUN_VERSION` | version | manifest default | Force bun install version | Overrides `packageManager` |
| `KAIJU_PNPM_VERSION` | version | manifest default | Force pnpm install version | Overrides `packageManager` |
| `KAIJU_YARN_VERSION` | version | manifest default | Force yarn install version | Overrides `packageManager` |
| `KAIJU_ALLOW_BUN_INSTALL_SCRIPT` | `1` / unset | unset | Explicit opt-in to bun.sh installer even when npm available | Additive |
| `KAIJU_TOOLCHAIN_ALLOW_GLOBAL_NPM` | `1` / `0` | `1` on macOS, `0` if `/usr/lib/node_modules` (Linux system npm) | Permit `npm i -g` | Overrides trust matrix |
| `KAIJU_TOOLCHAIN_LOCK_TIMEOUT` | seconds | `300` | Lock acquisition timeout | — |

Precedence when conflicts:
1. `KAIJU_NO_AUTO_INSTALL` and `KAIJU_TOOLCHAIN_OFFLINE` are veto-first — checked at the top of every install path.
2. `KAIJU_PIN_NODE` and `KAIJU_{PM}_VERSION` override repo declarations.
3. `KAIJU_TOOLCHAIN_TRUST` governs which install methods are legal.
4. Everything else is additive/permissive.

---

## 9. Bun Handling — Decision: HYBRID

**Chosen: Option C (Hybrid).** `npm install -g bun@X.Y.Z` when npm is available (the common case; registry-signed, mature). Fall back to pinned installer script (`https://bun.sh/install` at pinned version + SHA256 verified against shipped manifest) ONLY when npm is absent OR `KAIJU_ALLOW_BUN_INSTALL_SCRIPT=1` is set explicitly.

### Justification

- **npm path (default):** bun ships an npm package that is just a JS wrapper invoking the native bun binary from `node_modules/.bin`. It's registry-signed. The user's `npm i -g` prefix is already trusted for pnpm/yarn — bun is no worse.
- **installer script (fallback):** covers containers where node is present but npm registry unreachable, and hosts where user prefers `~/.bun` install location. Guarded by SHA verify (Metis Risk #2).

### Version manifest

Shipped as `tools/_toolchain_bun_manifest.json` (source-controlled). Structure:

```json
{
  "$schema": "1.0",
  "versions": {
    "1.1.29": {
      "installer_url": "https://bun.sh/install",
      "installer_sha256": "abcdef...",         // pinned; update by CI job
      "npm_package_version": "1.1.29"
    }
  },
  "default": "1.1.29"
}
```

### Update workflow

- `python -m tools.toolchain refresh-bun-manifest` — downloads latest bun.sh/install, computes SHA, appends to manifest, opens a PR (Metis-recommended).
- CI job runs weekly, opens PR if manifest is stale.

---

## 10. Migration Path (Ordered, Independently Mergeable)

| Step | Files touched | Tests | Rollback |
|---|---|---|---|
| **1. Fix bun.lock detection asymmetry** (pre-existing bug) | `tools/prepare_repo_ts.py:598-609` add `bun.lock` check | Extend `test_prepare_repo_ts_additions.py` with `test_detect_package_manager_bun_text_lock` | Revert diff |
| **2. Introduce `tools/_toolchain.py`** (types, config, journal, lock, no callers yet) | New file + `tools/tests/test_toolchain.py` | 20+ unit tests covering config load, lock, journal, error taxonomy | Delete files |
| **3. Add PM auto-install layer** (yarn/pnpm via npm, corepack self-bootstrap) | `_toolchain.py` extended | 10+ tests (macOS/Linux/dry-run/offline/trust modes) | Delete added functions |
| **4. Add Bun universal handling** (hybrid) | `_toolchain.py` + `tools/_toolchain_bun_manifest.json` | 6+ tests (SHA verify, missing npm, opt-in flag) | Delete added functions |
| **5. Migrate `prepare_repo_ts._ensure_pkg_manager`** (thin wrapper) | `tools/prepare_repo_ts.py:113-142` | Existing integration tests continue to pass | Restore original body |
| **6. Migrate `prepare_repo_js._ensure_pkg_manager`** (thin wrapper) | `tools/prepare_repo_js.py:376-402` | Same | Same |
| **7. Wire `spec_ts._package_manager_install` + `spec_js._install_cmd`** (emit provisioner call) | `commit0/harness/spec_ts.py:76-85`, `commit0/harness/spec_js.py:316-333` | Snapshot tests of emitted shell scripts | Restore originals |
| **8. Wire `agent/container/agent_image.py:91` eslint global install** (remove `\|\| true`) | `agent/container/agent_image.py:82-91` | Docker build test | Restore `\|\| true` |
| **9. Add Node switching** (fnm/nvm/volta, subprocess-scoped) | `_toolchain.py` + wire into `prepare_repo_ts/js` and `spec_ts/js` | 12+ tests (each switcher, no-switcher, lts/* aliases, I1 invariant) | Feature flag `KAIJU_TOOLCHAIN_NODE_SWITCHER=none` |
| **10. State journal + concurrency locks** (retrofit into existing paths) | `_toolchain.py` extended | Concurrency test (8 procs racing), crash-recovery test | Delete lock code (journal is additive-safe) |
| **11. `kaiju toolchain doctor` / `uninstall` / `ls` CLI** | New `tools/toolchain.py` (module runner) | CLI smoke tests | Delete CLI module |
| **12. Migrate 6 Dockerfiles** to `python3 -m tools.toolchain_provision_prewarm`; Node 14 special-cased with `corepack@0.24.0` | `commit0/harness/dockerfiles/Dockerfile.node{14,16,18,20,22,24}` | Docker build tests asserting `bun --version` + `pnpm --version` inside each | Restore original `RUN` lines |

Steps 1–4 are the MVP shipping the module + auto-install. Steps 5–8 wire it into callers. Steps 9–12 add advanced capabilities. Each step is <200 LOC diff.

---

## 11. Test Plan

### Unit tests (mock at subprocess boundary only; NEVER mock `shutil.which`)

- Trust matrix: 6 dimensions × 3 modes = 18 parametrized cases
- Config load: env var precedence, defaults, invalid values
- Error taxonomy: each subclass raised in the right scenario
- State journal: intent/success/failure lifecycle, orphan recovery, schema versioning
- Lock: acquisition timeout, LOCK_NB retry loop, timeout error content
- Bun manifest: SHA verify happy path, SHA mismatch, missing version
- Node version resolution: precedence chain including `lts/*` aliases
- Invariant I1: `build_env_for_repo` never mutates `os.environ`
- Invariant I3: `packageManager: unknown-pm@1.0.0` raises `ToolchainAllowlistError`
- Invariant I8: install failures always carry stderr tail

Target: 30+ unit tests, all in `tools/tests/test_toolchain.py`, all passing in <5s.

### Integration tests (real subprocess into scratch HOME)

- `HOME=$(mktemp -d) ensure_pm("pnpm")` on macOS + Linux
- `HOME=$(mktemp -d) ensure_pm("bun")` on macOS + Linux
- Corepack self-bootstrap when corepack is temporarily removed from PATH
- Concurrency: `xargs -P 8` running 8 `ensure_pm("pnpm")` in parallel — asserts single install

Gate behind `KAIJU_INTEGRATION_TESTS=1` env var; skip in CI by default (network required).

### Docker build tests

Run `docker build -f Dockerfile.node14` (etc.), then `docker run <image> sh -c "bun --version && pnpm --version && yarn --version"`. Asserts all three exit 0. Catches the current Dockerfile.node14 corepack silent failure.

### Smoke test (end-to-end)

- `run_trajectory.sh --repo szimek/signature_pad --lang ts` (yarn) — original failure case, MUST pass after Step 6.
- `run_trajectory.sh --repo sindresorhus/slugify --lang js` (npm + Node >=20) — validates Node switching after Step 9.
- `run_trajectory.sh --repo <bun-repo> --lang ts` (bun) — validates Bun path after Step 4.

---

## 12. Non-Goals (Explicit NOs)

Enumerated verbatim in `_toolchain.py`'s module docstring for future readers:

- Windows support.
- Deno / any PM outside `{npm, pnpm, yarn, bun}`.
- Modifying the user's repo (writing `.nvmrc`, upgrading lockfiles, editing `package.json`).
- Native compiler toolchain installation (gcc/make/python-for-node-gyp).
- CI cache management.
- Private registry auth (`.npmrc` manipulation).
- Auto-migrating classic yarn → berry.
- Running installs via `sudo`.
- Silent fallback when `packageManager` integrity hash mismatches.
- Managing Rust / Go / Java / Python versions (own modules).
- NFS-safe locking (advisory `fcntl.flock` only).
- Bun installer auto-refresh across major versions without human review.

---

## 13. Open Questions (require user decision post-implementation)

1. **Bun manifest refresh cadence.** Auto-PR weekly, monthly, or on-demand? Recommend: monthly + on-demand via `refresh-bun-manifest`.
2. **State journal retention.** Keep all entries forever, or rotate at 1000/10000? Recommend: keep all (JSON, small); trim on `uninstall`.
3. **`ensure_node` scope.** Currently designed to install the target Node if switcher supports it. Should install be opt-in (`KAIJU_TOOLCHAIN_INSTALL_NODE=1`) or default-on when switcher is present? Recommend: default-on with `TrustMode.normal`, off in `strict`.
4. **Container corepack version pinning.** Node 14 needs `corepack@0.24.0` (last supporting); newer Nodes bundle corepack. Should we pin corepack in EVERY container for reproducibility, or only in Node 14? Recommend: only Node 14 (others rely on bundled).
5. **`kaiju toolchain doctor` output format.** JSON, table, or both (with `--json` flag)? Recommend: table default, `--json` flag for CI parsing.

---

## Appendix A: Verified Facts (anchoring for future readers)

- `tools/prepare_repo_ts.py:113-142` and `tools/prepare_repo_js.py:376-402` — near-identical `_ensure_pkg_manager` bodies. Verified 2026-07-16.
- `tools/prepare_repo_ts.py:598-609` — `detect_package_manager` does NOT check `bun.lock`, only `bun.lockb`. Verified 2026-07-16.
- `tools/prepare_repo_js.py:289-292` — `_EXTRA_LOCKFILE_NAMES = {"bun": ("bun.lock",)}`. Verified 2026-07-16.
- `tools/node_version.py:1-297` — passive detection only; no switcher calls. Verified 2026-07-16.
- All 6 `Dockerfile.node{14,16,18,20,22,24}` — identical `RUN corepack enable && (corepack prepare pnpm@latest --activate || true)` at line 51. Verified 2026-07-16 via grep.
- `agent/container/run_pipeline_containerized.py:626` — only ONE mention of `ensure_pkg_manager` and it's a comment about Go. Provisioning is HOST-ONLY today. Verified 2026-07-16.
- `agent/container/agent_image.py:91` — `RUN command -v npm >/dev/null 2>&1 && npm install -g eslint@9 || true` — same anti-pattern. Verified 2026-07-16.
- `tools/_versioning.py:169 normalize_semver_range` — handles `>=X`, `^X`, `~X`, compound; does NOT handle `lts/*` (added here).
