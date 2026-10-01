"""Backup packages: zip the original fonts plus a JSON manifest.

A package looks like::

    FontHandlerBackup_YYYYmmdd_HHMMSS.zip
    ├─ manifest.json    [{"name","size","sha256","mtime","sddl","owner",...}]
    └─ fonts/<name>     original bytes

Restoring treats each backed-up file as a *replacement source* and re-runs the
normal replace pipeline (hot replace or reboot queue), then re-applies the
recorded security descriptor.  This is the piece the original script lacked,
where a replacement was irreversible.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import acl
from .config import TI_SID

__all__ = ["BackupEntry", "BackupPackage", "create_backup", "list_backups", "restore_backup"]


@dataclass
class BackupEntry:
    name: str
    size: int = 0
    sha256: str = ""
    mtime: float = 0.0
    sddl: str = ""
    owner: str = TI_SID
    inherited: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "mtime": self.mtime,
            "sddl": self.sddl,
            "owner": self.owner,
            "inherited": self.inherited,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "BackupEntry":
        return cls(
            name=raw.get("name", ""),
            size=int(raw.get("size", 0)),
            sha256=raw.get("sha256", ""),
            mtime=float(raw.get("mtime", 0.0)),
            sddl=raw.get("sddl", ""),
            owner=raw.get("owner", TI_SID),
            inherited=bool(raw.get("inherited", True)),
        )


@dataclass
class BackupPackage:
    path: Path
    created_at: float
    entries: list[BackupEntry] = field(default_factory=list)
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries)

    def summary(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        return f"{self.path.name}  {self.count} 个字体  {self.total_size / 1024 / 1024:.1f} MB  {stamp}"


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _unique_path(output_dir: Path) -> Path:
    """A never-colliding package path.

    Two backups taken inside the same second must not overwrite each other --
    the timestamp alone is not unique enough.
    """
    base = output_dir / f"FontHandlerBackup_{_timestamp()}"
    candidate = base.with_suffix(".zip")
    counter = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{counter}").with_suffix(".zip")
        counter += 1
    return candidate


def create_backup(
    font_names: Iterable[str],
    font_dir: Path,
    output_dir: Path,
    security: acl.SecurityBackend | None = None,
    note: str = "",
    progress: Callable[[str], None] | None = None,
) -> BackupPackage:
    """Zip the given fonts (from ``font_dir``) plus their ACLs into a package."""
    from .replace import sha256_file

    font_dir = Path(font_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    package_path = _unique_path(output_dir)

    entries: list[BackupEntry] = []
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in font_names:
            src = font_dir / name
            if not src.exists():
                continue
            if progress:
                progress(name)
            snap = acl.snapshot(src, security) if security else None
            entry = BackupEntry(
                name=name,
                size=src.stat().st_size,
                sha256=sha256_file(src),
                mtime=src.stat().st_mtime,
                sddl=snap.sddl if snap else "",
                owner=snap.owner_sid if snap else TI_SID,
                inherited=snap.inherited if snap else True,
            )
            entries.append(entry)
            zf.write(src, f"fonts/{name}")
        zf.writestr("manifest.json", json.dumps(
            {"created_at": time.time(), "note": note, "entries": [e.to_dict() for e in entries]},
            ensure_ascii=False, indent=2,
        ))

    return BackupPackage(path=package_path, created_at=time.time(), entries=entries, note=note)


def list_backups(backup_dir: Path) -> list[BackupPackage]:
    """Enumerate packages newest-first, reading each manifest."""
    backup_dir = Path(backup_dir)
    packages: list[BackupPackage] = []
    if not backup_dir.is_dir():
        return packages
    for zip_path in sorted(backup_dir.glob("FontHandlerBackup_*.zip"), reverse=True):
        try:
            with zipfile.ZipFile(zip_path) as zf:
                manifest = json.loads(zf.read("manifest.json"))
        except (OSError, KeyError, ValueError):
            continue
        entries = [BackupEntry.from_dict(e) for e in manifest.get("entries", [])]
        packages.append(
            BackupPackage(
                path=zip_path,
                created_at=float(manifest.get("created_at", zip_path.stat().st_mtime)),
                entries=entries,
                note=manifest.get("note", ""),
            )
        )
    packages.sort(key=lambda p: p.created_at, reverse=True)
    return packages


def extract_backup(package: BackupPackage, dest_dir: Path) -> dict[str, Path]:
    """Unpack a package into ``dest_dir``; returns ``{font_name: path}``."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    with zipfile.ZipFile(package.path) as zf:
        for entry in package.entries:
            member = f"fonts/{entry.name}"
            target = dest_dir / entry.name
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out[entry.name] = target
    return out


def restore_backup(
    package: BackupPackage,
    target_dir: Path,
    engine,
    security: acl.SecurityBackend | None = None,
    work_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> list:
    """Restore a package through the replace engine, then re-apply security.

    ``engine`` is a :class:`fonthandler.replace.ReplaceEngine` (or any object
    with ``replace_one``), so the restore path is identical to a normal replace
    and equally sandbox-testable.
    """
    import tempfile

    work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="fonthandler_restore_"))
    unpacked = extract_backup(package, work_dir)

    results = []
    for entry in package.entries:
        if progress:
            progress(entry.name)
        source = unpacked.get(entry.name)
        if source is None:
            continue
        # Temporarily point the engine at the unpacked copy as its source.
        original_source_dir = engine.source_dir
        original_source = engine.source_of(entry.name)
        try:
            # Stage from the unpacked file by writing it as the engine's source.
            engine.source_dir = work_dir
            result = engine.replace_one(entry.name)
            results.append(result)
            if result.ok and entry.sddl:
                try:
                    acl.apply_sddl(engine.target_of(entry.name), entry.sddl, security)
                    acl.set_owner_ti(engine.target_of(entry.name), security)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            engine.source_dir = original_source_dir
            del original_source
    return results