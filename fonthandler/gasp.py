"""GaspHack engine -- pure Python replacement for the original tool chain.

Original chain::

    ttx.exe -o out.ttf -m in.ttf GaspHack_v2.ttx     # per TTF
    UniteTTC.exe in.ttc                               # split a TTC (raw tables)
    AllUniteTTC.exe                                   # rebuild Fonts.TTC

This module reproduces all three, byte for byte, without any external binary:

* :func:`apply_gasp_hack` -- rewrite the ``gasp`` table to
  ``{rangeMaxPPEM: 0xFFFF -> rangeGaspBehavior: 0x000A}``
  (GASP_GRIDFIT | GASP_DOGRAY), i.e. grid-fit + grayscale AA at every size.
* :func:`split_ttc` -- raw table extraction, like UniteTTC.
* :func:`unite_ttc` -- rebuild with identical-table sharing, like AllUniteTTC.

Because only the ``gasp`` table is touched, everything else (glyphs, metrics,
``head``, layout tables) is preserved bit for bit, which is what makes the
operation idempotent and safe on a live system.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import sfnt
from .sfnt import Face, SfntError

__all__ = [
    "GASP_TABLE",
    "GASP_VERSION",
    "GASP_RANGES",
    "FontJobResult",
    "BatchReport",
    "gasp_table_bytes",
    "has_gasp_hack",
    "apply_gasp_hack",
    "split_ttc",
    "unite_ttc",
    "verify_ttc",
    "process_ttf",
    "process_ttc",
    "process_file",
    "run_batch",
    "read_family_names",
    "CancellationToken",
    "Cancelled",
]

# ---------------------------------------------------------------------------
# gasp constants (identical to GaspHack_v2.ttx)
# ---------------------------------------------------------------------------
GASP_RANGES: dict[int, int] = {0xFFFF: 0x000A}  # GASP_GRIDFIT(0x02) | GASP_DOGRAY(0x08)
GASP_VERSION = 0x0001
GASP_TABLE = "gasp"

ProgressCb = Callable[[str, str], None]   # (current-file, stage-text)
CancelCb = Callable[[], bool]             # -> True when the user aborted


class Cancelled(Exception):
    """Raised internally when a :class:`CancellationToken` trips."""


class CancellationToken:
    """Cooperative cancellation shared with the worker thread."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def reset(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def gasp_table_bytes(ranges: dict[int, int] | None = None) -> bytes:
    """Serialise a ``gasp`` table (sorted by ``rangeMaxPPEM``, per spec)."""
    items = sorted((ranges or GASP_RANGES).items())
    out = bytearray(struct.pack(">HH", GASP_VERSION, len(items)))
    for max_ppem, behaviour in items:
        out += struct.pack(">HH", max_ppem, behaviour)
    return bytes(out)


def gasp_ranges_of(face: Face) -> dict[int, int] | None:
    """Parse an existing ``gasp`` table into ``{rangeMaxPPEM: behaviour}``."""
    payload = face.table(GASP_TABLE)
    if payload is None or len(payload) < 4:
        return None
    try:
        _version, count = struct.unpack(">HH", payload[:4])
        if count == 0 or len(payload) < 4 + 4 * count:
            return None
        data = struct.unpack(f">{2*count}H", payload[4 : 4 + 4 * count])
        return {int(data[i]): int(data[i + 1]) for i in range(0, len(data), 2)}
    except struct.error:  # pragma: no cover - defensive
        return None


def has_gasp_hack(data: bytes) -> bool:
    """True when every face already carries the exact GaspHack ``gasp`` table."""
    try:
        faces = faces_of(data)
    except SfntError:
        return False
    return all(bool(gasp_ranges_of(face)) for face in faces) and all(
        gasp_ranges_of(face) == GASP_RANGES for face in faces
    )


def faces_of(data: bytes) -> list[Face]:
    """Normalise a font file into a list of faces."""
    if sfnt.is_ttc(data):
        return sfnt.split_ttc(data)[1]
    return [sfnt.split_sfnt(data)]


def materialise(face: Face, payload: bytes) -> Face:
    """Patch ``gasp`` into ``face`` and recompute its master checksum.

    Mirrors what ``ttx`` does to the standalone TTF that ``UniteTTC`` produces
    for every face of a collection -- this is why the reference pipeline's
    per-face ``head.checkSumAdjustment`` values can be reproduced exactly.
    """
    return sfnt.recalc_master_face(face.with_table(GASP_TABLE, payload))


def apply_gasp_hack(data: bytes, ranges: dict[int, int] | None = None) -> bytes:
    """Return ``data`` with the GaspHack ``gasp`` table applied.

    Accepts both standalone fonts and TTC collections; idempotent.
    """
    payload = gasp_table_bytes(ranges)
    if sfnt.is_ttc(data):
        header, faces = sfnt.split_ttc(data)
        return sfnt.build_ttc([materialise(face, payload) for face in faces], header)
    return sfnt.build_sfnt(materialise(sfnt.split_sfnt(data), payload))


# ---------------------------------------------------------------------------
# TTC split / merge (UniteTTC / AllUniteTTC equivalents)
# ---------------------------------------------------------------------------
def split_ttc(data: bytes) -> list[Face]:
    """Split a TTC into per-face table lists (shared tables duplicated)."""
    return sfnt.split_ttc(data)[1]


def unite_ttc(faces: Sequence[Face]) -> bytes:
    """Merge per-face tables back into a TTC with shared-table optimisation.

    Each face is first materialised as a standalone font so its
    ``head.checkSumAdjustment`` matches what ``ttx`` computed for the split TTF.
    """
    data = sfnt.build_ttc([sfnt.recalc_master_face(face) for face in faces])
    verify_ttc(data, expected_faces=len(faces))
    return data


def verify_ttc(data: bytes, expected_faces: int | None = None) -> None:
    """Sanity check a freshly written collection.

    Raises :class:`SfntError` when the result cannot be parsed back or when the
    face count drifted -- this is the safety net that replaces the original
    tools' zero error reporting.
    """
    if not sfnt.is_ttc(data):
        raise SfntError("rebuilt collection is not a TTC")
    _version, faces = sfnt.split_ttc(data)
    if expected_faces is not None and len(faces) != expected_faces:
        raise SfntError(f"face count drift: expected {expected_faces}, got {len(faces)}")
    for index, face in enumerate(faces):
        if not face.tables:
            raise SfntError(f"face #{index} has no tables")


# ---------------------------------------------------------------------------
# file level helpers
# ---------------------------------------------------------------------------
@dataclass
class FontJobResult:
    name: str
    kind: str  # "ttf" | "ttc"
    status: str  # "hacked" | "unchanged" | "failed" | "skipped"
    size: int = 0
    input_size: int = 0
    faces: int = 1
    already_hacked: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("hacked", "unchanged")


@dataclass
class BatchReport:
    results: list[FontJobResult] = field(default_factory=list)

    def add(self, result: FontJobResult) -> None:
        self.results.append(result)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hacked(self) -> int:
        return sum(1 for r in self.results if r.status == "hacked")

    @property
    def unchanged(self) -> int:
        return sum(1 for r in self.results if r.status == "unchanged")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")


def process_ttf(src: Path, dst: Path) -> FontJobResult:
    """Apply GaspHack to a single .ttf file."""
    data = src.read_bytes()
    already = has_gasp_hack(data)
    out = apply_gasp_hack(data)
    _atomic_write(dst, out)
    return FontJobResult(
        name=src.name,
        kind="ttf",
        status="unchanged" if out == data else "hacked",
        size=len(out),
        input_size=len(data),
        faces=1,
        already_hacked=already,
    )


def process_ttc(src: Path, dst: Path) -> FontJobResult:
    """Split a .ttc, GaspHack every face, merge back (AllUniteTTC equivalent)."""
    data = src.read_bytes()
    already = has_gasp_hack(data)
    header, faces = sfnt.split_ttc(data)
    payload = gasp_table_bytes()
    out = sfnt.build_ttc([materialise(face, payload) for face in faces], header)
    verify_ttc(out, expected_faces=len(faces))
    _atomic_write(dst, out)
    return FontJobResult(
        name=src.name,
        kind="ttc",
        status="unchanged" if out == data else "hacked",
        size=len(out),
        input_size=len(data),
        faces=len(faces),
        already_hacked=already,
    )


def process_file(src: Path, dst: Path, token: CancellationToken | None = None) -> FontJobResult:
    """Dispatch to :func:`process_ttf` / :func:`process_ttc` by file magic."""
    if token is not None and token.cancelled:
        raise Cancelled()
    try:
        head = src.read_bytes()[:4]
    except OSError as exc:
        return FontJobResult(src.name, "?", "failed", message=str(exc))
    try:
        if head == sfnt.Magic.TTCF:
            return process_ttc(src, dst)
        if sfnt.is_sfnt(head):
            return process_ttf(src, dst)
        return FontJobResult(src.name, "?", "failed", message="not a TrueType font")
    except (SfntError, OSError, struct.error) as exc:
        kind = "ttc" if head == sfnt.Magic.TTCF else "?"
        return FontJobResult(src.name, kind, "failed", message=str(exc))


def run_batch(
    input_dir: Path,
    output_dir: Path,
    files: Iterable[str] | None = None,
    excludes: Iterable[str] | None = None,
    token: CancellationToken | None = None,
    progress: ProgressCb | None = None,
) -> BatchReport:
    """Process every ``.ttf``/``.ttc`` found in ``input_dir``.

    ``files`` restricts the run to an explicit list of file names (used by the
    CJK-only mode); ``None`` means "everything in the directory".  ``excludes``
    is a list of file names to always skip (emoji/symbol fonts whose glyphs
    break under grid-fitting); it applies in both modes.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = BatchReport()

    if not input_dir.is_dir():
        raise NotADirectoryError(str(input_dir))

    excluded = {name.lower() for name in (excludes or ())}

    available = {
        p.name.lower(): p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".ttf", ".ttc")
        and p.name.lower() not in excluded
    }
    if files is None:
        candidates = [available[key] for key in sorted(available)]
    else:
        wanted = {name.lower() for name in files if name.lower() not in excluded}
        candidates = [available[key] for key in sorted(available) if key in wanted]

    total = len(candidates)
    for index, src in enumerate(candidates, 1):
        if token is not None and token.cancelled:
            raise Cancelled()
        if progress:
            progress(src.name, f"[{index}/{total}]")
        report.add(process_file(src, output_dir / src.name, token))

    # keep the output folder a faithful mirror of the processed set
    keep = {p.name.lower() for p in candidates}
    for stale in output_dir.iterdir():
        if (
            stale.is_file()
            and stale.suffix.lower() in (".ttf", ".ttc")
            and stale.name.lower() not in keep
        ):
            try:
                stale.unlink()
            except OSError:
                pass
    return report


def _atomic_write(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, dst)


# ---------------------------------------------------------------------------
# name table introspection (no fontTools required)
# ---------------------------------------------------------------------------
def read_family_names(data: bytes) -> list[str]:
    """Best effort family name per face; empty string when unavailable."""
    try:
        faces = faces_of(data)
    except SfntError:
        return []
    return [_parse_name_table(face.table("name")) if face.table("name") else "" for face in faces]


def _parse_name_table(raw: bytes | None) -> str:
    if not raw or len(raw) < 6:
        return ""
    try:
        count, string_offset = struct.unpack(">HH", raw[2:6])
        best = ""
        best_rank = -1
        for i in range(count):
            base = 6 + i * 12
            platform, encoding, _lang, name_id, length, offset = struct.unpack(
                ">HHHHHH", raw[base : base + 12]
            )
            if name_id not in (1, 4, 16):
                continue
            start = string_offset + offset
            text = _decode_name(raw[start : start + length], platform, encoding)
            if not text:
                continue
            rank = 2 if platform == 3 else (1 if platform == 0 else 0)
            if rank > best_rank or (rank == best_rank and len(text) > len(best)):
                best, best_rank = text, rank
        return best
    except (struct.error, IndexError):  # pragma: no cover - defensive
        return ""


def _decode_name(payload: bytes, platform: int, encoding: int) -> str:
    try:
        if platform == 3 or (platform == 0 and encoding in (3, 4, 5, 6)):
            return payload.decode("utf-16-be", "ignore").strip("\x00").strip()
        if platform == 1:
            return payload.decode("mac-roman", "ignore").strip()
        return payload.decode("latin-1", "ignore").strip()
    except UnicodeDecodeError:  # pragma: no cover
        return ""