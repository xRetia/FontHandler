"""Configuration: default paths, the CJK whitelist, and persisted settings.

Everything that the original scripts hardcoded lives here, so a test (or the
user) can point the whole application at a sandbox directory instead of
``C:\\Windows\\Fonts``.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "CJK_WHITELIST",
    "CJK_EXCLUSIONS",
    "GASP_EXCLUDES",
    "TI_SID",
    "TI_ACCOUNT",
    "SYSTEM_FONTS_DIR",
    "APP_NAME",
    "APP_HOME",
    "USER_DATA_DIR",
    "LOG_DIR",
    "Settings",
    "load_settings",
    "save_settings",
]

APP_NAME = "FontHandler"
APP_VERSION = "2.0.0"

#: Where a modern per-user Windows install keeps the executable's own data.
if sys.platform == "win32":
    APP_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP_NAME
else:  # pragma: no cover - development convenience
    APP_HOME = Path.home() / ".config" / APP_NAME

USER_DATA_DIR = APP_HOME / "userdata"
LOG_DIR = APP_HOME / "logs"

SYSTEM_FONTS_DIR = Path(r"C:\Windows\Fonts")

# ---------------------------------------------------------------------------
# The CJK whitelist, verbatim from FontReplace.ps1
# ---------------------------------------------------------------------------
CJK_WHITELIST: tuple[str, ...] = (
    # Simplified Chinese
    "msyh.ttc", "msyhbd.ttc", "msyhl.ttc",
    "msyi.ttf",
    "Deng.ttf", "Dengb.ttf", "Dengl.ttf",
    "simhei.ttf", "simfang.ttf", "simkai.ttf",
    "NotoSansSC-VF.ttf", "NotoSerifSC-VF.ttf",
    # Traditional Chinese
    "msjh.ttc", "msjhbd.ttc", "msjhl.ttc",
    "mingliub.ttc",
    # Japanese
    "YuGothB.ttc", "YuGothL.ttc", "YuGothM.ttc", "YuGothR.ttc",
    "msgothic.ttc",
    # Korean
    "malgun.ttf", "malgunbd.ttf", "malgunsl.ttf",
)

#: Never touched, even though they are CJK -- matching FontReplace.ps1.
CJK_EXCLUSIONS: tuple[str, ...] = ("simsun.ttc", "simsunb.ttf")

#: Symbol/emoji fonts whose glyphs break under grid-fitting; the original batch
#: file filtered them out of the GaspHack input set.  Emoji fonts carry colour
#: bitmap (COLR/CPAL or CBDT/CBLC) tables that grid-fitting cannot improve and
#: that the gasp table does not govern -- patching them is pointless and can
#: subtly damage emoji rendering.
GASP_EXCLUDES: tuple[str, ...] = (
    "webdings.ttf", "wingding.ttf", "marlett.ttf", "symbol.ttf",
    "seguiemj.ttf", "seguisym.ttf", "segmdl2.ttf",
)

#: NT SERVICE\TrustedInstaller
TI_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
TI_ACCOUNT = "NT SERVICE\\TrustedInstaller"


@dataclass
class Settings:
    """User preferences, persisted as JSON next to the app data."""

    #: Directory the *source* (GaspHacked) fonts are read from.
    source_dir: str = ""
    #: Directory treated as the font target.  Anything other than
    #: ``C:\\Windows\\Fonts`` puts the app into sandbox mode.
    target_dir: str = str(SYSTEM_FONTS_DIR)
    #: CJK-only mode, i.e. exactly what FontReplace.ps1 did.
    cjk_only: bool = True
    #: Create a backup before every destructive operation.
    backup_before_replace: bool = True
    #: Purge the Windows font cache after a hot replacement.
    purge_font_cache: bool = True
    #: Broadcast WM_FONTCHANGE so running apps pick the fonts up immediately.
    notify_font_change: bool = True
    #: Restore the file owner to TrustedInstaller after replacing.
    restore_trusted_installer: bool = True
    #: Retry the replacement at boot when the file is locked.
    queue_on_lock: bool = True
    #: Re-apply the parent directory DACL (pure inheritance) after replacing.
    reset_acl_to_inherited: bool = True
    #: Current GaspHack ranges, serialised as "maxPPEM:behaviour" pairs.
    gasp_ranges: str = "65535:10"

    def resolved_source_dir(self) -> Path:
        return Path(self.source_dir) if self.source_dir else DEFAULT_SOURCE_DIR

    def resolved_target_dir(self) -> Path:
        return Path(self.target_dir)

    def is_sandbox(self) -> bool:
        """True when the target directory is not the real system font dir."""
        try:
            return self.resolved_target_dir().resolve() != SYSTEM_FONTS_DIR.resolve()
        except OSError:  # pragma: no cover - defensive
            return True

    def gasp_range_map(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for item in self.gasp_ranges.replace(";", ",").split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            max_ppem, _sep, behaviour = item.partition(":")
            try:
                out[int(max_ppem)] = int(behaviour)
            except ValueError:
                continue
        return out or {0xFFFF: 0x000A}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


#: The gasp-hacked output of the built-in pipeline.
DEFAULT_SOURCE_DIR = APP_HOME / "gasp"


def settings_path() -> Path:
    return APP_HOME / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    """Read settings, falling back to defaults for anything unreadable."""
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    known = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(settings.to_json(), encoding="utf-8")
    return target