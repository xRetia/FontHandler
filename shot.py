"""Capture real screenshots of the running app for the docs site.

Launches the actual GUI, visits each tab, and grabs the window with
``QWidget.grab()``. These are the real widgets, not mockups.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))

from fonthandler.ui.main_window import FontHandlerApp  # noqa: E402
from fonthandler.ui.style import apply_style  # noqa: E402

# (tab index, file slug, caption shown on the docs site)
SHOTS = [
    (0, "gasp", "GaspHack 字体处理"),
    (1, "replace", "系统字体热替换"),
    (2, "backup", "备份与还原"),
    (3, "acl", "TrustedInstaller 权限管理"),
    (4, "pending", "待重启操作队列"),
]


def main() -> int:
    app = QApplication(sys.argv)
    apply_style(app)

    win = FontHandlerApp()
    win.resize(1280, 840)
    win.show()

    def capture_all() -> None:
        tabs = win.tabs
        for index, slug, caption in SHOTS:
            if index >= tabs.count():
                break
            tabs.setCurrentIndex(index)
            for _ in range(6):
                app.processEvents()
            pixmap = win.grab()
            target = OUT / f"{slug}.png"
            pixmap.save(str(target), "PNG")
            print(f"saved {target.name}  {pixmap.width()}x{pixmap.height()}  {caption}")

        # Docked log hidden, so the tab content is the whole picture.
        tabs.setCurrentIndex(0)
        win.dock_log.setVisible(False)
        for _ in range(6):
            app.processEvents()
        win.grab().save(str(OUT / "clean-gasp.png"), "PNG")
        print("saved clean-gasp.png")

        app.quit()

    QTimer.singleShot(1500, capture_all)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())