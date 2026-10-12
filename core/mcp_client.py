"""MCP (Model Context Protocol) client — Jade's spin.

Stdio transport per MCP spec:
  - Prefer Content-Length framed messages (official stdio transport)
  - Also accept newline-delimited JSON-RPC (common in simpler servers)

- stdlib only: no new dependencies.
- Servers are configured in ``~/.jade/config.json`` under "mcp_servers":
    {"mcp_servers": {"my-server": {"command": "npx",
                                   "args": ["-y", "some-mcp-server"],
                                   "env": {"FOO": "bar"},
                                   "allow": ["read_file"],
                                   "framing": "content-length"}}}
  ``framing`` is "content-length" (official MCP stdio transport, default) or
  "ndjson" (newline-delimited JSON-RPC, for simpler servers). The read side
  accepts both framings regardless.
- Tool calls from untrusted servers: the call is shown and confirmation is
  required before executing, unless ``--yes`` is passed or the tool is in
  the per-server "allow" list in config.
- Graceful when nothing is configured: /mcp prints setup docs, never errors.

Slash: ``/mcp servers|tools|call|setup``
CLI:   ``jade mcp servers|tools|call|setup``
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
from typing import Any, Dict, List, Optional, Union

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from core.approvals import confirm
from core.config import load_config

try:
    from core.update import CURRENT_VERSION
except Exception:  # pragma: no cover
    CURRENT_VERSION = "0.2.6"

console = Console()

# MCP protocol versions we advertise (newest first)
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26")


class MCPError(Exception):
    """Protocol or transport failure talking to an MCP server."""


def get_servers_config() -> Dict[str, Any]:
    try:
        cfg = load_config()
        servers = cfg.get("mcp_servers", {})
        return servers if isinstance(servers, dict) else {}
    except Exception:
        return {}


class MCPClient:
    """One stdio connection to an MCP server (Content-Length + NDJSON)."""

    # Defensive cap: if this many bytes accumulate without forming a
    # complete message, the peer is spewing garbage — drop it rather than
    # growing memory without bound.
    _MAX_BUFFER = 16 << 20
    _READ_CHUNK = 8192

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        framing: str = "content-length",
    ):
        self.command = command
        self.args = args or []
        framing = (framing or "content-length").lower()
        if framing not in ("content-length", "ndjson"):
            raise MCPError(f"unknown MCP framing {framing!r} (want 'content-length' or 'ndjson')")
        self.framing = framing
        merged_env = dict(os.environ)
        for k, v in (env or {}).items():
            merged_env[str(k)] = str(v)
        try:
            self.proc = subprocess.Popen(
                [command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # binary-friendly for Content-Length
                env=merged_env,
            )
        except FileNotFoundError:
            raise MCPError(f"command not found: {command}")
        except Exception as e:
            raise MCPError(f"could not start server: {e}")

        self._incoming: "queue.Queue[Dict]" = queue.Queue()
        self._ids = itertools.count(1)
        self._pending: Dict[Union[int, str], "queue.Queue[Dict]"] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.negotiated_version: str = MCP_PROTOCOL_VERSION

    # ------------------------------------------------------------------ transport
    def _read_loop(self) -> None:
        """Parse either Content-Length frames or newline-delimited JSON.

        Reads in 8 KiB chunks (not byte-at-a-time: a syscall per byte made
        large tool results painfully slow) and guards against unbounded
        buffer growth from a misbehaving server.
        """
        stdout = self.proc.stdout
        if stdout is None:
            return
        buffer = b""
        try:
            while not self._closed:
                chunk = stdout.read(self._READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    msg, buffer = self._try_extract_message(buffer)
                    if msg is None:
                        break
                    self._dispatch_message(msg)
                if len(buffer) > self._MAX_BUFFER:
                    buffer = b""
        except Exception:
            pass

    def _try_extract_message(self, buffer: bytes):
        """Return (message_dict|None, remaining_buffer)."""
        # Content-Length: N\r\n\r\n<body> — only lowercase the header prefix,
        # not the whole buffer (this runs once per message).
        if buffer[:15].lower().startswith(b"content-length:"):
            header_end = buffer.find(b"\r\n\r\n")
            if header_end < 0:
                header_end = buffer.find(b"\n\n")
                if header_end < 0:
                    return None, buffer
                sep_len = 2
            else:
                sep_len = 4
            header = buffer[:header_end].decode("utf-8", errors="replace")
            length = None
            for line in header.splitlines():
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = None
            if length is None:
                return None, buffer[header_end + sep_len :]
            body_start = header_end + sep_len
            if len(buffer) < body_start + length:
                return None, buffer
            body = buffer[body_start : body_start + length]
            rest = buffer[body_start + length :]
            try:
                msg = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return None, rest
            if isinstance(msg, dict):
                return msg, rest
            return None, rest

        # Newline-delimited JSON fallback
        nl = buffer.find(b"\n")
        if nl < 0:
            return None, buffer
        line = buffer[:nl].strip()
        rest = buffer[nl + 1 :]
        if not line:
            return None, rest
        # Ignore log noise that isn't JSON
        if not line.startswith(b"{") and not line.startswith(b"["):
            return None, rest
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            return None, rest
        if isinstance(msg, dict):
            return msg, rest
        return None, rest

    def _dispatch_message(self, msg: Dict) -> None:
        msg_id = msg.get("id")
        if msg_id is not None:
            with self._lock:
                q = self._pending.get(msg_id)
            if q is not None:
                q.put(msg)
                return
        self._incoming.put(msg)

    def _send(self, payload: Dict) -> None:
        if self.proc.stdin is None:
            raise MCPError("server stdin closed")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if self.framing == "ndjson":
            # Newline-delimited JSON-RPC (simpler servers, e.g. test fixture)
            frame = body + b"\n"
        else:
            # Official MCP stdio framing
            frame = (
                f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
        except Exception as e:
            raise MCPError(f"write to server failed: {e}")

    def _request(
        self,
        method: str,
        params: Optional[Dict] = None,
        timeout: float = 30.0,
    ) -> Dict:
        if self._closed:
            raise MCPError("client is closed")
        msg_id = next(self._ids)
        q: "queue.Queue[Dict]" = queue.Queue()
        with self._lock:
            self._pending[msg_id] = q
        self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
        )
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"request '{method}' timed out after {timeout}s")
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)
        if resp.get("error"):
            err = resp["error"]
            if isinstance(err, dict):
                raise MCPError(f"server error: {err.get('message', err)}")
            raise MCPError(f"server error: {err}")
        result = resp.get("result", {})
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params: Optional[Dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ------------------------------------------------------------------ protocol
    def initialize(self, timeout: float = 30.0) -> Dict:
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                    "roots": {"listChanged": False},
                },
                "clientInfo": {
                    "name": "jade",
                    "version": str(CURRENT_VERSION),
                },
            },
            timeout=timeout,
        )
        self.negotiated_version = (
            result.get("protocolVersion") or MCP_PROTOCOL_VERSION
        )
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        # Required notification after initialize
        self._notify("notifications/initialized")
        return result

    def list_tools(self, timeout: float = 30.0) -> List[Dict]:
        result = self._request("tools/list", {}, timeout=timeout)
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def list_resources(self, timeout: float = 30.0) -> List[Dict]:
        if not self.capabilities.get("resources"):
            return []
        try:
            result = self._request("resources/list", {}, timeout=timeout)
        except MCPError:
            return []
        resources = result.get("resources", [])
        return resources if isinstance(resources, list) else []

    def list_prompts(self, timeout: float = 30.0) -> List[Dict]:
        if not self.capabilities.get("prompts"):
            return []
        try:
            result = self._request("prompts/list", {}, timeout=timeout)
        except MCPError:
            return []
        prompts = result.get("prompts", [])
        return prompts if isinstance(prompts, list) else []

    def call_tool(
        self,
        name: str,
        arguments: Optional[Dict] = None,
        timeout: float = 60.0,
    ) -> Dict:
        return self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )

    def close(self) -> None:
        self._closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def __enter__(self) -> "MCPClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()


def connect_server(name: str, timeout: float = 15.0) -> MCPClient:
    """Build, initialize, and return a client for a configured server."""
    servers = get_servers_config()
    spec = servers.get(name)
    if spec is None:
        raise MCPError(f"no MCP server named '{name}' is configured")
    if not isinstance(spec, dict) or not spec.get("command"):
        raise MCPError(f"server '{name}' has no command configured")
    client = MCPClient(
        str(spec["command"]),
        list(spec.get("args") or []),
        dict(spec.get("env") or {}),
        framing=str(spec.get("framing") or "content-length"),
    )
    try:
        client.initialize(timeout=timeout)
    except Exception:
        client.close()
        raise
    return client


def setup_docs() -> Panel:
    return Panel(
        "[bold]No MCP servers configured.[/bold]\n\n"
        "MCP (Model Context Protocol) lets Jade use external tools through\n"
        "standardized servers. Add one to [cyan]~/.jade/config.json[/cyan]:\n\n"
        '[green]"mcp_servers"[/green]: {\n'
        '  [green]"fs"[/green]: {\n'
        '    [green]"command"[/green]: [yellow]"npx"[/yellow],\n'
        '    [green]"args"[/green]: [[yellow]"-y"[/yellow], '
        '[yellow]"@modelcontextprotocol/server-filesystem"[/yellow], '
        '[yellow]"/tmp"[/yellow]],\n'
        '    [green]"env"[/green]: {},\n'
        '    [green]"allow"[/green]: [[yellow]"read_file"[/yellow]]  '
        "[dim](tools that skip the confirm prompt)[/dim]\n"
        "  }\n"
        "}\n\n"
        "Then:\n"
        "  [cyan]cortana mcp servers[/cyan]  ·  [cyan]/mcp servers[/cyan]\n"
        "  [cyan]cortana mcp tools fs[/cyan] ·  [cyan]/mcp tools fs[/cyan]\n"
        "  [cyan]cortana mcp call fs read_file '{\"path\":\"/tmp/x\"}'[/cyan]",
        title="[cyan]🔌 MCP setup[/cyan]",
        border_style="cyan",
    )


def cmd_servers() -> None:
    servers = get_servers_config()
    if not servers:
        console.print(setup_docs())
        return
    table = Table(title="Configured MCP servers", show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Command")
    table.add_column("Allow")
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            table.add_row(name, "?", "")
            continue
        cmd = " ".join(
            [str(spec.get("command", ""))]
            + [str(a) for a in (spec.get("args") or [])]
        )
        allow = ", ".join(spec.get("allow") or [])
        table.add_row(name, cmd[:80], allow[:40])
    console.print(table)


def cmd_tools(server: Optional[str] = None) -> None:
    servers = get_servers_config()
    if not servers:
        console.print(setup_docs())
        return
    names = [server] if server else list(servers.keys())
    for name in names:
        if name not in servers:
            console.print(f"[red]No MCP server named '{name}'.[/red]")
            continue
        try:
            client = connect_server(name)
            tools = client.list_tools()
            info = client.server_info
            client.close()
        except MCPError as e:
            console.print(f"[red]{name}: {e}[/red]")
            continue
        title = f"🔧 {name} tools"
        if info:
            title += f"  ({info.get('name', '')} {info.get('version', '')})".rstrip()
        if not tools:
            console.print(f"[yellow]{name}: no tools exposed.[/yellow]")
            continue
        table = Table(title=title, show_header=True, header_style="bold dim")
        table.add_column("Tool")
        table.add_column("Description")
        for t in tools:
            table.add_row(
                t.get("name", "?"),
                (t.get("description", "") or "")[:80],
            )
        console.print(table)


def cmd_call(
    server: str,
    tool: str,
    args_json: str = "",
    auto_yes: bool = False,
) -> None:
    servers = get_servers_config()
    if not servers:
        console.print(setup_docs())
        return
    if server not in servers:
        console.print(f"[red]No MCP server named '{server}'.[/red]")
        return
    try:
        arguments = json.loads(args_json) if args_json.strip() else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
    except Exception as e:
        console.print(f"[red]Bad JSON arguments: {e}[/red]")
        return

    allowed = (
        (servers[server].get("allow") or [])
        if isinstance(servers.get(server), dict)
        else []
    )
    console.print(
        Panel(
            f"[bold]server:[/bold] {server}\n"
            f"[bold]tool:[/bold] {tool}\n"
            f"[bold]arguments:[/bold]\n{json.dumps(arguments, indent=2)}",
            title="[yellow]🔌 MCP tool call[/yellow]",
            border_style="yellow",
        )
    )
    if not auto_yes and tool not in allowed:
        console.print(
            "[dim]The server is untrusted code — confirm before it runs.[/dim]"
        )
        if not confirm("Execute this tool call?"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    try:
        client = connect_server(server)
        try:
            result = client.call_tool(tool, arguments)
        finally:
            client.close()
    except MCPError as e:
        console.print(f"[red]MCP error: {e}[/red]")
        return

    if result.get("isError"):
        console.print("[red]Tool reported an error:[/red]")
    for item in result.get("content", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            console.print(
                Panel(item.get("text", ""), title=f"[dim]{tool} output[/dim]")
            )
        else:
            console.print(
                f"[dim]({item.get('type', '?')} content omitted)[/dim]"
            )
    if not result.get("content"):
        console.print(json.dumps(result, indent=2)[:2000])


def handle_mcp_command(args: str) -> None:
    """Dispatch ``/mcp ...`` subcommands from the interactive REPL."""
    import shlex

    try:
        parts = shlex.split(args or "")
    except ValueError:
        parts = (args or "").split()
    if not parts or parts[0] in ("servers", "list"):
        cmd_servers()
        return
    if parts[0] == "tools":
        cmd_tools(parts[1] if len(parts) > 1 else None)
        return
    if parts[0] == "call":
        auto_yes = "--yes" in parts
        rest = [p for p in parts[1:] if p != "--yes"]
        if len(rest) < 2:
            console.print(
                "[red]Usage: /mcp call <server> <tool> ['{json args}'] [--yes][/red]"
            )
            return
        cmd_call(rest[0], rest[1], " ".join(rest[2:]), auto_yes=auto_yes)
        return
    if parts[0] == "setup":
        console.print(setup_docs())
        return
    console.print(
        "[red]Usage: /mcp [servers|tools [server]|call <server> <tool> [args] [--yes]|setup][/red]"
    )
