"""Page 2 -- Font replacement: backup + hot-replace + reboot queue."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from .. import pipeline
from ..pipeline import ScanRow
from ..replace import ReplaceStatus
from .common import BasePage, UiTask, WorkerHandle, confirm
from .style import COLORS

__all__ = ["ReplacePage"]


class ReplacePage(BasePage):
    def __init__(self, app, parent=None) -> None:
        super().__init__(app, parent)

        box, layout = self.group("目标")
        self.source_dir = QLabel("")
        self.target_dir = QLabel("")
        self.source_dir.setWordWrap(True)
        self.target_dir.setWordWrap(True)
        self.sandbox_badge = QLabel("")
        self.sandbox_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.admin_badge = QLabel("")
        self.admin_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heads = QGridLayout()
        heads.setContentsMargins(0, 0, 0, 0)
        heads.setHorizontalSpacing(12)
        heads.addWidget(self.source_dir, 0, 0)
        heads.addWidget(self.target_dir, 0, 1)
        badges = QHBoxLayout()
        badges.setContentsMargins(0, 0, 0, 0)
        badges.setSpacing(8)
        badges.addWidget(self.sandbox_badge)
        badges.addWidget(self.admin_badge)
        self.admin_badge.mouseReleaseEvent = self._ask_elevation
        badges.addStretch(1)
        layout.addLayout(heads)
        layout.addLayout(badges)

        opts, olayout = self.group("选项")
        self.cjk_only = QCheckBox("仅处理 CJK 白名单")
        self.backup = QCheckBox("替换前自动备份")
        self.backup.setChecked(True)
        self.force = QCheckBox("强制替换，忽略内容一致")
        self.cache = QCheckBox("替换后清理字体缓存")
        self.notify = QCheckBox("广播 WM_FONTCHANGE")
        self.ti = QCheckBox("替换后还原到 TrustedInstaller")
        self.ti.setChecked(True)
        self.reset_acl = QCheckBox("重置 DACL 为纯继承，清除冗余显式 ACE")
        self.reset_acl.setChecked(True)
        self.queue = QCheckBox("文件被占用时排入重启队列")
        self.queue.setChecked(True)

        switches = QGridLayout()
        switches.setContentsMargins(0, 0, 0, 0)
        switches.setHorizontalSpacing(16)
        switches.setVerticalSpacing(4)
        for index, box_widget in enumerate((self.cjk_only, self.backup, self.force,
                                            self.cache, self.notify, self.ti,
                                            self.reset_acl, self.queue)):
            switches.addWidget(box_widget, index // 2, index % 2)
        olayout.addLayout(switches)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["字体文件", "源存在", "目标存在", "已替换", "内容一致", "家族名"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self.table, 1)

        self.scan_button = QPushButton("扫描")
        self.replace_button = QPushButton("开始替换")
        self.replace_button.setProperty("accent", True)
        self.backup_button = QPushButton("手动备份")
        self.pending_button = QPushButton("查看重启队列")

        self.buttons(
            self.actions(),
            self.scan_button,
            self.replace_button,
            self.backup_button,
            self.pending_button,
        )

        self.scan_button.clicked.connect(self.scan)
        self.replace_button.clicked.connect(self.replace)
        self.backup_button.clicked.connect(self.backup_manual)
        self.pending_button.clicked.connect(self.app.show_pending)

        #: Snapshotted on the GUI thread, consumed by the worker.
        self._force = False
        self._backup = True

        self.reconfigure()

    def reconfigure(self) -> None:
        settings = self.app.settings
        self.cjk_only.setChecked(settings.cjk_only)
        self.backup.setChecked(settings.backup_before_replace)
        self.cache.setChecked(settings.purge_font_cache)
        self.notify.setChecked(settings.notify_font_change)
        self.ti.setChecked(settings.restore_trusted_installer)
        self.reset_acl.setChecked(settings.reset_acl_to_inherited)
        self.queue.setChecked(settings.queue_on_lock)
        self.source_dir.setText(f"源：{settings.resolved_source_dir()}")
        self.target_dir.setText(f"目标：{settings.resolved_target_dir()}")
        if settings.is_sandbox():
            self.sandbox_badge.setText("沙盒模式：不写入 C:\\Windows\\Fonts")
            self.sandbox_badge.setStyleSheet(f"background: {COLORS['sandbox']}; color:#fff; padding:4px 8px; border-radius:6px;")
        else:
            self.sandbox_badge.setText("")
            self.sandbox_badge.setStyleSheet("")
        if self.app.admin:
            self.admin_badge.setText("已是管理员")
            self.admin_badge.setToolTip("")
            self.admin_badge.setCursor(Qt.CursorShape.ArrowCursor)
            self.admin_badge.setStyleSheet(f"background: {COLORS['ok']}; color:#fff; padding:4px 8px; border-radius:6px;")
        else:
            self.admin_badge.setText("建议以管理员身份运行（点击提权）")
            self.admin_badge.setToolTip("点击或按 Ctrl+Shift+A 请求管理员权限")
            self.admin_badge.setCursor(Qt.CursorShape.PointingHandCursor)
            self.admin_badge.setStyleSheet(
                f"background: {COLORS['warn']}; color:#111; padding:4px 8px;"
                " border-radius:6px; text-decoration: underline;"
            )

    def _ask_elevation(self, event) -> None:
        """Clicking the amber badge is the same as Ctrl+Shift+A."""
        if not self.app.admin:
            self.app.request_elevation()

    def _sync(self) -> None:
        settings = self.app.settings
        settings.cjk_only = self.cjk_only.isChecked()
        settings.backup_before_replace = self.backup.isChecked()
        settings.purge_font_cache = self.cache.isChecked()
        settings.notify_font_change = self.notify.isChecked()
        settings.restore_trusted_installer = self.ti.isChecked()
        settings.reset_acl_to_inherited = self.reset_acl.isChecked()
        settings.queue_on_lock = self.queue.isChecked()
        self.app.save_settings()

    def scan(self) -> None:
        self._sync()
        self.start(self._scan_job)

    def _scan_job(self, handle: WorkerHandle) -> UiTask:
        ctx = self.app.context()
        ctx.log = handle.log
        ctx.progress = handle.progress
        rows = pipeline.scan_targets(ctx)
        return UiTask(
            f"扫描完成：{len(rows)} 个字体",
            lambda: self._fill(rows),
        )

    def _fill(self, rows: list[ScanRow]) -> None:
        from PyQt6.QtGui import QBrush, QColor

        highlight = QColor(COLORS["warn"])
        highlight.setAlpha(48)
        brush = QBrush(highlight)
        patched_brush = QBrush(QColor(COLORS["ok"]))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # A font is only "to do" when it is neither patched on disk nor
            # already staged for the next boot; an already-replaced font must
            # not stay highlighted even if the source was regenerated since.
            pending = (r.present_source and r.present_target
                       and not r.identical and not r.target_patched)
            if r.target_patched:
                replaced = "是"
            elif r.queued:
                replaced = "已排队"
            else:
                replaced = "否"
            cells = [
                QTableWidgetItem(r.name),
                QTableWidgetItem("是" if r.present_source else "否"),
                QTableWidgetItem("是" if r.present_target else "否"),
                QTableWidgetItem(replaced),
                QTableWidgetItem("是" if r.identical else "否"),
                QTableWidgetItem(r.families or "-"),
            ]
            for col, cell in enumerate(cells):
                if pending:
                    cell.setBackground(brush)
                self.table.setItem(i, col, cell)
            if r.target_patched:
                cells[3].setForeground(patched_brush)
                cells[3].setToolTip("目标字体已带 GaspHack 补丁")
            elif r.queued:
                cells[3].setToolTip("补丁字体已排入重启队列，下次开机生效")

    def replace(self) -> None:
        self._sync()
        force = self.force.isChecked()
        ctx = self.app.context()
        todo = [n for n in ctx.target_names()
                if (ctx.source_dir / n).exists() and (ctx.target_dir / n).exists()]
        if not todo:
            self.app.log("warn", "没有可替换的字体，源与目标都存在的字体为空")
            return
        where = "沙盒目录" if ctx.is_sandbox else str(ctx.target_dir)
        detail = f"目标：{where}\n备份：{'先备份' if self.backup.isChecked() else '不备份'}"
        if not confirm(self, "开始替换字体", f"将替换 {len(todo)} 个字体。", detail):
            return
        self._force = force
        self._backup = self.backup.isChecked()
        self.start(self._replace_job)

    def _replace_job(self, handle: WorkerHandle) -> str:
        ctx = self.app.context()
        ctx.log = handle.log
        ctx.progress = handle.progress
        results = pipeline.run_replace(ctx, force=self._force, backup_first=self._backup)
        hot = sum(1 for r in results if r.status is ReplaceStatus.HOT)
        queued = sum(1 for r in results if r.status is ReplaceStatus.QUEUED)
        skipped = sum(1 for r in results if r.status.is_skip)
        failed = sum(1 for r in results if r.status is ReplaceStatus.FAILED)
        # The purge (and whether the cache service may restart right away) is
        # decided from the results: a font still in the reboot queue only lands
        # on disk during the next boot, so restarting the service now would
        # rebuild the cache from the old files.
        pipeline.purge_cache_after_replace(
            results, log=handle.log,
            purge_enabled=ctx.settings.purge_font_cache,
            is_sandbox=ctx.is_sandbox,
        )
        if ctx.settings.notify_font_change and not ctx.is_sandbox:
            try:
                from .. import livefont

                livefont.broadcast_font_change()
                handle.log("info", "已广播 WM_FONTCHANGE")
            except Exception as exc:  # noqa: BLE001
                handle.log("warn", f"广播字体变化失败：{exc}")
        if ctx.is_sandbox:
            handle.log("debug", "沙盒模式：已跳过字体缓存清理与 WM_FONTCHANGE 广播")
        return f"完成：热替换 {hot}，重启队列 {queued}，跳过 {skipped}，失败 {failed}"

    def backup_manual(self) -> None:
        self._sync()
        self.start(self._backup_job)

    def _backup_job(self, handle: WorkerHandle):
        ctx = self.app.context()
        ctx.log = handle.log
        ctx.progress = handle.progress
        pkg = pipeline.run_backup(ctx)
        return f"备份完成：{pkg.path.name}，{pkg.count} 个字体"

    def on_finished(self, ok: bool, message: str) -> None:
        self.app.refresh()
