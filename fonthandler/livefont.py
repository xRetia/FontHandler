"""Apply a font to the live session with ``AddFontResourceEx`` (FR_PRIVATE).

This is what makes a just-replaced font visible to running GDI apps without a
log-off.  It is entirely optional and safe on any platform (no-op off-Windows).
"""

from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager
from pathlib import Path

__all__ = ["add_font_private", "remove_font_private", "installed_fonts", "session_fonts"]

FR_PRIVATE = 0x10

_INSTALLED: dict[str, int] = {}


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