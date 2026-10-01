"""Page 4 -- TrustedInstaller & ACL inspection."""

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
from .common import BasePage, UiTask, WorkerHandle, confirm

__all__ = ["AclPage"]


class AclPage(BasePage):
    def __init__(self, app, parent=None) -> None:
        super().__init__(app, parent)

        opts, olayout = self.group("选项")
        self.cjk_only = QCheckBox("仅列出 CJK 白名单")
        self.cjk_only.setChecked(True)
        olayout.addWidget(self.cjk_only)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["字体文件", "所有者", "SDDL"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self.table, 1)

        self.scan_button = QPushButton("扫描权限")
        self.scan_button.setProperty("accent", True)
        self.reset_button = QPushButton("重置选中文件的 DACL 为纯继承")
        self.cleanup_button = QPushButton("清理残留 .new 文件")

        self.buttons(self.actions(), self.scan_button, self.reset_button, self.cleanup_button)

        self.scan_button.clicked.connect(self.scan)
        self.reset_button.clicked.connect(self.reset)
        self.cleanup_button.clicked.connect(self.cleanup)

        self.reconfigure()

    def reconfigure(self) -> None:
        self.cjk_only.setChecked(self.app.settings.cjk_only)

    def scan(self) -> None:
        self.start(self._scan_job)

    def _scan_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        ctx.log = handle.log
        ctx.progress = handle.progress
        rows = pipeline.scan_acl(ctx)
        return UiTask(
            f"扫描完成：{len(rows)} 个文件",
            lambda: self._fill(rows),
        )

    def _fill(self, rows) -> None:
        self.table.setRowCount(len(rows))
        for i, (name, owner, sddl) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            self.table.setItem(i, 1, QTableWidgetItem(owner.rsplit("\\", 1)[-1] if "\\" in owner else owner))
            preview = sddl[:120] + ("…" if len(sddl) > 120 else "")
            self.table.setItem(i, 2, QTableWidgetItem(preview))

    def _selected_names(self) -> list[str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return []
        return [self.table.item(r.row(), 0).text() for r in rows]

    def reset(self) -> None:
        names = self._selected_names()
        if not names:
            self.app.log("warn", "请先在表中选择要重置权限的字体行")
            return
        if not confirm(self, "重置 DACL",
                       f"将把 {len(names)} 个字体的 DACL 重置为纯继承并还原 TrustedInstaller 所有者。",
                       "\n".join(names[:8]) + ("…" if len(names) > 8 else "")):
            return
        self.start(lambda h: self._reset_job(h, names))

    def _reset_job(self, handle: WorkerHandle, names: list[str]) -> UiTask:
        from .. import acl

        ctx = self.app.context()
        done = failed = 0
        for index, name in enumerate(names, 1):
            handle.check()
            handle.progress(name, index, len(names))
            path = ctx.target_dir / name
            if not path.exists():
                handle.log("warn", f"{name}: 文件不存在，跳过")
                continue
            try:
                acl.reset_to_inherited(path, ctx.security)
                acl.set_owner_ti(path, ctx.security)
                done += 1
                handle.log("info", f"{name}: 已重置为纯继承 + TrustedInstaller")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                handle.log("error", f"{name}: {exc}")
        rows = pipeline.scan_acl(ctx)
        return UiTask(f"重置完成：{done} 个成功，{failed} 个失败", lambda: self._fill(rows))

    def cleanup(self) -> None:
        if not confirm(self, "清理残留 .new 文件",
                       "将删除目标目录中所有 <字体名>.new 暂存文件。",
                       "这些文件是上一次替换留下的残留。"):
            return
        self.start(self._cleanup_job)

    def _cleanup_job(self, handle: WorkerHandle) -> str:
        ctx = self.app.context()
        removed = ctx.engine().cleanup_staged()
        for name in removed:
            handle.log("info", f"已删除 {name}")
        return f"清理残留：{len(removed)} 个 .new 文件"

    def on_finished(self, ok: bool, message: str) -> None:
        self.app.refresh()
