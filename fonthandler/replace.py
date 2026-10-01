"""The replacement engine.

Reproduces (and hardens) what ``FontReplace.ps1`` did:

1. stage the new bytes next to the target as ``<name>.new`` so it inherits the
   Fonts-directory DACL,
2. set that staged file's owner to TrustedInstaller,
3. hot-replace with ``MoveFileExW(..., MOVEFILE_REPLACE_EXISTING)``,
4. fall back to the reboot queue when the target is locked,
5. snapshot / restore the ACL so the result is genuinely identical to a
   freshly-installed system font.

Unlike the original this engine never touches the system when pointed at a
sandbox directory: the filesystem access goes through an injectable
:class:`FileOps`, so the exact same code path is exercised by the tests.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from . import acl, registry
from .config import TI_SID

__all__ = [
    "ReplaceStatus",
    "ReplaceResult",
    "FileOps",
    "RealFileOps",
    "SandboxFileOps",
    "ReplaceEngine",
    "sha256_file",
    "MOVEFILE_REPLACE_EXISTING",
    "MOVEFILE_DELAY_UNTIL_REBOOT",
]

MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4


def _move_file_ex():
    """Bound ``MoveFileExW`` with its real signatures.

    ``ctypes.windll`` does not thread a last-error value, so the
    ``ctypes.get_last_error()`` that turns a failure into an ``OSError`` used to
    report a stale, unrelated code -- which is how a genuine ``WinError 5``
    could surface as ``WinError 58`` ("指定的服务器无法运行请求的操作") and send the
    hunt after the network stack.
    """
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    return move


class ReplaceStatus(str, Enum):
    SKIP_NO_SOURCE = "skip-no-source"
    SKIP_NO_TARGET = "skip-no-target"
    SKIP_IDENTICAL = "skip-identical"
    HOT = "hot"
    QUEUED = "queued"
    FAILED = "failed"

    @property
    def ok(self) -> bool:
        return self in (ReplaceStatus.HOT, ReplaceStatus.QUEUED,
                        ReplaceStatus.SKIP_IDENTICAL, ReplaceStatus.SKIP_NO_SOURCE,
                        ReplaceStatus.SKIP_NO_TARGET)

    @property
    def is_skip(self) -> bool:
        return self.value.startswith("skip")


@dataclass
class ReplaceResult:
    name: str
    status: ReplaceStatus
    message: str = ""
    staged: Path | None = None
    acl_snapshot: acl.AclSnapshot | None = None
    size: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status.ok

    def describe(self) -> str:
        return {
            ReplaceStatus.SKIP_NO_SOURCE: "源缺失",
            ReplaceStatus.SKIP_NO_TARGET: "目标缺失",
            ReplaceStatus.SKIP_IDENTICAL: "已一致",
            ReplaceStatus.HOT: "已热替换",
            ReplaceStatus.QUEUED: "已排入重启队列",
            ReplaceStatus.FAILED: "失败",
        }.get(self.status, self.status.value)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Filesystem abstraction
# ---------------------------------------------------------------------------
class FileOps:
    """Everything the engine needs from the filesystem."""

    name = "base"

    def exists(self, path: Path) -> bool:
        raise NotImplementedError

    def read(self, path: Path) -> bytes:
        raise NotImplementedError

    def write(self, path: Path, data: bytes) -> None:
        raise NotImplementedError

    def copy(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def replace(self, src: Path, dst: Path) -> None:
        """Atomically replace ``dst`` with ``src`` (MoveFileExW / os.replace)."""
        raise NotImplementedError

    def queue_for_reboot(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def unlink(self, path: Path) -> None:
        raise NotImplementedError

    def stat_size(self, path: Path) -> int:
        raise NotImplementedError


class RealFileOps(FileOps):
    """Live Windows implementation."""

    name = "real"

    def exists(self, path: Path) -> bool:
        return path.exists()

    def read(self, path: Path) -> bytes:
        return path.read_bytes()

    def write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def copy(self, src: Path, dst: Path) -> None:
        shutil.copy2(src, dst)

    def replace(self, src: Path, dst: Path) -> None:
        move = _move_file_ex()
        ok = move(str(src), str(dst), MOVEFILE_REPLACE_EXISTING)
        if not ok:
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")

    def queue_for_reboot(self, src: Path, dst: Path) -> None:
        move = _move_file_ex()
        flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_DELAY_UNTIL_REBOOT
        ok = move(str(src), str(dst), flags)
        if not ok:
            raise OSError(ctypes.get_last_error(), "MoveFileExW(DELAY_UNTIL_REBOOT) failed")
        # Verify the kernel actually persisted it; the original script did not.
        self._assert_pending(src, dst)

    def _assert_pending(self, src: Path, dst: Path) -> None:
        entry = registry.PendingEntry(nt_path(src), nt_path(dst))
        queue = registry.read_pending()
        # Compare with same_rename, not equality: Windows stores the entry with
        # a leading ``*1`` and a ``!`` on a replacement destination, so an exact
        # match against the path we passed in would always fail even though the
        # rename was queued correctly.
        if not any(registry.same_rename(entry, queued) for queued in queue):
            found = ", ".join(f"{q.source} -> {q.dest}" for q in queue) or "(空)"
            raise OSError(
                "MoveFileExW 返回成功，但重启队列里找不到对应条目；"
                f"期望：{entry.source} -> {entry.dest}；实际：{found}"
            )

    def unlink(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def stat_size(self, path: Path) -> int:
        return path.stat().st_size


class SandboxFileOps(FileOps):
    """Same semantics against an arbitrary directory tree.

    ``locked`` lets a test simulate "font is in use by a running application":
    replacing such a target raises, exactly like the real kernel does.
    """

    name = "sandbox"

    def __init__(self, locked: Iterable[Path] = ()) -> None:
        self.locked = {Path(p).resolve() for p in locked}
        self.reboot_queue: list[registry.PendingEntry] = []

    def exists(self, path: Path) -> bool:
        return path.exists()

    def read(self, path: Path) -> bytes:
        return path.read_bytes()

    def write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def copy(self, src: Path, dst: Path) -> None:
        shutil.copy2(src, dst)

    def replace(self, src: Path, dst: Path) -> None:
        if dst.resolve() in self.locked:
            raise PermissionError(f"{dst} is in use")
        os.replace(src, dst)

    def queue_for_reboot(self, src: Path, dst: Path) -> None:
        if src.exists():
            dst.write_bytes(src.read_bytes())
        self.reboot_queue.append(registry.PendingEntry(nt_path(src), nt_path(dst)))

    def unlink(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def stat_size(self, path: Path) -> int:
        return path.stat().st_size


def nt_path(path: Path) -> str:
    """The ``\\??\\C:\\...`` form the kernel stores in the rename queue."""
    text = str(Path(path))
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return "\\??\\" + text


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
@dataclass
class ReplaceOptions:
    set_owner: bool = True
    reset_acl: bool = True
    queue_on_lock: bool = True
    owner_sid: str = TI_SID


class ReplaceEngine:
    """Replace system fonts from a GaspHacked source directory."""

    def __init__(
        self,
        source_dir: Path,
        target_dir: Path,
        ops: FileOps | None = None,
        security: acl.SecurityBackend | None = None,
        registry_backend: registry.RegistryBackend | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.source_dir = Path(source_dir)
        self.target_dir = Path(target_dir)
        self.ops = ops or (RealFileOps() if os.name == "nt" else SandboxFileOps())
        self.security = security
        self.registry = registry_backend
        self.log = log or (lambda level, msg: None)

    # -- helpers ---------------------------------------------------------
    def _security(self) -> acl.SecurityBackend:
        return self.security or acl.get_backend()

    def source_of(self, name: str) -> Path:
        return self.source_dir / name

    def target_of(self, name: str) -> Path:
        return self.target_dir / name

    # -- the operation ---------------------------------------------------
    def replace_one(
        self,
        name: str,
        options: ReplaceOptions | None = None,
        force: bool = False,
    ) -> ReplaceResult:
        """Replace a single font, following the full safe state machine."""
        opts = options or ReplaceOptions()
        src = self.source_of(name)
        dst = self.target_of(name)
        staged = dst.with_name(dst.name + ".new")

        if not self.ops.exists(src):
            return ReplaceResult(name, ReplaceStatus.SKIP_NO_SOURCE, "源文件不存在")
        if not self.ops.exists(dst):
            return ReplaceResult(name, ReplaceStatus.SKIP_NO_TARGET, "目标文件不存在（只替换不新增）")

        if not force:
            try:
                if sha256_file(src) == sha256_file(dst):
                    return ReplaceResult(
                        name, ReplaceStatus.SKIP_IDENTICAL, "内容已一致，无需替换",
                        size=self.ops.stat_size(dst),
                    )
            except OSError as exc:
                return ReplaceResult(name, ReplaceStatus.FAILED, f"哈希比较失败: {exc}")

        # Snapshot the *target's* security before anything touches it.
        snap = None
        try:
            snap = acl.snapshot(dst, self._security())
        except Exception as exc:  # noqa: BLE001 - never block a replace on this
            self.log("warn", f"{name}: 无法读取原权限 ({exc})")

        # Stage next to the target so it inherits the Fonts directory DACL.
        try:
            if self.ops.exists(staged):
                self.ops.unlink(staged)
            self.ops.copy(src, staged)
        except OSError as exc:
            return ReplaceResult(name, ReplaceStatus.FAILED, f"写入 {staged.name} 失败: {exc}",
                                 acl_snapshot=snap)

        # Fix the owner on the *staged* file, before it is moved into place.
        # This has to be fail-closed.  The replacement is invisible until the
        # next boot, by which point the user cannot log in to fix it: Windows
        # renders the logon UI with these very fonts, and a file left owned by
        # the administrator account instead of TrustedInstaller keeps the
        # machine stuck on the sign-in screen.  Carrying on with a warning here
        # replaces the font, breaks the boot, and reports success.
        if opts.set_owner:
            try:
                acl.set_owner_ti(staged, self._security())
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"{name}: 设置 TrustedInstaller 所有者失败，已中止该字体 ({exc})")
                self.ops.unlink(staged)
                return ReplaceResult(
                    name, ReplaceStatus.FAILED,
                    f"无法设置 TrustedInstaller 所有者，已中止替换（{exc}）",
                    staged=staged, acl_snapshot=snap,
                )

        # Hot replace first.
        try:
            self.ops.replace(staged, dst)
            self.log("info", f"{name}: 已热替换")
            if opts.reset_acl:
                self._finalise_security(dst, opts)
            return ReplaceResult(name, ReplaceStatus.HOT, staged=staged,
                                 acl_snapshot=snap, size=self.ops.stat_size(dst))
        except OSError as exc:
            if not opts.queue_on_lock:
                self.ops.unlink(staged)
                return ReplaceResult(name, ReplaceStatus.FAILED, f"热替换失败: {exc}",
                                     staged=staged, acl_snapshot=snap)

        # Fall back to the reboot queue.
        try:
            self.ops.queue_for_reboot(staged, dst)
            if self.registry is not None:
                registry.add_pending(
                    registry.PendingEntry(nt_path(staged), nt_path(dst)), self.registry
                )
            self.log("info", f"{name}: 已排入重启队列")
            return ReplaceResult(name, ReplaceStatus.QUEUED, "文件被占用，已排入重启队列",
                                 staged=staged, acl_snapshot=snap,
                                 size=self.ops.stat_size(src))
        except Exception as exc:  # noqa: BLE001
            self.ops.unlink(staged)
            return ReplaceResult(name, ReplaceStatus.FAILED, f"排入重启队列失败: {exc}",
                                 acl_snapshot=snap)

    def _finalise_security(self, path: Path, opts: ReplaceOptions) -> None:
        """Make the replaced file look like a natively-installed font again.

        Belt-and-braces only, and deliberately warn-only.  The staged file
        already inherited the directory DACL and got its owner fixed *before*
        the rename (see ``replace_one``), so the file in place is correct even
        if both calls here fail.  Warning is safe precisely because the
        fail-closed check upstream is not: a failure there means the font must
        not be installed at all.
        """
        if not opts.reset_acl:
            return
        try:
            acl.reset_to_inherited(path, self._security())
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"{path.name}: 权限继承重置失败 ({exc})")
        if opts.set_owner:
            try:
                acl.set_owner_ti(path, self._security())
            except Exception as exc:  # noqa: BLE001
                self.log("warn", f"{path.name}: 所有者还原失败 ({exc})")

    def replace_many(
        self,
        names: Iterable[str],
        options: ReplaceOptions | None = None,
        force: bool = False,
        progress: Callable[[str, ReplaceResult], None] | None = None,
    ) -> list[ReplaceResult]:
        results: list[ReplaceResult] = []
        for name in names:
            result = self.replace_one(name, options, force)
            results.append(result)
            if progress:
                progress(name, result)
        return results

    # -- maintenance -----------------------------------------------------
    def cleanup_staged(self, names: Iterable[str] | None = None) -> list[Path]:
        """Remove leftover ``*.new`` files from a previous aborted run."""
        removed: list[Path] = []
        targets = [self.target_of(n) for n in names] if names else sorted(self.target_dir.glob("*.new"))
        for staged in targets:
            if self.ops.exists(staged):
                try:
                    self.ops.unlink(staged)
                    removed.append(staged)
                    self.log("info", f"清理残留 {staged.name}")
                except OSError as exc:
                    self.log("warn", f"清理 {staged.name} 失败: {exc}")
        return removed