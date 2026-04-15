"""Shared helpers for locating Unity test projects.

Unity test projects live as git submodules under ``test_projects/``. When a
submodule is uninitialized (common on fresh clones, and the historical state
of many developer machines) the directory exists but is empty — so plain
``.exists()`` checks aren't enough to detect availability, and tests that
hardcoded such checks would fail with cryptic ``transpiled_scripts == 0``
errors instead of skipping cleanly.

``resolve_project`` distinguishes populated projects from empty stubs. The
canonical source is the submodule under ``test_projects/<Name>``; if that's
uninitialized, you can point at an external checkout via the
``UNITY2RBXLX_TEST_PROJECTS_ROOT`` environment variable — the resolver
will look for ``$UNITY2RBXLX_TEST_PROJECTS_ROOT/<Name>`` as a fallback.

Example for a developer who keeps Unity projects outside the repo:

    export UNITY2RBXLX_TEST_PROJECTS_ROOT=$HOME/unity-projects
    pytest tests/ -m slow

The module exports pre-resolved per-project constants for convenience;
callers can gate tests with ``is_populated(SIMPLEFPS_PATH)``.
"""

from __future__ import annotations

import os
from pathlib import Path

_CONVERTER_ROOT = Path(__file__).parent.parent
_REPO_ROOT = _CONVERTER_ROOT.parent
_TEST_PROJECTS = _REPO_ROOT / "test_projects"

# External test-project root override. Unset by default; set this in a
# developer's environment if their Unity projects live outside the repo.
_EXTERNAL_ROOT_ENV = "UNITY2RBXLX_TEST_PROJECTS_ROOT"


def resolve_project(name: str) -> Path:
    """Return the on-disk path for a Unity test project.

    Resolution order:
        1. ``test_projects/<name>`` submodule (if populated).
        2. ``$UNITY2RBXLX_TEST_PROJECTS_ROOT/<name>`` (if env var set and
           that path is populated).
        3. The submodule path again (even when unpopulated) so callers get
           a predictable Path to report in skip messages.
    """
    submodule = _TEST_PROJECTS / name
    if (submodule / "Assets").is_dir():
        return submodule

    external_root = os.environ.get(_EXTERNAL_ROOT_ENV)
    if external_root:
        external = Path(external_root).expanduser() / name
        if (external / "Assets").is_dir():
            return external

    return submodule


def is_populated(path: Path) -> bool:
    """Return True if ``path`` contains a populated Unity project.

    Handles both flat projects (``<path>/Assets``) and nested-root projects
    (``<path>/<child>/Assets``, e.g. ``ChopChop/UOP1_Project``) — mirroring
    the auto-detection the pipeline's preflight step performs.
    """
    if (path / "Assets").is_dir():
        return True
    if path.is_dir():
        for child in path.iterdir():
            if child.is_dir() and (child / "Assets").is_dir():
                return True
    return False


# Canonical per-project paths — resolved once at import time.
SIMPLEFPS_PATH = resolve_project("SimpleFPS")
PLATFORMER_PATH = resolve_project("3D-Platformer")
REDRUNNER_PATH = resolve_project("RedRunner")
CHOPCHOP_PATH = resolve_project("ChopChop")
GAMEKIT3D_PATH = resolve_project("Gamekit3D")
BOSSROOM_PATH = resolve_project("BossRoom")
BOATATTACK_PATH = resolve_project("BoatAttack")
SANANDREAS_PATH = resolve_project("SanAndreasUnity")
PREFABWORKFLOWS_PATH = resolve_project("PrefabWorkflows")
