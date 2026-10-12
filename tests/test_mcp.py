"""Track 3 tests: minimal MCP stdio client against a fake MCP server.

No network, no real servers — a tiny script speaks newline-delimited
JSON-RPC back to the client under test.
"""
import json
import sys
import textwrap

import pytest

import core.mcp_client as mcp


FAKE_SERVER = textwrap.dedent('''\
    import sys, json
    def respond(msg_id, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                     "result": result}) + "\\n")
        sys.stdout.flush()
    def err(msg_id, message):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                     "error": {"code": -32000, "message": message}}) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            continue  # notification
        if method == "initialize":
            respond(msg_id, {"protocolVersion": "2024-11-05",
                             "serverInfo": {"name": "fake", "version": "1"}})
        elif method == "tools/list":
            respond(msg_id, {"tools": [
                {"name": "echo", "description": "echoes args"},
                {"name": "fail", "description": "always errors"}]})
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name == "fail":
                respond(msg_id, {"content": [{"type": "text", "text": "bad"}],
                                 "isError": True})
            else:
                respond(msg_id, {"content": [{"type": "text",
                    "text": "echo:" + json.dumps(msg["params"]["arguments"])}]})
        else:
            err(msg_id, "unknown method " + str(method))
''')


@pytest.fixture()
def fake_server_path(tmp_path):
    p = tmp_path / "fake_mcp_server.py"
    p.write_text(FAKE_SERVER)
    return str(p)


@pytest.fixture()
def configured_server(monkeypatch, fake_server_path):
    monkeypatch.setattr(mcp, "get_servers_config", lambda: {
        "fake": {"command": sys.executable, "args": [fake_server_path],
                 "framing": "ndjson"}})
    return "fake"


def test_initialize_handshake(configured_server):
    client = mcp.connect_server(configured_server)
    try:
        assert client.server_info.get("name") == "fake"
    finally:
        client.close()


def test_list_tools(configured_server):
    client = mcp.connect_server(configured_server)
    try:
        tools = client.list_tools()
    finally:
        client.close()
    names = [t["name"] for t in tools]
    assert names == ["echo", "fail"]


def test_call_tool(configured_server):
    client = mcp.connect_server(configured_server)
    try:
        res = client.call_tool("echo", {"x": 1})
    finally:
        client.close()
    assert res["content"][0]["text"] == 'echo:{"x": 1}'


def test_call_tool_error_flag(configured_server):
    client = mcp.connect_server(configured_server)
    try:
        res = client.call_tool("fail", {})
    finally:
        client.close()
    assert res.get("isError") is True


def test_unknown_server_raises(monkeypatch):
    monkeypatch.setattr(mcp, "get_servers_config", lambda: {})
    with pytest.raises(mcp.MCPError):
        mcp.connect_server("nope")


def test_missing_command_raises():
    with pytest.raises(mcp.MCPError):
        mcp.MCPClient("/nonexistent/binary-xyz")


def test_server_error_surfaces(tmp_path):
    # Server that answers initialize with a JSON-RPC error.
    p = tmp_path / "err_server.py"
    p.write_text(textwrap.dedent('''\
        import sys, json
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            msg = json.loads(line)
            if msg.get("id") is None: continue
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32000, "message": "init denied"}}) + "\\n")
            sys.stdout.flush()
    '''))
    client = mcp.MCPClient(sys.executable, [str(p)], framing="ndjson")
    try:
        with pytest.raises(mcp.MCPError, match="init denied"):
            client.initialize(timeout=5)
    finally:
        client.close()


def test_request_timeout(tmp_path):
    # Server that never responds.
    p = tmp_path / "silent_server.py"
    p.write_text("import time\ntime.sleep(60)\n")
    client = mcp.MCPClient(sys.executable, [str(p)])
    try:
        with pytest.raises(mcp.MCPError, match="timed out"):
            client._request("tools/list", timeout=1.0)
    finally:
        client.close()


def test_handle_mcp_no_servers_shows_setup(monkeypatch, capsys):
    monkeypatch.setattr(mcp, "get_servers_config", lambda: {})
    mcp.handle_mcp_command("servers")
    out = capsys.readouterr().out
    assert "mcp_servers" in out


def test_handle_mcp_call_bad_json(monkeypatch, capsys, configured_server):
    mcp.handle_mcp_command("call fake echo '{not json'")
    out = capsys.readouterr().out
    assert "Bad JSON" in out


def test_handle_mcp_call_requires_confirmation(monkeypatch, capsys,
                                               configured_server):
    # Without --yes and not allow-listed, a declined confirm cancels the call.
    # (Single-quoted JSON survives the CLI's shlex round-trip.)
    monkeypatch.setattr(mcp.Confirm, "ask", lambda *a, **k: False)
    mcp.handle_mcp_command("call fake echo '{\"x\": 1}'")
    out = capsys.readouterr().out
    assert "Cancelled" in out
    assert "echo:" not in out  # the tool never ran


def test_handle_mcp_call_auto_yes_skips_confirm(monkeypatch, capsys,
                                                configured_server):
    def _boom(*a, **k):
        raise AssertionError("confirm must not be asked with --yes")
    monkeypatch.setattr(mcp.Confirm, "ask", _boom)
    mcp.handle_mcp_command("call fake echo '{\"x\": 1}' --yes")
    out = capsys.readouterr().out
    assert "echo:" in out
