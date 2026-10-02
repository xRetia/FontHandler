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
           "RECOMMENDED_BUILD", "MIN_BUILD",
           "SUPPORT_FULL", "SUPPORT_WARN", "SUPPORT_NONE"]


#: Windows 10 1709 (Fall Creators Update, build 16299).  At this point GDI
#: font rendering was rewritten to support full X/Y-axis anti-aliasing under
#: ClearType, which is where the GaspHack technique produces the best results.
#: Builds at or above this number get the full, no-warning experience.
RECOMMENDED_BUILD = 16299

#: Windows 7 SP1 (build 7601).  This is the absolute floor: the FontCache
#: service, PendingFileRenameOperations semantics and TrustedInstaller ACL
#: workflows this tool drives all exist on Win7 SP1, but GDI rendering is
#: older and GaspHack results are noticeably worse.  The user is warned and
#: must wait through a 3-second countdown before proceeding.
MIN_BUILD = 7601

# Support levels returned by check_windows_support.
SUPPORT_FULL = "full"   # >= RECOMMENDED_BUILD — no warning
SUPPORT_WARN = "warn"   # MIN_BUILD <= build < RECOMMENDED_BUILD — warning + 3s
SUPPORT_NONE = "none"   # < MIN_BUILD or unknown — refused


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
    #: Three-level support verdict: "full" | "warn" | "none".
    support_level: str = SUPPORT_FULL

    def summary(self) -> str:
        if not self.is_windows:
            return f"非 Windows 平台 ({platform.platform()})"
        text = f"{self.windows_version} (build {self.build}) · {self.architecture}"
        if self.support_level == SUPPORT_NONE:
            text += " · 版本不受支持"
        elif self.support_level == SUPPORT_WARN:
            text += " · 版本偏低（建议 1709+）"
        return text


def check_windows_support(build: str | int | None) -> tuple[str, str]:
    """(support_level, reason) for the running Windows build.

    Three outcomes:

    * ``SUPPORT_FULL`` — Windows 10 1709 (build 16299) or later, including all
      Windows 11 builds (22000+).  No warning.
    * ``SUPPORT_WARN`` — Windows 7 SP1 (build 7601) through pre-1709 builds.
      The application can run, but the user is warned about rendering quality
      and must wait through a 3-second countdown before proceeding.
    * ``SUPPORT_NONE`` — below Windows 7 SP1, or a build that cannot be
      determined.  The application refuses to start.

    Non-Windows platforms (development / sandbox) always get ``SUPPORT_FULL``
    -- nothing there can reach the system paths.
    """
    if os.name != "nt":
        return SUPPORT_FULL, ""
    try:
        number = int(str(build).strip())
    except (TypeError, ValueError):
        return SUPPORT_NONE, f"无法确定 Windows 内部版本号（build={build!r}），已拒绝运行"
    if number < MIN_BUILD:
        return SUPPORT_NONE, (
            f"需要 Windows 7 SP1（内部版本 {MIN_BUILD}）或更高版本，当前为 build {number}"
        )
    if number < RECOMMENDED_BUILD:
        return SUPPORT_WARN, (
            f"建议使用 Windows 10 1709（内部版本 {RECOMMENDED_BUILD}）或更高版本，"
            f"当前为 build {number}。GaspHack 在旧版 GDI 渲染下效果有限，"
            f"且系统兼容性风险较高。"
        )
    return SUPPORT_FULL, ""


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
    level, reason = check_windows_support(build)
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
        supported=level != SUPPORT_NONE,
        support_reason=reason,
        support_level=level,
    )