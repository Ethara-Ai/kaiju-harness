import bz2
import logging
from typing import List
import commit0
import os

logger = logging.getLogger(__name__)


def read(bz2_file: str) -> str:
    try:
        with bz2.open(bz2_file, "rt") as f:
            out = f.read()
        return out
    except (OSError, EOFError) as e:
        logger.error("Failed to read bz2 file %s: %s", bz2_file, e)
        raise


def main(repo: str, verbose: int) -> List[List[str]]:
    logger.debug("Reading test IDs for repo: %s", repo)
    repo = repo.lower()
    repo = repo.replace(".", "-")
    commit0_path = os.path.dirname(commit0.__file__)
    from kaiju.paths import find_test_ids_file

    def _resolve(fname: str) -> str:
        p = find_test_ids_file(commit0_path, "test_ids", fname)
        if p is None:
            raise FileNotFoundError(
                f"Test IDs file '{fname}' not found for repo '{repo}' "
                f"(searched KAIJU_TEST_IDS_DIR and {commit0_path}/data/test_ids/)."
            )
        return str(p)

    if "__" in repo:
        in_file_fail = read(_resolve(f"{repo}#fail_to_pass.bz2"))
        in_file_pass = read(_resolve(f"{repo}#pass_to_pass.bz2"))
    else:
        in_file_fail = read(_resolve(f"{repo}.bz2"))
        in_file_pass = ""
    out = [in_file_fail, in_file_pass]
    if verbose:
        print(f"FAIL TO PASS:\n{out[0]}\nPASS TO PASS:\n{out[1]}")
        logger.info("FAIL TO PASS: %d entries, PASS TO PASS: %d entries", len(out[0].split('\n')), len(out[1].split('\n')))
    out = [
        [x for x in out[0].split("\n") if x],
        [x for x in out[1].split("\n") if x],
    ]
    return out


__all__ = []
