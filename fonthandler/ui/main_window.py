"""Main window and application bootstrapping."""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
)

from .. import acl, config, elevation, pipeline, registry, systeminfo
from .log_panel import LogPanel
from .page_acl import AclPage
from .page_backup import BackupPage
from .page_gasp import GaspHackPage
from .page_pending import PendingPage
from .page_replace import ReplacePage
from .single_instance import SingleInstance
from .style import apply_style

__all__ = ["FontHandlerApp", "main"]

#: Local-socket name; keeps a second launch from opening another window.
APP_KEY = "FontHandler-2.0-pyqt-single-instance"


class AppContext:
    """Lightweight holder passed to pages."""

    def __init__(self, window: "FontHandlerApp") -> None:
        self.window = window

    @property
    def settings(self):
        return self.window.settings

    @property
    def admin(self) -> bool:
        return self.window.admin

    @property
    def security(self) -> acl.SecurityBackend:
        return self.window.security

    @property
    def registry(self) -> registry.RegistryBackend:
        return self.window.registry

    def save_settings(self) -> None:
        self.window.save_settings()

    def request_elevation(self) -> None:
        """Ask for UAC. Pages hold an AppContext, not the window."""
        self.window.request_elevation()

    def log(self, level: str, message: str) -> None:
        self.window.log(level, message)

    def context(self):
        ctx = pipeline.default_context(self.settings)
        ctx.security = self.window.security
        ctx.registry = self.window.registry
        ctx.log = self.log
        return ctx

    def refresh(self) -> None:
        self.window.refresh_all()

    def show_pending(self) -> None:
        self.window.show_pending_tab()


class FontHandlerApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            f"{config.APP_NAME} {config.APP_VERSION} — GaspHack + 字体替换")
        self.resize(1280, 840)

        self.settings = config.load_settings()
        self.security = acl.get_backend()
        self.registry = registry.get_backend()
        self.host = systeminfo.get_host_info()
        self.admin = elevation.is_admin()
        # Set by main(); the elevation action has to release it first.
        self.guard = None

        # Make sure the directories exist.
        self.settings.resolved_source_dir().mkdir(parents=True, exist_ok=True)
        config.APP_HOME.mkdir(parents=True, exist_ok=True)
        (config.APP_HOME / "backups").mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self.refresh_all()
        self._log_startup()
        self._initial_load()

    @classmethod
    def for_testing(cls, settings: config.Settings,
                   security: acl.SecurityBackend,
                   registry_backend: registry.RegistryBackend) -> "FontHandlerApp":
        """Build the whole window on sandbox backends (no real system paths)."""
        self = cls.__new__(cls)
        QMainWindow.__init__(self)
        self.setWindowTitle(f"{config.APP_NAME} {config.APP_VERSION} (test)")
        self.resize(1024, 700)
        self.settings = settings
        self.security = security
        self.registry = registry_backend
        self.host = systeminfo.get_host_info()
        self.admin = False
        self.guard = None
        self._build_ui()
        self.refresh_all()
        return self

    def _initial_load(self) -> None:
        """Kick off the two read-only listings the user sees right away."""
        self.page_backup.refresh_list()
        self.page_pending.refresh()

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.setCentralWidget(self.tabs)

        self.ctx = AppContext(self)

        self.page_gasp = GaspHackPage(self.ctx)
        self.page_replace = ReplacePage(self.ctx)
        self.page_backup = BackupPage(self.ctx)
        self.page_acl = AclPage(self.ctx)
        self.page_pending = PendingPage(self.ctx)

        self.tabs.addTab(self.page_gasp, "GaspHack")
        self.tabs.addTab(self.page_replace, "替换字体")
        self.tabs.addTab(self.page_backup, "备份恢复")
        self.tabs.addTab(self.page_acl, "TrustedInstaller")
        self.tabs.addTab(self.page_pending, "重启队列")

        self.log_panel = LogPanel()
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self.dock_log = QDockWidget("日志", self)
        self.dock_log.setObjectName("log")
        self.dock_log.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.dock_log.setWidget(self.log_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_log)

        self._build_menu()

    def _build_menu(self) -> None:
        """A status-bar button rather than a one-item menu bar.

        A menu bar holding a single entry reads as a broken shell, and the
        elevation prompt is a one-off action, not a place to browse.
        """
        self.elevate_button = QPushButton("以管理员身份重启")
        self.elevate_button.setToolTip("请求 UAC 提权（Ctrl+Shift+A）")
        self.elevate_button.setShortcut("Ctrl+Shift+A")
        self.elevate_button.clicked.connect(self.request_elevation)
        self.status.addPermanentWidget(self.elevate_button)

        self.privileges_button = QPushButton("权限")
        self.privileges_button.setToolTip("查看当前已启用的特权")
        self.privileges_button.clicked.connect(self.show_privileges)
        self.status.addPermanentWidget(self.privileges_button)

    def request_elevation(self) -> None:
        """Ask for UAC, restart elevated, and hand over cleanly."""
        if self.admin:
            QMessageBox.information(
                self, "已经是管理员",
                "当前进程已经以管理员身份运行，无需重启。",
            )
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("需要管理员权限")
        box.setText("替换系统字体需要管理员权限。")
        box.setInformativeText(
            "Windows 即将弹出 UAC 提示，请选择「是」。\n\n"
            "重启后会保留当前设置，未完成的操作不会自动继续。",
        )
        yes = box.addButton("请求管理员权限", QMessageBox.ButtonRole.YesRole)
        box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self._elevate()

    def _elevate(self) -> None:
        # Release the single-instance guard first. The elevated copy claims the
        # same named pipe, and if this process still holds it the new window
        # would exit immediately and the user would see nothing happen.
        if self.guard is not None:
            self.guard.release()
            self.guard = None
        if not elevation.elevate_and_exit():
            QMessageBox.critical(
                self, "提权失败",
                "无法启动管理员副本。\n\n"
                "请右键 run.py →「以管理员身份运行」，或从管理员 PowerShell 执行 "
                "python run.py。",
            )

    def show_privileges(self) -> None:
        names = elevation.REQUIRED_PRIVILEGES
        state = elevation.enable_required_privileges()
        lines = [f"{name}: {'已启用' if state.get(name) else '未启用'}" for name in names]
        lines.append("")
        lines.append(
            "管理员令牌：已提升" if self.admin else "管理员令牌：未提升（需要 UAC）"
        )
        if not self.admin:
            lines.append("未提权时下面这些权限都无法开启。")
        QMessageBox.information(self, "当前权限", "\n".join(lines))

    # -- lifecycle --------------------------------------------------------
    def save_settings(self) -> None:
        config.save_settings(self.settings)

    def log(self, level: str, message: str) -> None:
        self.log_panel.log(level, message)

    def refresh_all(self) -> None:
        for page in (
            self.page_gasp,
            self.page_replace,
            self.page_backup,
            self.page_acl,
            self.page_pending,
        ):
            try:
                page.reconfigure()
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"页面刷新失败：{exc}")

        badge = []
        if self.settings.is_sandbox():
            badge.append("沙盒")
        if not self.admin:
            badge.append("非管理员")
        text = " · ".join(badge) or "准备就绪"
        self.status.showMessage(f"{text}  ｜  {self.host.summary()}")
        self.elevate_button.setVisible(not self.admin)
        self.elevate_button.setText("以管理员身份重启" if not self.admin else "")

    def show_pending_tab(self) -> None:
        index = self.tabs.indexOf(self.page_pending)
        if index >= 0:
            self.tabs.setCurrentIndex(index)

    def _log_startup(self) -> None:
        self.log("info", "FontHandler 2.0.0 启动")
        self.log("info", f"工作目录：{config.APP_HOME}")
        self.log("info", f"字体目录：{self.settings.resolved_target_dir()}")
        self.log("info", f"源目录：{self.settings.resolved_source_dir()}")
        if self.settings.is_sandbox():
            self.log("warn", "目标目录不是 C:\\Windows\\Fonts → 沙盒模式，所有破坏性操作只作用于此目录")
        if self.admin:
            state = elevation.enable_required_privileges()
            granted = [name for name, ok in state.items() if ok]
            missing = [name for name, ok in state.items() if not ok]
            self.log("info", f"已提权，启用权限：{', '.join(granted) or '无'}")
            if missing:
                self.log(
                    "warn",
                    "以下特权启用失败（可能是精简令牌或组策略限制）："
                    + ", ".join(missing)
                    + "；设置 TrustedInstaller 所有者可能失败。"
                    "点「权限」按钮可再次查看。",
                )
        else:
            self.log("warn", "当前进程未提权；系统字体热替换和 TrustedInstaller 还原都会失败。")
            self.log("warn", "请点状态栏「以管理员身份重启」（Ctrl+Shift+A）")


def main(argv: list[str] | None = None) -> int:
    app = QApplication(sys.argv)
    apply_style(app)

    guard = SingleInstance(APP_KEY)
    if not guard.claim():
        # Another copy already owns the window.
        return 0

    win = FontHandlerApp()
    win.guard = guard
    win.show()
    try:
        return app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
