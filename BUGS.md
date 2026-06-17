# BUGS — kaiju-harness @ 4d901b7c

## Open (HOLD / BLOCK)

| Ticket | Sev | Disp | Title | Prior |
|--------|-----|------|-------|-------|
| Q-001 | LOW | HOLD | Type-check + lint baseline violations (inherited LOW HOLD) | BUG-Q-001 |
| S-001 | MEDIUM | HOLD | XML parsing without defusedxml (inherited MEDIUM HOLD) | BUG-S-001 |
| S-002 | MEDIUM | HOLD | urllib.urlopen without scheme validation (file:// SSRF risk) (inherited MEDIUM HOLD) | BUG-S-002 |
| T-001 | MEDIUM | BLOCK | Duplicate test classes silently shadow earlier tests (inherited MEDIUM BLOCK) | BUG-T-001 |
| D-001 | MEDIUM | HOLD | aider-chat + litellm pinned to moving branch 'main' (inherited MEDIUM HOLD) | BUG-D-001 |
| CG-001 | LOW | HOLD | shellcheck BLOCKED — 12,330 LOC Shell unverified this run | — |
| CG-002 | LOW | HOLD | gitleaks BLOCKED — no secrets scan this run | — |

## Coverage Gaps

| ID | Title | Remediation |
|----|-------|-------------|
| CG-001 | shellcheck not on PATH | `brew install shellcheck` (off-policy under A5) |
| CG-002 | gitleaks not on PATH | `brew install gitleaks` (off-policy under A5) |
