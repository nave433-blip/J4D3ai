import json
import os
import sys
from core.config import get_env_with_config

# Optional imports handled inside functions to avoid crash if deps fail
def get_dropbox_client():
    try:
        import dropbox
        token = get_env_with_config("dropbox_token")
        if not token: return None
        return dropbox.Dropbox(token)
    except Exception:
        return None

def _drive_access_token():
    """Resolve the Google Drive access token from config.

    Accepts a bare access-token string, a token dict, or a JSON-encoded
    token dict — whatever the user stored under ``gdrive_token``.
    """
    token = get_env_with_config("gdrive_token")
    if not token:
        return None
    if isinstance(token, dict):
        return token.get("access_token")
    text = str(token).strip()
    if text.startswith("{"):
        try:
            return json.loads(text).get("access_token")
        except Exception:
            return None
    return text

def list_dropbox(path=""):
    dbx = get_dropbox_client()
    if not dbx: return "Error: Dropbox not configured or dependency missing."
    try:
        res = dbx.files_list_folder(path)
        return [entry.name for entry in res.entries]
    except Exception as e:
        return f"Dropbox Error: {e}"

def list_gdrive(query="'root' in parents"):
    """List Google Drive files via direct REST (no vendor SDK)."""
    try:
        from core.connectors.oauth import api_get
    except Exception:
        return "Error: connector framework unavailable."
    token = _drive_access_token()
    if not token:
        return "Error: Google Drive not configured (set gdrive_token)."
    res = api_get(
        "https://www.googleapis.com/drive/v3/files", token,
        params={"q": query, "pageSize": "10", "fields": "files(id,name)"},
    )
    if not isinstance(res, dict) or "error" in res:
        err = res.get("error") if isinstance(res, dict) else res
        return f"GDrive Error: {err}"
    try:
        return [f.get("name", "") for f in res.get("files", [])]
    except Exception as e:
        return f"GDrive Error: {e}"

def get_icloud_path():
    if sys.platform == "darwin":
        path = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
        if os.path.exists(path):
            return path
    return None

def list_icloud(subpath=""):
    base = get_icloud_path()
    if not base: return "Error: iCloud Drive not found or not on macOS."
    target = os.path.join(base, subpath)
    try:
        return os.listdir(target)
    except Exception as e:
        return f"iCloud Error: {e}"
