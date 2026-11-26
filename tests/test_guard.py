"""Tests for the bench guard: drift check, baseline capture and restore."""

import json
from pathlib import Path

import pytest

from mechubbench.guard import (
    Baseline,
    capture_baseline,
    check_golden,
    commits_since,
    plant_fault,
    record_golden,
    restore,
)
from mechubbench.inventory import BenchDevice
from mechubbench.runner import MCPError

FIXTURES = Path(__file__).parent / "fixtures" / "guard"


def _device_of(arguments):
    return arguments.get("router_name") or arguments.get("device")


class FakeDevice:
    """One Junos device: commit history (newest first) and its configs."""

    def __init__(self, config="base"):
        self.clock = 0
        self.history = []  # [(entry text, config)], newest first
        self.config = config
        self.confirm_pending = False
        self._commit(config, "by root via cli")

    def _commit(self, config, how):
        self.clock += 1
        stamp = f"2026-11-26 10:{self.clock:02d}:00 UTC"
        self.history.insert(0, (f"{stamp} {how}", config))
        self.config = config

    def plain_commit(self, config):
        self._commit(config, "by netconf via netconf")

    def confirmed_commit(self, config):
        self._commit(
            config, "by netconf via netconf commit confirmed, rollback in 10mins"
        )
        self.confirm_pending = True

    def show_system_commit(self):
        lines = [f"{index}   {entry}" for index, (entry, _) in enumerate(self.history)]
        return "\n".join(lines) + "\n"


class FakeMCP:
    """Fake rust-junosmcp operator client over FakeDevice records."""

    def __init__(self, devices, change_sets=None):
        self.devices = devices
        self.change_sets = change_sets or []  # dicts as list_junos_change_sets
        self.can_confirm = True
        self.calls = []

    def names(self, tool):
        return [name for name, _ in self.calls if name == tool]

    def call_tool(self, tool_name, arguments, truncate=True):
        self.calls.append((tool_name, arguments))
        device = self.devices[_device_of(arguments)] if _device_of(arguments) else None
        if tool_name == "get_junos_candidate_fingerprint":
            return {
                "device": _device_of(arguments),
                "candidate_fingerprint": device.config,
            }
        if tool_name == "execute_junos_command":
            assert arguments["command"] == "show system commit"
            assert truncate is False
            return device.show_system_commit()
        if tool_name == "rollback_config":
            assert arguments["commit"] is True
            assert arguments["confirm_timeout_mins"] == 0
            config = device.history[arguments["version"]][1]
            device.plain_commit(config)
            device.confirm_pending = False
            return {"committed": True, "diff": "", "version": arguments["version"]}
        if tool_name == "load_and_commit_config":
            device.plain_commit(device.config + "+" + arguments["config_text"])
            return {"committed": True}
        if tool_name == "list_junos_change_sets":
            return [
                cs for cs in self.change_sets if cs["device"] == _device_of(arguments)
            ]
        if tool_name == "get_junos_change_set_status":
            record = next(
                cs for cs in self.change_sets if cs["id"] == arguments["change_set_id"]
            )
            return {"state": record["state"], **record.get("status", {})}
        if tool_name == "confirm_junos_change_set":
            if not self.can_confirm:
                raise MCPError("Tool execution failed: operation not found")
            record = next(
                cs
                for cs in self.change_sets
                if cs.get("operation_id") == arguments["operation_id"]
            )
            record["status"]["commit_confirmation"] = "committed"
            # A confirming commit is its own entry in the commit history.
            device.plain_commit(device.config)
            device.confirm_pending = False
            return {"confirmed": True}
        raise AssertionError(f"unexpected tool {tool_name}")


def _setup(device_name="bench-srx345", change_sets=None):
    device = FakeDevice()
    mcp = FakeMCP({device_name: device}, change_sets)
    return device, mcp, capture_baseline(mcp, device_name)


def test_capture_baseline_reads_fingerprint_and_top_commit():
    device, _, baseline = _setup()
    assert baseline == Baseline("bench-srx345", "base", device.history[0][0])


def test_commits_since_lists_newer_entries():
    device, mcp, baseline = _setup()
    device.plain_commit("a")
    device.plain_commit("b")
    assert commits_since(mcp, baseline) == [device.history[0][0], device.history[1][0]]


def test_commits_since_raises_when_baseline_entry_missing():
    device, mcp, baseline = _setup()
    device.history = [("2026-11-26 11:00:00 UTC by x via cli", "z")]
    with pytest.raises(RuntimeError):
        commits_since(mcp, baseline)


def test_restore_after_plain_commit():
    device, mcp, baseline = _setup()
    device.plain_commit("changed")
    restore(mcp, baseline)
    rollbacks = [args for name, args in mcp.calls if name == "rollback_config"]
    assert [args["version"] for args in rollbacks] == [1]
    assert device.config == "base"


def test_restore_with_pending_confirmed():
    """A CLI-level commit confirmed: the rollback commit itself ends the window."""
    device, mcp, baseline = _setup()
    device.confirmed_commit("changed")
    restore(mcp, baseline)
    rollbacks = [args for name, args in mcp.calls if name == "rollback_config"]
    assert [args["version"] for args in rollbacks] == [1]
    assert not device.confirm_pending
    assert device.config == "base"


def _awaiting_change_sets():
    return [
        {
            "id": "cs1",
            "device": "bench-srx345",
            "state": "applied",
            "operation_id": "op1",
            "status": {
                "operation_id": "op1",
                "commit_confirmation": "awaiting_confirmation",
            },
        },
        {
            "id": "cs0",
            "device": "bench-srx345",
            "state": "applied",
            "operation_id": "op0",
            "status": {"commit_confirmation": "committed"},
        },
    ]


def test_restore_confirms_awaiting_change_set_with_agent_client():
    """R14: the operator lists the op; only the agent (opener) confirms it."""
    device, mcp, baseline = _setup(change_sets=_awaiting_change_sets())
    mcp.can_confirm = False  # rust-junosmcp: only the opener may confirm
    agent = FakeMCP(mcp.devices, mcp.change_sets)
    device.confirmed_commit("changed")
    restore(mcp, baseline, agent_mcp=agent)
    assert agent.calls == [
        (
            "confirm_junos_change_set",
            {"router_name": "bench-srx345", "operation_id": "op1"},
        )
    ]
    assert "list_junos_change_sets" in mcp.names("list_junos_change_sets")
    # The confirming commit is a second entry above the baseline (R15).
    rollbacks = [args for name, args in mcp.calls if name == "rollback_config"]
    assert [args["version"] for args in rollbacks] == [2]
    assert device.config == "base"


def test_restore_pending_op_without_agent_client_raises():
    device, mcp, baseline = _setup(change_sets=_awaiting_change_sets())
    device.confirmed_commit("changed")
    with pytest.raises(RuntimeError, match="agent"):
        restore(mcp, baseline)
    assert mcp.names("rollback_config") == []


def test_restore_two_commits():
    device, mcp, baseline = _setup()
    device.plain_commit("a")
    device.plain_commit("b")
    restore(mcp, baseline)
    rollbacks = [args for name, args in mcp.calls if name == "rollback_config"]
    assert [args["version"] for args in rollbacks] == [2]
    assert device.config == "base"


def test_restore_readonly_noop():
    _, mcp, baseline = _setup()
    mcp.calls.clear()
    restore(mcp, baseline)
    assert mcp.names("rollback_config") == []
    assert mcp.names("get_junos_candidate_fingerprint") == [
        "get_junos_candidate_fingerprint"
    ]


def test_restore_fingerprint_mismatch_raises():
    device, mcp, baseline = _setup()
    device.config = "drifted"  # uncommitted residue: no commit entry
    with pytest.raises(RuntimeError):
        restore(mcp, baseline)


def test_restore_dirty_candidate_caught_by_fingerprint():
    device, mcp, baseline = _setup()
    device.plain_commit("changed")
    original = mcp.call_tool

    def leave_candidate_dirty(tool_name, arguments, truncate=True):
        result = original(tool_name, arguments, truncate)
        if tool_name == "rollback_config":
            device.config = "base+staged"  # uncommitted candidate edit
        return result

    mcp.call_tool = leave_candidate_dirty
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        restore(mcp, baseline)


def _override(mcp, tool, value):
    original = mcp.call_tool

    def patched(tool_name, arguments, truncate=True):
        if tool_name == tool:
            mcp.calls.append((tool_name, arguments))
            return value
        return original(tool_name, arguments, truncate)

    mcp.call_tool = patched


@pytest.mark.parametrize("shape", [{"change_sets": []}, "[]", None])
def test_restore_unexpected_change_set_list_shape_raises(shape):
    _, mcp, baseline = _setup()
    _override(mcp, "list_junos_change_sets", shape)
    with pytest.raises(RuntimeError, match="unexpected"):
        restore(mcp, baseline)
    assert mcp.names("rollback_config") == []


def test_restore_unexpected_status_shape_raises():
    _, mcp, baseline = _setup(change_sets=_awaiting_change_sets())
    _override(mcp, "get_junos_change_set_status", "not a dict")
    with pytest.raises(RuntimeError, match="unexpected"):
        restore(mcp, baseline)


def test_restore_malformed_change_set_record_raises_runtime_error():
    _, mcp, baseline = _setup()
    # Applied with an operation_id but no "id": KeyError inside the confirm path.
    _override(
        mcp,
        "list_junos_change_sets",
        [{"device": "bench-srx345", "state": "applied", "operation_id": "op1"}],
    )
    with pytest.raises(RuntimeError):
        restore(mcp, baseline)


def test_restore_mcp_error_becomes_runtime_error():
    device, mcp, baseline = _setup()
    device.plain_commit("changed")

    def broken(tool_name, arguments, truncate=True):
        raise MCPError("Tool execution failed: device unreachable")

    mcp.call_tool = broken
    with pytest.raises(RuntimeError):
        restore(mcp, baseline)


def test_plant_fault_commits_set_lines():
    device, mcp, _ = _setup()
    plant_fault(mcp, "bench-srx345", "# comment\ndelete security policies foo\n")
    loads = [args for name, args in mcp.calls if name == "load_and_commit_config"]
    assert loads[0]["config_text"] == "delete security policies foo"
    assert loads[0]["config_format"] == "set"
    assert loads[0]["confirm_timeout_mins"] == 0
    assert loads[0]["router_name"] == "bench-srx345"


def _inventory(*names):
    return [BenchDevice(name, "srx-classic", "25.4R1", False) for name in names]


def test_check_golden_reports_drift():
    mcp = FakeMCP({"bench-srx1600": FakeDevice("g1"), "bench-srx345": FakeDevice("x")})
    golden = {"bench-srx1600": "g1", "bench-srx345": "g2"}
    inventory = _inventory("bench-srx1600", "bench-srx345")
    assert check_golden(mcp, inventory, golden) == ["bench-srx345"]


def test_check_golden_missing_entry_is_drift():
    mcp = FakeMCP({"bench-ex2300": FakeDevice("g1")})
    assert check_golden(mcp, _inventory("bench-ex2300"), {}) == ["bench-ex2300"]


def test_record_golden_writes_fingerprints(tmp_path):
    mcp = FakeMCP({"bench-ex2300": FakeDevice("g1"), "bench-csrx": FakeDevice("g2")})
    path = tmp_path / "golden.json"
    record_golden(mcp, _inventory("bench-ex2300", "bench-csrx"), path)
    assert json.loads(path.read_text()) == {"bench-csrx": "g2", "bench-ex2300": "g1"}


def test_real_commit_history_fixture_parses():
    """Real srx440 output: XML wrapper and indented comment lines ignored."""
    output = (FIXTURES / "show_system_commit_srx440.txt").read_text()

    class RealOutput:
        def call_tool(self, tool_name, arguments, truncate=True):
            if tool_name == "execute_junos_command":
                return output
            return {"candidate_fingerprint": "fp"}

    baseline = capture_baseline(RealOutput(), "bench-srx440")
    assert baseline.top_commit == "2026-11-25 23:24:48 UTC by netconf via cli"
    older = Baseline(
        "bench-srx440", "fp", "2026-11-25 23:16:07 UTC by netconf via netconf"
    )
    newer = commits_since(RealOutput(), older)
    assert newer[0] == baseline.top_commit
    assert "commit confirmed" in newer[1]
    assert len(newer) == 2
