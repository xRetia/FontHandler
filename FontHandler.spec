# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成免安装的单文件 exe。

构建产物为 dist/FontHandler.exe，目标机器无需预装 Python。
"""

import sys
from pathlib import Path

# 打包时把 fonthandler 视为包，避免隐式导入的模块被漏掉。
hiddenimports = [
    # single_instance 用 QLocalServer 实现单实例守卫
    "PyQt6.QtNetwork",
    "fonthandler.ui.page_gasp",
    "fonthandler.ui.page_replace",
    "fonthandler.ui.page_backup",
    "fonthandler.ui.page_acl",
    "fonthandler.ui.page_pending",
    "fonthandler.ui.main_window",
    "fonthandler.ui.log_panel",
    "fonthandler.ui.single_instance",
    "fonthandler.ui.style",
    "fonthandler.ui.common",
]

block_cipher = None

ROOT = Path(SPECPATH).resolve()
icon_path = ROOT / "docs" / "images" / "app.ico"

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # "fonthandler/ui/assets" -> "fonthandler/ui/assets": the QSS references the
    # checkbox tick by relative path from the stylesheet's own directory, so the
    # tree has to keep its shape inside the bundle.  Dropping it does not raise
    # -- the image simply fails to load and the indicator renders as a bare
    # accent-coloured block.
    datas=(
        [
            (str(ROOT / "docs" / "images" / "app.ico"), "."),
            (str(ROOT / "fonthandler" / "ui" / "assets"), "fonthandler/ui/assets"),
        ]
        if icon_path.exists()
        else [(str(ROOT / "fonthandler" / "ui" / "assets"), "fonthandler/ui/assets")]
    ),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 注意：PyQt6.QtNetwork 被 single_instance 的 QLocalServer 依赖，
    # 不能排除。PyInstaller 的 PyQt6 hook 不会自动拉取以子模块形式
    # 导入的 Qt 模块，因此显式列出。
    excludes=["PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.Qt3DCore",
              "PyQt6.QtWebEngineCore", "PyQt6.QtMultimedia",
              "PyQt6.QtWebEngineWidgets", "tkinter", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="FontHandler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # onefile：单文件分发，用户不必处理 PyInstaller 的 _internal 目录
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
    version=str(ROOT / "version_info.txt")
    if (ROOT / "version_info.txt").exists()
    else None,
)