"""Windows font cache and live-notification helpers."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["clear_font_cache", "broadcast_font_change", "WM_FONTCHANGE"]


WM_FONTCHANGE = 0x1D


def _run(*cmd: str) -> bool:
    from .acl import run_hidden

    try:
        result = run_hidden(list(cmd))
        return result.returncode == 0
    except OSError:
        return False


def clear_font_cache() -> bool:
    """Attempt to purge Windows font cache (protective: no-ops off-Windows)."""
    if os.name != "nt":
        return True
    services = [
        ["net", "stop", "FontCache"],
        ["net", "stop", "FontCache3.0.0.0"],
    ]
    for s in services:
        _run(*s)
    cache_dirs = [
        Path(r"C:\Windows\ServiceProfiles\LocalService\AppData\Local\FontCache"),
        Path(r"C:\Windows\System32\FNTCACHE.DAT"),
        Path(r"C:\Windows\SysWOW64\FNTCACHE.DAT"),
    ]
    for d in cache_dirs:
        try:
            if d.is_dir():
                for f in d.rglob("*"):
                    try:
                        f.unlink(missing_ok=True)
                    except OSError:
                        pass
            elif d.exists():
                d.unlink(missing_ok=True)
        except OSError:
            pass
    for s in reversed(services):
        _run(*s)
    return True


def broadcast_font_change() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        HWND_BROADCAST = 0xFFFF
        user32.SendMessageW(HWND_BROADCAST, WM_FONTCHANGE, 0, 0)
        return True
    except Exception:  # noqa: BLE001
        return False
