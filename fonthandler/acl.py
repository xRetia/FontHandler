"""Windows file security: TrustedInstaller ownership, SDDL snapshots, restore.

The original ``FontReplace.ps1`` set the owner of the staged ``.new`` file to
TrustedInstaller and then replaced in place.  Two problems with that approach
are fixed here:

* it leaves a *redundant explicit* ``NT SERVICE\\TrustedInstaller:(F)`` ACE on
  the file instead of letting it inherit from the Fonts directory -- see
  :func:`reset_to_inherited`;
* it records nothing about the original descriptor, so it cannot be undone --
  :class:`AclSnapshot` captures the full descriptor before the write.

On Windows the security calls go through a tiny ``ctypes`` binding to
``advapi32``.  On other platforms (and in the sandbox tests) the module still
imports and every function degrades to a no-op / recording stub driven by an
injectable :class:`SecurityBackend`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import TI_SID

__all__ = [
    "TI_SID",
    "ADMIN_GROUP_SID",
    "TAKEOVER_ACES",
    "AclError",
    "AclSnapshot",
    "SecurityBackend",
    "WindowsSecurityBackend",
    "SandboxSecurityBackend",
    "get_backend",
    "is_admin",
    "enable_privilege",
    "get_owner",
    "get_sddl",
    "apply_sddl",
    "set_owner_ti",
    "take_over_for_replace",
    "reset_to_inherited",
    "snapshot",
    "restore",
    "has_explicit_ti_ace",
    "run_hidden",
    "console_encoding",
]


#: BUILTIN\Administrators.  A full SID (not the SDDL alias ``BA``) because
#: :meth:`SecurityBackend.set_owner` feeds it to ``ConvertStringSidToSidW``.
ADMIN_GROUP_SID = "S-1-5-32-544"

#: ACEs appended by :func:`take_over_for_replace`: full control for
#: Administrators and for SYSTEM.  SYSTEM is the one that matters most -- the
#: reboot rename queue is executed by smss in the SYSTEM context.
TAKEOVER_ACES = "(A;;FA;;;BA)(A;;FA;;;SY)"


class AclError(RuntimeError):
    """Raised when a security operation fails."""


#: ``AdjustTokenPrivileges`` clears this on success and leaves
#: ``ERROR_NOT_ALL_ASSIGNED`` (1300) when a filtered token refused the request.
ERROR_SUCCESS = 0


#: ``CREATE_NO_WINDOW``.  ``run.py`` frees this process' console, so without
#: this flag every child console application (icacls, net) is handed a *brand
#: new* console of its own and a black window flashes on screen.  One ACL pass
#: over the 24 CJK fonts spawns ~6 icacls per font, so a single "scan
#: permissions" click used to throw up well over a hundred windows.
HIDE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_hidden(cmd: list[str], **kwargs) -> "subprocess.CompletedProcess":
    """``subprocess.run`` that never shows a console window.

    ``stdin`` is forced to ``DEVNULL``.  ``icacls`` and ``net`` never read from
    stdin, and after ``run._detach_console`` an inherited stdin still points at
    a handle the console has invalidated -- handing the child a fresh null
    device removes that whole class of failure instead of relying on the
    parent's handle happening to stay valid.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", console_encoding())
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if os.name == "nt":
        kwargs.setdefault("creationflags", HIDE_WINDOW)
    return subprocess.run(cmd, **kwargs)


def console_encoding() -> str:
    """The OEM code page console tools emit, not UTF-8.

    ``icacls`` / ``net`` write their messages in the console code page (936 on a
    Chinese Windows install), so decoding as UTF-8 turned every diagnostic into
    mojibake in the log panel.
    """
    if os.name != "nt":
        return "utf-8"
    import ctypes
    import locale

    try:
        cp = ctypes.windll.kernel32.GetOEMCP()
    except (AttributeError, OSError):  # pragma: no cover - defensive
        cp = 0
    if cp:
        try:
            return f"cp{cp}"
        except LookupError:  # pragma: no cover - unmapped code page
            pass
    return locale.getpreferredencoding(False) or "utf-8"


# ---------------------------------------------------------------------------
# Security descriptors
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AclSnapshot:
    """Everything needed to put a file's security back the way it was."""

    path: str
    owner_sid: str = TI_SID
    sddl: str = ""
    inherited: bool = True
    captured_at: float = field(default_factory=lambda: 0.0)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "owner_sid": self.owner_sid,
            "sddl": self.sddl,
            "inherited": self.inherited,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "AclSnapshot":
        return cls(
            path=raw.get("path", ""),
            owner_sid=raw.get("owner_sid", TI_SID),
            sddl=raw.get("sddl", ""),
            inherited=bool(raw.get("inherited", True)),
        )


class SecurityBackend:
    """Interface every platform / test double implements."""

    name = "base"

    def is_admin(self) -> bool:
        raise NotImplementedError

    def enable_privilege(self, name: str) -> bool:
        raise NotImplementedError

    def get_owner(self, path: Path) -> str:
        raise NotImplementedError

    def get_sddl(self, path: Path) -> str:
        raise NotImplementedError

    def set_owner(self, path: Path, sid: str) -> None:
        raise NotImplementedError

    def apply_sddl(self, path: Path, sddl: str) -> None:
        raise NotImplementedError

    def reset_inherited(self, path: Path) -> None:
        raise NotImplementedError

    def icacls(self, path: Path, *args: str) -> subprocess.CompletedProcess:
        raise NotImplementedError


class SandboxSecurityBackend(SecurityBackend):
    """In-memory security descriptors for tests; never touches the OS."""

    name = "sandbox"

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.sddls: dict[str, str] = {}
        self.inherited: dict[str, bool] = {}
        self.privileges: set[str] = set()
        #: Make ``set_owner`` raise, so a test can check that a font which
        #: cannot be handed to TrustedInstaller is refused rather than
        #: installed anyway.  Real failures here need WRITE_OWNER on a file the
        #: process does not own, which is exactly what the sandbox cannot
        #: reproduce on its own.
        self.fail_set_owner: bool = False
        #: Restrict ``fail_set_owner`` to a single SID (e.g. ``TI_SID``): the
        #: takeover of the old file (owner -> Administrators) then succeeds
        #: while handing the staged file to TrustedInstaller still fails, so
        #: tests can exercise each failure separately.
        self.fail_set_owner_sid: str | None = None

    def is_admin(self) -> bool:
        return True

    def enable_privilege(self, name: str) -> bool:
        self.privileges.add(name)
        return True

    def _key(self, path: Path) -> str:
        return str(Path(path)).lower()

    def get_owner(self, path: Path) -> str:
        return self.owners.get(self._key(path), TI_SID)

    def get_sddl(self, path: Path) -> str:
        return self.sddls.get(self._key(path), "O:BAG:SYD:PAI(A;OICI;FA;;;SY)")

    def set_owner(self, path: Path, sid: str) -> None:
        if self.fail_set_owner and (
            self.fail_set_owner_sid is None or sid == self.fail_set_owner_sid
        ):
            raise AclError(f"simulated SetNamedSecurityInfoW failure on {path}")
        self.owners[self._key(path)] = sid

    def apply_sddl(self, path: Path, sddl: str) -> None:
        self.sddls[self._key(path)] = sddl

    def reset_inherited(self, path: Path) -> None:
        self.inherited[self._key(path)] = True

    def icacls(self, path: Path, *args: str) -> subprocess.CompletedProcess:
        # Record the intent so tests can assert on it.
        self.sddls[self._key(path)] = f"icacls:{'|'.join(args)}"
        return subprocess.CompletedProcess(args=("icacls", str(path), *args), returncode=0)


class WindowsSecurityBackend(SecurityBackend):
    """Real backend built on ``advapi32`` via ctypes + the ``icacls`` CLI.

    Reads and descriptor writes go through the security API directly
    (``GetNamedSecurityInfoW`` / ``SetFileSecurityW`` / ``SetNamedSecurityInfoW``)
    so they touch only the part of the descriptor asked for.  ``icacls`` is
    still used for the two things it does better -- ``/reset`` re-inheritance
    and reporting -- and every call goes through :func:`run_hidden`, because
    ``run.py`` frees this process' console and a child console app would
    otherwise be handed a brand new one and flash a window.
    """

    name = "windows"

    def __init__(self) -> None:
        self._advapi32 = None
        self._kernel32 = None
        if os.name == "nt":
            try:
                self._advapi32 = self._load_advapi32()
                self._kernel32 = self._load_kernel32()
            except OSError:  # pragma: no cover - unusual
                self._advapi32 = None
                self._kernel32 = None

    @staticmethod
    def _load_advapi32():  # pragma: no cover - Windows only
        import ctypes

        return ctypes.WinDLL("advapi32", use_last_error=True)

    @staticmethod
    def _load_kernel32():  # pragma: no cover - Windows only
        import ctypes

        return ctypes.WinDLL("kernel32", use_last_error=True)

    @staticmethod
    def _local_free():  # pragma: no cover - Windows only
        """``LocalFree`` lives in kernel32, not advapi32."""
        return WindowsSecurityBackend._load_kernel32().LocalFree

    def icacls(self, path: Path, *args: str) -> subprocess.CompletedProcess:
        return run_hidden(["icacls", str(path), *args])

    def _close_handle(self, handle) -> None:  # pragma: no cover - Windows only
        """Close a Win32 handle.

        ``CloseHandle`` lives in kernel32.  Calling it on the advapi32 handle
        raised ``AttributeError``, and because it sat in a ``finally:`` block
        that exception *replaced* an already-computed return value -- so
        ``is_admin()`` reported "not elevated" for a fully elevated process and
        every privilege reported as unset.  Swallow failures here: leaking a
        handle must never change an answer the caller already computed.
        """
        try:
            kernel32 = self._load_kernel32()
            from ctypes import wintypes

            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(handle)
        except (AttributeError, OSError):
            pass

    def is_admin(self) -> bool:
        """True only when the token is genuinely *elevated*.

        ``IsUserAnAdmin()`` is not the answer to this question: it reports
        membership of the Administrators *group*, which stays true for the
        filtered token UAC hands to an admin account running normally.  That
        token is not elevated, and every privilege we need is stripped from it
        -- so trusting it made the app claim "已提权" and then refuse to enable
        a single privilege.  ``TokenIsElevated`` is the authoritative check.
        """
        try:
            import ctypes
            from ctypes import wintypes

            if os.name != "nt":
                return False
            advapi = self._advapi32 or self._load_advapi32()
            if advapi is None:
                return False
            kernel32 = self._load_kernel32()
            TOKEN_QUERY = 0x0008
            TOKEN_ELEVATION = 20

            # GetCurrentProcess() returns the pseudo-handle (void*)-1. Left
            # untyped it comes back as a c_long and gets zero-extended, which
            # truncates it to 0x00000000FFFFFFFF on 64-bit.
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE

            advapi.OpenProcessToken.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            advapi.OpenProcessToken.restype = wintypes.BOOL
            advapi.GetTokenInformation.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            advapi.GetTokenInformation.restype = wintypes.BOOL

            token = wintypes.HANDLE()
            if not advapi.OpenProcessToken(kernel32.GetCurrentProcess(),
                                           TOKEN_QUERY, ctypes.byref(token)):
                return False
            try:
                elevated = wintypes.DWORD()
                returned = wintypes.DWORD()
                ok = advapi.GetTokenInformation(
                    token, TOKEN_ELEVATION, ctypes.byref(elevated),
                    ctypes.sizeof(elevated), ctypes.byref(returned))
                if not ok:
                    return False
                return bool(elevated.value)
            finally:
                self._close_handle(token)
        except (AttributeError, OSError):  # pragma: no cover - non-Windows
            return False

    def enable_privilege(self, name: str) -> bool:  # pragma: no cover - Windows only
        if self._advapi32 is None:
            self._advapi32 = self._load_advapi32()
        if self._advapi32 is None:
            return False
        import ctypes
        from ctypes import wintypes

        advapi = self._advapi32
        kernel32 = self._load_kernel32()
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x0002

        # Declare the signatures locally instead of inheriting whatever the
        # last caller left behind, and keep the pseudo-handle 64-bit wide.
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Luid", LUID),
                ("Attributes", ctypes.c_uint32),
            ]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", ctypes.c_uint32),
                ("Privileges", LUID_AND_ATTRIBUTES * 1),
            ]

        advapi.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        # LUID is two 32-bit fields, not a 64-bit integer.
        advapi.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
        advapi.LookupPrivilegeValueW.restype = wintypes.BOOL
        advapi.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        advapi.AdjustTokenPrivileges.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token),
        ):
            return False
        try:
            luid = LUID()
            if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ctypes.set_last_error(0)
            if not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None):
                return False
            # AdjustTokenPrivileges returns TRUE even when it could not assign
            # the privilege -- the verdict is in GetLastError, which is 0 on
            # success and 1300 (ERROR_NOT_ALL_ASSIGNED) for a filtered token.
            return ctypes.get_last_error() == ERROR_SUCCESS
        finally:
            self._close_handle(token)

    def get_owner(self, path: Path) -> str:  # pragma: no cover - Windows only
        # Ask the API directly; the icacls text form glues the path onto the
        # owner, so parsing it yielded e.g.
        # "C:\Windows\Fonts\arial.ttf NT AUTHORITY\SYSTEM:(I)(F)".
        sddl = self.get_sddl(path)
        owner = _sddl_owner(sddl)
        if owner:
            return owner
        return TI_SID

    def get_sddl(self, path: Path) -> str:  # pragma: no cover - Windows only
        # ``icacls /save`` needs an output file and returns icacls' own
        # binary layout, not an SDDL string -- it cannot be fed back to
        # ``apply_sddl``.  Go through the descriptor API instead.
        advapi = self._advapi32
        if advapi is None:
            return ""
        import ctypes
        from ctypes import wintypes

        SE_FILE_OBJECT = 1
        OWNER_SECURITY_INFORMATION = 0x00000001
        DACL_SECURITY_INFORMATION = 0x00000004
        SDDL_REVISION_1 = 1

        get_info = advapi.GetNamedSecurityInfoW
        get_info.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_info.restype = ctypes.c_uint32

        convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(ctypes.c_uint32),
        ]
        convert.restype = wintypes.BOOL
        local_free = self._local_free()
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        descriptor = ctypes.c_void_p()
        # No SACL: reading the audit log needs SeSecurityPrivilege, and asking
        # for it makes the whole call fail with 1314 on an ordinary account.
        status = get_info(
            str(path), SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            None, None, None, None, ctypes.byref(descriptor),
        )
        if status != 0 or not descriptor:
            raise AclError(f"GetNamedSecurityInfoW({path}) failed with {status}")
        try:
            text = wintypes.LPWSTR()
            length = ctypes.c_uint32()
            if not convert(descriptor, SDDL_REVISION_1,
                           OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                           ctypes.byref(text), ctypes.byref(length)):
                raise AclError(
                    f"ConvertSecurityDescriptorToStringSecurityDescriptorW({path}) failed"
                )
            try:
                return text.value or ""
            finally:
                local_free(text)
        finally:
            local_free(descriptor)

    def set_owner(self, path: Path, sid: str) -> None:  # pragma: no cover - Windows only
        """Set only the owner, leaving every ACE untouched.

        ``icacls /setowner`` rewrites the DACL as a side effect -- it turned an
        inherited ``(A;ID;FA;;;OW)`` into ``(A;IOID;FA;;;OW)``, so a
        snapshot/restore cycle no longer round-tripped.  Going through
        ``SetNamedSecurityInfo`` with just OWNER_SECURITY_INFORMATION is
        surgical.
        """
        advapi = self._advapi32
        if advapi is None:
            raise AclError("advapi32 unavailable; cannot set the owner")
        import ctypes
        from ctypes import wintypes

        SE_FILE_OBJECT = 1
        OWNER_SECURITY_INFORMATION = 0x00000001
        if sid and sid.lower() == _sddl_owner(self.get_sddl(path)).lower():
            # Already correct. Writing it again is not free: the owner change
            # makes Windows recompute inheritable ACEs, and the OW ACE gains a
            # spurious inherit-only (IOID) flag, so a snapshot/restore cycle
            # never quite round-tripped.
            return
        local_free = self._local_free()
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        convert = advapi.ConvertStringSidToSidW
        convert.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        convert.restype = wintypes.BOOL
        set_info = advapi.SetNamedSecurityInfoW
        set_info.argtypes = [
            wintypes.LPWSTR, ctypes.c_int, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_info.restype = ctypes.c_uint32

        token = ctypes.c_void_p()
        if not convert(sid, ctypes.byref(token)):
            raise AclError(f"cannot parse owner SID {sid!r} for {path}")
        try:
            status = set_info(
                str(path), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
                token, None, None, None, None,
            )
            if status != 0:
                raise AclError(
                    f"SetNamedSecurityInfoW({path}, owner={sid}) failed with "
                    f"{status} ({ctypes.FormatError(status).strip()})"
                )
        finally:
            local_free(token)

    def apply_sddl(self, path: Path, sddl: str) -> None:  # pragma: no cover - Windows only
        advapi = self._advapi32
        if advapi is None:
            raise AclError("advapi32 unavailable; cannot apply an SDDL")
        import ctypes
        from ctypes import wintypes

        SE_FILE_OBJECT = 1
        DACL_SECURITY_INFORMATION = 0x00000004
        SDDL_REVISION_1 = 1

        parse = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        parse.argtypes = [
            wintypes.LPCWSTR, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
        ]
        parse.restype = wintypes.BOOL
        set_file = advapi.SetFileSecurityW
        set_file.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32, ctypes.c_void_p]
        set_file.restype = wintypes.BOOL
        local_free = self._local_free()
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        descriptor = ctypes.c_void_p()
        if not parse(sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None):
            raise AclError(f"cannot parse stored SDDL for {path}: {sddl!r}")
        try:
            if not set_file(str(path), DACL_SECURITY_INFORMATION, descriptor):
                err = ctypes.get_last_error()
                raise AclError(
                    f"SetFileSecurityW({path}) failed with "
                    f"{err} ({ctypes.FormatError(err).strip()})"
                )
        finally:
            local_free(descriptor)

    def reset_inherited(self, path: Path) -> None:  # pragma: no cover - Windows only
        # `icacls <path> /reset` re-inherits the parent's DACL, removing the
        # redundant explicit ACE the original script left behind.
        result = self.icacls(path, "/reset")
        if result.returncode != 0:
            raise AclError(_icacls_error("reset", path, result))
        self.set_owner(path, TI_SID)


def _icacls_error(action: str, path: Path, result: subprocess.CompletedProcess) -> str:
    """Render an icacls failure, keeping the Windows message readable."""
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    message = detail[0] if detail else f"exit code {result.returncode}"
    return f"icacls {action} failed for {path}: {message}"


def _sddl_sections(sddl: str) -> dict[str, str]:
    """Split an SDDL string into its top-level ``O:``/``G:``/``D:``/``S:`` parts.

    A naive ``sddl.split(";")`` is wrong: the DACL that follows is itself
    ``;``-separated and full of parenthesised ACEs, so the first "chunk" runs
    from ``O:`` straight into the first ACE.  Section markers are the single
    letters ``O``, ``G``, ``D`` and ``S`` followed by a colon at paren depth 0
    -- note that the owner of ``O:S-1-5-21-1-2-3-1001D:...`` genuinely ends in
    a ``D``, so the colon is what identifies the marker, not the letter.
    """
    if not sddl:
        return {}
    import re

    marker = re.compile(r"([OGDS]):")
    depth = 0
    starts: list[tuple[str, int]] = []
    for index, char in enumerate(sddl):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char in "OGDS":
            match = marker.match(sddl, index)
            if match:
                starts.append((match.group(1), index + 2))
    sections: dict[str, str] = {}
    for position, (name, value_at) in enumerate(starts):
        end = starts[position + 1][1] - 2 if position + 1 < len(starts) else len(sddl)
        value = sddl[value_at:end].rstrip(";")
        if name not in sections:
            sections[name] = value
    return sections


def _sddl_owner(sddl: str) -> str:
    """The owner from an SDDL string, e.g. ``O:BAD:...`` -> ``BA``."""
    return _sddl_sections(sddl).get("O", "")


_BACKEND: SecurityBackend | None = None


def get_backend() -> SecurityBackend:
    """Return the process-wide security backend (created on first use)."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = WindowsSecurityBackend() if os.name == "nt" else SandboxSecurityBackend()
    return _BACKEND


def set_backend(backend: SecurityBackend | None) -> None:
    """Inject a backend (used by the sandbox tests)."""
    global _BACKEND
    _BACKEND = backend


# ---------------------------------------------------------------------------
# Public helpers -- all take an optional backend for testability
# ---------------------------------------------------------------------------
def is_admin(backend: SecurityBackend | None = None) -> bool:
    return (backend or get_backend()).is_admin()


def enable_privilege(name: str = "SeRestorePrivilege", backend: SecurityBackend | None = None) -> bool:
    return (backend or get_backend()).enable_privilege(name)


def get_owner(path: Path, backend: SecurityBackend | None = None) -> str:
    return (backend or get_backend()).get_owner(Path(path))


def get_sddl(path: Path, backend: SecurityBackend | None = None) -> str:
    return (backend or get_backend()).get_sddl(Path(path))


def set_owner_ti(path: Path, backend: SecurityBackend | None = None) -> None:
    """Set a file's owner to TrustedInstaller (like the original ``icacls``)."""
    (backend or get_backend()).set_owner(Path(path), TI_SID)


def apply_sddl(path: Path, sddl: str, backend: SecurityBackend | None = None) -> None:
    (backend or get_backend()).apply_sddl(Path(path), sddl)


def reset_to_inherited(path: Path, backend: SecurityBackend | None = None) -> None:
    """Re-inherit the parent DACL -- removes redundant explicit ACEs."""
    (backend or get_backend()).reset_inherited(Path(path))


def take_over_for_replace(path: Path, backend: SecurityBackend | None = None) -> str:
    """Take ownership of a protected file and grant Administrators/SYSTEM full control.

    System fonts carry an explicit protected DACL in which *even SYSTEM* has
    only read+execute -- no DELETE.  Two things break without this step:

    * the hot replace fails with ``ACCESS_DENIED`` (the process runs as an
      administrator, not as TrustedInstaller), and
    * far worse, the reboot rename queue is executed by ``smss`` in the
      SYSTEM context: a queued rename whose target SYSTEM cannot delete is
      silently dropped at boot, the queue is consumed anyway, and the staged
      ``.new`` file is left behind while the font stays unpatched.  That is
      exactly the "重启后没生效、.new 还在" trap.

    Ownership is taken first (``SeTakeOwnershipPrivilege``), because rewriting
    somebody else's DACL needs WRITE_DAC, which an RX-only entry does not
    grant.  The caller snapshots the original descriptor beforehand and
    restores it when the replacement fails; when the replacement succeeds the
    file on disk is the staged one, which never carried the takeover.

    Returns the SDDL that was applied (for logging).
    """
    be = backend or get_backend()
    path = Path(path)
    # Taking ownership needs the privilege; enabling it twice is harmless.
    try:
        be.enable_privilege("SeTakeOwnershipPrivilege")
    except Exception:  # noqa: BLE001 - best effort, set_owner reports the real error
        pass
    be.set_owner(path, ADMIN_GROUP_SID)
    current = be.get_sddl(path)
    sections = _sddl_sections(current)
    dacl = sections.get("D", "")
    # Already granted (e.g. a re-run, or an inherited full-control ACE)?
    # Appending duplicates is harmless but noisy, so skip instead.  Matching
    # the ACE body rather than the full string keeps inherited
    # ``(A;ID;FA;;;BA)`` counting as "already granted", which is correct:
    # inherited full control grants delete just the same.
    if "FA;;;BA)" in dacl and "FA;;;SY)" in dacl:
        return current
    owner = _sddl_owner(current) or ADMIN_GROUP_SID
    new_sddl = f"O:{owner}D:{dacl}{TAKEOVER_ACES}"
    be.apply_sddl(path, new_sddl)
    return new_sddl


def has_explicit_ti_ace(sddl: str) -> bool:
    """True when an SDDL string carries an explicit (non-inherited) TI ACE."""
    # Explicit ACEs end in ``(A;<flags>;...;<sid>)``; inherited ones carry the
    # ``ID`` flag.  A TrustedInstaller ACE without ``ID`` is redundant.
    for chunk in sddl.split("("):
        if chunk.startswith("A;") and TI_SID.rsplit("-", 1)[-1] in chunk:
            flags = chunk.split(";")[1] if ";" in chunk else ""
            if "ID" not in flags:
                return True
    return False


def snapshot(path: Path, backend: SecurityBackend | None = None) -> AclSnapshot:
    """Capture the current security descriptor for later restoration."""
    import time

    path = Path(path)
    be = backend or get_backend()
    sddl = be.get_sddl(path)
    return AclSnapshot(
        path=str(path),
        owner_sid=_sddl_owner(sddl) or be.get_owner(path),
        sddl=sddl,
        inherited=not has_explicit_ti_ace(sddl),
        captured_at=time.time(),
    )


def restore(snap: AclSnapshot, backend: SecurityBackend | None = None) -> None:
    """Restore a captured descriptor onto its original path."""
    be = backend or get_backend()
    path = Path(snap.path)
    if snap.sddl:
        try:
            be.apply_sddl(path, snap.sddl)
        except AclError:
            pass  # fall through to owner-only restore
    try:
        be.set_owner(path, snap.owner_sid)
    except AclError:
        pass