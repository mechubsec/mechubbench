"""Tests for export-tools CLI subcommand."""

import json
from unittest.mock import Mock, patch

from mechubbench import cli


def test_export_tools_writes_wellformed_schema():
    """export-tools writes wellformed schema from fake tools/list."""
    # Mock MCP tools/list response
    fake_tools = [
        {
            "name": "get_junos_config",
            "description": "Retrieve Junos configuration",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "config_path": {"type": "string"}
                },
                "required": ["device"]
            }
        },
        {
            "name": "create_junos_change_set",
            "description": "Create a change-set",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "config": {"type": "string"},
                    "expected_fingerprint": {"type": "string"}
                },
                "required": ["device", "config", "expected_fingerprint"]
            }
        },
        {
            "name": "get_junos_candidate_fingerprint",
            "description": "Get candidate fingerprint for state binding",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"}
                },
                "required": ["device"]
            }
        }
    ]

    # Mock MCP client
    mock_init = Mock()
    mock_init.headers = {"Mcp-Session-Id": "s1"}
    mock_init.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
    mock_init.raise_for_status = Mock()
    mock_init.text = ""

    mock_notif = Mock()
    mock_notif.headers = {}
    mock_notif.raise_for_status = Mock()

    mock_tools_list = Mock()
    mock_tools_list.headers = {"content-type": "application/json"}
    mock_tools_list.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": fake_tools}
    }
    mock_tools_list.raise_for_status = Mock()
    mock_tools_list.text = ""

    with patch("httpx.post", side_effect=[mock_init, mock_notif, mock_tools_list]):
        # Mock argparse namespace
        args = Mock()
        args.mcp_endpoint = "http://test/mcp"
        args.mcp_token = "test-token"
        args.out = "/tmp/test-tools.json"

        # Run export-tools
        result = cli.cmd_export_tools(args)

        assert result == 0

        # Verify output was written
        with open("/tmp/test-tools.json") as f:
            exported = json.load(f)

        # Should be wellformed schema
        assert len(exported) == 3
        assert exported[0]["name"] == "get_junos_config"
        assert "inputSchema" in exported[0]
        assert exported[1]["name"] == "create_junos_change_set"
        assert "expected_fingerprint" in exported[1]["inputSchema"]["required"]
        assert exported[2]["name"] == "get_junos_candidate_fingerprint"
