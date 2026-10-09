"""Minimal MCP (Model Context Protocol) client — Jade's spin.

Connects to MCP servers over stdio using newline-delimited JSON-RPC 2.0
(per the MCP stdio transport), performs the initialize handshake, lists
tools, and calls them.

- stdlib only: no new dependencies.
- Servers are configured in ``~/.jade/config.json`` under "mcp_servers":
    {"mcp_servers": {"my-server": {"command": "npx",
                                   "args": ["-y", "some-mcp-server"],
                                   "env": {"FOO": "bar"}}}}
- Tool calls from untrusted servers: the call is shown and confirmation is
  required before executing, unless ``--yes`` is passed or the tool is in
  the per-server "allow" list in config.
- Graceful when nothing is configured: /mcp prints setup docs, never errors.
"""
import itertools
import json
import os
import queue
import subprocess
import threading
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from core.approvals import confirm
from rich.table import Table

from core.config import load_config, save_config
from core.update import CURRENT_VERSION

console = Console()

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    pass


def get_servers_config() -> Dict:
    try:
        cfg = load_config()
        servers = cfg.get("mcp_servers", {})
        return servers if isinstance(servers, dict) else {}
    except Exception:
        return {}


class MCPClient:
    """One stdio connection to an MCP server."""

    def __init__(self, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None):
        self.command = command
        self.args = args or []
        merged_env = dict(os.environ)
        for k, v in (env or {}).items():
            merged_env[str(k)] = str(v)
        try:
            self.proc = subprocess.Popen(
                [command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1, env=merged_env)
        except FileNotFoundError:
            raise MCPError(f"command not found: {command}")
        except Exception as e:
            raise MCPError(f"could not start server: {e}")
        self._incoming: "queue.Queue[Dict]" = queue.Queue()
        self._ids = itertools.count(1)
        self._pending: Dict[int, "queue.Queue[Dict]"] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.server_info: Dict = {}

    # -- transport ------------------------------------------------------
    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is not None:
                    with self._lock:
                        q = self._pending.get(msg_id)
                    if q is not None:
                        q.put(msg)
                        continue
                self._incoming.put(msg)
        except Exception:
            pass

    def _send(self, payload: Dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            raise MCPError(f"write to server failed: {e}")

    def _request(self, method: str, params: Optional[Dict] = None,
                 timeout: float = 30.0) -> Dict:
        if self._closed:
            raise MCPError("client is closed")
        msg_id = next(self._ids)
        q: "queue.Queue[Dict]" = queue.Queue()
        with self._lock:
            self._pending[msg_id] = q
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method,
                    "params": params or {}})
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"request '{method}' timed out after {timeout}s")
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)
        if resp.get("error"):
            err = resp["error"]
            raise MCPError(f"server error: {err.get('message', err)}")
        return resp.get("result", {})

    def _notify(self, method: str, params: Optional[Dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method,
                    "params": params or {}})

    # -- MCP protocol ---------------------------------------------------
    def initialize(self, timeout: float = 30.0) -> Dict:
        result = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "jade", "version": CURRENT_VERSION},
        }, timeout=timeout)
        self._notify("notifications/initialized")
        self.server_info = result.get("serverInfo", {})
        return result

    def list_tools(self, timeout: float = 30.0) -> List[Dict]:
        result = self._request("tools/list", {}, timeout=timeout)
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Optional[Dict] = None,
                  timeout: float = 60.0) -> Dict:
        return self._request("tools/call",
                             {"name": name, "arguments": arguments or {}},
                             timeout=timeout)

    def close(self):
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


def connect_server(name: str, timeout: float = 15.0) -> MCPClient:
    """Build, initialize, and return a client for a configured server."""
    servers = get_servers_config()
    spec = servers.get(name)
    if spec is None:
        raise MCPError(f"no MCP server named '{name}' is configured")
    if not isinstance(spec, dict) or not spec.get("command"):
        raise MCPError(f"server '{name}' has no command configured")
    client = MCPClient(spec["command"], spec.get("args", []), spec.get("env", {}))
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
        '  [green]"my-server"[/green]: {\n'
        '    [green]"command"[/green]: [yellow]"npx"[/yellow],\n'
        '    [green]"args"[/green]: [[yellow]"-y"[/yellow], [yellow]"@modelcontextprotocol/server-filesystem"[/yellow], [yellow]"/tmp"[/yellow]],\n'
        '    [green]"env"[/green]: {},\n'
        '    [green]"allow"[/green]: [[yellow]"read_file"[/yellow]]  [dim](tools that skip the confirm prompt)[/dim]\n'
        "  }\n"
        "}\n\n"
        "Then: [cyan]/mcp servers[/cyan] · [cyan]/mcp tools my-server[/cyan] ·\n"
        "[cyan]/mcp call my-server read_file '{\"path\": \"/tmp/x\"}'[/cyan]\n\n"
        "[dim]Any stdio MCP server works: filesystem, sqlite, fetch, …[/dim]",
        title="[bold magenta]🔌 MCP setup[/bold magenta]", border_style="magenta")


def cmd_servers() -> None:
    servers = get_servers_config()
    if not servers:
        console.print(setup_docs())
        return
    table = Table(title="🔌 MCP servers", show_header=True, header_style="bold dim")
    table.add_column("Name"); table.add_column("Command"); table.add_column("Status")
    for name, spec in servers.items():
        cmdline = str(spec.get("command", "?"))
        args = spec.get("args") or []
        if args:
            cmdline += " " + " ".join(str(a) for a in args[:3])
            if len(args) > 3:
                cmdline += " …"
        try:
            client = connect_server(name, timeout=8.0)
            info = client.server_info or {}
            status = f"[green]✓ {info.get('name', 'ok')}[/green]"
            client.close()
        except MCPError as e:
            status = f"[red]✗ {str(e)[:60]}[/red]"
        except Exception as e:
            status = f"[red]✗ {type(e).__name__}[/red]"
        table.add_row(name, cmdline[:60], status)
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
            client.close()
        except MCPError as e:
            console.print(f"[red]{name}: {e}[/red]")
            continue
        if not tools:
            console.print(f"[yellow]{name}: no tools exposed.[/yellow]")
            continue
        table = Table(title=f"🔧 {name} tools", show_header=True,
                      header_style="bold dim")
        table.add_column("Tool"); table.add_column("Description")
        for t in tools:
            table.add_row(t.get("name", "?"),
                          (t.get("description", "") or "")[:80])
        console.print(table)


def cmd_call(server: str, tool: str, args_json: str = "",
             auto_yes: bool = False) -> None:
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

    # Untrusted-server guard: show the exact call, require confirmation
    # unless the user passed --yes, allow-listed the tool, or enabled the
    # global auto-approve toggle (honored inside confirm()).
    allowed = (servers[server].get("allow") or []) if isinstance(servers.get(server), dict) else []
    console.print(Panel(
        f"[bold]server:[/bold] {server}\n"
        f"[bold]tool:[/bold] {tool}\n"
        f"[bold]arguments:[/bold]\n{json.dumps(arguments, indent=2)}",
        title="[yellow]🔌 MCP tool call[/yellow]", border_style="yellow"))
    if not auto_yes and tool not in allowed:
        console.print("[dim]The server is untrusted code — confirm before it runs.[/dim]")
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
        console.print(f"[red]Tool reported an error:[/red]")
    for item in result.get("content", []):
        if item.get("type") == "text":
            console.print(Panel(item.get("text", ""), title=f"[dim]{tool} output[/dim]"))
        else:
            console.print(f"[dim]({item.get('type', '?')} content omitted)[/dim]")
    if not result.get("content"):
        console.print(json.dumps(result, indent=2)[:2000])


def handle_mcp_command(args: str) -> None:
    """Dispatch '/mcp ...' subcommands."""
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
            console.print("[red]Usage: /mcp call <server> <tool> ['{json args}'] [--yes][/red]")
            return
        cmd_call(rest[0], rest[1], " ".join(rest[2:]), auto_yes=auto_yes)
        return
    if parts[0] == "setup":
        console.print(setup_docs())
        return
    console.print("[red]Usage: /mcp [servers|tools [server]|call <server> <tool> [args] [--yes]|setup][/red]")
