"""Python wrapper for JavaStubber.

Builds the stubber JAR if needed, then invokes it on the target directory.

JavaStubber CLI contract:
    java -jar javastubber.jar <source-dir> [--config config.json]

Returns parsed JSON with stub results per file.
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from commit0.harness.constants_java import resolve_build_cmd

logger = logging.getLogger(__name__)

STUBBER_DIR = Path(__file__).parent / "javastubber"
STUBBER_JAR = STUBBER_DIR / "target" / "javastubber-1.0-SNAPSHOT.jar"


def ensure_stubber_built() -> Path:
    """Build the stubber JAR if it doesn't exist. Returns path to JAR."""
    if not STUBBER_JAR.exists():
        logger.info("Building JavaStubber JAR...")
        mvn = resolve_build_cmd("maven", str(STUBBER_DIR))
        subprocess.run(
            [mvn, "package", "-q", "-DskipTests", "-B"],
            cwd=STUBBER_DIR,
            check=True,
        )
    return STUBBER_JAR


def stub_java_sources(
    src_dir: str,
    marker: str = 'throw new UnsupportedOperationException("STUB: not implemented")',
    write_in_place: bool = True,
    # QC-C6-008: default to STRIPPING Javadoc from stubs. This is a
    # training-data/leaderboard engine — a Javadoc block that spells out the
    # algorithm hands the model the answer for free and inflates the score. The
    # method CONTRACT still lives in the spec/test signals; the stub itself must
    # not carry implementation prose. Matches StubConfig.java's default.
    preserve_javadoc: bool = False,
    stub_private_methods: bool = False,
    stub_constructors: bool = False,
    max_file_lines: int = 50000,
    skip_annotations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Invoke JavaStubber on a source directory.

    Parameters map 1:1 to JavaStubber's StubConfig JSON fields.
    CLI contract: java -jar stubber.jar <source-dir> [--config config.json]

    Returns
    -------
        Dict with keys: sourceDir, totalFiles, totalStubs, files (list of per-file results).
        Each file result has: file, stubCount.

    """
    jar = ensure_stubber_built()

    # Strip @ prefix from annotations — JavaStubber expects simple names
    if skip_annotations is None:
        skip_annotations = ["Deprecated"]
    else:
        skip_annotations = [a.lstrip("@") for a in skip_annotations]

    config = {
        "writeInPlace": write_in_place,
        "preserveJavadoc": preserve_javadoc,
        "stubPrivateMethods": stub_private_methods,
        "stubConstructors": stub_constructors,
        "maxFileLines": max_file_lines,
        "stubMarker": marker,
        "skipAnnotations": skip_annotations,
    }

    cfg_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as cfg_file:
            json.dump(config, cfg_file)
            cfg_path = cfg_file.name

        cmd = ["java", "-jar", str(jar), src_dir, "--config", cfg_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        stdout = result.stdout.strip()
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                logger.warning(
                    "JavaStubber returned non-JSON output: %s", stdout[:500]
                )
                return {
                    "sourceDir": src_dir,
                    "totalFiles": 0,
                    "totalStubs": 0,
                    "files": [],
                    "rawOutput": stdout,
                }
        else:
            logger.warning("JavaStubber produced no output for %s", src_dir)
            return {
                "sourceDir": src_dir,
                "totalFiles": 0,
                "totalStubs": 0,
                "files": [],
            }

    except subprocess.CalledProcessError as e:
        logger.error("JavaStubber failed: %s\nstderr: %s", e, e.stderr[:1000] if e.stderr else "")
        raise
    finally:
        if cfg_path and os.path.exists(cfg_path):
            os.unlink(cfg_path)
