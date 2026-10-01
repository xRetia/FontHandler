"""Registry access with an injectable backend.

Two things the original tool chain needed from the registry, and one it did
not but should have:

* ``HKLM\\...\\Fonts`` -- inspect/enumerate registered font names.
* ``PendingFileRenameOperations`` -- the reboot rename queue that a locked
  font file falls back to.
* recording the *original* ACL/owner of a replaced font so it can be restored
  later (the original script had no such record).

A :class:`DictRegistryBackend` provides the same surface with plain dicts so
the whole pipeline can be exercised in the sandbox without touching HKLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "RegistryError",
    "RegistryBackend",
    "WinRegBackend",
    "DictRegistryBackend",
    "get_backend",
    "set_backend",
    "PENDING_KEY",
    "FONT_KEY",
    "PendingEntry",
    "read_pending",
    "write_pending",
    "add_pending",
    "remove_pending",
    "clear_our_pending",
    "list_fonts",
    "strip_pending_markers",
    "same_rename",
]

PENDING_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager"
PENDING_VALUE = "PendingFileRenameOperations"
FONT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

#: Files staged by this tool end in ".new"; used to recognise "our" pending
#: entries so we never delete somebody else's queued rename.
OUR_SUFFIX = ".new"


class RegistryError(RuntimeError):
    """Raised when a registry operation fails."""


class RegistryBackend:
    """Interface implemented by the real and the sandbox backends."""

    name = "base"

    def get_value(self, key: str, value: str) -> object | None:
        raise NotImplementedError

    def set_value(self, key: str, value: str, data, type_code: int) -> None:
        raise NotImplementedError

    def delete_value(self, key: str, value: str) -> bool:
        raise NotImplementedError

    def list_values(self, key: str) -> list[tuple[str, object]]:
        raise NotImplementedError

    def key_exists(self, key: str) -> bool:
        raise NotImplementedError


class DictRegistryBackend(RegistryBackend):
    """Pure-dict registry for tests and sandbox runs."""

    name = "dict"

    def __init__(self, seed: dict[str, dict[str, tuple[object, int]]] | None = None) -> None:
        # {key: {value_name: (data, type)}}
        self.data: dict[str, dict[str, tuple[object, int]]] = {
            k: dict(v) for k, v in (seed or {}).items()
        }

    @staticmethod
    def _norm(key: str) -> str:
        return key.replace("/", "\\").strip("\\").upper()

    def get_value(self, key: str, value: str):
        entry = self.data.get(self._norm(key), {})
        item = entry.get(value)
        return item[0] if item else None

    def set_value(self, key: str, value: str, data, type_code: int) -> None:
        self.data.setdefault(self._norm(key), {})[value] = (data, type_code)

    def delete_value(self, key: str, value: str) -> bool:
        return self.data.get(self._norm(key), {}).pop(value, None) is not None

    def list_values(self, key: str) -> list[tuple[str, object]]:
        return [(name, item[0]) for name, item in self.data.get(self._norm(key), {}).items()]

    def key_exists(self, key: str) -> bool:
        return self._norm(key) in self.data


class WinRegBackend(RegistryBackend):
    """Real ``winreg`` backend, always under HKLM."""

    name = "winreg"

    def _open(self, key: str, access: int):  # pragma: no cover - Windows only
        import winreg

        return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, access)

    def get_value(self, key: str, value: str):  # pragma: no cover - Windows only
        import winreg

        try:
            with self._open(key, winreg.KEY_READ) as handle:
                data, _type = winreg.QueryValueEx(handle, value)
                return data
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RegistryError(str(exc)) from exc

    def set_value(self, key: str, value: str, data, type_code: int) -> None:  # pragma: no cover
        import winreg

        try:
            with self._open(key, winreg.KEY_SET_VALUE) as handle:
                winreg.SetValueEx(handle, value, 0, type_code, data)
        except OSError as exc:
            raise RegistryError(str(exc)) from exc

    def delete_value(self, key: str, value: str) -> bool:  # pragma: no cover
        import winreg

        try:
            with self._open(key, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, value)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RegistryError(str(exc)) from exc

    def list_values(self, key: str) -> list[tuple[str, object]]:  # pragma: no cover
        import winreg

        out: list[tuple[str, object]] = []
        try:
            with self._open(key, winreg.KEY_READ) as handle:
                index = 0
                while True:
                    try:
                        name, data, _type = winreg.EnumValue(handle, index)
                    except OSError:
                        break
                    out.append((name, data))
                    index += 1
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise RegistryError(str(exc)) from exc
        return out

    def key_exists(self, key: str) -> bool:  # pragma: no cover
        import winreg

        try:
            with self._open(key, winreg.KEY_READ):
                return True
        except OSError:
            return False


_BACKEND: RegistryBackend | None = None


def get_backend() -> RegistryBackend:
    global _BACKEND
    if _BACKEND is None:
        try:
            import winreg  # noqa: F401

            _BACKEND = WinRegBackend()
        except ImportError:  # pragma: no cover
            _BACKEND = DictRegistryBackend()
    return _BACKEND


def set_backend(backend: RegistryBackend | None) -> None:
    global _BACKEND
    _BACKEND = backend


# ---------------------------------------------------------------------------
# PendingFileRenameOperations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PendingEntry:
    """One queued rename: a ``source -> dest`` pair, both NT paths."""

    source: str
    dest: str

    @property
    def is_ours(self) -> bool:
        return strip_pending_markers(self.source).lower().endswith(OUR_SUFFIX)


#: Windows tags entries in ``PendingFileRenameOperations`` with a leading
#: marker: ``*1`` on every entry, and ``!`` on a destination that is to be
#: replaced (the form produced by ``MOVEFILE_REPLACE_EXISTING``).  These appear
#: in the queue but are not part of the path, so they have to be stripped before
#: comparing an entry we built ourselves with one read back from the registry --
#: otherwise a perfectly good rename looks like it never got queued.
_PENDING_MARKER = re.compile(r"^[*!\d]+")


def strip_pending_markers(path: str) -> str:
    """Drop the leading ``*1``/``!`` markers from a queued path."""
    return _PENDING_MARKER.sub("", path or "")


def _same_path(a: str, b: str) -> bool:
    return (
        strip_pending_markers(a).replace("/", "\\").casefold()
        == strip_pending_markers(b).replace("/", "\\").casefold()
    )


def same_rename(a: "PendingEntry", b: "PendingEntry") -> bool:
    """True when two entries request the same source -> destination rename.

    Compares with the Windows markers stripped and case-insensitively, because
    the same rename is written one way by :func:`MoveFileEx` and another way by
    this module.
    """
    return _same_path(a.source, b.source) and _same_path(a.dest, b.dest)


def _normalise_multisz(raw: object) -> list[str]:
    """Split a REG_MULTI_SZ into its strings, preserving empty ones.

    An empty string is meaningful here: a pair whose destination is empty means
    "delete the source at reboot".  Dropping the empties shifts every following
    pair, so deletions get read as renames and the queue is silently corrupted
    the next time it is written back.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        # winreg hands back the strings already split, with no terminator.
        return [str(item) for item in raw]
    parts = str(raw).split("\x00")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def read_pending(backend: RegistryBackend | None = None) -> list[PendingEntry]:
    """Read the reboot queue as structured pairs.

    The value is REG_MULTI_SZ and may be split across
    ``PendingFileRenameOperations`` and ``...Operations2`` by Session Manager
    when the list overflows; both are merged, 2 first.
    """
    be = backend or get_backend()
    raw: list[str] = []
    for value_name in (PENDING_VALUE + "2", PENDING_VALUE):
        raw.extend(_normalise_multisz(be.get_value(PENDING_KEY, value_name)))
    entries: list[PendingEntry] = []
    for index in range(0, len(raw) - 1, 2):
        entries.append(PendingEntry(raw[index], raw[index + 1]))
    return entries


def write_pending(entries: Iterable[PendingEntry], backend: RegistryBackend | None = None) -> None:
    """Persist the queue, clearing the overflow slot first."""
    be = backend or get_backend()
    flat: list[str] = []
    for entry in entries:
        flat.append(entry.source)
        flat.append(entry.dest)
    # Drop the overflow value; we keep everything in the primary slot.
    be.delete_value(PENDING_KEY, PENDING_VALUE + "2")
    if not flat:
        be.delete_value(PENDING_KEY, PENDING_VALUE)
        return
    try:
        import winreg

        type_code = winreg.REG_MULTI_SZ
    except ImportError:  # pragma: no cover
        type_code = 7  # REG_MULTI_SZ
    be.set_value(PENDING_KEY, PENDING_VALUE, flat, type_code)


def add_pending(entry: PendingEntry, backend: RegistryBackend | None = None) -> list[PendingEntry]:
    """Append one rename to the queue (idempotent: skips equivalent duplicates).

    "Equivalent" is marker- and case-insensitive.  ``MoveFileEx`` and this
    module write the same rename in different spellings, so an exact string
    comparison would queue every rename twice: once by the kernel (with the
    ``*1``/``!`` markers) and once by us (without).
    """
    be = backend or get_backend()
    current = read_pending(be)
    if any(same_rename(entry, existing) for existing in current):
        return current
    current.append(entry)
    write_pending(current, be)
    return current


def remove_pending(entry: PendingEntry, backend: RegistryBackend | None = None) -> list[PendingEntry]:
    be = backend or get_backend()
    current = [e for e in read_pending(be) if not same_rename(e, entry)]
    write_pending(current, be)
    return current


def clear_our_pending(backend: RegistryBackend | None = None) -> list[PendingEntry]:
    """Remove only the entries this tool staged (``*.new -> target``)."""
    be = backend or get_backend()
    kept = [e for e in read_pending(be) if not e.is_ours]
    write_pending(kept, be)
    return kept


def list_fonts(backend: RegistryBackend | None = None) -> dict[str, str]:
    """Map registered font display name -> registered file name."""
    be = backend or get_backend()
    out: dict[str, str] = {}
    for name, data in be.list_values(FONT_KEY):
        if isinstance(data, str):
            out[name] = data
    return out