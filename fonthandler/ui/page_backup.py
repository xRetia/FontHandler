"""Page 3 -- Backup and restore."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from .. import pipeline
from .common import BasePage, PathRow, UiTask, WorkerHandle, confirm

__all__ = ["BackupPage"]


class BackupPage(BasePage):
    def __init__(self, app, parent=None) -> None:
        super().__init__(app, parent)

        box, layout = self.group("备份包")
        self.backup_dir = PathRow("备份目录", "", "选择备份目录")
        layout.addWidget(self.backup_dir)

        opts, olayout = self.group("选项")
        self.include_all = QCheckBox("备份全部 CJK 字体，包括目标目录里没有的")
        olayout.addWidget(self.include_all)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["备份包", "创建时间", "字体数", "大小"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self.table, 1)

        self.refresh_button = QPushButton("刷新列表")
        self.backup_button = QPushButton("创建备份")
        self.backup_button.setProperty("accent", True)
        self.restore_button = QPushButton("恢复选中")
        self.restore_button.setProperty("danger", True)
        self.explore_button = QPushButton("打开备份目录")

        self.buttons(
            self.actions(),
            self.refresh_button,
            self.backup_button,
            self.restore_button,
            self.explore_button,
        )

        self.refresh_button.clicked.connect(self.refresh_list)
        self.backup_button.clicked.connect(self.backup)
        self.restore_button.clicked.connect(self.restore)
        self.explore_button.clicked.connect(self.explore)

        #: Snapshotted on the GUI thread, consumed by the worker.
        self._include_all = False

        self.reconfigure()

    def reconfigure(self) -> None:
        from .. import config

        self.backup_dir.set_value(str(config.APP_HOME / "backups"))

    def refresh_list(self) -> None:
        self.start(self._refresh_job)

    def _refresh_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        ctx.progress = handle.progress
        packages = pipeline.list_backups(ctx)
        return UiTask(
            f"共 {len(packages)} 个备份包",
            lambda: self._fill(packages),
        )

    def _fill(self, packages) -> None:
        import time

        self.table.setRowCount(len(packages))
        for i, pkg in enumerate(packages):
            self.table.setItem(i, 0, QTableWidgetItem(pkg.path.name))
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pkg.created_at))
            self.table.setItem(i, 1, QTableWidgetItem(ts))
            self.table.setItem(i, 2, QTableWidgetItem(str(pkg.count)))
            mb = pkg.total_size / 1024 / 1024
            self.table.setItem(i, 3, QTableWidgetItem(f"{mb:.2f} MB"))

    def backup(self) -> None:
        self._include_all = self.include_all.isChecked()
        self.start(self._backup_job)

    def _backup_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        ctx.log = handle.log
        ctx.progress = handle.progress
        names = ctx.target_names() if self._include_all else None
        pkg = pipeline.run_backup(ctx, names=names)
        packages = pipeline.list_backups(ctx)
        return UiTask(
            f"备份完成：{pkg.path.name}，{pkg.count} 个字体",
            lambda: self._fill(packages),
        )

    def restore(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.app.log("warn", "请先选择一个备份包")
            return
        idx = rows[0].row()
        package = self.table.item(idx, 0)
        name = package.text() if package else f"#{idx}"
        if not confirm(self, "恢复备份",
                       f"将用备份包 {name} 覆盖目标目录中的字体。",
                       "被覆盖的当前版本会先自动备份。是否继续？"):
            return
        self.start(lambda h: self._restore_job(h, idx))

    def _restore_job(self, handle: WorkerHandle, idx: int) -> str:
        ctx = self.app.context()
        ctx.log = handle.log
        ctx.progress = handle.progress
        pkgs = pipeline.list_backups(ctx)
        if idx >= len(pkgs):
            return "备份包不存在"
        pkg = pkgs[idx]
        handle.log("warn", f"正在恢复备份 {pkg.path.name}，{pkg.count} 个字体")
        results = pipeline.run_restore(ctx, pkg, progress=lambda n: handle.progress(n, 0, pkg.count))
        failed = sum(1 for r in results if not r.ok)
        packages = pipeline.list_backups(ctx)
        return UiTask(
            f"恢复完成：{len(results)} 个条目，失败 {failed}",
            lambda: self._fill(packages),
        )

    def explore(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        from .. import config

        path = config.APP_HOME / "backups"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def on_finished(self, ok: bool, message: str) -> None:
        self.app.refresh()
