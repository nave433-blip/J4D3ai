"""OS-native presence: system tray / menu bar / panel icon.

  - Windows: notification-area (system tray) icon
  - macOS: menu-bar extra
  - Linux: panel/status-bar applet (via StatusNotifier/AppIndicator where
    the desktop supports it)

Implemented on the optional ``pystray`` package (``pip install pystray``).
Everything is best-effort per OS with honest "not supported here" messages —
never a fake icon. The CLI works with zero tray dependencies.
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple


def platform_slot() -> Tuple[str, str]:
    """Return (os_name, ui_slot) e.g. ("windows", "system tray")."""
    if sys.platform == "darwin":
        return "macos", "menu bar"
    if sys.platform.startswith("win"):
        return "windows", "system tray"
    return "linux", "panel / status bar"


def tray_supported() -> Tuple[bool, str]:
    """Check whether the tray icon can run here."""
    try:
        import pystray  # noqa: F401
    except ImportError:
        return False, "the 'pystray' package is not installed (pip install pystray)"
    os_name, slot = platform_slot()
    if os_name == "linux":
        import shutil
        # pystray on Linux needs an AppIndicator/StatusNotifier host.
        return True, (f"Linux {slot} via pystray — needs a StatusNotifier host "
                      f"(e.g. GNOME with the AppIndicator extension)")
    return True, f"{os_name} {slot} via pystray"


def _icon_image():
    from core.desktop import app_icon_path
    from PIL import Image
    path = app_icon_path()
    if path is not None:
        try:
            return Image.open(path)
        except Exception:
            pass
    # Fallback: simple generated glyph so the tray still shows something real.
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    px = img.load()
    for y in range(64):
        for x in range(64):
            dx, dy = x - 32, y - 32
            d = (dx * dx + dy * dy) ** 0.5
            if 20 <= d <= 30:
                glow = max(0, 255 - int(abs(d - 25) * 40))
                px[x, y] = (88, 166, 255, glow)
    return img


def _get_flag(key: str, default: bool = True) -> bool:
    from core.config import load_config
    return bool(load_config().get(key, default))


def _set_flag(key: str, value: bool) -> None:
    from core.config import load_config, save_config
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def run_tray() -> None:
    """Run the tray / menu-bar / panel icon (blocks until Quit)."""
    ok, reason = tray_supported()
    os_name, slot = platform_slot()
    if not ok:
        print(f"[jade] No {slot} icon on {os_name} right now: {reason}.")
        print("[jade] The CLI, dashboard (`jade dashboard`), and overlay "
              "(`jade overlay`) all work without it.")
        return
    print(f"[jade] {reason}")

    import pystray
    from pystray import MenuItem as Item, Menu

    icon = pystray.Icon("jade", _icon_image(), "Jade",
                        menu=Menu(
                            Item("Show overlay",
                                 lambda: _show_overlay()),
                            Item(lambda item: f"Listening: {'on' if _get_flag('wakeword_enabled', False) else 'off'}",
                                 lambda: _toggle_wakeword()),
                            Item(lambda item: f"Voice: {'on' if _get_flag('voice_enabled', True) else 'off'}",
                                 lambda: _toggle_voice()),
                            Item("Personality", Menu(
                                *_personality_items())),
                            Item("Quit", lambda: _quit(icon)),
                        ))
    icon.run()


def _show_overlay() -> None:
    import threading
    from core.desktop import launch_overlay
    threading.Thread(target=launch_overlay, daemon=True).start()


def _toggle_wakeword() -> None:
    from core.voice import wakeword as ww
    current = _get_flag("wakeword_enabled", False)
    if not current and not ww.notice_acknowledged():
        # First enable: privacy notice goes through the terminal, not a popup.
        print("[jade] " + ww.WAKEWORD_NOTICE)
        try:
            answer = input("Enable 'Hey Jade' wake-word listening? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            print("[jade] Wake-word listening stays off.")
            return
        ww.acknowledge_notice()
    _set_flag("wakeword_enabled", not current)
    print(f"[jade] Wake-word listening {'enabled' if not current else 'disabled'}.")


def _toggle_voice() -> None:
    current = _get_flag("voice_enabled", True)
    _set_flag("voice_enabled", not current)
    print(f"[jade] Voice output {'enabled' if not current else 'disabled'}.")


def _personality_items():
    from pystray import MenuItem as Item
    from core.personalities import list_personalities
    items = []
    for p in list_personalities():
        def _make(name=p.name, title=p.title):
            def _set(icon=None, item=None):
                from core.config import load_config, save_config
                cfg = load_config()
                cfg["personality"] = name
                save_config(cfg)
                print(f"[jade] Personality: {title}")
            return _set
        items.append(Item(p.title, _make()))
    return items


def _quit(icon) -> None:
    try:
        icon.stop()
    except Exception:
        pass
