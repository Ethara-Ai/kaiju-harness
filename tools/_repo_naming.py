"""Repo-name normalization helpers.

Two distinct concepts:

* :func:`canonical_repo_name` — the *storage* form (preserves hyphens vs
  underscores as found upstream, only lowercases). Used for dataset folder
  names; matches what the existing HF dataset uses today.
* :func:`normalized_for_dedup` — the *dedup* form (PEP 503-style: collapse
  ``[-_.]+`` into a single ``-`` plus lowercase). Used ONLY for detecting
  hyphen/underscore duplicate pairs like ``bard-api`` vs ``bard_api``. Never
  used for storage.

See Section 5 of ``MISSING_TEST_IDS_BZ2_ISSUE.md`` for the rationale.
"""

from __future__ import annotations

import re

__all__ = ["canonical_repo_name", "normalized_for_dedup", "split_org_repo"]


_DEDUP_RE = re.compile(r"[-_.]+")


def split_org_repo(repo: str) -> tuple[str, str]:
    """Parse ``"org/short"`` or ``"org_short"`` into ``(org, short)``.

    Splits on the first ``/`` if present, otherwise on the first ``_``.
    Returns ``(repo, "")`` if neither separator is found.
    """
    if "/" in repo:
        org, _, short = repo.partition("/")
        return org, short
    if "_" in repo:
        org, _, short = repo.partition("_")
        return org, short
    return repo, ""


def canonical_repo_name(repo: str) -> str:
    """Return the canonical storage form: ``lower(org)_lower(short)``.

    Preserves hyphens vs underscores *within* the short name verbatim. Only
    lowercase normalization is applied. Suitable for use as a dataset folder
    name.

    Examples::

        >>> canonical_repo_name("Pallets-eco/Flask-SQLAlchemy")
        'pallets-eco_flask-sqlalchemy'
        >>> canonical_repo_name("dsdanielpark_bard_api")
        'dsdanielpark_bard_api'
        >>> canonical_repo_name("just-repo-no-org")
        'just-repo-no-org'
    """
    org, short = split_org_repo(repo)
    if not short:
        return org.lower()
    return f"{org.lower()}_{short.lower()}"


def normalized_for_dedup(repo: str) -> str:
    """Return a PEP 503-style normalized form (hyphens only, lowercase).

    Collapses *all* runs of ``[-_.]`` into a single ``-``. Used ONLY for
    grouping duplicate variants together (e.g. ``bard-api`` and ``bard_api``
    collapse to ``bard-api``). NEVER use this for storage — it loses
    information that's needed for ``git clone`` URLs.

    Examples::

        >>> normalized_for_dedup("dsdanielpark_bard-api")
        'dsdanielpark-bard-api'
        >>> normalized_for_dedup("dsdanielpark_bard_api")
        'dsdanielpark-bard-api'
        >>> normalized_for_dedup("Pallets-Eco/Flask_SQLAlchemy")
        'pallets-eco-flask-sqlalchemy'
    """
    # Treat / and _ uniformly so org/repo collapses the same as org_repo
    s = repo.replace("/", "-").lower()
    return _DEDUP_RE.sub("-", s).strip("-")
