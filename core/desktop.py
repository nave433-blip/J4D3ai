"""Cortana desktop app: chat window, Perplexity-style overlay, global hotkey.

The desktop UI is web tech served by the existing local dashboard HTTP core
(:mod:`core.dashboard`, stdlib only). The desktop *shell* (native window,
tray icon, global hotkey) is strictly opt-in:

  - ``pywebview``  — native window for the app and the overlay
  - ``pystray``    — system tray / menu-bar / panel icon (see core/tray.py)
  - ``keyboard``   — global hotkey to summon the overlay

None of these are installed by default and the CLI works with zero GUI
dependencies. Every launcher degrades honestly: if the shell package is
missing you get a clear install hint, and the dashboard still opens in your
browser.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Optional

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

#: Default global hotkey that summons the overlay (``keyboard`` package syntax).
DEFAULT_HOTKEY = "ctrl+shift+space"


# ---------------------------------------------------------------------------
# Overlay page (glassmorphism) — registered with the dashboard HTTP core.
# ---------------------------------------------------------------------------

OVERLAY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Cortana overlay</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font-family:system-ui,-apple-system,sans-serif}
body{
  background:rgba(20,24,32,.55);
  -webkit-backdrop-filter:blur(22px) saturate(1.4);
  backdrop-filter:blur(22px) saturate(1.4);
  color:#e8eef6;display:flex;flex-direction:column;align-items:center;
  border:1px solid rgba(255,255,255,.14);border-radius:18px;overflow:hidden;
}
/* graceful fallback where backdrop-filter is unsupported */
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
  body{background:rgba(18,22,30,.94)}
}
header{width:100%;display:flex;align-items:center;gap:10px;padding:12px 16px;
  border-bottom:1px solid rgba(255,255,255,.08)}
header img{width:28px;height:28px;border-radius:50%}
header .t{font-weight:600;letter-spacing:.3px}
header .s{margin-left:auto;font-size:12px;opacity:.6}
#log{flex:1;width:100%;overflow-y:auto;padding:12px 16px;font-size:14px;line-height:1.5}
#log .u{color:#8ab8ff;margin:8px 0 2px}
#log .a{color:#e8eef6;margin:2px 0 8px;white-space:pre-wrap}
#bar{width:100%;display:flex;gap:8px;padding:12px 16px;border-top:1px solid rgba(255,255,255,.08)}
#q{flex:1;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);
  border-radius:10px;color:#fff;padding:10px 12px;font-size:14px;outline:none}
#q:focus{border-color:rgba(120,180,255,.55)}
button{background:rgba(88,166,255,.22);border:1px solid rgba(120,180,255,.4);color:#dceaff;
  border-radius:10px;padding:10px 14px;font-size:14px;cursor:pointer}
button:hover{background:rgba(88,166,255,.34)}
.hint{font-size:11px;opacity:.5;padding:0 16px 10px}
</style></head><body>
<header>
  <img id="mark" alt="Cortana">
  <div class="t">Cortana</div>
  <div class="s" id="status">ready</div>
</header>
<div id="log"></div>
<div id="bar">
  <input id="q" placeholder="Ask anything…  (Esc dismisses)" autocomplete="off">
  <button onclick="ask()">Ask</button>
</div>
<div class="hint">Wake me when you need me. — token-authenticated local session</div>
<script>
const T = new URLSearchParams(location.search).get("token") || "";
document.getElementById("mark").src = "assets-mark?token=" + encodeURIComponent(T);
const log = document.getElementById("log"), q = document.getElementById("q"),
      status = document.getElementById("status");
async function api(path, opts){
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(T), opts);
  return r.json();
}
function say(who, cls, text){
  const d = document.createElement("div"); d.className = cls;
  d.textContent = (who ? who + ": " : "") + text; log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
async function ask(){
  const m = q.value.trim(); if(!m) return; q.value = "";
  say("you", "u", m); status.textContent = "thinking…";
  try{
    const r = await api("/api/chat", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({message:m})});
    say("cortana", "a", r.reply || "(no reply)");
  }catch(e){ say("", "a", "chat unavailable: " + e); }
  status.textContent = "ready"; q.focus();
}
q.addEventListener("keydown", e => {
  if(e.key === "Enter") ask();
  if(e.key === "Escape") window.close();
});
q.focus();
</script></body></html>"""


def overlay_html() -> str:
    return OVERLAY_HTML


def _webview():
    try:
        import webview  # pywebview
        return webview
    except ImportError:
        return None


def _open_native_window(url: str, title: str, width: int, height: int,
                        frameless: bool = False) -> bool:
    """Open a native window via pywebview. Returns False when unavailable."""
    wv = _webview()
    if wv is None:
        return False
    wv.create_window(title, url, width=width, height=height,
                     frameless=frameless, easy_drag=True,
                     background_color="#14181F")
    wv.start()
    return True


def _open_browser(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _start_dashboard() -> Dict:
    from core.dashboard import start_dashboard_background, register_page
    register_page("/overlay", overlay_html())
    # Serve the tray/app icon to the overlay page.
    mark = ASSETS_DIR / "jade-mark.png"
    if mark.exists():
        register_page("/assets-mark", mark.read_bytes(),
                       content_type="image/png")
    return start_dashboard_background()


def _shell_hint(what: str) -> None:
    print(f"[cortana] {what} needs the desktop shell package: "
          f"pip install pywebview")
    print("[cortana] Opening in your browser instead — same app, no native window.")


def launch_gui(width: int = 1100, height: int = 760) -> Dict:
    """Launch the Cortana desktop app (dashboard UI in a native window).

    Falls back to the system browser when pywebview is not installed.
    """
    info = _start_dashboard()
    if info.get("error"):
        print(f"[cortana] Could not start the local UI: {info['error']}")
        return info
    url = info["url"]
    print(f"[cortana] Desktop UI: {url}")
    if not _open_native_window(url, "Cortana", width, height):
        _shell_hint("The native window")
        _open_browser(url)
    return info


def launch_overlay(width: int = 560, height: int = 420,
                   hotkey: Optional[str] = None) -> Dict:
    """Launch the floating overlay. With the ``keyboard`` package installed,
    ``hotkey`` (default Ctrl+Shift+Space) summons it globally."""
    info = _start_dashboard()
    if info.get("error"):
        print(f"[cortana] Could not start the local UI: {info['error']}")
        return info
    url = info["url"].replace("/?token=", "/overlay?token=")
    print(f"[cortana] Overlay: {url}")
    hk = hotkey or os.environ.get("CORTANA_HOTKEY", DEFAULT_HOTKEY)

    def _show():
        if not _open_native_window(url, "Cortana overlay", width, height):
            _shell_hint("The overlay window")
            _open_browser(url)

    try:
        import keyboard  # optional
        try:
            keyboard.add_hotkey(hk, _show)
            print(f"[cortana] Global hotkey armed: {hk} (Ctrl+C here quits)")
            keyboard.wait()
        except Exception as e:
            print(f"[cortana] Could not arm the global hotkey ({e}). "
                  f"Showing the overlay once instead.")
            _show()
    except ImportError:
        print("[cortana] Install the 'keyboard' package for a global hotkey "
              "(pip install keyboard). Showing the overlay once instead.")
        _show()
    return info


def app_icon_path() -> Optional[Path]:
    """Best available app icon file, or None."""
    for name in ("jade-mark.png", "cortana-logo.png", "icon.png"):
        p = ASSETS_DIR / name
        if p.exists():
            return p
    return None
