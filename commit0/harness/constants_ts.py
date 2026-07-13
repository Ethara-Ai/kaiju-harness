"""TypeScript pipeline constants — co-located alongside Python constants.py."""

from enum import Enum
from pathlib import Path
from typing import Dict

from commit0.harness.constants import RepoInstance


class Language(str, Enum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"


class TsRepoInstance(RepoInstance):
    language: Language = Language.TYPESCRIPT
    test_framework: str = "jest"


# Curated subsets only — the "all" / "all_ts" subset is derived dynamically
    # from the loaded dataset by ``commit0.harness.split_utils.resolve_split``.
TS_SPLIT: Dict[str, list[str]] = {}

# Per-repo branch created by setup_ts (one per repo clone)
TS_BASE_BRANCH = "commit0"
# Branch used for combined/all-repo dataset references
TS_DATASET_BRANCH = "commit0_all"

DEFAULT_NODE_VERSION = "20"
CONTAINER_WORKDIR = "/testbed"

TS_SOURCE_EXTS = (".ts", ".tsx")

TS_STUB_MARKER = 'throw new Error("STUB")'

TS_TEST_FILE_PATTERNS = ("*.test.ts", "*.spec.ts", "*.test.tsx", "*.spec.tsx")

# 18 (older libs; EOL upstream but still widely targeted), 20/22 (active LTS),
# 24 (current). Each has a matching commit0/harness/dockerfiles/Dockerfile.node<v>.
SUPPORTED_NODE_VERSIONS = {"18", "20", "22", "24"}

RUN_TS_TEST_LOG_DIR = Path("logs/ts_test")

TS_GITIGNORE_ENTRIES = ["node_modules/", "dist/", ".aider*", "logs/"]
