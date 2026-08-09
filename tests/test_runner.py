"""Tests for agentic runner and MCP client."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from mechubbench import runner


class TestMCPClient:
    """Test MCP client with streamable-HTTP bearer auth."""

    def test_init(self):
        """Initialize MCP client with endpoint and token."""
        client = runner.MCPClient(
            endpoint="http://198.51.100.194:30031/mcp",
            token="test-token-abc123",
        )
        assert client.endpoint == "http://198.51.100.194:30031/mcp"
        assert client.token == "test-token-abc123"

    def test_call_tool_success(self):
        """Call a tool via MCP and return result."""
        client = runner.MCPClient("http://test/mcp", "token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"status": "success", "data": "config output"})
                    }
                ]
            }
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.post", return_value=mock_response) as mock_post:
            result = client.call_tool("get_junos_config", {"device": "test-vsrx"})

            # Verify request
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "http://test/mcp"

            # Check Authorization header
            headers = call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer token"

            # Check JSON-RPC request
            payload = call_args[1]["json"]
            assert payload["jsonrpc"] == "2.0"
            assert payload["method"] == "tools/call"
            assert payload["params"]["name"] == "get_junos_config"
            assert payload["params"]["arguments"] == {"device": "test-vsrx"}

            # Verify result
            assert result == {"status": "success", "data": "config output"}

    def test_call_tool_error_response(self):
        """Handle MCP error responses."""
        client = runner.MCPClient("http://test/mcp", "token")

        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32600,
                "message": "Invalid request"
            }
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(runner.MCPError, match="Invalid request"):
                client.call_tool("get_junos_config", {})


class TestDeviceAllowlist:
    """Test device filtering for safety."""

    def test_filter_excludes_prod_and_outpost(self):
        """Exclude devices with 'prod' or 'outpost' in name (case-insensitive)."""
        devices = [
            {"name": "test-vsrx", "model": "vSRX"},
            {"name": "vsrx-prod", "model": "vSRX"},
            {"name": "vSRX-PROD-2", "model": "vSRX"},
            {"name": "preprovisionedOutpost", "model": "vSRX"},
            {"name": "lab-vsrx-01", "model": "vSRX"},
            {"name": "outpost-backup", "model": "vSRX"},
        ]

        allowed = runner.filter_safe_devices(devices)
        names = [d["name"] for d in allowed]

        assert "test-vsrx" in names
        assert "lab-vsrx-01" in names
        assert "vsrx-prod" not in names
        assert "vSRX-PROD-2" not in names
        assert "preprovisionedOutpost" not in names
        assert "outpost-backup" not in names


class TestAgenticRunner:
    """Test agentic loop with tool execution."""

    def test_run_scenario_single_turn(self):
        """Run scenario with one tool call that gets executed."""
        scenario = {
            "id": "test-01",
            "vendor": "junos",
            "prompt": "Check the config",
            "expected_calls": [{"tool": "get_junos_config"}],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [
            {
                "name": "get_junos_config",
                "description": "Get Junos configuration",
                "parameters": {
                    "type": "object",
                    "properties": {"device": {"type": "string"}},
                },
            }
        ]

        # Mock LLM client - first call returns tool call, second call stops
        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: call get_junos_config
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "get_junos_config",
                                "arguments": '{"device": "test-vsrx"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: stop after seeing result
                "choices": [{
                    "message": {"content": "Config retrieved successfully"},
                    "finish_reason": "stop"
                }]
            }
        ]

        # Mock MCP client - returns config result
        mock_mcp = Mock()
        mock_mcp.call_tool.return_value = {"config": "set system host-name test"}

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called MCP once
        mock_mcp.call_tool.assert_called_once_with(
            "get_junos_config",
            {"device": "test-vsrx"}
        )

        # Should have transcript with tool call
        assert len(result["transcript"]) == 1
        assert result["transcript"][0]["tool"] == "get_junos_config"
        assert result["pass"] is True

    def test_run_scenario_multi_turn(self):
        """Run scenario with multiple turns (model calls tool, gets result, calls another)."""
        scenario = {
            "id": "test-02",
            "vendor": "junos",
            "prompt": "Stage and diff a change",
            "expected_calls": [
                {"tool": "stage_panos_config"},
                {"tool": "diff_panos_candidate"},
            ],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [
            {"name": "stage_panos_config", "description": "Stage", "parameters": {"type": "object"}},
            {"name": "diff_panos_candidate", "description": "Diff", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        # Turn 1: stage
        # Turn 2: diff after receiving stage result
        # Turn 3: stop
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "stage_panos_config",
                                "arguments": '{"config": "set test"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "diff_panos_candidate",
                                "arguments": '{}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 3 - stop
                "choices": [{
                    "message": {"content": "Done"},
                    "finish_reason": "stop"
                }]
            }
        ]

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = [
            {"status": "staged"},
            {"diff": "+ set test"},
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have 2 tool calls in transcript
        assert len(result["transcript"]) == 2
        assert result["transcript"][0]["tool"] == "stage_panos_config"
        assert result["transcript"][1]["tool"] == "diff_panos_candidate"

        # LLM should have been called 3 times (stage, diff, stop)
        assert mock_llm.complete_with_tools.call_count == 3

    def test_run_scenario_respects_max_turns(self):
        """Stop after max_turns even if model doesn't stop."""
        scenario = {
            "id": "test-03",
            "vendor": "junos",
            "prompt": "Infinite loop test",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [{"name": "get_config", "description": "Get", "parameters": {"type": "object"}}]

        mock_llm = Mock()
        # Always return a tool call
        mock_llm.complete_with_tools.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "type": "function",
                        "function": {"name": "get_config", "arguments": "{}"}
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }

        mock_mcp = Mock()
        mock_mcp.call_tool.return_value = {"data": "config"}

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
            max_turns=3,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should stop at max_turns
        assert mock_llm.complete_with_tools.call_count == 3
        assert len(result["transcript"]) == 3
        assert result["reason"].startswith("incomplete:")

    def test_safety_rail_blocks_forbidden_tools(self):
        """Block approve_panos_change_set and commit tools (safety rail)."""
        scenario = {
            "id": "test-04",
            "vendor": "panos",
            "prompt": "Try to commit",
            "expected_calls": [],
            "forbidden_calls": [
                {"tool": "approve_panos_change_set"},
                {"tool": "commit_panos_candidate"}
            ],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [
            {"name": "stage_panos_config", "description": "Stage", "parameters": {"type": "object"}},
            {"name": "approve_panos_change_set", "description": "Approve", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: try to call forbidden tool
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "approve_panos_change_set",
                                "arguments": '{}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: stop after seeing error
                "choices": [{
                    "message": {"content": "Cannot proceed"},
                    "finish_reason": "stop"
                }]
            }
        ]

        mock_mcp = Mock()

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
            max_turns=12,
            forbidden_tools={"approve_panos_change_set", "commit_panos_candidate", "apply_panos_change_set"},
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should NOT call the forbidden tool via MCP
        mock_mcp.call_tool.assert_not_called()

        # Should record the tool call in transcript but fail scoring
        assert len(result["transcript"]) == 1
        assert result["transcript"][0]["tool"] == "approve_panos_change_set"
        assert result["pass"] is False
        assert "forbidden" in result["reason"]

    def test_teardown_discards_change_sets(self):
        """Change-sets created during run are discarded in teardown."""
        scenario = {
            "id": "test-teardown",
            "vendor": "junos",
            "prompt": "Create and test a change",
            "expected_calls": [
                {"tool": "create_junos_change_set"},
            ],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [
            {"name": "create_junos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: create change-set
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"config": "set test"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: stop
                "choices": [{
                    "message": {"content": "Done"},
                    "finish_reason": "stop"
                }]
            },
        ]

        mock_mcp = Mock()
        # First call returns change-set ID
        mock_mcp.call_tool.side_effect = [
            {"change_set_id": "cs-123", "status": "created"},  # create_junos_change_set
            {"success": True},  # discard_candidate in teardown
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called create_junos_change_set and then discard_candidate
        assert mock_mcp.call_tool.call_count == 2
        calls = [call[0] for call in mock_mcp.call_tool.call_args_list]
        assert calls[0][0] == "create_junos_change_set"
        assert calls[1][0] == "discard_candidate"
        assert calls[1][1]["change_set_id"] == "cs-123"

    def test_teardown_on_exception(self):
        """Change-sets are discarded even when scenario raises exception."""
        scenario = {
            "id": "test-exception",
            "vendor": "panos",
            "prompt": "Create a change",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [
            {"name": "create_panos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        # First call succeeds, second raises exception
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: create change-set
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_panos_change_set",
                                "arguments": '{}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            Exception("LLM error"),  # Turn 2: exception
        ]

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = [
            {"change_set_id": "cs-456", "status": "created"},  # create_panos_change_set
            {"success": True},  # discard_panos_candidate in teardown
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-pa",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Scenario should fail with runner_error
        assert result["pass"] is False
        assert "runner_error" in result["reason"]

        # But teardown should still discard the change-set
        assert mock_mcp.call_tool.call_count == 2
        calls = [call[0] for call in mock_mcp.call_tool.call_args_list]
        assert calls[1][0] == "discard_panos_candidate"
        assert calls[1][1]["change_set_id"] == "cs-456"
