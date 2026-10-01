"""Sandbox harness: a fake Fonts directory plus in-memory registry & ACL.

Nothing in here ever touches ``C:\\Windows\\Fonts`` -- everything happens in a
throwaway temp directory.  The sandbox is what makes it safe to exercise the
full replace / backup / restore / reboot-queue workflows during development.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Make the package importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fonthandler import acl, config, gasp, pipeline, registry, replace, sfnt  # noqa: E402
from fonthandler.replace import ReplaceEngine  # noqa: E402

__all__ = ["Sandbox", "build_font"]


def build_font(family: str = "TestSans") -> bytes:
    """Create a tiny but structurally valid font file for tests."""
    def table(tag: str, payload: bytes) -> sfnt.TableEntry:
        return sfnt.TableEntry(tag, payload)

    tables = [
        table("head", bytes(54)),                       # zeroed head is fine for tests
        table("hhea", bytes(36)),
        table("maxp", bytes(32)),
        table("cmap", bytes(8) + b"\x00\x00\x00\x00"),
        table("glyf", bytes(16)),
        table("loca", bytes(8)),
        table("name", _name_table(family)),
    ]
    face = sfnt.Face(sfnt.Magic.SFNT, tuple(tables))
    return sfnt.build_sfnt(face)


def _name_table(family: str) -> bytes:
    import struct

    text = family.encode("utf-16-be")
    header = struct.pack(">HHH", 0, 1, 6 + 12)          # format, count, stringOffset
    record = struct.pack(">HHHHHH", 3, 1, 0x0409, 1, len(text), 0)
    return header + record + text


class Sandbox:
    """A disposable target/source font directory with fake security state."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else Path(
            tempfile.mkdtemp(prefix=f"FontHandlerSandbox_{__import__('os').getpid()}_")
        )
        self.fonts_dir = self.root / "Fonts"
        self.source_dir = self.root / "gasp"
        self.backup_dir = self.root / "backups"
        for d in (self.fonts_dir, self.source_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.security = acl.SandboxSecurityBackend()
        self.registry = registry.DictRegistryBackend()
        self.file_ops = replace.SandboxFileOps()
        self.settings = config.Settings(
            source_dir=str(self.source_dir),
            target_dir=str(self.fonts_dir),
            cjk_only=True,
        )
        # Keep config.APP_HOME inside the sandbox so nothing leaks out.
        self._app_home_backup = config.APP_HOME
        config.APP_HOME = self.root

    # -- fixture population ----------------------------------------------
    def add_target(self, name: str, hacked: bool = False) -> Path:
        data = gasp.apply_gasp_hack(build_font()) if hacked else build_font()
        path = self.fonts_dir / name
        path.write_bytes(data)
        return path

    def add_source(self, name: str) -> Path:
        path = self.source_dir / name
        path.write_bytes(gasp.apply_gasp_hack(build_font()))
        return path

    def populate(self, names: list[str] | None = None) -> list[str]:
        names = names or list(config.CJK_WHITELIST)
        for n in names:
            self.add_target(n, hacked=False)
            self.add_source(n)
        return names

    # -- context / engine helpers ----------------------------------------
    def context(self, **overrides) -> pipeline.PipelineContext:
        ctx = pipeline.PipelineContext(
            settings=self.settings,
            security=self.security,
            file_ops=self.file_ops,
            registry=self.registry,
        )
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    def engine(self) -> ReplaceEngine:
        return ReplaceEngine(
            source_dir=self.source_dir,
            target_dir=self.fonts_dir,
            ops=self.file_ops,
            security=self.security,
            registry_backend=self.registry,
        )

    def window(self):
        """The real UI, wired to this sandbox's settings/backends.

        Used by the ``ui`` test group; skips itself (returning ``None``) when
        PyQt6 is not installed so the rest of the suite still runs.
        """
        try:
            from fonthandler.ui.main_window import FontHandlerApp
        except ImportError:  # pragma: no cover - PyQt6 missing
            return None
        return FontHandlerApp.for_testing(self.settings, self.security, self.registry)

    def cleanup(self) -> None:
        config.APP_HOME = self._app_home_backup
        shutil.rmtree(self.root, ignore_errors=True)