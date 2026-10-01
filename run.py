"""Entry point: launches the PyQt6 UI without a console window.

``python.exe`` is a console-subsystem binary, so double-clicking this file used
to pop up a black console behind the UI.  Detaching from the console in place
keeps a single process and leaves the caller (Explorer, cmd, a shortcut) alone.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# Portable distributions ship a ``python314._pth`` which disables the automatic
# prepending of the script directory, so make the import explicit.
sys.path.insert(0, str(Path(__file__).resolve().parent))

CRASH_LOG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FontHandler" / "crash.log"


def _log_crash(exc_type, exc, tb) -> None:
    """Record an unhandled exception where it can still be read.

    ``_detach_console`` has already redirected stdout and stderr to the null
    device by the time this can fire, so an uncaught error would otherwise
    vanish with no traceback anywhere.
    """
    if exc_type is None:
        # Qt calls sys.excepthook(None, None, None) on some shutdown paths.
        # traceback.format_exc() would write a useless "NoneType: None" line.
        return
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
    except Exception:
        text = f"{exc_type.__name__}: {exc}"
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"--- {text}\n")
    except OSError:
        pass
    # Belt and braces: a visible dialog beats a silent exit.
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        # Only reuse an existing QApplication. Building one here would need a
        # full event loop and would hang the shutdown path.
        app = QApplication.instance()
        if app is not None:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("FontHandler 启动失败")
            box.setText(f"{exc_type.__name__}: {exc}")
            box.setInformativeText(f"完整错误信息已写入：\n{CRASH_LOG}")
            box.setDetailedText(text)
            box.exec()
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc, tb)


def _detach_console() -> None:
    """Drop the console window, if this process has one.

    A no-op when launched from Explorer with an already-hidden console or from
    ``pythonw.exe``.  When a console *is* visible all three standard handles are
    re-opened on the null device first: ``FreeConsole`` invalidates them, and a
    child process cannot be spawned from a handle that no longer resolves --
    ``subprocess`` raises ``OSError: [WinError 50]`` from
    ``_make_inheritable`` when ``icacls`` runs against a detached console.
    """
    if sys.platform != "win32":
        return
    if Path(sys.executable).stem.lower().startswith("pythonw"):
        return

    import ctypes
    import os

    kernel32 = ctypes.windll.kernel32
    if not kernel32.GetConsoleWindow():
        return  # no console to drop

    # stdin must be readable so children inherit a valid handle, not a dead one.
    null_in = os.open(os.devnull, os.O_RDONLY)
    null_out = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null_in, 0)
    os.dup2(null_out, 1)
    os.dup2(null_out, 2)
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream, None) is not None:
            stream_obj = getattr(sys, stream)
            try:
                os.dup2(null_out, stream_obj.fileno())
            except (OSError, ValueError):
                pass  # stream already closed
    os.close(null_in)
    os.close(null_out)
    kernel32.FreeConsole()


if __name__ == "__main__":
    _detach_console()
    sys.excepthook = _log_crash

    from fonthandler.ui.main_window import main  # noqa: E402

    sys.exit(main())