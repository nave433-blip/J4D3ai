"""MCP client against a real-world-style filesystem MCP server fixture.

tests/fixtures/mcp_fileserver.py speaks MCP over newline-delimited
JSON-RPC stdio with filesystem tools (list_dir/read_file/write_file/
file_info), input schemas, content blocks, isError results, and path
confinement — the shape of real MCP servers, not the minimal fake in
test_mcp.py.
"""
import sys
from pathlib import Path

import pytest

import core.mcp_client as mcp

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_fileserver.py")


@pytest.fixture()
def fs_server(tmp_path, monkeypatch):
    root = tmp_path / "fsroot"
    root.mkdir()
    (root / "hello.txt").write_text("hello world")
    (root / "sub").mkdir()
    monkeypatch.setattr(mcp, "get_servers_config", lambda: {
        "fs": {"command": sys.executable,
               "args": [FIXTURE, "--root", str(root)],
               "framing": "ndjson"}})
    client = mcp.connect_server("fs")
    yield client, root
    client.close()


def _text(result):
    assert isinstance(result, dict)
    assert not result.get("isError"), f"tool errored: {result}"
    return result["content"][0]["text"]


def test_list_tools_has_schemas(fs_server):
    client, _ = fs_server
    tools = {t["name"]: t for t in client.list_tools()}
    assert {"list_dir", "read_file", "write_file", "file_info"} <= set(tools)
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"


def test_server_info(fs_server):
    client, _ = fs_server
    assert client.server_info.get("name") == "mcp-fileserver-fixture"


def test_list_dir(fs_server):
    client, _ = fs_server
    import json
    entries = json.loads(_text(client.call_tool("list_dir", {"path": "."})))
    assert "hello.txt" in entries and "sub" in entries


def test_read_file(fs_server):
    client, _ = fs_server
    assert _text(client.call_tool("read_file", {"path": "hello.txt"})) == "hello world"


def test_write_then_read(fs_server):
    client, root = fs_server
    _text(client.call_tool("write_file", {"path": "sub/new.txt", "content": "abc123"}))
    assert (root / "sub" / "new.txt").read_text() == "abc123"
    assert _text(client.call_tool("read_file", {"path": "sub/new.txt"})) == "abc123"


def test_file_info(fs_server):
    client, _ = fs_server
    import json
    info = json.loads(_text(client.call_tool("file_info", {"path": "hello.txt"})))
    assert info["size"] == 11 and "mtime" in info


def test_path_traversal_refused(fs_server):
    client, _ = fs_server
    result = client.call_tool("read_file", {"path": "../../etc/passwd"})
    assert result.get("isError")
    assert "escapes root" in result["content"][0]["text"]


def test_missing_file_is_error_not_crash(fs_server):
    client, _ = fs_server
    result = client.call_tool("read_file", {"path": "nope.txt"})
    assert result.get("isError")


def test_unknown_tool_is_error(fs_server):
    client, _ = fs_server
    result = client.call_tool("rm_rf", {})
    assert result.get("isError")
