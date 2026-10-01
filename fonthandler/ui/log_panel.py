"""Collapsible log panel shared by every page."""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .style import COLORS, mono_font

__all__ = ["LogPanel", "LogView", "LEVELS"]

#: Filter order: showing level N also shows everything less severe than N.
LEVELS = ("debug", "info", "warn", "error")

_LEVEL_COLORS = {
    "debug": COLORS["muted"],
    "info": COLORS["text"],
    "warn": COLORS["warn"],
    "error": COLORS["err"],
}


class LogView(QPlainTextEdit):
    """Read-only, colour-coded, line-capped log output."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def append_line(self, level: str, message: str, stamp: str | None = None,
                    scroll: bool = True) -> None:
        stamp = stamp or time.strftime("%H:%M:%S")
        prefix = {"debug": "·", "info": " ", "warn": "!", "error": "×"}.get(level, " ")
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt_time = QTextCharFormat()
        fmt_time.setForeground(QColor(COLORS["muted"]))
        fmt_level = QTextCharFormat()
        fmt_level.setForeground(QColor(_LEVEL_COLORS.get(level, COLORS["text"])))

        cursor.insertText(f"{stamp} {prefix} ", fmt_time)
        cursor.insertText(message, fmt_level)
        cursor.insertBlock()
        self.setTextCursor(cursor)
        if scroll:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def render(self, entries, scroll: bool = True) -> None:
        """Redraw the whole buffer (used when the filter changes)."""
        self.clear()
        for level, stamp, message in entries:
            self.append_line(level, message, stamp=stamp, scroll=False)
        if scroll:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class LogPanel(QWidget):
    """A collapsible panel: header row (badge / filter / buttons) + log view.

    Lines are buffered in memory and the view is re-rendered whenever the
    filter changes, so raising the level back up does not lose history.
    """

    cleared = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._filter = "info"
        self._lines: list[tuple[str, str, str]] = []

        self.toggle = QToolButton()
        self.toggle.setText("▾")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setFixedWidth(24)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setToolTip("折叠 / 展开日志")
        self.toggle.toggled.connect(self._on_toggle)

        self.title = QLabel("日志")
        self.title.setStyleSheet(f"color: {COLORS['muted']}; font-weight: 600;")

        self.level_box = QComboBox()
        self.level_box.addItems(list(LEVELS))
        self.level_box.setCurrentText("info")
        self.level_box.setFixedWidth(90)
        self.level_box.setToolTip("显示该级别及更严重级别的日志")
        self.level_box.currentTextChanged.connect(self._set_filter)

        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)

        self.copy_button = QPushButton("复制")
        self.copy_button.setFixedWidth(64)
        self.copy_button.clicked.connect(self._copy)

        self.clear_button = QPushButton("清空")
        self.clear_button.setFixedWidth(64)
        self.clear_button.clicked.connect(self.clear)
        self.clear_button.clicked.connect(self.cleared)

        header = QHBoxLayout()
        header.setContentsMargins(6, 4, 6, 4)
        header.setSpacing(8)
        header.addWidget(self.toggle)
        header.addWidget(self.title)
        header.addSpacing(12)
        header.addWidget(QLabel("级别"))
        header.addWidget(self.level_box)
        header.addWidget(self.autoscroll)
        header.addStretch(1)
        header.addWidget(self.copy_button)
        header.addWidget(self.clear_button)

        self.view = LogView()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self.view)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    # -- behaviour --------------------------------------------------------
    def _visible(self, level: str) -> bool:
        """True when ``level`` passes the current filter (and everything below).

        Unknown levels are shown rather than raising: a caller passing a typo
        must not take the whole window down mid-startup.
        """
        if level not in LEVELS:
            return True
        return LEVELS.index(level) >= LEVELS.index(self._filter)

    def _set_filter(self, level: str) -> None:
        if level not in LEVELS:
            return
        self._filter = level
        self._rerender()

    def _rerender(self) -> None:
        self.view.render([entry for entry in self._lines if self._visible(entry[0])],
                         scroll=self.autoscroll.isChecked())

    def _on_toggle(self, expanded: bool) -> None:
        self.toggle.setText("▾" if expanded else "▸")
        self.view.setVisible(expanded)

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.view.toPlainText())

    def clear(self) -> None:
        self._lines.clear()
        self.view.clear()

    def log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        if len(self._lines) >= 5000:
            del self._lines[:len(self._lines) - 4999]
        known = level in LEVELS
        self._lines.append((level, stamp, message))
        if self._visible(level):
            shown = level if known else "info"
            self.view.append_line(shown, message, stamp=stamp,
                                  scroll=self.autoscroll.isChecked())
