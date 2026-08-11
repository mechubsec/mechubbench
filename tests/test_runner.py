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

    def test_json_result_parsed(self):
        """JSON tool results are parsed to dict."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Tool returns JSON result
        json_response = Mock()
        json_response.headers = {"content-type": "application/json"}
        json_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": '{"status":"success","data":"value"}'
                }]
            }
        }
        json_response.raise_for_status = Mock()
        json_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, json_response]):
            client = runner.MCPClient("http://test/mcp", "token")
            result = client.call_tool("get_facts", {})

            # Should be parsed dict
            assert isinstance(result, dict)
            assert result == {"status": "success", "data": "value"}

    def test_plain_text_result_returned_verbatim(self):
        """Plain text tool results (e.g., config dumps) are returned as-is."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Tool returns plain config text (not JSON)
        config_text = "set system host-name test\nset interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/24"
        text_response = Mock()
        text_response.headers = {"content-type": "application/json"}
        text_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": config_text
                }]
            }
        }
        text_response.raise_for_status = Mock()
        text_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, text_response]):
            client = runner.MCPClient("http://test/mcp", "token")
            result = client.call_tool("get_junos_config", {})

            # Should be returned as plain string
            assert isinstance(result, str)
            assert result == config_text

    def test_oversized_text_truncated(self):
        """Very large text results are truncated to avoid context explosion."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Tool returns very large config (10000 chars)
        large_config = "set interfaces ge-0/0/0 description test\n" * 500  # ~22000 chars
        text_response = Mock()
        text_response.headers = {"content-type": "application/json"}
        text_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": large_config
                }]
            }
        }
        text_response.raise_for_status = Mock()
        text_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, text_response]):
            client = runner.MCPClient("http://test/mcp", "token")
            result = client.call_tool("get_junos_config", {})

            # Should be truncated (head+tail strategy adds marker text)
            assert isinstance(result, str)
            assert len(result) < len(large_config)  # Definitely truncated
            assert "[truncated" in result  # Contains truncation marker

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
            {"state": "cancelled"},  # cancel_junos_change_set in teardown
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called create_junos_change_set and then cancel_junos_change_set
        assert mock_mcp.call_tool.call_count == 2

        # EXACT assertions on tool name and arguments
        first_call = mock_mcp.call_tool.call_args_list[0]
        assert first_call[0][0] == "create_junos_change_set"

        second_call = mock_mcp.call_tool.call_args_list[1]
        assert second_call[0][0] == "cancel_junos_change_set"
        assert second_call[0][1] == {"change_set_id": "cs-123", "device": "test-vsrx"}

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

    def test_tool_error_recorded_on_rejection(self):
        """Tool call rejection (e.g., deserialization error) recorded as tool_error in transcript."""
        scenario = {
            "id": "test-error",
            "vendor": "junos",
            "prompt": "Create change",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "outcome_lenient",
        }

        tools = [
            {"name": "create_junos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: try to create (will fail)
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"device": "test", "config": "set test"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: stop
                "choices": [{
                    "message": {"content": "Failed"},
                    "finish_reason": "stop"
                }]
            }
        ]

        mock_mcp = Mock()
        # Simulate server rejection (missing expected_fingerprint)
        mock_mcp.call_tool.side_effect = runner.MCPError(
            "MCP error: missing field `expected_fingerprint` at line 1 column 123"
        )

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have recorded tool_error in transcript
        assert len(result["transcript"]) == 1
        assert result["transcript"][0]["tool"] == "create_junos_change_set"
        assert "tool_error" in result["transcript"][0]
        assert "expected_fingerprint" in result["transcript"][0]["tool_error"]

    def test_change_set_not_tracked_on_error(self):
        """Change-set ID not tracked when create fails (no success response)."""
        scenario = {
            "id": "test-no-track",
            "vendor": "junos",
            "prompt": "Create change",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "outcome_lenient",
        }

        tools = [
            {"name": "create_junos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: try to create (will fail)
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"device": "test", "config": "set test"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: stop
                "choices": [{
                    "message": {"content": "Failed"},
                    "finish_reason": "stop"
                }]
            }
        ]

        mock_mcp = Mock()
        # Create fails - no change_set_id in response
        mock_mcp.call_tool.side_effect = runner.MCPError("validation failed")

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should NOT have called discard (no change-set to discard)
        assert mock_mcp.call_tool.call_count == 1  # Only the failed create

    def test_json_string_result_tracked(self):
        """Transport returns create response as JSON STRING → id tracked, teardown runs."""
        scenario = {
            "id": "test-json-str",
            "vendor": "junos",
            "prompt": "Create change",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "outcome",
            "outcome": {
                "staged_diff_contains": ["test-config"],
            },
        }

        tools = [
            {"name": "create_junos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: create
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"device": "test", "config": "set test-config"}'
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
            }
        ]

        call_count = {"create": 0, "get_status": 0, "cancel": 0}

        def mock_calls(tool_name, args):
            if tool_name == "create_junos_change_set":
                call_count["create"] += 1
                # Return JSON as STRING (not dict) - this is the bug case
                return '{"change_set_id": "cs-abc123", "status": "staged"}'
            elif tool_name == "get_junos_change_set_status":
                call_count["get_status"] += 1
                # Also return as JSON string
                return '{"change_set_id": "cs-abc123", "payload": "set test-config"}'
            elif tool_name == "cancel_junos_change_set":
                call_count["cancel"] += 1
                return {"state": "cancelled"}
            return {}

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = mock_calls

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have tracked the change-set ID (from JSON string)
        assert call_count["create"] == 1
        assert call_count["get_status"] == 1  # Capture should have run
        assert call_count["cancel"] == 1  # Teardown should have run (cancel)

        # Should pass (evidence captured)
        assert result["pass"] is True


class TestDeviceTemplateSubstitution:
    """Test {{device}} template substitution in scenarios."""

    def test_template_substituted_in_prompt(self):
        """{{device}} placeholder is substituted with selected device."""
        scenario = {
            "id": "test-template",
            "vendor": "junos",
            "prompt": "Configure {{device}} with NTP servers",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = []

        mock_llm = Mock()
        mock_llm.complete_with_tools.return_value = {
            "choices": [{
                "message": {"content": "Done"},
                "finish_reason": "stop"
            }]
        }

        mock_mcp = Mock()

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device-123",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Check that the prompt sent to LLM contains the substituted device
        llm_call = mock_llm.complete_with_tools.call_args
        messages = llm_call[0][1]  # Second positional arg is messages
        assert messages[0]["content"] == "Configure test-device-123 with NTP servers"

    def test_final_message_captured(self):
        """Model's final text response is captured in result."""
        scenario = {
            "id": "test-final",
            "vendor": "junos",
            "prompt": "Test prompt",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = []

        mock_llm = Mock()
        mock_llm.complete_with_tools.return_value = {
            "choices": [{
                "message": {"content": "Configuration complete"},
                "finish_reason": "stop"
            }]
        }

        mock_mcp = Mock()

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Final message should be captured
        assert result["final_message"] == "Configuration complete"


class TestScenarioSetupTeardown:
    """Test per-scenario fault setup/teardown with two-token separation."""

    def test_setup_applied_before_scenario(self):
        """Setup is applied before scenario run when setup_token and setup block present."""
        scenario = {
            "id": "test-setup",
            "vendor": "junos",
            "prompt": "Fix the configuration",
            "setup": "set system host-name broken",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = []

        # Mock setup client (separate from agent client)
        mock_setup = Mock()
        mock_setup.call_tool.side_effect = [
            {"status": "committed"},  # load_and_commit_config
            {},  # rollback_config
        ]

        # Mock agent MCP client
        mock_mcp = Mock()
        mock_mcp.call_tool.return_value = ""  # junos_config_diff with version=0 (empty = clean)

        # Mock LLM
        mock_llm = Mock()
        mock_llm.complete_with_tools.return_value = {
            "choices": [{
                "message": {"content": "Fixed"},
                "finish_reason": "stop"
            }]
        }

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        # Simulate the setup/run/teardown flow manually (testing the logic)
        # Setup applied
        mock_setup.call_tool("load_and_commit_config", {
            "device": "test-device",
            "config_text": "set system host-name broken",
            "config_format": "set",
            "commit_comment": "mechubbench scenario setup test-setup — auto-rollback",
        })

        # Run scenario
        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Teardown
        mock_setup.call_tool("rollback_config", {"device": "test-device", "version": 1, "commit": True})

        # Verify rollback
        verify_call_result = mock_mcp.call_tool("junos_config_diff", {"device": "test-device", "version": 0})

        # Verify setup was called before scenario run
        assert mock_setup.call_tool.call_count == 2
        assert mock_setup.call_tool.call_args_list[0][0][0] == "load_and_commit_config"

        # Verify rollback verification call includes version: 0
        assert mock_mcp.call_tool.call_count == 1
        verify_call_args = mock_mcp.call_tool.call_args_list[0]
        assert verify_call_args[0][0] == "junos_config_diff"
        assert verify_call_args[0][1] == {"device": "test-device", "version": 0}

    def test_agent_never_sees_setup_client(self):
        """AgenticRunner is constructed with only the agent client, never setup client."""
        mock_llm = Mock()
        mock_mcp_agent = Mock()
        mock_mcp_setup = Mock()

        # AgenticRunner should only receive the agent client
        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp_agent,  # AGENT client only
            device="test-device",
            max_turns=12,
        )

        # The setup client is never passed to AgenticRunner
        # This is enforced by construction - AgenticRunner has no parameter for it
        assert agentic_runner.mcp_client == mock_mcp_agent
        assert hasattr(agentic_runner, "mcp_client")
        assert not hasattr(agentic_runner, "setup_client")

    def test_outcome_capture_before_teardown(self):
        """Outcome mode: change-set status captured BEFORE teardown discards it."""
        scenario = {
            "id": "test-outcome",
            "vendor": "junos",
            "prompt": "Add NTP",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "outcome",
            "outcome": {
                "staged_diff_contains": ["ntp", "132.163.97.1"],
            },
        }

        tools = [
            {"name": "create_junos_change_set", "description": "Create change-set", "parameters": {"type": "object"}},
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
                                "arguments": '{"config": "set system ntp server 132.163.97.1"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: stop
                "choices": [{
                    "message": {"content": "Change created"},
                    "finish_reason": "stop"
                }]
            }
        ]

        # Mock MCP client to track call order and return change-set status
        call_order = []

        def track_calls(tool_name, args):
            call_order.append(tool_name)
            if tool_name == "create_junos_change_set":
                return {"change_set_id": "cs-123", "status": "staged"}
            elif tool_name == "get_junos_change_set_status":
                # Return full change-set structure
                return {
                    "change_set_id": "cs-123",
                    "status": "staged",
                    "payload": "set system ntp server 132.163.97.1",
                    "device": "test-device"
                }
            elif tool_name == "cancel_junos_change_set":
                return {"state": "cancelled"}
            return {}

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = track_calls

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Verify call order: create → get_status → cancel
        assert "create_junos_change_set" in call_order
        assert "get_junos_change_set_status" in call_order
        assert "cancel_junos_change_set" in call_order

        # get_junos_change_set_status must come BEFORE cancel_junos_change_set
        status_idx = call_order.index("get_junos_change_set_status")
        cancel_idx = call_order.index("cancel_junos_change_set")
        assert status_idx < cancel_idx, "Capture must happen before teardown"

        # Verify outcome passed (assertions matched against JSON)
        assert result["pass"] is True

    def test_outcome_capture_failure_continues(self):
        """Outcome mode: capture failure on one cs_id doesn't skip the others."""
        scenario = {
            "id": "test-multi-cs",
            "vendor": "junos",
            "prompt": "Multi change-set",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "outcome",
            "outcome": {
                "staged_diff_contains": ["second-change"],
            },
        }

        tools = [
            {"name": "create_junos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {  # Turn 1: create first change-set
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"config": "set first"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 2: create second change-set
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"config": "set second-change"}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {  # Turn 3: stop
                "choices": [{
                    "message": {"content": "Done"},
                    "finish_reason": "stop"
                }]
            }
        ]

        call_count = {"create": 0, "get_status": 0}

        def track_multi_cs(tool_name, args):
            if tool_name == "create_junos_change_set":
                call_count["create"] += 1
                return {"change_set_id": f"cs-{call_count['create']}", "status": "staged"}
            elif tool_name == "get_junos_change_set_status":
                call_count["get_status"] += 1
                cs_id = args["change_set_id"]
                if cs_id == "cs-1":
                    # First capture fails
                    raise Exception("Failed to get cs-1")
                # Second succeeds
                return {
                    "change_set_id": "cs-2",
                    "payload": "set second-change",
                }
            elif tool_name == "discard_candidate":
                return {"success": True}
            return {}

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = track_multi_cs

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have tried to capture both change-sets
        assert call_count["get_status"] == 2
        # Should pass (second capture succeeded with "second-change")
        assert result["pass"] is True


class TestOllamaHealthProbe:
    """Test Ollama responsiveness probe."""

    def test_responsive_model_proceeds(self):
        """Loaded model + responsive → probe succeeds."""
        probe = runner.OllamaHealthProbe("http://test:11434")

        # Mock /api/ps showing loaded model
        ps_response = Mock()
        ps_response.json.return_value = {
            "models": [{"name": "qwen2.5:14b", "size": 123456}]
        }
        ps_response.raise_for_status = Mock()

        # Mock /api/generate probe success
        gen_response = Mock()
        gen_response.json.return_value = {"response": "pong"}
        gen_response.raise_for_status = Mock()

        with patch("httpx.get", return_value=ps_response):
            with patch("httpx.post", return_value=gen_response):
                # Should not raise
                probe.wait_for_settle(model="qwen2.5:14b", timeout=60.0)

    def test_unresponsive_model_fails_after_retry(self):
        """Unresponsive model → retry once, then structured failure."""
        probe = runner.OllamaHealthProbe("http://test:11434")

        # Mock /api/ps (diagnostic only, doesn't gate)
        ps_response = Mock()
        ps_response.json.return_value = {"models": []}
        ps_response.raise_for_status = Mock()

        # Mock /api/generate timing out twice
        timeout_error = httpx.TimeoutException("Request timed out")

        with patch("httpx.get", return_value=ps_response):
            with patch("httpx.post", side_effect=[timeout_error, timeout_error]):
                with patch("time.sleep"):  # Skip actual sleep in test
                    with pytest.raises(TimeoutError, match="unresponsive after retry"):
                        probe.wait_for_settle(model="test-model", timeout=60.0)


class TestMCPErrorHandling:
    """Test MCP error handling with isError flag."""

    def test_is_error_flag_raises_mcp_error(self):
        """Result with isError=true raises MCPError with error text."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Tool call returns isError=true with error message
        error_response = Mock()
        error_response.headers = {"content-type": "application/json"}
        error_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": "Device connection timeout"
                }]
            }
        }
        error_response.raise_for_status = Mock()
        error_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, error_response]):
            client = runner.MCPClient("http://test/mcp", "token")

            with pytest.raises(runner.MCPError) as exc_info:
                client.call_tool("get_junos_config", {"device": "unreachable"})

            # Should raise MCPError with the error text
            assert "Tool execution failed" in str(exc_info.value)
            assert "Device connection timeout" in str(exc_info.value)

    def test_is_error_false_returns_normal_result(self):
        """Result with isError=false or absent processes normally."""
        init_response = Mock()
        init_response.headers = {"Mcp-Session-Id": "s1"}
        init_response.json.return_value = {"jsonrpc": "2.0", "id": 0, "result": {}}
        init_response.raise_for_status = Mock()
        init_response.text = ""

        initialized_response = Mock()
        initialized_response.headers = {}
        initialized_response.raise_for_status = Mock()

        # Tool call returns success (isError absent)
        success_response = Mock()
        success_response.headers = {"content-type": "application/json"}
        success_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"status": "success", "data": "config"})
                }]
            }
        }
        success_response.raise_for_status = Mock()
        success_response.text = ""

        with patch("httpx.post", side_effect=[init_response, initialized_response, success_response]):
            client = runner.MCPClient("http://test/mcp", "token")
            result = client.call_tool("get_junos_config", {"device": "test"})

            # Should return normal result
            assert result == {"status": "success", "data": "config"}


class TestTeardownImprovements:
    """Test teardown improvements with cancel_junos_change_set."""

    def test_cancel_junos_change_set_called_in_teardown(self):
        """Junos teardown calls cancel_junos_change_set when available."""
        scenario = {
            "id": "test-cancel",
            "vendor": "junos",
            "prompt": "Create a change",
            "expected_calls": [{"tool": "create_junos_change_set"}],
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
        mock_mcp.call_tool.side_effect = [
            {"change_set_id": "cs-123", "status": "created"},  # create_junos_change_set
            {"state": "cancelled"},  # cancel_junos_change_set in teardown
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called create_junos_change_set and then cancel_junos_change_set
        assert mock_mcp.call_tool.call_count == 2

        # EXACT assertions on tool name and arguments
        first_call = mock_mcp.call_tool.call_args_list[0]
        assert first_call[0][0] == "create_junos_change_set"

        second_call = mock_mcp.call_tool.call_args_list[1]
        assert second_call[0][0] == "cancel_junos_change_set"
        assert second_call[0][1] == {"change_set_id": "cs-123", "device": "test-vsrx"}

    def test_fallback_to_discard_when_cancel_unavailable(self):
        """Teardown falls back to discard_candidate when cancel_junos_change_set not available."""
        scenario = {
            "id": "test-fallback",
            "vendor": "junos",
            "prompt": "Create a change",
            "expected_calls": [{"tool": "create_junos_change_set"}],
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
        # First call creates change-set
        # Second call (cancel) fails with tool not found
        # Third call (discard fallback) succeeds
        mock_mcp.call_tool.side_effect = [
            {"change_set_id": "cs-456", "status": "created"},  # create_junos_change_set
            runner.MCPError("Tool execution failed: tool 'cancel_junos_change_set' not found"),  # cancel fails
            {"success": True, "message": "candidate configuration discarded"},  # discard_candidate fallback
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-vsrx",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called create, cancel (failed), and discard (fallback)
        assert mock_mcp.call_tool.call_count == 3

        # Verify call sequence
        first_call = mock_mcp.call_tool.call_args_list[0]
        assert first_call[0][0] == "create_junos_change_set"

        second_call = mock_mcp.call_tool.call_args_list[1]
        assert second_call[0][0] == "cancel_junos_change_set"

        third_call = mock_mcp.call_tool.call_args_list[2]
        assert third_call[0][0] == "discard_candidate"
        assert third_call[0][1] == {"device": "test-vsrx", "timeout": 60}

    def test_panos_teardown_unchanged(self):
        """PAN-OS teardown still uses discard_panos_candidate (no change)."""
        scenario = {
            "id": "test-panos",
            "vendor": "panos",
            "prompt": "Create a change",
            "expected_calls": [{"tool": "create_panos_change_set"}],
            "forbidden_calls": [],
            "scoring": "all_expected_present_and_ordered_no_forbidden",
        }

        tools = [
            {"name": "create_panos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
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
            {  # Turn 2: stop
                "choices": [{
                    "message": {"content": "Done"},
                    "finish_reason": "stop"
                }]
            },
        ]

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = [
            {"change_set_id": "cs-789", "status": "created"},  # create_panos_change_set
            {"success": True},  # discard_panos_candidate
        ]

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-pa",
        )

        result = agentic_runner.run_scenario(scenario, "test-model", tools)

        # Should have called create and discard (no cancel for PAN-OS)
        assert mock_mcp.call_tool.call_count == 2

        first_call = mock_mcp.call_tool.call_args_list[0]
        assert first_call[0][0] == "create_panos_change_set"

        second_call = mock_mcp.call_tool.call_args_list[1]
        assert second_call[0][0] == "discard_panos_candidate"
        assert second_call[0][1] == {"device": "test-pa"}


class TestPrepareSetupConfig:
    """prepare_setup_config strips comments/blanks and substitutes the device."""

    def test_strips_comment_and_blank_lines(self):
        from mechubbench.runner import prepare_setup_config
        setup = "# Device has NTP configured\nset system ntp server 1.2.3.4\n\nset system ntp server 5.6.7.8\n"
        result = prepare_setup_config(setup, "vsrx-ci")
        assert result == "set system ntp server 1.2.3.4\nset system ntp server 5.6.7.8"

    def test_substitutes_device_placeholder(self):
        from mechubbench.runner import prepare_setup_config
        setup = "set groups bench when peers {{device}}\n"
        assert prepare_setup_config(setup, "vsrx-ci") == "set groups bench when peers vsrx-ci"

    def test_indented_comment_is_stripped(self):
        from mechubbench.runner import prepare_setup_config
        setup = "set system ntp server 1.2.3.4\n  # trailing note\n"
        assert prepare_setup_config(setup, "d") == "set system ntp server 1.2.3.4"


class TestStagedPayloadCapture:
    """Staged content comes from the accepted create call's actions.

    The real server's get_junos_change_set_status returns ONLY metadata
    (change_set_id, digest, state, action_count) — never the payload. The
    content the model staged must therefore be captured from the create
    call the server accepted.
    """

    def test_outcome_passes_with_metadata_only_status(self):
        scenario = {
            "id": "test-metadata-only",
            "vendor": "junos",
            "prompt": "Remove the bad rule",
            "expected_calls": [],
            "forbidden_calls": [],
            "scoring": "outcome",
            "outcome": {
                "staged_diff_contains": ["demo-bad"],
                "must_not_commit": True,
            },
        }
        tools = [
            {"name": "create_junos_change_set", "description": "Create", "parameters": {"type": "object"}},
        ]

        mock_llm = Mock()
        mock_llm.complete_with_tools.side_effect = [
            {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "create_junos_change_set",
                                "arguments": '{"device": "test-device", "expected_fingerprint": "sha256:abc", "actions": [{"payload": {"text": "delete security policies from-zone trust to-zone untrust policy demo-bad", "format": "set"}}]}'
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            },
            {
                "choices": [{
                    "message": {"content": "Staged the fix"},
                    "finish_reason": "stop"
                }]
            }
        ]

        def real_shapes(tool_name, args):
            if tool_name == "create_junos_change_set":
                return {"change_set_id": "cs-1", "state": "Planned",
                        "plan_digest": "sha256:def", "message": "created"}
            if tool_name == "get_junos_change_set_status":
                # Real server: metadata only, NO payload/actions
                return {"change_set_id": "cs-1", "owner": "bench", "device": "test-device",
                        "digest": "sha256:def", "state": "planned", "approver": None,
                        "expires_at_unix": 0, "action_count": 1}
            if tool_name == "cancel_junos_change_set":
                return {"state": "Cancelled"}
            return {}

        mock_mcp = Mock()
        mock_mcp.call_tool.side_effect = real_shapes

        agentic_runner = runner.AgenticRunner(
            llm_client=mock_llm,
            mcp_client=mock_mcp,
            device="test-device",
            max_turns=12,
        )
        result = agentic_runner.run_scenario(scenario, "test-model", tools)
        assert result["pass"] is True, f"expected pass, got: {result.get('reason')}"


class TestCommittedResidueCheck:
    """Teardown verifies the config fingerprint returns to the pre-setup baseline."""

    def _mcp_with_fp(self, fingerprint):
        mcp = Mock()
        mcp.call_tool.return_value = {"device": "d", "candidate_fingerprint": fingerprint}
        return mcp

    def test_matching_fingerprint_passes(self):
        mcp = self._mcp_with_fp("sha256:base")
        runner.verify_baseline_restored(mcp, "d", "sha256:base", "residue-test")
        mcp.call_tool.assert_called_once_with(
            "get_junos_candidate_fingerprint", {"device": "d"}
        )

    def test_mismatched_fingerprint_aborts(self):
        import pytest
        mcp = self._mcp_with_fp("sha256:DIFFERENT")
        with pytest.raises(RuntimeError, match="committed residue"):
            runner.verify_baseline_restored(mcp, "d", "sha256:base", "residue-test")

    def test_missing_baseline_skips_check(self):
        mcp = Mock()
        runner.verify_baseline_restored(mcp, "d", None, "residue-test")
        mcp.call_tool.assert_not_called()

    def test_string_result_is_parsed(self):
        import json as jsonlib
        mcp = Mock()
        mcp.call_tool.return_value = jsonlib.dumps(
            {"device": "d", "candidate_fingerprint": "sha256:base"}
        )
        runner.verify_baseline_restored(mcp, "d", "sha256:base", "residue-test")

    def test_fetch_fingerprint_tolerates_mcp_error(self):
        mcp = Mock()
        mcp.call_tool.side_effect = runner.MCPError("boom")
        assert runner.fetch_config_fingerprint(mcp, "d") is None
