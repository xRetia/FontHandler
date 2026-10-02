"""Windows font cache and live-notification helpers."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "clear_font_cache",
    "restart_font_cache_service",
    "broadcast_font_change",
    "WM_FONTCHANGE",
]


WM_FONTCHANGE = 0x1D

#: The two services that hold the cache files open.  ``FontCache3.0.0.0`` is
#: the real service name on Windows 10/11; getting it wrong makes ``net stop``
#: fail silently and the cache files stay locked.
FONT_CACHE_SERVICES = ("FontCache", "FontCache3.0.0.0")

_CACHE_PATHS = (
    Path(r"C:\Windows\ServiceProfiles\LocalService\AppData\Local\FontCache"),
    Path(r"C:\Windows\System32\FNTCACHE.DAT"),
    Path(r"C:\Windows\SysWOW64\FNTCACHE.DAT"),
)


def _run(*cmd: str) -> bool:
    from .acl import run_hidden

    try:
        result = run_hidden(list(cmd))
        return result.returncode == 0
    except OSError:
        return False


def stop_font_cache_service() -> list[str]:
    """Stop the font cache services.  Returns the ones that are still up.

    Windows treats a font cache service that refuses to stop as a hint to use
    the Service Control Manager instead, so a leftover service is reported
    rather than assumed gone.
    """
    if os.name != "nt":
        return []
    for name in FONT_CACHE_SERVICES:
        _run("net", "stop", name)
    still_running = []
    for name in FONT_CACHE_SERVICES:
        try:
            from .acl import run_hidden

            result = run_hidden(["sc", "query", name])
            if result.returncode == 0 and "RUNNING" in _decode(result):
                still_running.append(name)
        except Exception:  # noqa: BLE001 - service state is advisory only
            continue
    return still_running


def _decode(result) -> str:
    """Render a child's output as text, whatever ``run_hidden`` returned.

    ``run_hidden`` runs with ``text=True`` so ``stdout`` is already a ``str``
    -- calling ``.decode()`` on it raised ``AttributeError``, which the
    surrounding ``except Exception`` swallowed.  The "service refused to stop"
    check then quietly saw nothing at all, so ``stop_font_cache_service``
    reported every service as stopped even while the files stayed locked.
    """
    from .acl import console_encoding

    text = result.stdout
    if isinstance(text, bytes):
        return text.decode(console_encoding(), errors="replace")
    return text or ""


def restart_font_cache_service() -> bool:
    """Start the font cache services again (they autostart, so this is optional)."""
    if os.name != "nt":
        return True
    ok = True
    for name in FONT_CACHE_SERVICES:
        ok = _run("net", "start", name) and ok
    return ok


def clear_font_cache(restart_service: bool = True) -> bool:
    """Purge the Windows font cache.

    ``restart_service`` has to be ``False`` when some fonts are still sitting in
    the reboot queue.  Restarting the service immediately makes it re-read the
    *current* font files -- which are still the old ones, because the queued
    renames only run during the next boot -- and the freshly built cache is then
    wrong for the fonts that were replaced.  Leaving the service stopped lets it
    start during the next boot and build the cache from the new files instead.

    Returns True when the cache files were purged; a service that would not stop
    is reported through the log rather than treated as a failure, since the
    files may still be deletable.
    """
    if os.name != "nt":
        return True
    stop_font_cache_service()
    removed = 0
    for path in _CACHE_PATHS:
        try:
            if path.is_dir():
                for f in path.rglob("*"):
                    try:
                        f.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
            elif path.exists():
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if restart_service:
        restart_font_cache_service()
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
