"""Shared QSS and small visual helpers.

Kept deliberately small: the goal is a readable, dense tool UI rather than a
decorative one, so there is exactly one stylesheet and a couple of colour
constants the pages import instead of hard-coding.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication

__all__ = ["COLORS", "QSS", "apply_style", "mono_font"]

ASSETS = Path(__file__).resolve().parent / "assets"


def _asset_url(name: str) -> str:
    """Absolute forward-slash URL for a stylesheet image reference."""
    return (ASSETS / name).as_posix()

COLORS = {
    "bg": "#1e1f22",
    "panel": "#26282c",
    "panel_alt": "#2b2d32",
    "border": "#3a3d43",
    "text": "#e4e6ea",
    "muted": "#9aa0a8",
    "accent": "#4c8dff",
    "ok": "#4caf7d",
    "warn": "#e0a33a",
    "err": "#e05c5c",
    "sandbox": "#a06cd5",
}

QSS = f"""
QWidget {{
    background: {COLORS['bg']};
    color: {COLORS['text']};
    font-size: 12px;
}}
QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    background: {COLORS['panel']};
}}
QTabBar::tab {{
    background: {COLORS['panel_alt']};
    border: 1px solid {COLORS['border']};
    border-bottom: none;
    padding: 7px 16px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {COLORS['accent']};
    color: #ffffff;
}}
QTabBar::tab:hover:!selected {{
    background: {COLORS['border']};
}}
QGroupBox {{
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {COLORS['muted']};
}}
QPushButton {{
    background: {COLORS['panel_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: 3px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ background: {COLORS['border']}; }}
QPushButton:pressed {{ background: {COLORS['accent']}; }}
QPushButton:disabled {{
    color: {COLORS['muted']};
    background: {COLORS['panel']};
}}
QPushButton[accent="true"] {{
    background: {COLORS['accent']};
    color: #ffffff;
    border-color: {COLORS['accent']};
}}
QPushButton[accent="true"]:hover {{ background: #3d7ae8; }}
QPushButton[danger="true"] {{
    background: {COLORS['err']};
    color: #ffffff;
    border-color: {COLORS['err']};
}}
QTableWidget {{
    gridline-color: {COLORS['border']};
    selection-background-color: {COLORS['accent']};
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background: {COLORS['panel_alt']};
    border: none;
    border-right: 1px solid {COLORS['border']};
    border-bottom: 1px solid {COLORS['border']};
    padding: 5px 8px;
    color: {COLORS['muted']};
}}
QPlainTextEdit, QTextEdit, QLineEdit, QComboBox, QSpinBox {{
    background: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    border-radius: 3px;
    padding: 4px 6px;
    selection-background-color: {COLORS['accent']};
}}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {COLORS['border']};
    border-radius: 3px;
    background: {COLORS['panel']};
}}
QCheckBox::indicator:hover {{
    border-color: {COLORS['accent']};
}}
QCheckBox::indicator:checked {{
    background: {COLORS['accent']};
    border-color: {COLORS['accent']};
    image: url({_asset_url('check.svg')});
}}
QCheckBox::indicator:checked:disabled {{
    background: {COLORS['border']};
    border-color: {COLORS['border']};
    image: none;
}}
QCheckBox:disabled {{
    color: {COLORS['muted']};
}}
QProgressBar {{
    border: 1px solid {COLORS['border']};
    border-radius: 3px;
    background: {COLORS['panel']};
    text-align: center;
    height: 18px;
}}
QProgressBar::chunk {{
    background: {COLORS['accent']};
    border-radius: 2px;
}}
QStatusBar {{
    background: {COLORS['panel']};
    border-top: 1px solid {COLORS['border']};
}}
QStatusBar::item {{ border: none; }}
QToolTip {{
    background: {COLORS['panel_alt']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
}}
"""


def apply_style(app: QApplication) -> None:
    """Install the palette + stylesheet on the running application."""
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, QColor(COLORS["bg"]))
    palette.setColor(palette.ColorRole.Base, QColor(COLORS["panel"]))
    palette.setColor(palette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(palette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(palette.ColorRole.Highlight, QColor(COLORS["accent"]))
    app.setPalette(palette)
    app.setStyleSheet(QSS)


def mono_font() -> QFont:
    """A fixed-width font that is guaranteed to exist on Windows."""
    for family in ("Cascadia Mono", "Consolas", "Courier New"):
        if family in QFontDatabase.families():
            font = QFont(family)
            font.setStyleHint(QFont.StyleHint.Monospace)
            return font
    return QFont()
