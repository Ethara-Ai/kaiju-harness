"""C-specific patch generation — filters diffs to C source files only."""

import git as gitpython

from commit0.harness.utils import generate_patch_between_commits


C_PATCH_EXTENSIONS = (".c", ".h")
C_PATCH_FILENAMES = ("CMakeLists.txt",)


def _is_c_file(file_path: str) -> bool:
    if any(file_path.endswith(ext) for ext in C_PATCH_EXTENSIONS):
        return True
    base = file_path.rsplit("/", 1)[-1]
    return base in C_PATCH_FILENAMES


def generate_c_patch(repo_path: str, old_commit: str, new_commit: str) -> str:
    """Generate a patch filtered to .c/.h/CMakeLists.txt files.

    Prevents LLM-generated non-C files from contaminating the diff.
    """
    repo = gitpython.Repo(repo_path)
    full_patch = generate_patch_between_commits(repo, old_commit, new_commit)

    if not full_patch or not full_patch.strip():
        return full_patch

    filtered_lines: list[str] = []
    include_hunk = False

    for line in full_patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" ")
            if len(parts) >= 4:
                file_path = parts[2][2:]  # strip "a/" prefix
                include_hunk = _is_c_file(file_path)
            else:
                include_hunk = False

        if include_hunk:
            filtered_lines.append(line)

    return "\n".join(filtered_lines) if filtered_lines else ""


__all__ = ["generate_c_patch", "C_PATCH_EXTENSIONS", "C_PATCH_FILENAMES"]
