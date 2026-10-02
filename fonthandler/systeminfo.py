"""Host environment discovery (read-only, defensive)."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import SYSTEM_FONTS_DIR, USER_DATA_DIR

__all__ = ["HostInfo", "get_host_info", "check_windows_support",
           "MIN_SUPPORTED_BUILD"]


#: Windows 10 1809 (Redstone 5).  Older builds -- including every Windows 7
#: and 8/8.1 machine -- are refused outright: the FontCache service set, the
#: reboot-rename-queue semantics and the TrustedInstaller workflows this tool
#: drives are the Win10 1809+ / Win11 ones, and "probably works" is not an
#: acceptable answer for a tool that replaces the fonts the logon screen
#: renders with.
MIN_SUPPORTED_BUILD = 17763


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
    supported: bool = True
    support_reason: str = ""
    python: str = sys.version.split()[0]
    caveat: str = field(default="")

    def summary(self) -> str:
        if not self.is_windows:
            return f"非 Windows 平台 ({platform.platform()})"
        text = f"{self.windows_version} (build {self.build}) · {self.architecture}"
        if not self.supported:
            text += " · 版本不受支持"
        return text


def check_windows_support(build: str | int | None) -> tuple[bool, str]:
    """(supported, reason) for the running Windows build.

    Requires Windows 10 1809 (build 17763) or later, which covers every
    Windows 11 build (22000+).  Windows 7 / 8 / 8.1 and the early Windows 10
    releases all fall below the threshold and are refused.

    A build that cannot be determined is refused too: this gate runs before
    anything touches ``C:\\Windows\\Fonts``, and proceeding on "unknown" would
    turn the gate into decoration.  Non-Windows platforms (development /
    sandbox) are accepted -- nothing there can reach the system paths.
    """
    if os.name != "nt":
        return True, ""
    try:
        number = int(str(build).strip())
    except (TypeError, ValueError):
        return False, f"无法确定 Windows 内部版本号（build={build!r}），已拒绝运行"
    if number < MIN_SUPPORTED_BUILD:
        return False, (
            f"需要 Windows 10 1809（内部版本 {MIN_SUPPORTED_BUILD}）"
            f"或更高版本 / Windows 11，当前为 build {number}"
        )
    return True, ""


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
    supported, reason = check_windows_support(build)
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
        supported=supported,
        support_reason=reason,
    )