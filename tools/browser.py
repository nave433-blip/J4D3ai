"""Optional browser capability — no required dependencies.

Backend preference:
  1. Playwright (if installed): full rendering, screenshots, automation.
  2. System Chromium / Google Chrome headless: rendering + screenshots.
  3. Plain HTTP text-only fallback: no JavaScript, no screenshots.

fetch_text() always works. screenshot()/browse() need a rendering backend;
when none exists they return an explicit error with install guidance instead
of pretending.
"""

import html
import re
import shutil
import subprocess
import urllib.request

INSTALL_GUIDE = (
    "For rendering/screenshots install one of: "
    "'pip install playwright && python -m playwright install chromium', "
    "or a system browser ('apt install chromium', 'brew install chromium')."
)

_BACKEND = None
_CHROMIUM_EXE = None
_CHROMIUM_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
)


def _detect_backend() -> str:
    """Probe backends in preference order; result is cached."""
    global _BACKEND, _CHROMIUM_EXE
    if _BACKEND is not None:
        return _BACKEND
    try:
        import playwright  # noqa: F401
        _BACKEND = "playwright"
    except ImportError:
        for exe in _CHROMIUM_CANDIDATES:
            if shutil.which(exe):
                _BACKEND = "chromium"
                _CHROMIUM_EXE = exe
                break
        else:
            _BACKEND = "http"
    return _BACKEND


def probe_backend() -> str:
    """'playwright' | 'chromium' | 'http'."""
    return _detect_backend()


def _check_url(url: str) -> str:
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"browser only fetches http(s) URLs, got: {url!r}")
    return url


def _html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]*>", " ", page)
    text = html.unescape(page)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- fetch_text

def _fetch_http(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jade-browser/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as e:
        return {"ok": False, "url": url, "backend": "http",
                "error": f"HTTP fetch failed: {e}"}
    try:
        page = raw.decode(charset, errors="replace")
    except Exception:
        page = raw.decode("utf-8", errors="replace")
    return {"ok": True, "url": url, "backend": "http",
            "rendered_js": False,
            "text": _html_to_text(page),
            "note": "text-only fallback: no JavaScript rendering"}


def _fetch_chromium(url: str, timeout: float) -> dict:
    exe = _CHROMIUM_EXE or "chromium"
    try:
        proc = subprocess.run(
            [exe, "--headless", "--disable-gpu", "--no-sandbox",
             "--dump-dom", url],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium binary {exe!r} not found", "install": INSTALL_GUIDE}
    except subprocess.TimeoutExpired:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium failed: {e}"}
    if proc.returncode != 0:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium exited {proc.returncode}: {proc.stderr[:300]}"}
    return {"ok": True, "url": url, "backend": "chromium",
            "rendered_js": True, "text": _html_to_text(proc.stdout)}


def _fetch_playwright(url: str, timeout: float) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=int(timeout * 1000))
                text = page.inner_text("body")
            finally:
                browser.close()
    except Exception as e:
        return {"ok": False, "url": url, "backend": "playwright",
                "error": f"Playwright fetch failed: {e}"}
    return {"ok": True, "url": url, "backend": "playwright",
            "rendered_js": True, "text": text}


def fetch_text(url: str, timeout: float = 20) -> dict:
    """Fetch a page and return its visible text.

    Always works: falls back to plain HTTP (no JavaScript) when no
    rendering backend is installed. The result says which backend ran.
    """
    _check_url(url)
    backend = _detect_backend()
    if backend == "playwright":
        return _fetch_playwright(url, timeout)
    if backend == "chromium":
        return _fetch_chromium(url, timeout)
    return _fetch_http(url, timeout)


# ---------------------------------------------------------------- screenshot

def screenshot(url: str, path: str, timeout: float = 30,
               width: int = 1280, height: int = 800) -> dict:
    """Save a page screenshot to `path`. Needs Playwright or Chromium."""
    _check_url(url)
    backend = _detect_backend()
    if backend == "http":
        return {"ok": False, "url": url, "backend": "http",
                "error": "Screenshots need a rendering backend (none installed).",
                "install": INSTALL_GUIDE}
    if backend == "playwright":
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": width, "height": height})
                    page.goto(url, timeout=int(timeout * 1000))
                    page.screenshot(path=path)
                finally:
                    browser.close()
        except Exception as e:
            return {"ok": False, "url": url, "backend": "playwright",
                    "error": f"Playwright screenshot failed: {e}"}
        return {"ok": True, "url": url, "path": path, "backend": "playwright"}
    exe = _CHROMIUM_EXE or "chromium"
    try:
        proc = subprocess.run(
            [exe, "--headless", "--disable-gpu", "--no-sandbox",
             f"--window-size={width},{height}", f"--screenshot={path}", url],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium binary {exe!r} not found", "install": INSTALL_GUIDE}
    except Exception as e:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium screenshot failed: {e}"}
    if proc.returncode != 0:
        return {"ok": False, "url": url, "backend": "chromium",
                "error": f"Chromium exited {proc.returncode}: {proc.stderr[:300]}"}
    return {"ok": True, "url": url, "path": path, "backend": "chromium"}


# ---------------------------------------------------------------- browse

def browse(url: str, actions=None, timeout: float = 30) -> dict:
    """Minimal scripted browsing primitive (Playwright only).

    actions: list of {"type": "goto"|"click"|"fill"|"wait"|"text"|"title",
                      "selector"?, "value"?, "url"?, "ms"?}.
    Returns {"ok", "backend", "results": [...]} with one entry per action.
    """
    _check_url(url)
    if _detect_backend() != "playwright":
        return {"ok": False, "url": url, "backend": _detect_backend(),
                "error": "browse() automation needs Playwright installed.",
                "install": INSTALL_GUIDE}
    results = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=int(timeout * 1000))
                for act in actions or []:
                    atype = act.get("type")
                    if atype == "goto":
                        page.goto(act["url"], timeout=int(timeout * 1000))
                        results.append({"type": "goto", "url": act["url"]})
                    elif atype == "click":
                        page.click(act["selector"], timeout=int(timeout * 1000))
                        results.append({"type": "click", "selector": act["selector"]})
                    elif atype == "fill":
                        page.fill(act["selector"], act.get("value", ""),
                                  timeout=int(timeout * 1000))
                        results.append({"type": "fill", "selector": act["selector"]})
                    elif atype == "wait":
                        page.wait_for_timeout(int(act.get("ms", 1000)))
                        results.append({"type": "wait", "ms": act.get("ms", 1000)})
                    elif atype == "text":
                        results.append({"type": "text",
                                        "text": page.inner_text(act.get("selector", "body"))})
                    elif atype == "title":
                        results.append({"type": "title", "title": page.title()})
                    else:
                        results.append({"type": atype, "error": "unknown action"})
            finally:
                browser.close()
    except Exception as e:
        return {"ok": False, "url": url, "backend": "playwright",
                "error": f"browse() failed: {e}", "results": results}
    return {"ok": True, "url": url, "backend": "playwright", "results": results}


def backend_status() -> dict:
    """Human-readable backend report for docs/diagnostics."""
    backend = _detect_backend()
    return {
        "backend": backend,
        "rendering": backend in ("playwright", "chromium"),
        "screenshots": backend in ("playwright", "chromium"),
        "automation": backend == "playwright",
        "install": None if backend != "http" else INSTALL_GUIDE,
    }
