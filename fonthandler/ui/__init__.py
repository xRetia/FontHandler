"""PyQt6 front end for FontHandler."""

from __future__ import annotations

__all__ = ["main_window", "style", "log_panel", "common"]

try:  # pragma: no cover - PyQt6 may be absent in headless installs
    import PyQt6  # noqa: F401  # presence probe

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False
