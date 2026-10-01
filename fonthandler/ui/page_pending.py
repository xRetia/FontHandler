"""Page 5 -- Reboot queue (PendingFileRenameOperations)."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from .. import pipeline
from .common import BasePage, UiTask, WorkerHandle, confirm

__all__ = ["PendingPage"]


class PendingPage(BasePage):
    def __init__(self, app, parent=None) -> None:
        super().__init__(app, parent)

        self.hint = QLabel(
            "重启后才会执行的文件替换队列。"
            "只有本工具写入的 *.new 条目会被「清理本工具条目」按钮移除。"
        )
        self.hint.setWordWrap(True)
        self.body.addWidget(self.hint)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["源 \\\\??\\...", "目标 \\\\??\\..."])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self.table, 1)

        self.refresh_button = QPushButton("刷新")
        self.clear_ours_button = QPushButton("清理本工具写入的 *.new 条目")
        self.clear_all_button = QPushButton("清空全部队列，危险")
        self.clear_all_button.setProperty("danger", True)

        self.buttons(self.actions(), self.refresh_button, self.clear_ours_button,
                     self.clear_all_button)

        self.refresh_button.clicked.connect(self.refresh)
        self.clear_ours_button.clicked.connect(self.clear_ours)
        self.clear_all_button.clicked.connect(self.clear_all)

    def refresh(self) -> None:
        self.start(self._refresh_job)

    def _refresh_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        ctx.progress = handle.progress
        pending = pipeline.list_pending(ctx)
        return UiTask(
            f"队列中 {len(pending)} 项",
            lambda: self._fill(pending),
        )

    def _fill(self, pending) -> None:
        self.table.setRowCount(len(pending))
        for i, entry in enumerate(pending):
            self.table.setItem(i, 0, QTableWidgetItem(entry.source))
            self.table.setItem(i, 1, QTableWidgetItem(entry.dest))

    def clear_ours(self) -> None:
        self.start(self._clear_ours_job)

    def _clear_ours_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        removed = pipeline.clear_pending(ctx)
        handle.log("info", f"移除了 {removed} 项本工具写入的队列条目")
        pending = pipeline.list_pending(ctx)
        return UiTask(f"已清理 {removed} 项", lambda: self._fill(pending))

    def clear_all(self) -> None:
        ctx = self.app.context()
        count = len(pipeline.list_pending(ctx))
        if not count:
            self.app.log("info", "队列已经是空的")
            return
        if not confirm(self, "清空重启队列",
                       f"将删除 PendingFileRenameOperations 中的全部 {count} 项。",
                       "其中可能包含 Windows 更新等其它程序的待重启操作，"
                       "删除后它们将不会执行。"):
            return
        self.start(self._clear_all_job)

    def _clear_all_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        from .. import session

        before = len(session.read_pending(ctx.registry))
        session.write_pending([], ctx.registry)
        handle.log("warn", f"清空 PendingFileRenameOperations，共 {before} 项")
        remaining = session.read_pending(ctx.registry)
        return UiTask(f"清空了 {before} 项", lambda: self._fill(remaining))
