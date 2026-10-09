"""Windows environment integration for Jade.

Every public function is gated on ``sys.platform == "win32"``. On any
other platform they return ``{"ok": False, "error": "Windows only"}`` —
never a fake result.

What this module does:
- toast notifications (``winrt`` if installed, else a PowerShell fallback
  using the built-in Windows.UI.Notifications API — no third-party
  BurntToast dependency required),
- startup registration via the HKCU Run key (the ONLY registry write this
  module performs), with on/off toggle and status check,
- Windows system summary (OS build, install date) layered on top of
  core.hardware_check's cross-platform psutil data,
- default protocol-handler checks (read-only registry reads).

What it deliberately does NOT do: install kernel drivers, touch services,
or write anywhere in the registry outside the documented Run value. A
user-space Python app cannot install drivers; any claim otherwise would
be a lie, so the module says so in its docs and refuses to fake it.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

ON_WINDOWS = sys.platform == "win32"

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "Jade"


def _not_windows() -> Dict[str, Any]:
    return {"ok": False, "error": "Windows only (this machine is not Windows)."}


# ---------------------------------------------------------------- toasts
def _toast_ps_script(title: str, message: str, app_id: str) -> str:
    """PowerShell script that raises a toast via the built-in
    Windows.UI.Notifications API. Escapes single quotes for PS."""
    t = title.replace("'", "''")
    m = message.replace("'", "''")
    a = app_id.replace("'", "''")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null\n"
        "$xml = @\"\n"
        "<toast><visual><binding template='ToastGeneric'>\n"
        f"<text>{t}</text><text>{m}</text>\n"
        "</binding></visual></toast>\n"
        "\"@\n"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$doc.LoadXml($xml)\n"
        f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{a}')\n"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)\n"
        "$notifier.Show($toast)\n"
    )


def toast(title: str, message: str,
          app_id: str = "Jade") -> Dict[str, Any]:
    """Show a Windows toast notification. Returns {"ok": True} or an error."""
    if not ON_WINDOWS:
        return _not_windows()
    # Preferred path: winrt package, if the user installed it.
    try:
        import winrt.windows.ui.notifications as _wn  # type: ignore
        import winrt.windows.data.xml.dom as _dom  # type: ignore
        xml = (f"<toast><visual><binding template='ToastGeneric'>"
               f"<text>{title}</text><text>{message}</text>"
               f"</binding></visual></toast>")
        doc = _dom.XmlDocument()
        doc.load_xml(xml)
        notifier = _wn.ToastNotificationManager.create_toast_notifier(app_id)
        notifier.show(_wn.ToastNotification(doc))
        return {"ok": True, "via": "winrt"}
    except ImportError:
        pass
    except Exception as e:
        return {"ok": False, "error": f"winrt toast failed: {e}"}
    # Fallback: built-in API via PowerShell, no extra packages.
    import subprocess
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _toast_ps_script(title, message, app_id)],
            capture_output=True, timeout=30, check=False)
        return {"ok": True, "via": "powershell"}
    except Exception as e:
        return {"ok": False, "error": f"PowerShell toast failed: {e}"}


# ---------------------------------------------------------------- startup
def _exe_path() -> str:
    import os
    return os.path.abspath(sys.argv[0])


def startup_status() -> Dict[str, Any]:
    """Check whether Jade is registered to run at Windows login."""
    if not ON_WINDOWS:
        return _not_windows()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE)
        return {"ok": True, "enabled": True, "command": value}
    except FileNotFoundError:
        return {"ok": True, "enabled": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_startup(enabled: bool) -> Dict[str, Any]:
    """Toggle Jade's HKCU Run entry. This is the only registry write
    this module performs."""
    if not ON_WINDOWS:
        return _not_windows()
    import winreg
    try:
        if enabled:
            cmd = f'"{sys.executable}" "{_exe_path()}"'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, cmd)
            return {"ok": True, "enabled": True, "command": cmd}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, _RUN_VALUE)
            except FileNotFoundError:
                pass
        return {"ok": True, "enabled": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- system info
def windows_system_summary() -> Dict[str, Any]:
    """Windows-specific system facts (OS build, install date) merged with
    the cross-platform psutil summary from core.hardware_check."""
    if not ON_WINDOWS:
        return _not_windows()
    import winreg
    info: Dict[str, Any] = {}
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            def _q(name: str) -> Optional[str]:
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except Exception:
                    return None
            info["product_name"] = _q("ProductName")
            info["build"] = _q("CurrentBuildNumber")
            info["display_version"] = _q("DisplayVersion")
            info["registered_owner"] = _q("RegisteredOwner")
    except Exception as e:
        info["registry_error"] = str(e)
    try:
        from core.hardware_check import get_hardware_specs
        info.update(get_hardware_specs())
    except Exception as e:
        info["hardware_error"] = str(e)
    return {"ok": True, "system": info}


# ---------------------------------------------------------------- protocols
def default_protocol_handler(protocol: str) -> Dict[str, Any]:
    """Read-only check: which app handles a URL protocol (e.g. 'http',
    'mailto'). Returns {"ok": False} with a reason when unregistered."""
    if not ON_WINDOWS:
        return _not_windows()
    import winreg
    proto = protocol.lstrip(":").lower()
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\{proto}\UserChoice") as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
    except FileNotFoundError:
        return {"ok": False, "error": f"No handler registered for '{proto}:'."}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "protocol": proto, "prog_id": str(prog_id)}


# ---------------------------------------------------------------- drivers
def driver_info() -> Dict[str, Any]:
    """Honest answer about drivers: a user-space Python app cannot install
    kernel drivers. This function exists so the assistant can say so
    plainly instead of hallucinating driver management."""
    return {"ok": False,
            "error": ("Driver installation is not possible from Jade: "
                      "Windows kernel drivers require an EV-signed .inf/.sys "
                      "package installed via pnputil/Device Manager with "
                      "administrator rights. Jade is a user-space app and "
                      "does not ship, install, or manage drivers.")}
