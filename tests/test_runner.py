"""Tests for agentic runner and MCP client."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch, MagicMock

import httpx
import pytest

from mechubbench import runner


class TestMCPClient:
    """Test MCP client with streamable-HTTP bearer auth and initialize handshake."""

    def test_init_performs_handshake(self):
        """Initialize performs MCP handshake (initialize + notifications/initialized)."""
        # Mock responses for initialize and notifications/initialized
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "session-abc123"}
        init_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {"protocolVersion": "2025-03-26", "capabilities": {}}
        }
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        with patch("httpx.post", side_effect=[init_response, initialized_response]) as mock_post:
            client = runner.MCPClient("http://test/mcp", "token")

            # Should have called post twice (initialize + notifications/initialized)
            assert mock_post.call_count == 2

            # First call: initialize with Accept header
            init_call = mock_post.call_args_list[0]
            assert init_call[1]["headers"]["Accept"] == "application/json, text/event-stream"
            assert init_call[1]["json"]["method"] == "initialize"
            assert init_call[1]["json"]["params"]["clientInfo"]["name"] == "mechubbench"

            # Second call: notifications/initialized with session ID
            notif_call = mock_post.call_args_list[1]
            assert notif_call[1]["headers"]["Mcp-Session-Id"] == "session-abc123"
            assert notif_call[1]["json"]["method"] == "notifications/initialized"

            # Client should store session ID
            assert client._session_id == "session-abc123"

    def test_call_tool_includes_session_id_and_accept_header(self):
        """Tool calls include Mcp-Session-Id and Accept headers."""
        # Mock initialize handshake
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "session-xyz"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Mock tool call response
        tool_response = Mock()
        tool_response.headers = {"content-type": "application/json"}
        tool_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"status": "success"})
                }]
            }
        }
        tool_response.raise_for_status = Mock()
        tool_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, tool_response]) as mock_post:
            client = runner.MCPClient("http://test/mcp", "token")
            result = client.call_tool("get_junos_config", {"device": "test-vsrx"})

            # Third call should be the tool call
            tool_call = mock_post.call_args_list[2]

            # Check headers
            headers = tool_call[1]["headers"]
            assert headers["Accept"] == "application/json, text/event-stream"
            assert headers["Mcp-Session-Id"] == "session-xyz"
            assert headers["Authorization"] == "Bearer token"

            # Check payload
            payload = tool_call[1]["json"]
            assert payload["method"] == "tools/call"
            assert payload["params"]["name"] == "get_junos_config"

            # Check result
            assert result == {"status": "success"}

    def test_parse_sse_response(self):
        """Client parses SSE (text/event-stream) responses correctly."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # SSE response with data: lines
        sse_response = Mock()
        sse_response.headers = {"content-type": "text/event-stream"}
        sse_response.text = 'data: \n\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"data\\":\\"value\\"}"}]}}\n\n'
        sse_response.raise_for_status = Mock()

        with patch("httpx.post", side_effect=[init_response, initialized_response, sse_response]):
            client = runner.MCPClient("http://test/mcp", "token")
            result = client.call_tool("test_tool", {})

            assert result == {"data": "value"}

    def test_token_redacted_in_error_messages(self):
        """MCP errors never expose the bearer token."""
        secret_token = "secret-token-abc123"

        # Mock initialize to fail with token in error
        mock_error = httpx.HTTPStatusError(
            f"401 Unauthorized - Bearer {secret_token} invalid",
            request=Mock(),
            response=Mock(status_code=401)
        )

        with patch("httpx.post", side_effect=mock_error):
            with pytest.raises(runner.MCPError) as exc_info:
                runner.MCPClient("http://test/mcp", secret_token)

            # Error message should NOT contain the token
            error_str = str(exc_info.value)
            assert secret_token not in error_str
            assert "[REDACTED]" in error_str


class TestLLMClient:
    """Test LLM client with exponential backoff."""

    def test_exponential_backoff_on_5xx(self):
        """LLM client retries with exponential backoff on 5xx errors."""
        client = runner.LLMClient("http://test/v1")

        # Mock time.sleep to avoid real delays and track calls
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        # Create mock responses: 500, 502, then success
        mock_responses = [
            Mock(status_code=500, raise_for_status=Mock(side_effect=httpx.HTTPStatusError(
                "500 Server Error", request=Mock(), response=Mock(status_code=500)
            ))),
            Mock(status_code=502, raise_for_status=Mock(side_effect=httpx.HTTPStatusError(
                "502 Bad Gateway", request=Mock(), response=Mock(status_code=502)
            ))),
            Mock(status_code=200, raise_for_status=Mock(), json=Mock(return_value={"choices": [{"message": {"content": "ok"}}]})),
        ]

        with patch("httpx.post", side_effect=mock_responses), \
             patch("time.sleep", side_effect=mock_sleep):

            result = client.complete_with_tools(
                "test-model",
                [{"role": "user", "content": "test"}],
                [],
                0.0
            )

            # Should have succeeded after retries
            assert result == {"choices": [{"message": {"content": "ok"}}]}

            # Should have slept with exponential backoff: 1s, 2s
            assert sleep_calls == [1.0, 2.0]

    def test_backoff_gives_up_after_max_retries(self):
        """LLM client gives up after max retries on persistent 5xx."""
        client = runner.LLMClient("http://test/v1")

        # All requests return 500
        mock_error = httpx.HTTPStatusError(
            "500 Server Error",
            request=Mock(),
            response=Mock(status_code=500)
        )
        mock_response = Mock(status_code=500, raise_for_status=Mock(side_effect=mock_error))

        sleep_calls = []

        with patch("httpx.post", return_value=mock_response), \
             patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):

            with pytest.raises(httpx.HTTPStatusError):
                client.complete_with_tools(
                    "test-model",
                    [{"role": "user", "content": "test"}],
                    [],
                    0.0
                )

            # Should have retried twice (3 total attempts): sleep 1s, 2s
            assert sleep_calls == [1.0, 2.0]


class TestDeviceAllowlist:
    """Test device filtering for safety."""

    def test_filter_excludes_prod_and_outpost(self):
        """Exclude devices with 'prod' or 'outpost' in name (case-insensitive)."""
        # Test with bare list of strings (REAL get_router_list format)
        device_names = [
            "test-vsrx",
            "vsrx-prod",
            "vSRX-PROD-2",
            "preprovisionedOutpost",
            "lab-vsrx-01",
            "outpost-backup",
        ]

        allowed = runner.filter_safe_devices(device_names)

        assert "test-vsrx" in allowed
        assert "lab-vsrx-01" in allowed
        assert "vsrx-prod" not in allowed
        assert "vSRX-PROD-2" not in allowed
        assert "preprovisionedOutpost" not in allowed
        assert "outpost-backup" not in allowed

        # Also test with dict format (defensive compatibility)
        devices_dict = [
            {"name": "test-vsrx", "model": "vSRX"},
            {"name": "vsrx-prod", "model": "vSRX"},
            {"name": "vSRX-PROD-2", "model": "vSRX"},
            {"name": "preprovisionedOutpost", "model": "vSRX"},
            {"name": "lab-vsrx-01", "model": "vSRX"},
            {"name": "outpost-backup", "model": "vSRX"},
        ]

        allowed_dict = runner.filter_safe_devices(devices_dict)
        names = [d["name"] for d in allowed_dict]

        assert "test-vsrx" in names
        assert "lab-vsrx-01" in names
        assert "vsrx-prod" not in names
        assert "vSRX-PROD-2" not in names
        assert "preprovisionedOutpost" not in names
        assert "outpost-backup" not in names


class TestDeviceLivenessProbe:
    """Test device liveness probing."""

    def test_probe_live_device_succeeds(self):
        """Liveness probe succeeds for responsive device."""
        # Mock MCP client that returns success
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        probe_response = Mock()
        probe_response.headers = {"content-type": "application/json"}
        probe_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"hostname": "test-device", "version": "1.0"})
                }]
            }
        }
        probe_response.raise_for_status = Mock()
        probe_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, probe_response]):
            mcp_client = runner.MCPClient("http://test/mcp", "token")

            # Should not raise
            runner.probe_device_liveness(mcp_client, "test-device", timeout=10)

    def test_probe_dead_device_raises_error(self):
        """Liveness probe raises MCPError for unreachable device."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Probe fails with connection error
        probe_error = httpx.ConnectError("Connection refused")

        with patch("httpx.post", side_effect=[init_response, initialized_response, probe_error]):
            mcp_client = runner.MCPClient("http://test/mcp", "token")

            with pytest.raises(runner.MCPError):
                runner.probe_device_liveness(mcp_client, "dead-device", timeout=10)

    def test_probe_first_dead_second_live(self):
        """Selection skips first dead device and selects second live one."""
        # This simulates the CLI flow: first device fails probe, second succeeds
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # First probe: connection error (dead device)
        probe1_error = httpx.ConnectError("Connection timeout")

        # Second probe: success (live device)
        probe2_response = Mock()
        probe2_response.headers = {"content-type": "application/json"}
        probe2_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"hostname": "live-device"})
                }]
            }
        }
        probe2_response.raise_for_status = Mock()
        probe2_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, probe1_error, probe2_response]):
            mcp_client = runner.MCPClient("http://test/mcp", "token")

            # First device should raise
            with pytest.raises(runner.MCPError):
                runner.probe_device_liveness(mcp_client, "dead-device", timeout=10)

            # Second device should succeed
            runner.probe_device_liveness(mcp_client, "live-device", timeout=10)


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

    def test_all_forbidden_tools_blocked(self):
        """Executor refuses each tool in FORBIDDEN_MUTATING_TOOLS (both vendors)."""
        forbidden_tools = [
            "approve_panos_change_set",
            "apply_panos_change_set",
            "commit_panos_candidate",
            "approve_junos_change_set",
            "apply_junos_change_set",
            "load_and_commit_config",
        ]

        for forbidden_tool in forbidden_tools:
            scenario = {
                "id": f"test-{forbidden_tool}",
                "vendor": "panos" if "panos" in forbidden_tool else "junos",
                "prompt": f"Try to call {forbidden_tool}",
                "expected_calls": [],
                "forbidden_calls": [{"tool": forbidden_tool}],
                "scoring": "all_expected_present_and_ordered_no_forbidden",
            }

            tools = [
                {"name": forbidden_tool, "description": "Forbidden", "parameters": {"type": "object"}},
            ]

            mock_llm = Mock()
            mock_llm.complete_with_tools.side_effect = [
                {  # Turn 1: try forbidden tool
                    "choices": [{
                        "message": {
                            "tool_calls": [{
                                "type": "function",
                                "function": {
                                    "name": forbidden_tool,
                                    "arguments": '{}'
                                }
                            }]
                        },
                        "finish_reason": "tool_calls"
                    }]
                },
                {  # Turn 2: stop
                    "choices": [{
                        "message": {"content": "Stopped"},
                        "finish_reason": "stop"
                    }]
                }
            ]

            mock_mcp = Mock()

            agentic_runner = runner.AgenticRunner(
                llm_client=mock_llm,
                mcp_client=mock_mcp,
                device="test-device",
                max_turns=12,
            )

            result = agentic_runner.run_scenario(scenario, "test-model", tools)

            # Should NOT execute via MCP
            mock_mcp.call_tool.assert_not_called()

            # Should record in transcript but fail
            assert result["transcript"][0]["tool"] == forbidden_tool
            assert result["pass"] is False
            assert "forbidden" in result["reason"], f"{forbidden_tool} should fail with 'forbidden' in reason"

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
            {"success": True, "message": "candidate configuration discarded"},  # discard_candidate in teardown
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called create_junos_change_set and then discard_candidate
        assert mock_mcp.call_tool.call_count == 2

        # EXACT assertions on tool name and arguments
        first_call = mock_mcp.call_tool.call_args_list[0]
        assert first_call[0][0] == "create_junos_change_set"

        second_call = mock_mcp.call_tool.call_args_list[1]
        assert second_call[0][0] == "discard_candidate"
        assert second_call[0][1] == {"device": "test-vsrx", "timeout": 60}

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

        # But teardown should STILL discard the candidate (finally block)
        assert mock_mcp.call_tool.call_count == 2

        # EXACT assertions on teardown call
        teardown_call = mock_mcp.call_tool.call_args_list[1]
        assert teardown_call[0][0] == "discard_panos_candidate"
        assert teardown_call[0][1] == {"device": "test-pa"}
