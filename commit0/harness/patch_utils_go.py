"""Go-specific patch generation \u2014 filters diffs to Go source files only.

Mirrors ``patch_utils_rust.py`` posture:
- :func:`generate_go_patch` filters via the extension whitelist below.
- :func:`validate_go_patch` returns False when a filtered patch still carries
  non-Go paths (a filter regression or upstream ``git diff`` format change).
- :class:`InvalidGoPatchError` is raised when a strict caller sees validation
  fail; the raw filtered patch is preserved on the exception for postmortem.
"""

from __future__ import annotations

import logging
import re

import git as gitpython

from commit0.harness.utils import generate_patch_between_commits

logger = logging.getLogger(__name__)


GO_PATCH_EXTENSIONS = (".go", "go.mod", "go.sum")

# A ``diff --git`` header we're happy to keep points at a Go source file or a
# module-graph file (``go.mod``/``go.sum``). Anything else \u2014 README.md, .py,
# Makefile, generated binaries \u2014 must have been stripped by the filter. Match
# the "b/" side because ``git apply`` reads the destination path; ``a/`` mirrors
# it on renames.
_ALLOWED_PATH_RE = re.compile(
    r"^diff --git a/(\S+) b/(\S+)$"
)


class InvalidGoPatchError(Exception):
    """Raised when a filtered Go patch fails post-filter validation.

    Indicates a regression in :func:`generate_go_patch` (the extension filter
    let a non-Go file through) or an upstream ``git diff`` format change. The
    raw patch text is preserved on ``.patch`` so callers can persist it for
    postmortem instead of losing the evidence.
    """

    def __init__(
        self, patch: str, message: str = "patch contains non-Go paths"
    ) -> None:
        super().__init__(message)
        self.patch = patch


def _path_is_allowed(path: str) -> bool:
    """Return True if *path* ends with a Go source or module-graph extension."""
    return any(path.endswith(ext) for ext in GO_PATCH_EXTENSIONS)


def validate_go_patch(patch_content: str) -> bool:
    """Return True if every ``diff --git`` section in the patch names a Go file.

    Mirrors :func:`commit0.harness.patch_utils_rust.validate_rust_patch`: a
    filtered patch is valid iff no diff header carries a non-Go path. Content
    lines (not headers) are ignored because a legitimate Go file can mention
    a non-Go path in a string literal or comment.
    """
    for line in patch_content.splitlines():
        if not line.startswith("diff --git"):
            continue
        m = _ALLOWED_PATH_RE.match(line)
        if not m:
            # Malformed diff header \u2014 be strict; a well-formed section always
            # matches ``diff --git a/<x> b/<y>``.
            return False
        a_path, b_path = m.group(1), m.group(2)
        # ``git diff`` may write "/dev/null" on delete/create sides; that's not
        # a Go path but is safe when the OTHER side is a Go path.
        a_ok = a_path == "/dev/null" or _path_is_allowed(a_path)
        b_ok = b_path == "/dev/null" or _path_is_allowed(b_path)
        if not (a_ok and b_ok):
            return False
    return True


def generate_go_patch(
    repo_path: str,
    old_commit: str,
    new_commit: str,
    *,
    strict: bool = True,
) -> str:
    """Generate a patch filtered to .go/go.mod/go.sum files that existed at base commit.

    Prevents LLM-generated non-Go files from contaminating the diff. Uses a
    per-hunk include/exclude walk keyed on the ``diff --git`` header extension.

    Parameters
    ----------
    repo_path : str
        Path to the local git repository.
    old_commit : str
        Base commit sha.
    new_commit : str
        Target commit sha.
    strict : bool, keyword-only, default True
        When True (default), raise :class:`InvalidGoPatchError` if post-filter
        validation still sees a non-Go path (indicates a filter regression).
        When False, log a warning and return the filtered patch unchanged.

    Raises
    ------
    InvalidGoPatchError
        When *strict* is True and validation fails. The filtered patch is
        preserved on ``.patch`` for postmortem.
    """
    repo = gitpython.Repo(repo_path)
    full_patch = generate_patch_between_commits(repo, old_commit, new_commit)

    if not full_patch or not full_patch.strip():
        return full_patch

    filtered_lines: list[str] = []
    include_hunk = False

    dropped_headers: list[str] = []
    for line in full_patch.split("\n"):
        if line.startswith("diff --git"):
            # N6: a diff --git header is exactly `diff --git a/<x> b/<y>`, so we
            # need >=4 space-separated tokens AND parts[2] must start with `a/`.
            # Previously any malformed header silently dropped its whole hunk
            # (include_hunk=False, no log). Now we WARN + strict-fail so the
            # sender sees the drop instead of a corrupted patch reaching eval.
            parts = line.split(" ")
            malformed = (len(parts) < 4 or not parts[2].startswith("a/"))
            if malformed:
                include_hunk = False
                dropped_headers.append(line[:120])
            else:
                file_path = parts[2][2:]
                include_hunk = _path_is_allowed(file_path)

        if include_hunk:
            filtered_lines.append(line)

    filtered = "\n".join(filtered_lines) if filtered_lines else ""

    # N6: surface malformed headers explicitly instead of silently dropping.
    if dropped_headers:
        msg = (
            f"Go patch has {len(dropped_headers)} malformed 'diff --git' header(s) "
            f"in repo_path={repo_path!r}: {dropped_headers[:3]!r}..."
        )
        if strict:
            raise InvalidGoPatchError(filtered, msg)
        logger.warning("%s (strict=False, hunks were silently dropped)", msg)

    if filtered and not validate_go_patch(filtered):
        msg = (
            f"Go patch validation failed for repo_path={repo_path!r}: "
            "generate_go_patch let non-Go paths through. This indicates a "
            "regression in the extension filter or an upstream change to "
            "git diff output."
        )
        if strict:
            raise InvalidGoPatchError(filtered, msg)
        logger.warning("%s (strict=False, returning patch as-is)", msg)

    return filtered


__all__ = [
    "GO_PATCH_EXTENSIONS",
    "InvalidGoPatchError",
    "generate_go_patch",
    "validate_go_patch",
]
