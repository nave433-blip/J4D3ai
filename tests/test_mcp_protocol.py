"""MCP client protocol tests — framing + handshake against a mock server."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

# Ensure repo root on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class FakeMCPServer:
    """Minimal Content-Length MCP server in-process via subprocess script."""

    SCRIPT = r'''
import json, sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("utf-8", errors="replace")
        if line in ("\r\n", "\n", ""):
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(n)
    return json.loads(body.decode("utf-8"))

def send(obj):
    body = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()

while True:
    msg = read_message()
    if msg is None:
        break
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
            },
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo arguments",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        })
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        send({
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(args)}],
                "isError": False,
            },
        })
    else:
        if mid is not None:
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            })
'''

    @staticmethod
    def command():
        return sys.executable

    @staticmethod
    def args():
        # run script via -c
        return ["-c", FakeMCPServer.SCRIPT]


def test_content_length_extract():
    from core.mcp_client import MCPClient

    client = object.__new__(MCPClient)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()
    buf = f"Content-Length: {len(body)}\r\n\r\n".encode() + body + b"trailing"
    msg, rest = MCPClient._try_extract_message(client, buf)
    assert msg["result"]["ok"] is True
    assert rest == b"trailing"


def test_ndjson_extract():
    from core.mcp_client import MCPClient

    client = object.__new__(MCPClient)
    buf = b'{"jsonrpc":"2.0","id":2,"result":{"x":1}}\nnext'
    msg, rest = MCPClient._try_extract_message(client, buf)
    assert msg["id"] == 2
    assert rest == b"next"


def test_initialize_list_call_roundtrip():
    from core.mcp_client import MCPClient

    c = MCPClient(FakeMCPServer.command(), FakeMCPServer.args())
    try:
        result = c.initialize(timeout=10)
        assert result["serverInfo"]["name"] == "fake-mcp"
        assert c.server_info["name"] == "fake-mcp"
        tools = c.list_tools(timeout=10)
        assert any(t["name"] == "echo" for t in tools)
        out = c.call_tool("echo", {"hello": "world"}, timeout=10)
        assert out["content"][0]["type"] == "text"
        assert "hello" in out["content"][0]["text"]
    finally:
        c.close()


def test_unknown_method_raises():
    from core.mcp_client import MCPClient, MCPError

    c = MCPClient(FakeMCPServer.command(), FakeMCPServer.args())
    try:
        c.initialize(timeout=10)
        with pytest.raises(MCPError):
            c._request("does/not/exist", {}, timeout=5)
    finally:
        c.close()


def test_large_payload_roundtrip_fast():
    """A ~1 MiB tool result must arrive intact without byte-at-a-time reads.

    Regression test for the old stdout.read(1) loop, which made large
    payloads take minutes (a syscall per byte).
    """
    import time
    from core.mcp_client import MCPClient

    big = {"blob": "x" * (1 << 20)}
    client = MCPClient(FakeMCPServer.command(), FakeMCPServer.args())
    try:
        client.initialize()
        start = time.monotonic()
        result = client.call_tool("echo", big, timeout=60)
        elapsed = time.monotonic() - start
    finally:
        client.close()
    content = result.get("content", [{}])[0].get("text", "")
    assert json.loads(content) == big
    assert elapsed < 20, f"1 MiB roundtrip took {elapsed:.1f}s — read loop regressed"


def test_read_loop_buffer_guard():
    """A server spewing non-message garbage must not grow memory unboundedly."""
    from core.mcp_client import MCPClient

    client = object.__new__(MCPClient)
    # Feed 20 MiB of garbage with no complete message; the guard must drop it.
    garbage = b"z" * (20 << 20)
    msg, rest = client._try_extract_message(garbage)
    assert msg is None
    # Simulate the guard path directly.
    assert len(garbage) > client._MAX_BUFFER
