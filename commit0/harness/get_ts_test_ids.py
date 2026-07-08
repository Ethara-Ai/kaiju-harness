import bz2
import logging
import os
from typing import List

import commit0

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
    logger.debug("Reading TS test IDs for repo: %s", repo)
    repo = repo.lower()
    repo = repo.replace(".", "-")
    commit0_path = os.path.dirname(commit0.__file__)

    from kaiju.paths import find_test_ids_file
    resolved = find_test_ids_file(commit0_path, "test_ids", f"{repo}.bz2")
    if resolved is None:
        raise FileNotFoundError(
            f"Test IDs file not found for repo '{repo}' (searched KAIJU_TEST_IDS_DIR "
            f"and {commit0_path}/data/test_ids/). "
            f"Run 'python -m tools.generate_test_ids_ts' first to generate test IDs."
        )
    content = read(str(resolved))

    if verbose:
        print(f"TEST IDS:\n{content}")
        lines = [x for x in content.split("\n") if x]
        logger.info("Total test IDs: %d", len(lines))

    test_ids = [x for x in content.split("\n") if x]
    return [test_ids]
