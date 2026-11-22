"""Run real Home Assistant tests in a fixture-owned configuration directory."""

from pathlib import Path

import pytest


@pytest.fixture
def hass_config_dir(tmp_path):
    component_dir = Path(__file__).parents[1] / "custom_components"
    (tmp_path / "custom_components").symlink_to(component_dir, target_is_directory=True)
    return str(tmp_path)
