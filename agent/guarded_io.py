"""GuardedInputOutput — security boundary preventing Aider auto-add bypass.

Closes V17 (Aider auto-add bypass) from the reward-hacking threat model.

WHY THIS EXISTS
================
`aider.io.InputOutput(yes=True)` auto-confirms ALL `confirm_ask` prompts as 'y'.
Aider invokes `confirm_ask` for three file-add paths:

  1. "Add file to the chat?"
     - Path: file-mention in user/assistant message → file added to abs_fnames
     - Aider source: aider/coders/base_coder.py:1854

  2. "Allow edits to file that has not been added to the chat?"
     - Path: model emits SEARCH/REPLACE against a file in read_only_fnames or
       discovered via repo-map → file promoted to abs_fnames, edit applies
     - Aider source: aider/coders/base_coder.py:2360-2363
     - **EMPIRICAL EXPLOIT PATH** (flake8/kimik25 sample5 Stage 3)

  3. "Create new file?"
     - Path: model emits SEARCH/REPLACE creating a non-existent file
     - Aider source: aider/coders/base_coder.py:2341

With `yes=True`, all three prompts auto-confirm 'y' → file added → edit applies.
This means `read_only_fnames` provides NO enforcement in Aider 0.86.3.dev; it is
context-window optics only. See verified flow at base_coder.py:2323-2376 where
`allowed_to_edit` falls through to confirm_ask, then line 1893 appends the path
to abs_fnames after 'y'.

GuardedInputOutput IS the enforcement layer. It intercepts the three file-add
prompts via substring match, resolves the subject path through symlinks via
Path.resolve(), and refuses based on two sets:

  - protected_paths: NEVER add (e.g., absolute paths of test files)
  - allowed_add_paths: ONLY add if path is in this set (when non-empty)

Fails CLOSED on `subject=None` (returns False rather than delegating).

ATOMIC SHIP REQUIREMENT
========================
This module MUST ship together with the `read_only_fnames` plumbing in
`agents.py` and `agent_utils.py`. Shipping `read_only_fnames` alone makes
exploits WORSE: model sees test files labeled read-only, attempts SEARCH/REPLACE
against them as the only edit option, and prompt #2 auto-confirms 'y' under
`yes=True`. The pair must land in one PR.

Aider 0.86.3.dev citations:
  - confirm_ask signature: aider/io.py:807-815
  - 3 prompt strings: base_coder.py:1854, :2341, :2360-2363
  - Read-only fallthrough flow: base_coder.py:2323-2376
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from aider.io import InputOutput

_logger = logging.getLogger(__name__)


def _resolve_path(p: str) -> str:
    """Resolve a path to its canonical absolute form (follows symlinks).

    Resilient to non-existent paths (uses ``strict=False`` semantics): we want
    to compare what a path resolves to syntactically, even if the file does
    not yet exist (e.g., the "Create new file?" prompt).
    """
    try:
        return str(Path(p).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        # RuntimeError can fire on infinite-symlink-loop; OSError on weird paths.
        # Fall back to absolute-without-resolve so caller still gets a comparable string.
        return os.path.abspath(os.path.expanduser(p))


def _normalize_set(paths: Iterable[str] | None) -> set[str]:
    if not paths:
        return set()
    return {_resolve_path(p) for p in paths}


class GuardedInputOutput(InputOutput):
    """InputOutput subclass that refuses file-add prompts for protected paths.

    Construction is identical to ``aider.io.InputOutput`` plus two new kwargs:

        allowed_add_paths: Optional set of paths Aider IS allowed to add.
            When non-empty, any file-add prompt for a path NOT in this set is
            refused. When None or empty, no allowlist filtering happens (only
            ``protected_paths`` is enforced).

        protected_paths: Paths Aider is NEVER allowed to add. Takes precedence
            over ``allowed_add_paths`` — protected always wins.

    Both sets are stored as resolved absolute paths (symlinks followed).

    The three prompts this class intercepts are the substrings declared in
    ``_FILE_ADD_PROMPTS``. Substring match is used (not exact match) to be
    resilient to minor Aider wording changes on upgrade; a CI test on Aider
    version pin should re-verify all three strings still exist in Aider source.
    """

    # Substring fragments that identify file-add prompts in Aider 0.86.3.dev.
    # Substring (not exact) matching tolerates trailing punctuation drift.
    _FILE_ADD_PROMPTS: tuple[str, ...] = (
        "Add file to the chat?",
        "Allow edits to file that has not been added to the chat?",
        "Create new file?",
    )

    # Substrings that identify aider's shell-execution prompts. When yes=True,
    # the parent class auto-approves ALL confirm_ask questions — including shell
    # commands the MODEL emits (e.g. `yarn install`, `curl exfil.example.com`,
    # `rm -rf tests/`). File-add filtering doesn't cover this because shell
    # prompts arrive with no subject path. Default-deny at prompt level so a
    # model can't spawn arbitrary subprocesses under our watch. This does NOT
    # affect aider's auto_test / auto_lint execution: those run `test_cmd` /
    # `lint_cmds` directly (not via confirm_ask), so pipeline test/lint stays
    # fully functional. Only model-triggered shell escapes get blocked.
    _SHELL_EXECUTION_PROMPTS: tuple[str, ...] = (
        "Run shell command?",
        "Run these shell commands?",
        "Run shell commands?",
        "Execute this shell command?",
    )

    def __init__(
        self,
        *args,
        allowed_add_paths: Iterable[str] | None = None,
        protected_paths: Iterable[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._allowed_add_paths: set[str] = _normalize_set(allowed_add_paths)
        self._protected_paths: set[str] = _normalize_set(protected_paths)

    def _is_file_add_prompt(self, question: str | None) -> bool:
        if not question:
            return False
        return any(p in question for p in self._FILE_ADD_PROMPTS)

    def _is_shell_prompt(self, question: str | None) -> bool:
        if not question:
            return False
        return any(p in question for p in self._SHELL_EXECUTION_PROMPTS)

    def _refuse(self, question: str, subject: str | None, reason: str) -> bool:
        _logger.warning(
            "GuardedInputOutput refused prompt (%s): question=%r subject=%r",
            reason,
            question,
            subject,
        )
        # Aider treats 'n' / False as decline. Returning False is the documented
        # contract for confirm_ask when the answer is no.
        return False

    def confirm_ask(
        self,
        question,
        default="y",
        subject=None,
        explicit_yes_required=False,
        group=None,
        allow_never=False,
    ):
        """Intercept file-add and shell-execution prompts; delegate all others to super.

        Signature MUST match aider/io.py:807-815 exactly (Aider 0.86.3.dev).
        Shell prompts are default-deny (no subject to check — the whole class of
        model-triggered shell execution is refused). Fixed test_cmd / lint_cmds
        still run because aider invokes those DIRECTLY (not via confirm_ask).
        """
        if self._is_shell_prompt(question):
            return self._refuse(question, subject, "shell execution not permitted (anti-cheat + safety)")

        if self._is_file_add_prompt(question):
            # Fail CLOSED: if no subject was passed, we cannot tell which file
            # Aider wants to add. Default-deny instead of delegating to super
            # (which under yes=True would silently return 'y').
            if subject is None:
                return self._refuse(question, subject, "subject=None (fail-closed)")

            resolved = _resolve_path(str(subject))

            if resolved in self._protected_paths:
                return self._refuse(question, subject, "path in protected_paths")

            if self._allowed_add_paths and resolved not in self._allowed_add_paths:
                return self._refuse(question, subject, "path not in allowed_add_paths")

        return super().confirm_ask(
            question,
            default=default,
            subject=subject,
            explicit_yes_required=explicit_yes_required,
            group=group,
            allow_never=allow_never,
        )
