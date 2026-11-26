from pathlib import Path

import pytest

from mechubbench.clients.base import isolated_home
from mechubbench.core import load_scenario, validate_scenario
from mechubbench.inventory import devices_for, load_inventory

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = load_inventory(ROOT / "bench" / "devices.yaml")
SRX = ["srx-classic", "srx-evo", "vsrx", "csrx"]


def names(scenario):
    return {device.name for device in devices_for(scenario, INVENTORY)}


def test_devices_for_platform_filter():
    assert names({"platforms": SRX}) == {
        "bench-srx1600",
        "bench-srx440",
        "bench-srx345",
        "bench-vsrx-inet",
        "bench-csrx",
    }


def test_devices_for_requires_mgmt_instance():
    scenario = {"platforms": SRX, "requires": ["mgmt_instance"]}
    assert names(scenario) == {"bench-srx1600", "bench-srx440", "bench-vsrx-inet"}


def test_devices_for_path_scope():
    assert devices_for({"device_scope": "path"}, INVENTORY) == [None]


def test_devices_for_unknown_platform_raises():
    with pytest.raises(ValueError):
        devices_for({"platforms": ["mx"]}, INVENTORY)


def test_isolated_home_has_no_user_config(tmp_path):
    home = isolated_home(tmp_path)
    assert home.is_dir()
    assert list(home.iterdir()) == []
    assert tmp_path in home.parents


def test_bench_scenarios_validate():
    paths = sorted((ROOT / "scenarios" / "bench").glob("*.yaml"))
    if not paths:
        pytest.skip("no bench scenarios yet")
    for path in paths:
        validate_scenario(load_scenario(path), ROOT / "scenarios" / "schema.json")
