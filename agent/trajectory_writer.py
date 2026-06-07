"""Write human-readable trajectory Markdown with thinking blocks."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.thinking_capture import Turn

logger = logging.getLogger(__name__)


def write_trajectory_md(
    output_path: Path,
    repo_name: str,
    turns: "list[Turn]",
) -> None:
    """Write a Markdown file with all conversation turns including thinking.

    Parameters
    ----------
    output_path : Path
        Where to write the Markdown file.
    repo_name : str
        Repository name for the document title.
    turns : list[Turn]
        All accumulated turns from the pipeline run.

    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(output_path, "w") as f:
            f.write(f"# Trajectory: {repo_name}\n\n")

            current_stage = ""
            current_module = ""

            for turn in turns:
                # Stage header
                if turn.stage != current_stage:
                    current_stage = turn.stage
                    stage_title = {
                        "draft": "Stage 1: Draft Implementation",
                        "lint": "Stage 2: Lint Refinement",
                        "test": "Stage 3: Test Refinement",
                    }.get(current_stage, current_stage)
                    f.write(f"\n## {stage_title}\n\n")

                # Module header
                if turn.module != current_module:
                    current_module = turn.module
                    f.write(f"### Module: {current_module}\n\n")

                # Turn header
                f.write(f"#### Turn {turn.turn_number} — {turn.role.title()}\n\n")

                # Thinking block (assistant only)
                if turn.role == "assistant" and turn.thinking:
                    token_count = turn.thinking_tokens or "unknown"
                    f.write("<details>\n")
                    f.write(f"<summary>Thinking ({token_count} tokens)</summary>\n\n")
                    f.write(f"{turn.thinking}\n\n")
                    f.write("</details>\n\n")

                # Content
                f.write(f"{turn.content}\n\n")
                f.write("---\n\n")
    except OSError as e:
        logger.error("Failed to write trajectory to %s: %s", output_path, e)
        raise
