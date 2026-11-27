"""Bench-lab device inventory and scenario-to-device selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

PLATFORMS = frozenset({"ex", "srx-classic", "srx-evo", "vsrx", "csrx"})


@dataclass(frozen=True)
class BenchDevice:
    """One lab device a scenario attempt can run against."""

    name: str
    platform: str
    release: str
    mgmt_instance: bool


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_inventory(path: Path) -> list[BenchDevice]:
    """Load bench/devices.yaml into BenchDevice records."""
    return [BenchDevice(**entry) for entry in _load(path)["devices"]]


def load_hosts(path: Path) -> dict[str, str]:
    """Return the hosts map (label -> address) from bench/devices.yaml."""
    return dict(_load(path)["hosts"])


def devices_for(
    scenario: dict, inventory: list[BenchDevice]
) -> list[BenchDevice | None]:
    """Select the devices a scenario runs on.

    A ``device_scope: path`` scenario is one multi-device record, returned as
    ``[None]``. Otherwise filter by platforms, requires and explicit devices.

    Raises:
        ValueError: If the scenario names an unknown platform.
    """
    if scenario.get("device_scope") == "path":
        return [None]
    platforms = scenario.get("platforms")
    if platforms is not None:
        unknown = set(platforms) - PLATFORMS
        if unknown:
            raise ValueError(f"unknown platform(s): {sorted(unknown)}")
    selected = []
    for device in inventory:
        if platforms is not None and device.platform not in platforms:
            continue
        if "mgmt_instance" in scenario.get("requires", []) and not (
            device.mgmt_instance
        ):
            continue
        if "devices" in scenario and device.name not in scenario["devices"]:
            continue
        selected.append(device)
    return selected
