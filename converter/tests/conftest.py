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
    return TEST_PROJECTS_DIR / "SimpleFPS"


@pytest.fixture
def redrunner_project() -> Path:
    return TEST_PROJECTS_DIR / "RedRunner"


@pytest.fixture
def chopchop_project() -> Path:
    return TEST_PROJECTS_DIR / "ChopChop" / "UOP1_Project"
