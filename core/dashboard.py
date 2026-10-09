"""Local web dashboard for Jade — stdlib only.

A small loopback-bound web UI served with :mod:`http.server`:

  - live status: providers, P2P peers, Ollama models, scheduler jobs
  - chat box that talks to the brain
  - command runner (strict read-only whitelist — no mutations, no shell)
  - log tail

Security:
  - Binds 127.0.0.1 by default. ``bind_lan=True`` binds 0.0.0.0 but prints
    a loud warning and requires explicit opt-in.
  - Every request needs the bearer token (``?token=`` or
    ``X-Jade-Token`` header), compared with :func:`hmac.compare_digest`.
    The token is random per creation, stored at ``~/.jade/dashboard_token``
    (0600), and printed once at startup. It is never logged.
  - No credentials are ever rendered: provider/model NAMES only.
  - Every panel reads live state; on failure it shows "unavailable",
    never fabricated data.
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import socketserver
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import CONFIG_DIR

TOKEN_FILE = CONFIG_DIR / "dashboard_token"
LOG_DIR = CONFIG_DIR / "logs"

# Extra pages registered by optional UI layers (desktop overlay, etc.).
# Maps path -> (body_bytes, content_type). Served with the same token auth.
_EXTRA_PAGES: Dict[str, tuple] = {}


def register_page(path: str, body, content_type: str = "text/html; charset=utf-8") -> None:
    """Register an extra page on the dashboard (desktop shell integration)."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    _EXTRA_PAGES[path] = (bytes(body), content_type)


# Read-only commands exposed to the dashboard command runner.
# Each entry: (label, callable returning a short string).
_READONLY: List[tuple] = []


def _get_token() -> str:
    if TOKEN_FILE.exists():
        try:
            return TOKEN_FILE.read_text().strip()
        except OSError:
            pass
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)
    return token


def _authed(handler: http.server.BaseHTTPRequestHandler, token: str) -> bool:
    parsed = urllib.parse.urlparse(handler.path)
    qs = urllib.parse.parse_qs(parsed.query)
    supplied = handler.headers.get("X-Jade-Token") or (qs.get("token", [""])[0])
    return bool(supplied) and hmac.compare_digest(supplied, token)


# ---------------------------------------------------------------------------
# Live state collectors (each defensive — failure becomes "unavailable")
# ---------------------------------------------------------------------------

def _providers() -> Dict[str, Any]:
    try:
        from core.connect import AuthManager, is_configured
        configured = [p for p in AuthManager.PROVIDERS.keys() if is_configured(p)]
        return {"configured": sorted(configured),
                "total_known": len(AuthManager.PROVIDERS)}
    except Exception as e:
        return {"error": f"unavailable: {e}"}


def _p2p() -> Dict[str, Any]:
    try:
        from core.config import CONFIG_DIR, load_config
        from core import p2p as p2p_mod
        cfg = load_config()
        enabled = bool(cfg.get("p2p_enabled", True))
        peers: List[Dict[str, Any]] = []
        if enabled:
            try:
                eps = p2p_mod.scan_for_jade_peer_endpoints(timeout=1.0)
                peers = [{"ip": ip, "port": port} for ip, port in eps]
            except Exception:
                peers = []
        return {"enabled": enabled, "peers": peers,
                "tls": bool(cfg.get("p2p_tls", False))}
    except Exception as e:
        return {"error": f"unavailable: {e}"}


def _ollama() -> Dict[str, Any]:
    try:
        from core.ollama_mgmt import OllamaManager
        mgr = OllamaManager()
        models: List[str] = []
        try:
            for m in mgr.library():
                name = m.get("name") or m.get("model")
                if name:
                    models.append(name)
        except Exception:
            models = []
        hosts = list(getattr(mgr, "hosts", []) or [])
        return {"models": sorted(set(models)), "hosts": hosts}
    except Exception as e:
        return {"error": f"unavailable: {e}"}


def _scheduler_state() -> Dict[str, Any]:
    try:
        from core.scheduler import get_scheduler
        sched = get_scheduler()
        jobs = sched.list_jobs()
        return {"running": sched.running, "jobs": len(jobs),
                "enabled": sum(1 for j in jobs if j.get("enabled")),
                "recent": sched.recent_runs(5)}
    except Exception as e:
        return {"error": f"unavailable: {e}"}


def _briefs_state() -> Dict[str, Any]:
    try:
        from core import briefs
        return {"enabled": briefs.is_enabled(),
                "pending": len(briefs.pending_events())}
    except Exception as e:
        return {"error": f"unavailable: {e}"}


def collect_status() -> Dict[str, Any]:
    from core.update import CURRENT_VERSION
    return {
        "version": CURRENT_VERSION,
        "providers": _providers(),
        "p2p": _p2p(),
        "ollama": _ollama(),
        "scheduler": _scheduler_state(),
        "briefs": _briefs_state(),
    }


def tail_logs(n: int = 100) -> List[str]:
    try:
        files = sorted(LOG_DIR.glob("error_*.log"))
        if not files:
            return ["no log files yet"]
        lines = files[-1].read_text(errors="replace").splitlines()
        return lines[-n:]
    except Exception as e:
        return [f"unavailable: {e}"]


# ---------------------------------------------------------------------------
# Personality & voice profile pickers (shared with the desktop GUI)
# ---------------------------------------------------------------------------

def get_personality_state() -> Dict[str, Any]:
    from core.personalities import list_personalities, resolve_personality
    from core.config import load_config
    current = resolve_personality(load_config().get("personality")).name
    return {"current": current,
            "options": [{"name": p.name, "title": p.title,
                         "description": p.description} for p in list_personalities()]}


def set_personality_state(name: str) -> Dict[str, Any]:
    from core.personalities import is_known_personality, resolve_personality
    from core.config import load_config, save_config
    if not is_known_personality(name):
        return {"ok": False, "error": f"unknown personality: {name}"}
    cfg = load_config()
    cfg["personality"] = resolve_personality(name).name
    save_config(cfg)
    return {"ok": True, "current": cfg["personality"]}


def get_voice_profile_state() -> Dict[str, Any]:
    from core.voice.profiles import list_profiles
    from core.config import load_config
    current = load_config().get("voice_profile", "default")
    return {"current": current,
            "options": [{"name": p.name, "description": p.description,
                         "engine": p.engine, "note": p.note}
                        for p in list_profiles()]}


def set_voice_profile_state(name: str) -> Dict[str, Any]:
    from core.voice.profiles import get_profile
    from core.config import load_config, save_config
    profile = get_profile(name)
    if profile is None:
        return {"ok": False, "error": f"unknown voice profile: {name}"}
    cfg = load_config()
    cfg["voice_profile"] = profile.name
    save_config(cfg)
    return {"ok": True, "current": profile.name}


# ---------------------------------------------------------------------------
# Chat + read-only commands
# ---------------------------------------------------------------------------

def chat_reply(message: str) -> str:
    message = message.strip()
    if not message:
        return "Say something first."
    try:
        from core.brain import think
        return str(think("", message))
    except Exception as e:
        return f"chat unavailable: {type(e).__name__}: {e}"


def _register_readonly() -> None:
    if _READONLY:
        return

    def _health() -> str:
        from core.health import check_system_health
        r = check_system_health()
        return json.dumps(r, indent=1)[:2000]

    def _models() -> str:
        from core.config import CONFIG_DIR, load_config
        cfg = load_config()
        return "model: " + str(cfg.get("jade_model", "unknown"))

    def _sched_list() -> str:
        from core.scheduler import get_scheduler
        jobs = get_scheduler().list_jobs()
        if not jobs:
            return "no scheduled jobs"
        return "\n".join(f"{j['id']} {j['name']} [{j['action']}] next={j.get('next_run')}"
                         for j in jobs)

    def _sched_log() -> str:
        from core.scheduler import get_scheduler
        runs = get_scheduler().recent_runs(10)
        if not runs:
            return "no job runs logged"
        return "\n".join(f"{r['ts']} {r['name']} {r['status']}: {r['summary']}" for r in runs)

    def _ollama_ps() -> str:
        from core.ollama_mgmt import OllamaManager
        ps = OllamaManager().ps_all()
        if not ps:
            return "no models loaded"
        return "\n".join(str(p) for p in ps)[:2000]

    def _ollama_stats() -> str:
        from core.ollama_mgmt import OllamaManager
        return json.dumps(OllamaManager().stats(), indent=1)[:2000]

    def _brief_status() -> str:
        from core import briefs
        return f"enabled={briefs.is_enabled()} pending={len(briefs.pending_events())}"

    def _p2p_status() -> str:
        return json.dumps(_p2p(), indent=1)[:2000]

    _READONLY.extend([
        ("/health", _health),
        ("/models", _models),
        ("/schedule list", _sched_list),
        ("/schedule log", _sched_log),
        ("/ollama ps", _ollama_ps),
        ("/ollama stats", _ollama_stats),
        ("/brief status", _brief_status),
        ("/p2p-status", _p2p_status),
    ])


def run_readonly(command: str) -> str:
    _register_readonly()
    for label, fn in _READONLY:
        if command.strip() == label:
            try:
                return str(fn())
            except Exception as e:
                return f"{label} failed: {type(e).__name__}: {e}"
    allowed = ", ".join(label for label, _ in _READONLY)
    return f"not allowed. Read-only commands: {allowed}"


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Jade dashboard</title>
<style>
body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;max-width:980px;margin:0 auto;padding:16px}
h1{color:#58a6ff;font-size:22px}h2{color:#58a6ff;font-size:16px;margin-top:24px}
.panel{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin:8px 0}
pre{white-space:pre-wrap;word-break:break-word;font-size:13px}
input,button,select{font-size:14px;padding:6px 10px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9}
button{background:#238636;border-color:#238636;cursor:pointer}button:hover{background:#2ea043}
#chatlog{height:220px;overflow-y:auto;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px}
#chatlog div{margin:4px 0}.u{color:#79c0ff}.a{color:#c9d1d9}
.row{display:flex;gap:8px;margin:6px 0}input[type=text]{flex:1}
.warn{color:#f85149}
</style></head><body>
<h1>🤖 Jade dashboard <span id="ver" style="font-size:13px;color:#8b949e"></span></h1>
<div style="color:#8b949e;font-size:13px;margin:-8px 0 12px 2px"><i>Wake me when you need me.</i></div>
<h2>Status</h2><div class="panel"><pre id="status">loading…</pre><button onclick="refresh()">refresh</button></div>
<h2>Chat</h2><div class="panel"><div id="chatlog"></div>
<div class="row"><input type="text" id="msg" placeholder="Ask Jade…" onkeydown="if(event.key==='Enter')sendChat()">
<button onclick="sendChat()">send</button></div></div>
<h2>Read-only commands</h2><div class="panel"><div class="row">
<select id="cmd"></select><button onclick="runCmd()">run</button></div><pre id="cmdout"></pre></div>
<h2>Settings</h2><div class="panel"><div class="row">
<select id="setcat"></select><button onclick="loadSettings()">load</button></div>
<div id="settings"></div><div class="row"><input type="text" id="setkey" placeholder="setting key" style="max-width:220px">
<input type="text" id="setval" placeholder="new value"><button onclick="saveSetting()">save</button></div>
<div id="setmsg" style="font-size:13px"></div>
<p style="font-size:12px;color:#8b949e">Secrets (API keys, tokens, client ids) are never shown here and cannot be changed from the dashboard — use the CLI.</p></div>
<h2>Personality &amp; voice</h2><div class="panel">
<div class="row"><label style="width:110px">Personality</label><select id="pers"></select><button onclick="setPers()">set</button></div>
<div class="row"><label style="width:110px">Voice</label><select id="vprof"></select><button onclick="setVprof()">set</button></div>
<div id="pvmsg" style="font-size:13px;color:#8b949e"></div></div>
<h2>Logs</h2><div class="panel"><pre id="logs">loading…</pre><button onclick="loadLogs()">refresh</button></div>
<script>
const T = new URLSearchParams(location.search).get("token") || "";
async function api(path, opts){ const r = await fetch(path + (path.includes("?")?"&":"?") + "token=" + encodeURIComponent(T), opts); return r.json(); }
async function refresh(){ const s = await api("/api/status"); document.getElementById("ver").textContent = "v" + (s.version||"?"); document.getElementById("status").textContent = JSON.stringify(s, null, 1); }
async function sendChat(){ const el = document.getElementById("msg"); const m = el.value.trim(); if(!m) return; el.value=""; const log=document.getElementById("chatlog"); log.innerHTML += "<div><span class='u'>you:</span> "+m.replace(/</g,"&lt;")+"</div>"; const r = await api("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:m})}); log.innerHTML += "<div><span class='a'>jade:</span> "+String(r.reply).replace(/</g,"&lt;")+"</div>"; log.scrollTop=log.scrollHeight; }
async function runCmd(){ const c=document.getElementById("cmd").value; const r = await api("/api/cmd",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({command:c})}); document.getElementById("cmdout").textContent = r.output; }
async function loadLogs(){ const r = await api("/api/logs"); document.getElementById("logs").textContent = r.lines.join("\\n"); }
async function loadSettings(){
  const r = await api("/api/settings"); if(r.error){ document.getElementById("settings").textContent = r.error; return; }
  const cat = document.getElementById("setcat").value;
  const cats = [...new Set(r.schema.map(s=>s.category))];
  const sel = document.getElementById("setcat");
  if(!sel.options.length){ cats.forEach(c=>{const o=document.createElement("option");o.value=c;o.textContent=c;sel.appendChild(o);}); const a=document.createElement("option");a.value="";a.textContent="all";sel.appendChild(a); }
  let html = "<table style='font-size:13px;border-collapse:collapse'>";
  r.schema.filter(s=>!cat||s.category===cat).forEach(s=>{
    let v = r.values[s.key]; if(v===true)v="on"; if(v===false)v="off";
    html += "<tr><td style='padding:3px 8px;color:#79c0ff'>"+s.key+"</td><td style='padding:3px 8px'>"+String(v).replace(/</g,"&lt;")+"</td><td style='padding:3px 8px;color:#8b949e'>"+s.description.replace(/</g,"&lt;")+"</td></tr>";
  });
  document.getElementById("settings").innerHTML = html + "</table>";
}
async function saveSetting(){
  const key = document.getElementById("setkey").value.trim(), value = document.getElementById("setval").value;
  if(!key) return;
  const r = await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,value})});
  document.getElementById("setmsg").textContent = r.ok ? (r.message||"saved") : ("error: "+(r.error||"unknown"));
  loadSettings();
}
async function loadPickers(){
  const p = await api("/api/personality"); const ps = document.getElementById("pers"); ps.innerHTML = "";
  p.options.forEach(o => { const el = document.createElement("option"); el.value = o.name;
    el.textContent = o.title + " — " + o.description; if(o.name === p.current) el.selected = true; ps.appendChild(el); });
  const v = await api("/api/voice-profile"); const vs = document.getElementById("vprof"); vs.innerHTML = "";
  v.options.forEach(o => { const el = document.createElement("option"); el.value = o.name;
    el.textContent = o.name + " — " + o.description; if(o.name === v.current) el.selected = true; vs.appendChild(el); });
}
async function setPers(){ const r = await api("/api/personality", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:document.getElementById("pers").value})}); document.getElementById("pvmsg").textContent = r.ok ? "personality: " + r.current : ("error: " + r.error); }
async function setVprof(){ const r = await api("/api/voice-profile", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:document.getElementById("vprof").value})}); document.getElementById("pvmsg").textContent = r.ok ? "voice: " + r.current : ("error: " + r.error); }
(async function(){ const cmds = ["…"]; const r = await api("/api/commands"); const sel=document.getElementById("cmd"); sel.innerHTML=""; r.commands.forEach(c=>{const o=document.createElement("option");o.value=c;o.textContent=c;sel.appendChild(o);}); refresh(); loadLogs(); loadSettings(); loadPickers(); })();
</script></body></html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    token: str = ""
    server_version = "JadeDashboard/1.0"

    def log_message(self, *a):  # keep quiet; never log tokens
        pass

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if not _authed(self, self.token):
            self._json({"error": "unauthorized"}, 401)
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            if not self._require_auth():
                return
            body = _PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._require_auth():
            return
        if parsed.path == "/api/status":
            self._json(collect_status())
        elif parsed.path == "/api/logs":
            self._json({"lines": tail_logs()})
        elif parsed.path == "/api/settings":
            try:
                from core.settings import get_schema, export_settings
                schema = get_schema()
                # Never leak secrets to the dashboard page.
                values = export_settings()
                for entry in schema:
                    k = entry["key"]
                    if any(s in k for s in ("secret", "api_key", "token", "client_id")) and values.get(k):
                        values[k] = "••••••••"
                self._json({"schema": schema, "values": values})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif parsed.path == "/api/commands":
            _register_readonly()
            self._json({"commands": [label for label, _ in _READONLY]})
        elif parsed.path == "/api/personality":
            self._json(get_personality_state())
        elif parsed.path == "/api/voice-profile":
            self._json(get_voice_profile_state())
        elif parsed.path in _EXTRA_PAGES:
            body, content_type = _EXTRA_PAGES[parsed.path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 65536)) if length else b""
        try:
            data = json.loads(raw.decode()) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        if parsed.path == "/api/chat":
            self._json({"reply": chat_reply(str(data.get("message", "")))})
        elif parsed.path == "/api/cmd":
            self._json({"output": run_readonly(str(data.get("command", "")))})
        elif parsed.path == "/api/settings":
            key = str(data.get("key", ""))
            value = data.get("value")
            if not key:
                self._json({"ok": False, "error": "missing key"}, 400)
            else:
                try:
                    from core.settings import set_setting
                    # Never accept secrets through the dashboard settings page.
                    if any(s in key for s in ("secret", "api_key", "token", "client_id")):
                        self._json({"ok": False, "error": "Secrets cannot be changed from the dashboard. Use the CLI."}, 403)
                    else:
                        self._json(set_setting(key, value, _confirm_sensitive=False))
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
        elif parsed.path == "/api/personality":
            self._json(set_personality_state(str(data.get("name", ""))))
        elif parsed.path == "/api/voice-profile":
            self._json(set_voice_profile_state(str(data.get("name", ""))))
        else:
            self._json({"error": "not found"}, 404)


class DashboardServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _bind_server(host: str, port: int, token: str):
    """Bind the dashboard server, or return (None, error_message)."""
    handler = type("AuthedHandler", (_Handler,), {"token": token})
    try:
        return DashboardServer((host, port), handler), None
    except OSError as e:
        hint = "try `/dashboard --port 0` for a random free port" if port else "no free port found"
        return None, f"Couldn't bind the dashboard to {host}:{port or 'a free port'} ({e}). {hint}."


def run_dashboard(port: int = 0, bind_lan: bool = False,
                  open_browser: bool = False) -> Dict[str, Any]:
    """Start the dashboard. Returns {"url", "port"}. Blocks until Ctrl-C."""
    token = _get_token()
    host = "0.0.0.0" if bind_lan else "127.0.0.1"
    if bind_lan:
        print("⚠️  WARNING: dashboard bound to ALL interfaces (LAN). "
              "Anyone on your network with the token can read status and chat.")
    server, err = _bind_server(host, port, token)
    if server is None:
        print(f"[red]{err}[/red]")
        return {"url": None, "port": None, "error": err}
    actual_port = server.server_address[1]
    display_host = "127.0.0.1"
    url = f"http://{display_host}:{actual_port}/?token={token}"
    print(f"🖥️  Jade dashboard: http://{display_host}:{actual_port}/")
    print("   Token: printed below (also in ~/.jade/dashboard_token, 0600).")
    print(f"   {token}")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"url": url, "port": actual_port}


def start_dashboard_background(port: int = 0, bind_lan: bool = False) -> Dict[str, Any]:
    """Start the dashboard on a background thread. Returns url/port/stop()."""
    token = _get_token()
    host = "0.0.0.0" if bind_lan else "127.0.0.1"
    server, err = _bind_server(host, port, token)
    if server is None:
        return {"url": None, "port": None, "token": token,
                "stop": lambda: None, "error": err}
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="jade-dashboard")
    thread.start()
    url = f"http://127.0.0.1:{actual_port}/?token={token}"

    def _stop():
        server.shutdown()
        server.server_close()

    return {"url": url, "port": actual_port, "token": token, "stop": _stop}
