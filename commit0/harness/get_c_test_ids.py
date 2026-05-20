"""Read C test IDs from *.bz2 compressed files."""

import bz2
import logging
import os
from typing import List

import commit0

logger = logging.getLogger(__name__)


def read(bz2_file: str) -> str:
    try:
        with bz2.open(bz2_file, "rt") as f:
            return f.read()
    except (OSError, EOFError) as e:
        logger.error("Failed to read bz2 file %s: %s", bz2_file, e)
        raise


def main(repo: str, verbose: int) -> List[List[str]]:
    logger.debug("Reading C test IDs for repo: %s", repo)
    repo = repo.lower()
    repo = repo.replace(".", "-")
    commit0_path = os.path.dirname(commit0.__file__)

    try:
        if "__" in repo:
            in_file_fail = read(f"{commit0_path}/data/c_test_ids/{repo}#fail_to_pass.bz2")
            in_file_pass = read(f"{commit0_path}/data/c_test_ids/{repo}#pass_to_pass.bz2")
        else:
            in_file_fail = read(f"{commit0_path}/data/c_test_ids/{repo}.bz2")
            in_file_pass = ""
    except (OSError, EOFError):
        logger.warning(
            "No C test ID files found for %s. "
            "Run tools/generate_test_ids_c.py first to create them. "
            "Returning empty test IDs — evaluation will compare against all discovered tests.",
            repo,
        )
        return [[], []]

    out = [in_file_fail, in_file_pass]
    if verbose:
        print(f"FAIL TO PASS:\n{out[0]}\nPASS TO PASS:\n{out[1]}")
        logger.info(
            "FAIL TO PASS: %d entries, PASS TO PASS: %d entries",
            len(out[0].split("\n")),
            len(out[1].split("\n")),
        )
    return [
        [x for x in out[0].split("\n") if x],
        [x for x in out[1].split("\n") if x],
    ]


__all__: list = []
