import json
import logging
import sys
from pathlib import Path

import docker

from commit0.harness.docker_build_rust import build_rust_repo_images

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RUST_DATASET_GLOB = "*_rust_dataset.json"


def _load_datasets(path: Path) -> list[dict]:
    """Load Rust repo instances from a file or directory.

    If *path* is a JSON file, load it directly.
    If *path* is a directory, glob for ``*_rust_dataset.json`` and merge all entries.
    """
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.glob(RUST_DATASET_GLOB))
        if not files:
            logger.error("No %s files found in %s", RUST_DATASET_GLOB, path)
            sys.exit(1)
    else:
        logger.error("Path not found: %s", path)
        sys.exit(1)

    instances: list[dict] = []
    for f in files:
        logger.info("Loading dataset: %s", f)
        # Per-file error handling so one malformed/unreadable dataset file is
        # skipped (with a clear log) instead of aborting the whole multi-file
        # load with an unhandled JSONDecodeError/OSError.
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Skipping dataset file %s: %s", f, exc)
            continue
        if isinstance(data, dict):
            data = list(data.values())
        instances.extend(data)

    return instances


def main(
    dataset_path: str,
    num_workers: int = 4,
    verbose: int = 1,
) -> None:
    path = Path(dataset_path)
    instances = _load_datasets(path)

    logger.info("Loaded %d Rust repo instance(s) total", len(instances))

    client = docker.from_env()
    successful, failed = build_rust_repo_images(
        client, instances, max_workers=num_workers, verbose=verbose
    )

    logger.info("Built %d images successfully.", len(successful))
    if failed:
        logger.error("Failed to build %d image(s): %s", len(failed), failed)
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build Rust repo Docker images")
    parser.add_argument(
        "dataset",
        help=(
            "Path to a single *_rust_dataset.json file, "
            "or a directory to glob for all *_rust_dataset.json files"
        ),
    )
    parser.add_argument(
        "-j", "--workers", type=int, default=4, help="Parallel build workers"
    )
    parser.add_argument("-v", "--verbose", type=int, default=1, help="Verbosity level")
    args = parser.parse_args()
    main(args.dataset, num_workers=args.workers, verbose=args.verbose)
