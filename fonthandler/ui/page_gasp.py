"""Page 1 -- GaspHack: generate the patched fonts from the system originals."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from .. import config, gasp, sfnt
from .common import BasePage, PathRow, UiTask, WorkerHandle, confirm
from .style import COLORS

__all__ = ["GaspHackPage"]

_STATUS = {
    "hacked": ("已打补丁", COLORS["ok"]),
    "unchanged": ("已带补丁", COLORS["muted"]),
    "error": ("失败", COLORS["err"]),
    "skipped": ("跳过", COLORS["muted"]),
}


class GaspHackPage(BasePage):
    """Reproduces the original ``GaspHack_v2_MOD.bat`` split/merge pipeline."""

    def __init__(self, app, parent=None) -> None:
        super().__init__(app, parent)

        box, layout = self.group("输入 / 输出")
        self.source_row = PathRow("源目录", "", "选择 GaspHack 输出目录")
        self.target_row = PathRow("系统字体", str(config.SYSTEM_FONTS_DIR), "选择字体目录")
        layout.addWidget(self.source_row)
        layout.addWidget(self.target_row)

        opts, olayout = self.group("选项")
        # Whitelist comes straight from FontReplace.ps1; see config.CJK_WHITELIST.
        self.cjk_only = QCheckBox("仅处理 CJK 白名单")
        self.cjk_only.setChecked(True)
        olayout.addWidget(self.cjk_only)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["字体文件", "状态", "大小", "家族名", "输出", "说明"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self.table, 1)

        self.run_button = QPushButton("生成 GaspHack 字体")
        self.run_button.setProperty("accent", True)
        self.run_button.clicked.connect(self.run)

        self.verify_button = QPushButton("校验输出")
        self.verify_button.clicked.connect(self.verify)

        self.open_button = QPushButton("打开输出目录")
        self.open_button.clicked.connect(self._open_output)

        self.buttons(self.actions(), self.run_button, self.verify_button, self.open_button)

        self.reconfigure()

    # -- settings sync ---------------------------------------------------
    def reconfigure(self) -> None:
        settings = self.app.settings
        self.source_row.set_value(str(settings.resolved_source_dir()))
        self.target_row.set_value(str(settings.resolved_target_dir()))
        self.cjk_only.setChecked(settings.cjk_only)

    def _sync_settings(self) -> None:
        settings = self.app.settings
        settings.source_dir = self.source_row.value()
        settings.target_dir = self.target_row.value()
        settings.cjk_only = self.cjk_only.isChecked()

    # -- actions ---------------------------------------------------------
    def run(self) -> None:
        self._sync_settings()
        self.app.save_settings()
        source = Path(self.source_row.value())
        target = Path(self.target_row.value())
        cjk_only = self.cjk_only.isChecked()
        if cjk_only and not confirm(self, "生成 GaspHack 字体",
                                    f"将读取 {target} 中的白名单字体，写入 {source}。",
                                    "目标目录不会被修改。是否继续？"):
            return
        self.table.setRowCount(0)
        self.start(lambda h: self._job(h, target, source, cjk_only))

    def _job(self, handle: WorkerHandle, target: Path, source: Path,
             cjk_only: bool) -> UiTask:
        source.mkdir(parents=True, exist_ok=True)

        files = None
        if cjk_only:
            files = [n for n in config.CJK_WHITELIST if (target / n).exists()]
            missing = [n for n in config.CJK_WHITELIST if not (target / n).exists()]
            if missing:
                handle.log("debug", "系统缺少: " + ", ".join(missing))
            if not files:
                handle.log("error", "白名单里没有任何字体存在于目标目录")
                return UiTask("没有可处理的字体")

        handle.log("info", f"读取 {target}")
        handle.log("info", f"写入 {source}")
        total = len(files) if files else max(1, len(list(target.glob("*.tt*"))))
        index = 0

        def report(name: str, stage: str) -> None:
            nonlocal index
            index += 1
            handle.progress(name, index, total)

        report_holder = gasp.run_batch(
            input_dir=target,
            output_dir=source,
            files=files,
            token=handle.token,
            progress=report,
        )

        rows = [self._describe(item, source) for item in report_holder.results]
        handle.log(
            "info",
            f"完成：{report_holder.total} 个文件，"
            f"打补丁 {report_holder.hacked}，已带补丁 {report_holder.unchanged}，"
            f"失败 {report_holder.failed}",
        )
        return UiTask(
            f"{report_holder.total} 个字体",
            lambda: self._fill(rows),
        )

    @staticmethod
    def _describe(item, source: Path) -> dict:
        """Everything the table needs, gathered off the GUI thread."""
        families: list[str] = []
        try:
            families = gasp.read_family_names((source / item.name).read_bytes())
        except OSError:
            families = []
        note = item.message
        if item.already_hacked:
            note = "原文件已带 gasp 补丁；" + note
        delta = item.size - item.input_size
        if delta:
            note = f"{note}  (Δ{delta:+d} 字节)".strip()
        return {
            "name": item.name,
            "status": item.status,
            "size": item.size,
            "faces": item.faces,
            "families": ", ".join(families) or "-",
            "note": note or "-",
        }

    def _fill(self, rows: list[dict]) -> None:
        from PyQt6.QtGui import QBrush, QColor

        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            label, colour = _STATUS.get(row["status"], (row["status"], COLORS["muted"]))
            status = QTableWidgetItem(label)
            status.setForeground(QBrush(QColor(colour)))

            self.table.setItem(index, 0, QTableWidgetItem(row["name"]))
            self.table.setItem(index, 1, status)
            self.table.setItem(index, 2, QTableWidgetItem(f"{row['size']:,}"))
            self.table.setItem(index, 3, QTableWidgetItem(row["families"]))
            self.table.setItem(index, 4, QTableWidgetItem(f"{row['faces']}"))
            self.table.setItem(index, 5, QTableWidgetItem(row["note"]))

    def verify(self) -> None:
        source = Path(self.source_row.value())
        self.start(lambda h: self._verify_job(h, source))

    def _verify_job(self, handle: WorkerHandle, source: Path) -> str:
        files = [n for n in config.CJK_WHITELIST if (source / n).exists()]
        if not files:
            handle.log("warn", "输出目录里没有白名单字体")
            return "无可校验文件"

        bad = 0
        for index, name in enumerate(files, 1):
            handle.check()
            handle.progress(name, index, len(files))
            data = (source / name).read_bytes()
            faces = gasp.split_ttc(data) if sfnt.is_ttc(data) else []
            detail = f"{len(faces)} 个 face" if faces else "单字体"
            every = all(gasp.has_gasp_hack(sfnt.build_sfnt(face)) for face in faces) if faces \
                else gasp.has_gasp_hack(data)
            if every:
                handle.log("info", f"{name}: gasp 补丁正确，{detail}")
            else:
                bad += 1
                handle.log("error", f"{name}: gasp 表不正确，{detail}")
        return f"{len(files) - bad}/{len(files)} 通过"

    def _open_output(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        target = Path(self.source_row.value())
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def on_finished(self, ok: bool, message: str) -> None:
        self.app.refresh()
