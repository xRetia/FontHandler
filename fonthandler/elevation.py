"""Administrator detection, privilege enablement and self-elevation.

Windows will not let a normal process hand a file's ownership to
TrustedInstaller, and ``icacls /setowner`` fails with access-denied without
both an elevated token *and* ``SeTakeOwnershipPrivilege``.  So there are two
separate things here:

* :func:`is_admin` -- is the token elevated at all?
* :func:`enable_required_privileges` -- turn on the specific privileges the
  ACL work needs.

Being an administrator is necessary but not sufficient; a stock elevated
token has both privileges present but *disabled*.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from . import acl

__all__ = [
    "REQUIRED_PRIVILEGES",
    "is_admin",
    "enable_required_privileges",
    "elevate_and_exit",
    "restart_as_admin",
    "subprocess_params",
    "elevate_self",
]

#: Privileges the TrustedInstaller ACL workflow needs.  ``SeRestorePrivilege``
#: lets us write a saved descriptor back, ``SeTakeOwnershipPrivilege`` lets us
#: become the owner of a file we do not own, and ``SeSecurityPrivilege`` is what
#: allows the SACL to be read at all.
REQUIRED_PRIVILEGES: tuple[str, ...] = (
    "SeRestorePrivilege",
    "SeTakeOwnershipPrivilege",
    "SeBackupPrivilege",
    "SeSecurityPrivilege",
)

SW_SHOWNORMAL = 1


def is_admin() -> bool:
    """True when the current process holds the Administrators token."""
    return acl.is_admin()


def enable_required_privileges(names: Sequence[str] = REQUIRED_PRIVILEGES) -> dict[str, bool]:
    """Enable every privilege in ``names``; report which ones took.

    Returns a ``{privilege: enabled}`` map so the caller can log what Windows
    actually granted instead of assuming success -- a standard account in the
    Administrators group gets a filtered token where these simply cannot be
    turned on.
    """
    if not is_admin():
        return {name: False for name in names}
    return {name: acl.enable_privilege(name) for name in names}


def elevate_and_exit(extra_args: Sequence[str] = ()) -> bool:
    """Re-launch elevated and quit. Returns ``False`` if the user declined.

    The single-instance guard must already be released by the caller: the new
    process claims the same named pipe, and if this one still owns it the
    elevated copy would silently exit thinking a window is already open.
    """
    started = restart_as_admin(extra_args)
    if started:
        os._exit(0)
    return started


def restart_as_admin(extra_args: Sequence[str] = ()) -> bool:
    """Re-launch this application elevated via ``ShellExecuteW`` ``runas``.

    The interpreter is the executable and the script is an argument.  Handing
    ``run.py`` straight to ``ShellExecuteW`` instead depends on a ``.py`` file
    association existing, which a bare install usually does not have.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p

        interpreter = Path(sys.executable)
        if getattr(sys, "frozen", False):
            # Packaged as one .exe: it is its own interpreter.
            target, params = str(interpreter), subprocess_params(extra_args)
        else:
            script = Path(sys.argv[0]).resolve()
            if not script.is_file():
                return False
            target = str(interpreter)
            params = subprocess_params((script, *extra_args))

        cwd = str(Path(__file__).resolve().parents[1])
        # ShellExecuteW returns an HINSTANCE; anything <= 32 is an error, with
        # the reason in the low word.  1223 is ERROR_CANCELLED, i.e. the user
        # pressed "No" on the UAC prompt.
        result = shell32.ShellExecuteW(None, "runas", target, params, cwd, SW_SHOWNORMAL)
        return int(result or 0) > 32
    except OSError:
        return False


def subprocess_params(extra_args: Sequence[str] = ()) -> str:
    """Rebuild the CLI arguments as the elevated process' parameter string."""
    parts: list[str] = []
    for arg in (*sys.argv[1:], *extra_args):
        text = str(arg)
        parts.append(f'"{text}"' if not text or any(ch.isspace() for ch in text) else text)
    return " ".join(parts)


# Backwards-friendly alias.
elevate_self = restart_as_admin