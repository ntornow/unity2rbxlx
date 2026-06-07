"""
conftest.py -- Shared pytest fixtures.
"""

import sys
from pathlib import Path

import pytest

# Add converter root to path
CONVERTER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(CONVERTER_ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_PROJECTS_DIR = CONVERTER_ROOT.parent / "test_projects"

from tests._project_paths import SIMPLEFPS_PATH, is_populated  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")


@pytest.fixture(autouse=True)
def _disable_auto_verify(monkeypatch):
    """Default the slice-1.6 ``--verify`` auto-mode OFF across the suite.

    ``u2r.py convert`` / ``convert_interactive assemble`` now auto-run the
    non-interactive Studio smoke test when a Studio binary resolves on macOS.
    Unstubbed, every ``convert`` test on a macOS dev box with Studio installed
    would launch Studio.

    The load-bearing disable is the ``U2R_DISABLE_AUTO_VERIFY=1`` env var:
    ``verify_hook.studio_available`` honors it, and env vars inherit into child
    processes, so this covers BOTH in-process CliRunner tests AND subprocess CLI
    tests (``test_byte_equivalence``, ``test_integration`` run ``u2r.py convert``
    / ``convert_interactive assemble`` via ``subprocess``) — a monkeypatch alone
    only reaches in-process imports. The in-process ``studio_available`` stub is
    kept too as belt-and-suspenders.

    Tests that exercise the genuine auto-on path (``test_verify_hook``) undo both
    overrides at function scope (env var unset + the real ``studio_available``).
    """
    import verify_hook

    monkeypatch.setenv("U2R_DISABLE_AUTO_VERIFY", "1")
    monkeypatch.setattr(verify_hook, "studio_available", lambda: False)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def simple_scene_yaml() -> str:
    return (FIXTURES_DIR / "simple_scene.yaml").read_text()


@pytest.fixture
def simple_prefab_yaml() -> str:
    return (FIXTURES_DIR / "simple_prefab.yaml").read_text()


@pytest.fixture
def sample_material_yaml() -> str:
    return (FIXTURES_DIR / "sample_material.yaml").read_text()


@pytest.fixture
def test_projects_dir() -> Path:
    return TEST_PROJECTS_DIR


@pytest.fixture
def platformer_project() -> Path:
    return TEST_PROJECTS_DIR / "3D-Platformer"


@pytest.fixture
def simplefps_project() -> Path:
    if not is_populated(SIMPLEFPS_PATH):
        pytest.skip(
            "SimpleFPS project not available "
            "(submodule not pulled, no external checkout at ../unity-3d-simplefps)"
        )
    return SIMPLEFPS_PATH


@pytest.fixture
def redrunner_project() -> Path:
    return TEST_PROJECTS_DIR / "RedRunner"


@pytest.fixture
def chopchop_project() -> Path:
    return TEST_PROJECTS_DIR / "ChopChop" / "UOP1_Project"
