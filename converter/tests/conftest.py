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
