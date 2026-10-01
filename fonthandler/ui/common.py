"""Shared page scaffolding: background workers and common widgets."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..gasp import CancellationToken, Cancelled
from .style import COLORS

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from .main_window import AppContext

__all__ = ["Worker", "UiTask", "BasePage", "badge", "confirm", "PathRow"]


@dataclass
class UiTask:
    """What a worker body returns.

    ``message`` lands in the page's status line.  ``on_main`` is invoked on the
    GUI thread once the worker is done -- workers must never touch widgets
    themselves, because Qt is not thread safe.
    """

    message: str = ""
    on_main: Callable[[], None] | None = None


class Worker(QThread):
    """Run ``fn(handle)`` on a thread, forwarding log/progress to the bus.

    ``fn`` receives a :class:`WorkerHandle` giving it a cooperative
    cancellation token plus the log/progress callbacks, so long jobs
    (24 CJK fonts, ~400 MB) stay interruptible instead of freezing the window.
    """

    log = pyqtSignal(str, str)
    progress = pyqtSignal(str, int, int)
    #: ``(ok, status message, optional main-thread callback)``
    completed = pyqtSignal(bool, str, object)

    def __init__(self, fn: Callable[["WorkerHandle"], object], parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn
        self._token = CancellationToken()
        self.setParent(parent)
        # Let Qt reclaim the finished thread object instead of piling up
        # children on the page widget.
        self.finished.connect(self.deleteLater)

    # -- cancellation -----------------------------------------------------
    def cancel(self) -> None:
        self._token.cancel()

    @property
    def cancelled(self) -> bool:
        return self._token.cancelled

    def run(self) -> None:  # pragma: no cover - exercised through the UI
        handle = WorkerHandle(self._token, self)
        try:
            result = self._fn(handle)
        except Cancelled:
            self.log.emit("warn", "操作已取消")
            self.completed.emit(False, "已取消", None)
            return
        except Exception as exc:  # noqa: BLE001
            self.log.emit("error", f"{type(exc).__name__}: {exc}")
            self.log.emit("debug", traceback.format_exc())
            self.completed.emit(False, str(exc), None)
            return
        if isinstance(result, UiTask):
            self.completed.emit(True, result.message, result.on_main)
            return
        self.completed.emit(True, result if isinstance(result, str) else "", None)


class WorkerHandle:
    """What a worker body receives."""

    def __init__(self, token: CancellationToken, worker: Worker) -> None:
        self.token = token
        self._worker = worker

    def log(self, level: str, message: str) -> None:
        self._worker.log.emit(level, message)

    def progress(self, label: str, current: int, total: int) -> None:
        self._worker.progress.emit(label, current, total)

    def check(self) -> None:
        if self.token.cancelled:
            raise Cancelled()


def confirm(parent: QWidget, title: str, text: str, detail: str = "") -> bool:
    """Yes/No guard for destructive actions.  Defaults to *No*.

    Every operation that overwrites, resets permissions or empties a system
    queue goes through here -- the default button is always the safe one.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    yes = box.addButton("确定", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(box.buttons()[-1])
    box.exec()
    return box.clickedButton() is yes


def badge(text: str, color: str | None = None) -> QLabel:
    """A small coloured pill used for the sandbox / admin indicators."""
    colour = color or COLORS["muted"]
    label = QLabel(text)
    label.setStyleSheet(
        f"background: {colour}; color: #ffffff; border-radius: 8px;"
        f"padding: 2px 10px; font-size: 11px; font-weight: 600;"
    )
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


class PathRow(QWidget):
    """A read-only-ish line edit with a Browse button."""

    def __init__(self, caption: str, value: str = "", chooser_title: str = "选择目录",
                 parent: QWidget | None = None) -> None:
        from PyQt6.QtWidgets import QFileDialog, QLineEdit

        super().__init__(parent)
        self.caption = caption
        self.edit = QLineEdit(value)
        self.edit.setPlaceholderText("未设置")
        self.button = QPushButton("浏览…")
        self.button.clicked.connect(self._browse)
        self._chooser_title = chooser_title
        self._file_dialog = QFileDialog

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button, 0)

        outer = QHBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        label = QLabel(caption)
        label.setFixedWidth(72)
        outer.addWidget(label, 0)
        outer.addLayout(row, 1)
        self.setLayout(outer)

    def _browse(self) -> None:
        from pathlib import Path

        start = self.edit.text() or str(Path.home())
        chosen = self._file_dialog.getExistingDirectory(self, self._chooser_title, start)
        if chosen:
            self.edit.setText(chosen)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, text: str) -> None:
        self.edit.setText(text)


class BasePage(QWidget):
    """Common layout: a vertical stack with a progress row pinned at the bottom."""

    def __init__(self, app: "AppContext", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.worker: Worker | None = None
        self._busy = False

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {COLORS['muted']};")

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(12, 12, 12, 12)
        self.body.setSpacing(10)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.cancel_button, 0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addLayout(self.body, 1)
        root.addWidget(self.progress, 0)
        root.addLayout(bottom, 0)

    # -- worker plumbing --------------------------------------------------
    def start(self, fn: Callable[[WorkerHandle], object]) -> None:
        """Run ``fn`` on a worker thread.

        ``fn`` returns a :class:`UiTask` (or a plain status string).  It must
        only gather data; the optional ``on_main`` callback is where the table
        filling happens, back on the GUI thread.
        """
        if self._busy:
            return
        self._busy = True
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText("运行中…")
        self.worker = Worker(fn, parent=self)
        self.worker.log.connect(self.app.log)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()

    def _on_progress(self, label: str, current: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(current, total))
            self.progress.setFormat(f"{label}  %v/%m")
        else:
            self.progress.setRange(0, 0)

    def _on_completed(self, ok: bool, message: str, on_main: object = None) -> None:
        self._busy = False
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if ok else 0)
        self.status.setText(message or ("完成" if ok else "失败"))
        if callable(on_main):
            try:
                on_main()
            except Exception as exc:  # noqa: BLE001
                self.app.log("error", f"刷新界面失败：{exc}")
                ok = False
        self.on_finished(ok, message)

    def on_finished(self, ok: bool, message: str) -> None:
        """Hook for subclasses."""

    def reconfigure(self) -> None:
        """Called when the app settings change."""

    # -- helpers ----------------------------------------------------------
    def group(self, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setSpacing(8)
        self.body.addWidget(box)
        return box, layout

    def buttons(self, layout: QVBoxLayout, *widgets: QWidget) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)

    def actions(self, title: str = "操作") -> QVBoxLayout:
        """Create the trailing button bar group (called last, see ``insertWidget``)."""
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setSpacing(8)
        self.body.addWidget(box)
        return layout
