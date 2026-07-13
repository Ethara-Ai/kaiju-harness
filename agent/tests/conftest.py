"""Install ``sys.modules`` stubs for ``aider.*`` and ``import_deps`` when the
optional ``[agent]`` extras are not installed. Lets ``unittest.mock.patch``
resolve targets like ``aider.models.register_models`` even without a real aider,
and lets ``agent.agent_utils`` (``from import_deps import ModuleSet``) import.

The stub definitions live in a single shared module so this directory and
``commit0/harness/tests`` install byte-for-byte identical module objects — a full
run that collects both directories must not end up with two different (or invalid)
``aider`` stubs racing in ``sys.modules``. When the extras ARE installed
(production / CI with ``pip install -e ".[agent]"``) this conftest does nothing.
"""

from __future__ import annotations

from commit0.harness._optional_dep_stubs import install_missing_optional_dep_stubs

install_missing_optional_dep_stubs()
