"""The high-level workflows, independent of Qt.

Each function takes a :class:`PipelineContext` (settings + injectable ops +
a ``log``/``progress`` callback) and drives the matching engine, emitting
progress as it goes.  This is exactly what the UI worker threads call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import acl, backup as backup_mod, config, gasp, registry, replace, session
from .config import Settings
from .gasp import CancellationToken, Cancelled
from .replace import ReplaceEngine, ReplaceOptions, ReplaceResult

__all__ = ["PipelineContext", "default_context", "generate_gasp",
           "scan_targets", "run_replace", "run_backup", "run_restore",
           "list_pending", "clear_pending", "scan_acl"]


def default_file_ops(security: acl.SecurityBackend | None) -> replace.FileOps:
    """Pick a real filesystem driver only when we are really on Windows.

    A test that injects its own security backend gets the sandbox filesystem
    automatically, which is the belt-and-braces version of the safety policy.
    """
    import os as _os

    if _os.name != "nt":
        return replace.SandboxFileOps()
    if isinstance(security, acl.SandboxSecurityBackend):
        return replace.SandboxFileOps()
    return replace.RealFileOps()


@dataclass
class PipelineContext:
    """Everything a workflow needs; ops are injectable for sandbox tests."""

    settings: Settings
    security: acl.SecurityBackend | None = None
    file_ops: replace.FileOps | None = None
    registry: registry.RegistryBackend | None = None
    log: Callable[[str, str], None] = lambda level, msg: None
    progress: Callable[[str, int, int], None] = lambda label, cur, total: None
    token: CancellationToken = field(default_factory=CancellationToken)

    # -- convenience -----------------------------------------------------
    @property
    def source_dir(self) -> Path:
        return self.settings.resolved_source_dir()

    @property
    def target_dir(self) -> Path:
        return self.settings.resolved_target_dir()

    @property
    def is_sandbox(self) -> bool:
        return self.settings.is_sandbox()

    def target_names(self) -> list[str]:
        """Which font files to operate on, honouring CJK-only + excludes."""
        if self.settings.cjk_only:
            return list(config.CJK_WHITELIST)
        if self.target_dir.is_dir():
            return sorted(
                p.name for p in self.target_dir.iterdir()
                if p.suffix.lower() in (".ttf", ".ttc")
                and p.name.lower() not in {e.lower() for e in config.GASP_EXCLUDES}
            )
        return list(config.CJK_WHITELIST)

    def engine(self, source: Path | None = None) -> ReplaceEngine:
        return ReplaceEngine(
            source_dir=source or self.source_dir,
            target_dir=self.target_dir,
            ops=self.file_ops or default_file_ops(self.security),
            security=self.security,
            registry_backend=self.registry,
            log=self.log,
        )

    def _check_cancel(self) -> None:
        if self.token.cancelled:
            raise Cancelled()

    def _backup_dir(self) -> Path:
        return config.APP_HOME / "backups"


def default_context(settings: Settings | None = None) -> PipelineContext:
    settings = settings or config.load_settings()
    return PipelineContext(settings=settings)


# ---------------------------------------------------------------------------
# 1. GaspHack generation
# ---------------------------------------------------------------------------
def generate_gasp(ctx: PipelineContext, input_dir: Path | None = None,
                  output_dir: Path | None = None) -> gasp.BatchReport:
    """Run the GaspHack engine over the system font directory."""
    input_dir = Path(input_dir or ctx.target_dir)
    output_dir = Path(output_dir or ctx.source_dir)
    files = ctx.target_names() if ctx.settings.cjk_only else None

    def progress(name: str, stage: str) -> None:
        # stage carries "[i/total]"
        try:
            idx, total = stage.strip("[]").split("/")
            ctx.progress(name, int(idx), int(total))
        except (ValueError, AttributeError):
            ctx.progress(name, 0, 0)

    return gasp.run_batch(
        input_dir=input_dir,
        output_dir=output_dir,
        files=files,
        token=ctx.token,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# 2. Scan
# ---------------------------------------------------------------------------
@dataclass
class ScanRow:
    name: str
    present_source: bool
    present_target: bool
    identical: bool
    source_size: int = 0
    target_size: int = 0
    families: str = ""


def scan_targets(ctx: PipelineContext) -> list[ScanRow]:
    """Compare source vs target without modifying anything."""
    from .replace import sha256_file

    rows: list[ScanRow] = []
    names = ctx.target_names()
    for name in names:
        src = ctx.source_dir / name
        dst = ctx.target_dir / name
        row = ScanRow(
            name=name,
            present_source=src.exists(),
            present_target=dst.exists(),
            identical=False,
        )
        if row.present_source:
            row.source_size = src.stat().st_size
            try:
                row.families = ", ".join(
                    n for n in gasp.read_family_names(src.read_bytes()) if n
                )
            except Exception:  # noqa: BLE001
                row.families = ""
        if row.present_target:
            row.target_size = dst.stat().st_size
        if row.present_source and row.present_target:
            try:
                row.identical = sha256_file(src) == sha256_file(dst)
            except OSError:
                row.identical = False
        rows.append(row)
        ctx.progress(name, len(rows), len(names))
    return rows


# ---------------------------------------------------------------------------
# 3. Replace
# ---------------------------------------------------------------------------
def run_replace(
    ctx: PipelineContext,
    names: Iterable[str] | None = None,
    force: bool = False,
    backup_first: bool | None = None,
) -> list[ReplaceResult]:
    """Back up (optional) then replace the selected system fonts."""
    engine = ctx.engine()
    names = list(names) if names is not None else ctx.target_names()
    do_backup = ctx.settings.backup_before_replace if backup_first is None else backup_first

    if do_backup:
        # Only back up targets that actually exist and will be touched.
        to_backup = [n for n in names if (ctx.target_dir / n).exists()]
        if to_backup:
            try:
                backup_mod.create_backup(
                    to_backup,
                    ctx.target_dir,
                    ctx._backup_dir(),
                    security=ctx.security,
                    note="auto before replace",
                    progress=lambda n: ctx.progress(n, 0, len(to_backup)),
                )
                ctx.log("info", f"已自动备份 {len(to_backup)} 个字体")
            except Exception as exc:  # noqa: BLE001
                ctx.log("error", f"自动备份失败: {exc}")

    options = ReplaceOptions(
        set_owner=ctx.settings.restore_trusted_installer,
        reset_acl=ctx.settings.reset_acl_to_inherited,
        queue_on_lock=ctx.settings.queue_on_lock,
    )

    results: list[ReplaceResult] = []
    for index, name in enumerate(names, 1):
        ctx._check_cancel()
        ctx.progress(name, index, len(names))
        result = engine.replace_one(name, options, force)
        results.append(result)
        ctx.log("info" if result.ok else "error",
                f"{result.name}: {result.status.value} {result.message}".strip())
    return results


# ---------------------------------------------------------------------------
# 4. Backup / restore (manual)
# ---------------------------------------------------------------------------
def run_backup(ctx: PipelineContext, names: Iterable[str] | None = None,
               note: str = "manual") -> backup_mod.BackupPackage:
    names = list(names) if names is not None else [n for n in ctx.target_names()
                                                    if (ctx.target_dir / n).exists()]
    return backup_mod.create_backup(
        names,
        ctx.target_dir,
        ctx._backup_dir(),
        security=ctx.security,
        note=note,
        progress=lambda n: ctx.progress(n, 0, len(names)),
    )


def list_backups(ctx: PipelineContext) -> list[backup_mod.BackupPackage]:
    return backup_mod.list_backups(ctx._backup_dir())


def run_restore(ctx: PipelineContext, package: backup_mod.BackupPackage,
                progress: Callable[[str], None] | None = None) -> list[ReplaceResult]:
    engine = ctx.engine(source=ctx.target_dir)  # replaced below per-entry
    return backup_mod.restore_backup(
        package,
        ctx.target_dir,
        engine,
        security=ctx.security,
        progress=progress or (lambda n: ctx.progress(n, 0, package.count)),
    )


# ---------------------------------------------------------------------------
# 5. Pending / ACL inspection
# ---------------------------------------------------------------------------
def list_pending(ctx: PipelineContext) -> list[registry.PendingEntry]:
    return session.read_pending(ctx.registry)


def clear_pending(ctx: PipelineContext) -> int:
    before = len(session.read_pending(ctx.registry))
    session.clear_our_pending(ctx.registry)
    after = len(session.read_pending(ctx.registry))
    return before - after


def scan_acl(ctx: PipelineContext) -> list[tuple[str, str, str]]:
    """Return ``[(name, owner, sddl)]`` for the CJK targets (read-only)."""
    out: list[tuple[str, str, str]] = []
    for name in ctx.target_names():
        path = ctx.target_dir / name
        if not path.exists():
            continue
        owner = acl.get_owner(path, ctx.security)
        sddl = acl.get_sddl(path, ctx.security)
        out.append((name, owner, sddl))
        ctx.progress(name, len(out), len(ctx.target_names()))
    return out