"""Guard against function-local imports that shadow a module-level import.

QC-CORR-001: create_dataset{,_c,_go}.py had a redundant ``import os`` inside
``main()``'s ``if args.upload:`` block. Because Python binds a name as
function-local for the WHOLE function scope once it is imported/assigned
anywhere in that function, every earlier ``os.environ`` reference in ``main()``
raised ``UnboundLocalError`` the moment ``main()`` ran. This AST guard pins ALL
create_dataset_*.py siblings so a re-shadowing local import cannot recur.
"""

import ast
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1]
_FILES = sorted(_TOOLS.glob("create_dataset*.py"))


def _module_level_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _local_import_names(func: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


@pytest.mark.parametrize("path", _FILES, ids=[p.name for p in _FILES])
def test_no_local_import_shadows_module_import(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_imports = _module_level_imports(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed = _local_import_names(node) & module_imports
            assert not shadowed, (
                f"{path.name}:{node.name}() re-imports {sorted(shadowed)} that is "
                f"already imported at module level — this makes the name "
                f"function-local for the whole scope and raises UnboundLocalError "
                f"for earlier uses (F823)."
            )


def test_at_least_the_three_flagged_files_present():
    names = {p.name for p in _FILES}
    assert {"create_dataset.py", "create_dataset_c.py", "create_dataset_go.py"} <= names
