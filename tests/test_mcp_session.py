"""MCPClient re-initializes once when the server drops its session."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from mechubbench import runner


class FakeServer:
    """Minimal streamable-HTTP MCP server; forgets sessions on demand."""

    def __init__(self, expire_with: str = "http", expiries: int = 1):
        self.sessions = 0
        self.live: str | None = None
        self.expire_with = expire_with
        self.expiries = expiries
        self.calls: list[str] = []

    def __call__(self, url, json: dict, headers: dict, timeout):
        request = httpx.Request("POST", url)
        method = json["method"]
        if method == "initialize":
            self.sessions += 1
            self.live = f"s{self.sessions}"
            return self._reply(request, {"result": {}}, {"mcp-session-id": self.live})
        if method == "notifications/initialized":
            return httpx.Response(202, request=request)
        self.calls.append(headers["Mcp-Session-Id"])
        if self.expiries:
            self.expiries -= 1
            if self.expire_with == "http":
                response = httpx.Response(
                    404, text="Not Found: Session not found", request=request
                )
                response.raise_for_status()
            return self._reply(request, {"error": {"message": "Session not found"}})
        text = {"ok": headers["Mcp-Session-Id"]}
        return self._reply(
            request, {"result": {"content": [{"type": "text", "text": dumps(text)}]}}
        )

    @staticmethod
    def _reply(request, body: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, **body},
            headers=headers or {},
            request=request,
        )


def dumps(value) -> str:
    """JSON-encode a tool result payload."""
    return json.dumps(value)


def test_expired_session_reinitializes_and_retries_once():
    server = FakeServer("http")
    with patch("httpx.post", side_effect=server):
        client = runner.MCPClient("http://bench/mcp", "tok-secret")
        assert client.call_tool("get_router_list", {}) == {"ok": "s2"}
    assert server.sessions == 2
    assert server.calls == ["s1", "s2"]


def test_jsonrpc_session_error_is_not_retried():
    """A NETCONF "session expired" from a device tool may follow a write."""
    server = FakeServer("jsonrpc")
    with patch("httpx.post", side_effect=server):
        client = runner.MCPClient("http://bench/mcp", "tok-secret")
        with pytest.raises(runner.MCPError) as caught:
            client.call_tool("load_and_commit_config", {})
    assert not isinstance(caught.value, runner.MCPSessionExpired)
    assert server.sessions == 1 and server.calls == ["s1"]


def test_second_expiry_is_raised_not_looped():
    server = FakeServer(expiries=2)
    with patch("httpx.post", side_effect=server):
        client = runner.MCPClient("http://bench/mcp", "tok-secret")
        with pytest.raises(runner.MCPSessionExpired) as caught:
            client.call_tool("get_router_list", {})
    assert server.sessions == 2
    assert "tok-secret" not in str(caught.value)


def test_tool_error_mentioning_session_is_not_retried():
    """A tool failure about flow sessions is a plain error, never a reconnect."""
    server = FakeServer(expiries=0)

    def tool_error(url, json, headers, timeout):
        if json["method"] != "tools/call":
            return server(url, json, headers, timeout)
        body = {"isError": True, "content": [{"text": "session not found in flow"}]}
        return FakeServer._reply(httpx.Request("POST", url), {"result": body})

    with patch("httpx.post", side_effect=tool_error):
        client = runner.MCPClient("http://bench/mcp", "tok-secret")
        with pytest.raises(runner.MCPError) as caught:
            client.call_tool("execute_junos_command", {})
    assert not isinstance(caught.value, runner.MCPSessionExpired)
    assert server.sessions == 1
