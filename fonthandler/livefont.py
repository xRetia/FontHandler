"""Apply a font to the live session with ``AddFontResourceEx`` (FR_PRIVATE).

This is what makes a just-replaced font visible to running GDI apps without a
log-off.  It is entirely optional and safe on any platform (no-op off-Windows).
"""

from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager
from pathlib import Path

__all__ = ["add_font_private", "remove_font_private", "installed_fonts",
           "session_fonts", "broadcast_font_change"]

FR_PRIVATE = 0x10

HWND_BROADCAST = 0xFFFF
WM_FONTCHANGE = 0x001D
SMTO_ABORTIFHUNG = 0x0002
FONTCHANGE_TIMEOUT_MS = 5000

_INSTALLED: dict[str, int] = {}


def broadcast_font_change(timeout_ms: int = FONTCHANGE_TIMEOUT_MS) -> bool:
    """Tell every top-level window that the font set changed (WM_FONTCHANGE).

    Running apps pick the new glyphs up without a log-off.  ``SendMessageTimeout``
    is deliberate over plain ``SendMessage``: a single hung application must not
    wedge the replace job; ``SMTO_ABORTIFHUNG`` skips such a window instead.

    The replace pipeline calls this by name after a successful batch -- the
    function has to exist or the pipeline logs a warning instead of notifying
    anyone (which is exactly how its absence surfaced in the wild).
    """
    if os.name != "nt":
        return True
    try:
        result = ctypes.c_ulong()
        sent = ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0,
            SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result),
        )
        return bool(sent)
    except Exception:  # noqa: BLE001
        return False


def add_font_private(path: Path) -> bool:
    """Register a font for this session only. Returns success."""
    if os.name != "nt":
        return True
    try:
        count = ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0)
        if count:
            _INSTALLED[str(path).lower()] = count
        return bool(count)
    except Exception:  # noqa: BLE001
        return False


def remove_font_private(path: Path) -> bool:
    if os.name != "nt":
        return True
    key = str(path).lower()
    count = _INSTALLED.pop(key, 0)
    if not count:
        return False
    try:
        for _ in range(count):
            ctypes.windll.gdi32.RemoveFontResourceExW(str(path), FR_PRIVATE, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


@contextmanager
def session_fonts(paths):
    """Temporarily install fonts for this session (restored on exit)."""
    applied = []
    for p in paths:
        if add_font_private(Path(p)):
            applied.append(Path(p))
    try:
        yield applied
    finally:
        for p in applied:
            remove_font_private(p)


def installed_fonts() -> list[str]:
    return list(_INSTALLED)