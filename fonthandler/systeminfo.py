"""Host environment discovery (read-only, defensive)."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import SYSTEM_FONTS_DIR, USER_DATA_DIR

__all__ = ["HostInfo", "get_host_info"]


@dataclass
class HostInfo:
    windows_version: str = ""
    build: str = ""
    architecture: str = ""
    is_admin: bool = False
    fonts_dir: str = str(SYSTEM_FONTS_DIR)
    fonts_dir_exists: bool = False
    user_fonts_dir: str = str(USER_DATA_DIR)
    is_windows: bool = os.name == "nt"
    python: str = sys.version.split()[0]
    caveat: str = field(default="")

    def summary(self) -> str:
        if not self.is_windows:
            return f"非 Windows 平台 ({platform.platform()})"
        return f"{self.windows_version} (build {self.build}) · {self.architecture}"


def _windows_version() -> tuple[str, str]:
    if os.name != "nt":
        return ("", "")
    try:
        # RtlGetVersion avoids the manifest issues of GetVersionEx on modern builds.
        class OSVERSIONINFOEXW(ctypes.Structure):
            _fields_ = [
                ("dwOSVersionInfoSize", ctypes.c_uint32),
                ("dwMajorVersion", ctypes.c_uint32),
                ("dwMinorVersion", ctypes.c_uint32),
                ("dwBuildNumber", ctypes.c_uint32),
                ("dwPlatformId", ctypes.c_uint32),
                ("szCSDVersion", ctypes.c_wchar * 128),
            ]

        info = OSVERSIONINFOEXW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        ntdll = ctypes.WinDLL("ntdll")
        status = ntdll.RtlGetVersion(ctypes.byref(info))
        if status != 0:
            return ("Windows", "?")
        return (
            f"Windows {info.dwMajorVersion}.{info.dwMinorVersion}",
            str(info.dwBuildNumber),
        )
    except Exception:  # noqa: BLE001
        return ("Windows", "?")


def _is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def get_host_info() -> HostInfo:
    version, build = _windows_version()
    fonts_dir = Path(SYSTEM_FONTS_DIR)
    return HostInfo(
        windows_version=version,
        build=build,
        architecture=platform.machine(),
        is_admin=_is_admin(),
        fonts_dir=str(fonts_dir),
        fonts_dir_exists=fonts_dir.is_dir(),
        user_fonts_dir=str(USER_DATA_DIR),
        is_windows=os.name == "nt",
    )