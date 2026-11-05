"""Shared Playwright launcher for the render (PDF) and prefill stages.

Playwright is an optional dependency (`pip install -e ".[prefill]"`), so it is imported
lazily and a missing install becomes a clear error instead of an ImportError at startup.
"""
from __future__ import annotations

import os
from typing import Any

INSTALL_HINT = (
    'Playwright is not installed. Run: pip install -e ".[prefill]" && python -m playwright install chromium'
)


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is missing, or no display is available."""


def start_playwright() -> Any:
    """Start Playwright and return the running instance (call `.stop()` when done)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(INSTALL_HINT) from e
    return sync_playwright().start()


def launch_chromium(pw: Any, headless: bool) -> Any:
    """Launch Chromium. `AUTOAPPLY_CHROMIUM` may point at a specific browser binary."""
    kwargs: dict[str, Any] = {"headless": headless}
    if exe := os.environ.get("AUTOAPPLY_CHROMIUM"):
        kwargs["executable_path"] = exe
    try:
        return pw.chromium.launch(**kwargs)
    except Exception as e:  # missing browser build, no display for a headed window, ...
        raise BrowserUnavailable(
            f"Could not start Chromium ({type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}). "
            "Run `python -m playwright install chromium`. A visible browser needs a desktop session."
        ) from e
