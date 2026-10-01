"""Post-reboot self-check.

A font replacement is not finished when the files are written.  Two things
still have to happen, and both of them are invisible from inside the running
program:

* the queued renames are applied by the Session Manager during the next boot,
  and the font cache has to be purged *after* that, or Windows keeps serving
  the old glyphs from the cache it built before the reboot;
* the installed files have to be verified to be owned by TrustedInstaller.

That second check is the one that matters most.  Windows renders the sign-in
screen with these very fonts, so a file that is not owned by TrustedInstaller
can leave the machine unable to reach the desktop -- at which point there is no
way to run this program and fix it.  Detecting it at the next logon, while the
desktop is still reachable, is the difference between a warning and a dead
machine.

The program therefore registers a ``RunOnce`` entry when it queues a
replacement, and on the following logon it purges the cache, verifies the
owners, and offers a one-click restore from the backup taken before the
replacement.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "PendingCheck",
    "STATE_FILENAME",
    "record_pending_check",
    "load_pending_check",
    "clear_pending_check",
    "schedule_runonce",
    "clear_runonce",
    "runonce_command",
    "verify_owners",
    "OwnerProblem",
]


STATE_FILENAME = "post_reboot_check.json"

#: Registry key used for the one-shot post-logon run.
RUNONCE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_VALUE = "FontHandlerPostRebootCheck"

#: How long to wait before concluding the OS is not letting us log on.  The
#: font cache service and the shell both have to be up before the check is
#: meaningful, and on a slow boot that takes a while.
STARTUP_GRACE_SECONDS = 20


@dataclass
class PendingCheck:
    """What still has to be verified after the next boot.

    Persisted as JSON in the app-data directory, because it has to survive the
    reboot that the check is about.
    """

    #: Font file names that were queued for replacement during the reboot.
    queued: list[str] = field(default_factory=list)
    #: Font file names that were replaced immediately.
    replaced: list[str] = field(default_factory=list)
    #: When the state was written, as a unix timestamp.
    created: float = 0.0
    #: Backup package to restore from, if the user asks for a rollback.
    backup: str = ""
    #: Directory the fonts were installed into.
    target_dir: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "queued": list(self.queued),
                "replaced": list(self.replaced),
                "created": self.created,
                "backup": self.backup,
                "target_dir": self.target_dir,
            },
            indent=2,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "PendingCheck":
        data = json.loads(raw)
        return cls(
            queued=[str(n) for n in data.get("queued", [])],
            replaced=[str(n) for n in data.get("replaced", [])],
            created=float(data.get("created", 0.0)),
            backup=str(data.get("backup", "")),
            target_dir=str(data.get("target_dir", "")),
        )

    @property
    def is_empty(self) -> bool:
        return not self.queued and not self.replaced


@dataclass
class OwnerProblem:
    """A font that is installed but not in the state Windows expects."""

    name: str
    owner: str
    reason: str

    def describe(self) -> str:
        return f"{self.name}: 所有者是 {self.owner or '未知'}（{self.reason}）"


# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------
def state_path(app_home: Path | None = None) -> Path:
    from . import config

    return (app_home or config.APP_HOME) / STATE_FILENAME


def record_pending_check(
    queued: list[str] | None = None,
    replaced: list[str] | None = None,
    backup: str = "",
    target_dir: str = "",
    app_home: Path | None = None,
) -> Path:
    """Write the post-reboot checklist and register the one-shot logon run."""
    check = PendingCheck(
        queued=sorted(queued or []),
        replaced=sorted(replaced or []),
        created=time.time(),
        backup=backup,
        target_dir=target_dir,
    )
    path = state_path(app_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(check.to_json(), encoding="utf-8")
    schedule_runonce()
    return path


def load_pending_check(app_home: Path | None = None) -> PendingCheck | None:
    """Read the checklist, or None when there is nothing pending.

    A state file that cannot be parsed is treated as absent *and removed*: it
    describes work from a previous boot, and refusing to start because of it
    would leave the user with no way to reach the fonts at all.
    """
    path = state_path(app_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        check = PendingCheck.from_json(raw)
    except (ValueError, TypeError):
        clear_pending_check(app_home)
        return None
    return None if check.is_empty else check


def clear_pending_check(app_home: Path | None = None) -> None:
    try:
        state_path(app_home).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# RunOnce
# ---------------------------------------------------------------------------
def runonce_command() -> str:
    """The command line that re-launches this program for the self-check.

    Quoted because the install path routinely contains spaces, and ``--reboot-
    check`` because the RunOnce entry has to be distinguishable from a normal
    manual launch.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --reboot-check'
    return f'"{sys.executable}" "{Path(__file__).resolve().parents[1] / "run.py"}" --reboot-check'


def schedule_runonce(registry_backend=None) -> bool:
    """Ask Windows to launch this program once, at the next logon.

    ``RunOnce`` rather than ``Run``: the check is a one-off tied to a specific
    reboot, and a permanent entry would pop a window on every single logon
    forever.
    """
    if os.name != "nt":
        return False
    from . import registry as reg

    backend = registry_backend or reg.get_backend()
    try:
        backend.set_value(RUNONCE_KEY, RUNONCE_VALUE, [runonce_command()], 1)
        return True
    except Exception:  # noqa: BLE001 - autostart is a convenience, not a requirement
        return False


def clear_runonce(registry_backend=None) -> bool:
    """Remove the one-shot entry.

    Windows deletes ``RunOnce`` values on its own after running them, but only
    for the exact command it stored, and only when it got that far.  Clearing
    it explicitly means a check that found nothing to do does not come back.
    """
    if os.name != "nt":
        return False
    from . import registry as reg

    backend = registry_backend or reg.get_backend()
    try:
        return bool(backend.delete_value(RUNONCE_KEY, RUNONCE_VALUE))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def verify_owners(
    names: list[str],
    target_dir: Path,
    security=None,
) -> list[OwnerProblem]:
    """Check that every listed font is owned by TrustedInstaller.

    Read-only apart from the ``get_owner`` call, so it is safe to run on every
    logon.  A font that is missing is reported too: the queued rename not having
    happened is a different failure from the rename having produced a badly
    owned file, and the user needs to be able to tell them apart.
    """
    from . import acl
    from . import config

    backend = security or acl.get_backend()
    target_dir = Path(target_dir)
    problems: list[OwnerProblem] = []

    for name in names:
        path = target_dir / name
        if not path.exists():
            problems.append(OwnerProblem(name, "", "文件不存在，可能未完成替换"))
            continue
        try:
            owner = backend.get_owner(path)
        except Exception as exc:  # noqa: BLE001
            problems.append(OwnerProblem(name, "", f"无法读取所有者：{exc}"))
            continue
        if not owner or owner.upper() != config.TI_SID.upper():
            problems.append(OwnerProblem(name, owner or "未知", "不是 TrustedInstaller"))
    return problems
