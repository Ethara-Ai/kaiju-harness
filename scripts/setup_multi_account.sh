#!/usr/bin/env bash
# scripts/setup_multi_account.sh
#
# Manage multiple Claude Code subscription accounts for rate-limit failover via
# the bridge's existing `MultiAccountCredentialProvider` pool.
#
# Why this exists: the `claude` CLI uses ONE fixed macOS keychain service name
# (`Claude Code-credentials`). Logging in account 2 would overwrite account 1.
# This script backs up account 1, lets you switch accounts in the CLI, captures
# account 2 to a second keychain service, then restores account 1.
#
# Usage:
#   scripts/setup_multi_account.sh add-from-cli      # interactive (Path A)
#   scripts/setup_multi_account.sh add-from-file F   # paste-in (Path B)
#   scripts/setup_multi_account.sh enable            # uncomment KAIJU_CC_ACCOUNT_POOL in .env
#   scripts/setup_multi_account.sh disable           # comment it back out
#   scripts/setup_multi_account.sh status            # show pool + keychain state
#   scripts/setup_multi_account.sh restore-acct1     # rescue account 1 from backup
#   scripts/setup_multi_account.sh remove-acct2      # delete account 2 keychain entry
#
# See docs/MULTI_ACCOUNT_SETUP.md for the full runbook.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
BACKUP_DIR="${HOME}/.cache/kaiju-harness"
ACCT1_SERVICE="${CC_ACCT1_SERVICE:-Claude Code-credentials}"
ACCT2_SERVICE="${CC_ACCT2_SERVICE:-Claude Code-credentials-acct2}"
POOL_SPEC="keychain:${ACCT1_SERVICE}:keychain:${ACCT2_SERVICE}"

# ─── ui helpers ──────────────────────────────────────────────────────────────
red()    { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || die "This script only runs on macOS (uses /usr/bin/security)."
}

require_security_cmd() {
  command -v security >/dev/null 2>&1 || die "macOS \`security\` CLI not found."
}

# ─── keychain helpers ────────────────────────────────────────────────────────
keychain_read() {
  local svc="$1"
  security find-generic-password -s "$svc" -w 2>/dev/null
}

keychain_write() {
  local svc="$1" payload="$2"
  security add-generic-password -s "$svc" -a "$USER" -w "$payload" -U
}

keychain_delete() {
  local svc="$1"
  security delete-generic-password -s "$svc" >/dev/null 2>&1 || true
}

validate_payload() {
  # Validate that a string is a valid Claude Code credentials JSON.
  local payload="$1"
  python3 - <<PY 2>/dev/null || return 1
import json, sys
data = json.loads('''$payload''')
cc = data.get("claudeAiOauth", data)
required = ("accessToken", "refreshToken", "expiresAt")
missing = [k for k in required if k not in cc]
if missing:
    print(f"missing keys: {missing}", file=sys.stderr)
    sys.exit(1)
PY
}

# ─── subcommands ─────────────────────────────────────────────────────────────

cmd_add_from_cli() {
  require_macos
  require_security_cmd

  bold "=== Multi-account setup: Path A (via Claude CLI) ==="

  # Step 1: backup account 1
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  local backup_path="${BACKUP_DIR}/cc_acct1_backup_$(date +%Y%m%d-%H%M%S).json"
  local acct1
  acct1=$(keychain_read "$ACCT1_SERVICE") || die "Could not read $ACCT1_SERVICE — is account 1 logged in?"
  validate_payload "$acct1" || die "Account 1 keychain entry is malformed."
  printf '%s' "$acct1" > "$backup_path"
  chmod 600 "$backup_path"
  green "✓ Backed up account 1 to $backup_path"

  # Step 2: prompt user to switch accounts
  yellow ""
  yellow "Now log out of account 1 and log into account 2 via the Claude CLI."
  yellow "In another terminal:"
  yellow "    claude logout"
  yellow "    claude login    # ← log in with account 2's credentials"
  yellow ""
  read -rp "Press ENTER once you've logged into ACCOUNT 2 (or Ctrl-C to abort)... " _

  # Step 3: capture account 2
  local acct2
  acct2=$(keychain_read "$ACCT1_SERVICE") || die "Could not read $ACCT1_SERVICE after switch — login may have failed."
  validate_payload "$acct2" || die "Account 2 keychain entry is malformed."
  if [[ "$acct1" == "$acct2" ]]; then
    die "Account 1 and account 2 credentials are identical — did you actually switch accounts?"
  fi
  keychain_write "$ACCT2_SERVICE" "$acct2"
  green "✓ Saved account 2 to keychain entry: $ACCT2_SERVICE"

  # Step 4: restore account 1
  keychain_write "$ACCT1_SERVICE" "$acct1"
  green "✓ Restored account 1 to keychain entry: $ACCT1_SERVICE"

  yellow ""
  yellow "Backup retained at $backup_path (delete when comfortable)."
  bold "Next steps:"
  echo "  1) ${0##*/} enable            # uncomment KAIJU_CC_ACCOUNT_POOL in .env"
  echo "  2) scripts/claude_code_bridge.sh stop && scripts/claude_code_bridge.sh start"
  echo "  3) ${0##*/} status            # verify pool is active"
}

cmd_add_from_file() {
  require_macos
  require_security_cmd
  local file="${1:-}"
  [[ -n "$file" ]] || die "Usage: ${0##*/} add-from-file <path-to-account2-credentials.json>"
  [[ -f "$file" ]] || die "File not found: $file"

  bold "=== Multi-account setup: Path B (from file) ==="
  local payload
  payload=$(cat "$file")
  validate_payload "$payload" || die "File is not a valid Claude Code credentials JSON."
  keychain_write "$ACCT2_SERVICE" "$payload"
  green "✓ Saved account 2 to keychain entry: $ACCT2_SERVICE (from $file)"

  bold "Next steps:"
  echo "  1) ${0##*/} enable"
  echo "  2) scripts/claude_code_bridge.sh stop && scripts/claude_code_bridge.sh start"
  echo "  3) ${0##*/} status"
}

cmd_enable() {
  [[ -f "$ENV_FILE" ]] || die ".env not found at $ENV_FILE"

  # Idempotent: respect a non-empty value, fill an empty one, uncomment a
  # commented one, or append if missing. (An uncommented-but-empty line — as
  # shipped by older .env.example — must be POPULATED, not treated as "active".)
  if grep -qE '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE"; then
    local current
    current=$(grep -E '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE" | head -1 | sed -E 's/^[^=]*=//')
    current="${current%\"}"; current="${current#\"}"; current="${current//[[:space:]]/}"
    if [[ -n "$current" ]]; then
      green "KAIJU_CC_ACCOUNT_POOL is already set in $ENV_FILE."
      grep -nE '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE"
      return 0
    fi
    # Present but empty -> write the value in place.
    sed -i '' -E "s|^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=.*\$|KAIJU_CC_ACCOUNT_POOL=\"${POOL_SPEC}\"|" "$ENV_FILE"
    green "✓ Set KAIJU_CC_ACCOUNT_POOL in $ENV_FILE"
    grep -nE '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE"
    return 0
  fi

  if grep -qE '^[[:space:]]*#[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE"; then
    # Uncomment the commented line AND ensure it carries the value (the shipped
    # example may be commented-and-empty).
    sed -i '' -E "s|^[[:space:]]*#[[:space:]]*KAIJU_CC_ACCOUNT_POOL=.*\$|KAIJU_CC_ACCOUNT_POOL=\"${POOL_SPEC}\"|" "$ENV_FILE"
    green "✓ Enabled KAIJU_CC_ACCOUNT_POOL in $ENV_FILE"
  else
    {
      echo ""
      echo "# Multi-account failover (added $(date +%Y-%m-%d) by setup_multi_account.sh)"
      echo "KAIJU_CC_ACCOUNT_POOL=\"${POOL_SPEC}\""
    } >> "$ENV_FILE"
    green "✓ Appended KAIJU_CC_ACCOUNT_POOL to $ENV_FILE"
  fi

  yellow "Restart the bridge to activate: scripts/claude_code_bridge.sh stop && start"
}

cmd_disable() {
  [[ -f "$ENV_FILE" ]] || die ".env not found at $ENV_FILE"
  if ! grep -qE '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE"; then
    yellow "KAIJU_CC_ACCOUNT_POOL is not active in $ENV_FILE."
    return 0
  fi
  sed -i '' -E 's|^[[:space:]]*(KAIJU_CC_ACCOUNT_POOL=.*)$|# \1|' "$ENV_FILE"
  green "✓ Commented out KAIJU_CC_ACCOUNT_POOL in $ENV_FILE"
  yellow "Restart the bridge to deactivate: scripts/claude_code_bridge.sh stop && start"
}

cmd_status() {
  require_macos
  require_security_cmd
  bold "=== Keychain entries ==="
  for svc in "$ACCT1_SERVICE" "$ACCT2_SERVICE"; do
    if security find-generic-password -s "$svc" >/dev/null 2>&1; then
      green "  ✓ $svc"
    else
      red   "  ✗ $svc  (not present)"
    fi
  done
  echo ""
  bold "=== .env state ==="
  if [[ -f "$ENV_FILE" ]] && grep -E '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE" >/dev/null; then
    green "  active:"
    grep -nE '^[[:space:]]*KAIJU_CC_ACCOUNT_POOL=' "$ENV_FILE"
  else
    yellow "  KAIJU_CC_ACCOUNT_POOL not active in .env (run \`${0##*/} enable\`)"
  fi
  echo ""
  bold "=== Bridge /quota (live state — only useful if bridge already running) ==="
  if command -v curl >/dev/null 2>&1; then
    local resp
    resp=$(curl -sf http://127.0.0.1:8765/quota 2>/dev/null || true)
    if [[ -n "$resp" ]]; then
      if command -v jq >/dev/null 2>&1; then
        echo "$resp" | jq
      else
        echo "$resp"
      fi
    else
      yellow "  bridge not reachable on http://127.0.0.1:8765"
    fi
  fi
}

cmd_restore_acct1() {
  require_macos
  require_security_cmd
  bold "=== Restore account 1 from backup ==="
  local latest
  latest=$(ls -t "${BACKUP_DIR}"/cc_acct1_backup_*.json 2>/dev/null | head -1 || true)
  [[ -n "$latest" ]] || die "No backup found in ${BACKUP_DIR}/cc_acct1_backup_*.json"
  yellow "Restoring from: $latest"
  local payload
  payload=$(cat "$latest")
  validate_payload "$payload" || die "Backup is malformed."
  keychain_write "$ACCT1_SERVICE" "$payload"
  green "✓ Account 1 restored to keychain entry: $ACCT1_SERVICE"
}

cmd_remove_acct2() {
  require_macos
  require_security_cmd
  bold "=== Remove account 2 keychain entry ==="
  keychain_delete "$ACCT2_SERVICE"
  green "✓ Removed (or was already absent): $ACCT2_SERVICE"
  yellow "Consider also: ${0##*/} disable (to comment out KAIJU_CC_ACCOUNT_POOL)"
}

cmd_help() {
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ─── dispatch ────────────────────────────────────────────────────────────────
case "${1:-help}" in
  add-from-cli)   shift; cmd_add_from_cli "$@" ;;
  add-from-file)  shift; cmd_add_from_file "$@" ;;
  enable)         shift; cmd_enable "$@" ;;
  disable)        shift; cmd_disable "$@" ;;
  status)         shift; cmd_status "$@" ;;
  restore-acct1)  shift; cmd_restore_acct1 "$@" ;;
  remove-acct2)   shift; cmd_remove_acct2 "$@" ;;
  -h|--help|help) cmd_help ;;
  *)              red "Unknown command: $1"; cmd_help; exit 1 ;;
esac
