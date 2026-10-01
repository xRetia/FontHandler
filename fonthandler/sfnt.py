"""Low level sfnt / TTC (font collection) reader & writer.

Everything in this module works on raw table bytes.  It deliberately avoids a
full font parser: the GaspHack only needs to swap a single table, and the TTC
split/merge needs to reproduce the exact byte layout that ``ttx.exe`` +
``UniteTTC.exe`` / ``AllUniteTTC.exe`` produce (including their shared-table
optimisation).

Byte-for-byte compatibility notes (verified against the original tool chain):

* tables are written in their **original physical order** (ascending offset),
  while the table *directory* is sorted by tag -- both ``ttx`` and ``UniteTTC``
  behave this way;
* the ``head`` directory entry checksum is computed with ``checkSumAdjustment``
  zeroed out;
* ``head.checkSumAdjustment`` is recomputed as
  ``0xB1B0AFBA - (sum of table checksums + checksum of the directory)``;
* for a collection each face is first materialised as a standalone font (which
  is where the reference tools compute its adjustment), then merged back with
  identical ``(tag, payload)`` pairs written only once.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "SfntError",
    "TableEntry",
    "Face",
    "split_sfnt",
    "build_sfnt",
    "read_table",
    "replace_table",
    "drop_table",
    "split_ttc",
    "build_ttc",
    "is_ttc",
    "is_sfnt",
    "sfnt_checksum",
    "font_count",
    "Magic",
    "MASTER_CHECKSUM_MAGIC",
]

MASTER_CHECKSUM_MAGIC = 0xB1B0AFBA
_HEAD = "head"


class SfntError(ValueError):
    """Raised when a font file cannot be parsed."""


class Magic:
    SFNT = b"\x00\x01\x00\x00"   # TrueType outlines
    TRUE = b"true"              # legacy Apple TrueType
    OTTO = b"OTTO"              # CFF outlines
    TTCF = b"ttcf"              # font collection

    @classmethod
    def name(cls, version: bytes) -> str:
        return {
            cls.SFNT: "TrueType",
            cls.TRUE: "TrueType(legacy)",
            cls.OTTO: "CFF/OTTO",
        }.get(version, version.decode("latin-1", "replace"))


@dataclass(frozen=True)
class TableEntry:
    """One table of a font file."""

    tag: str
    data: bytes


@dataclass(frozen=True)
class Face:
    """One font: its ``sfnt`` version plus its tables in **physical order**.

    ``tables`` is ordered by ascending offset in the source file, which is the
    order the reference tools write them back in.  The on-disk *directory* is
    always sorted by tag, so the two orders may differ.
    """

    version: bytes
    tables: tuple[TableEntry, ...]

    def table(self, tag: str) -> bytes | None:
        for entry in self.tables:
            if entry.tag == tag:
                return entry.data
        return None

    def tags(self) -> list[str]:
        return [entry.tag for entry in self.tables]

    def with_table(self, tag: str, payload: bytes) -> "Face":
        """Return a copy where ``tag`` is replaced (or appended)."""
        _check_tag(tag)
        out: list[TableEntry] = []
        replaced = False
        for entry in self.tables:
            if entry.tag == tag:
                if not replaced:
                    out.append(TableEntry(tag, payload))
                    replaced = True
            else:
                out.append(entry)
        if not replaced:
            out.append(TableEntry(tag, payload))
        return Face(self.version, tuple(out))


def _check_tag(tag: str) -> bytes:
    raw = tag.encode("latin-1") if isinstance(tag, str) else tag
    if len(raw) != 4:
        raise SfntError(f"invalid table tag {tag!r}")
    return raw


def _pad4(n: int) -> int:
    return (4 - (n & 3)) & 3


def sfnt_checksum(data: bytes) -> int:
    """Standard sfnt table checksum (sum of big endian uint32, mod 2**32)."""
    total = 0
    full = len(data) & ~3
    if full:
        for (value,) in struct.iter_unpack(">I", data[:full]):
            total += value
    total += int.from_bytes(data[full:].ljust(4, b"\x00"), "big")
    return total & 0xFFFFFFFF


def _head_checksum(payload: bytes) -> int:
    """``head`` checksum with ``checkSumAdjustment`` (bytes 8..12) zeroed."""
    return sfnt_checksum(payload[:8] + b"\x00\x00\x00\x00" + payload[12:])


def _table_checksum(tag: str, payload: bytes) -> int:
    return _head_checksum(payload) if tag == _HEAD else sfnt_checksum(payload)


def _search_params(num_tables: int) -> tuple[int, int, int]:
    entry_selector = max(num_tables.bit_length() - 1, 0)
    search_range = (2**entry_selector) * 16 if num_tables else 0
    range_shift = num_tables * 16 - search_range
    return search_range, entry_selector, range_shift


def is_ttc(data: bytes) -> bool:
    return data[:4] == Magic.TTCF


def is_sfnt(data: bytes) -> bool:
    return data[:4] in (Magic.SFNT, Magic.TRUE, Magic.OTTO)


def font_count(data: bytes) -> int:
    """Number of faces in the file (1 for a standalone font)."""
    if not is_ttc(data):
        return 1
    return _parse_ttc_header(data)[1]


# --------------------------------------------------------------------------
# plain sfnt (single font)
# --------------------------------------------------------------------------
def split_sfnt(data: bytes, base: int = 0) -> Face:
    """Split a single font into its ``sfnt`` version + tables.

    ``base`` is the offset of the font's table directory inside ``data``.  It is
    non-zero for TTC members, whose per-face directories record *absolute* file
    offsets rather than offsets relative to the face.

    Tables come back sorted by ascending offset, mirroring how the reference
    tools read and re-write them.
    """
    if len(data) - base < 12:
        raise SfntError("file too small to be a font")
    version = data[base : base + 4]
    if version == Magic.TTCF:
        raise SfntError("file is a TTC collection, use split_ttc()")
    if not is_sfnt(version):
        raise SfntError(f"unknown sfnt version {version!r}")
    (num_tables,) = struct.unpack(">H", data[base + 4 : base + 6])
    if num_tables == 0 or num_tables > 512:
        raise SfntError(f"implausible table count {num_tables}")

    raw: list[tuple[int, TableEntry]] = []
    seen: set[str] = set()
    for index in range(num_tables):
        entry = base + 12 + index * 16
        if entry + 16 > len(data):
            raise SfntError("truncated table directory")
        tag = data[entry : entry + 4].decode("latin-1")
        _checksum, offset, length = struct.unpack(">III", data[entry + 4 : entry + 16])
        end = offset + length
        if end > len(data) or end < 0 or offset < 0:
            raise SfntError(f"table {tag!r} points outside the file")
        if tag in seen:
            raise SfntError(f"duplicate table tag {tag!r}")
        seen.add(tag)
        raw.append((offset, TableEntry(tag, data[offset:end])))

    raw.sort(key=lambda item: item[0])
    return Face(version, tuple(entry for _offset, entry in raw))


def _check_tag_all(tables: Iterable[TableEntry]) -> None:
    for entry in tables:
        _check_tag(entry.tag)


#: Physical write order used by the reference tool chain, derived from the
#: output of ``ttx.exe`` / ``UniteTTC.exe``.  Tags outside this list are emitted
#: afterwards in alphabetical order.
#: Empirically derived from the physical table order that ``ttx.exe`` 2.5
#: emits; validated against 177 fonts (139 system TTFs + every face of every
#: system collection) with zero inconsistencies.  Tags that are not listed are
#: appended in alphabetical order.
CANONICAL_TABLE_ORDER: tuple[str, ...] = (
    "head",
    "hhea",
    "maxp",
    "OS/2",
    "hmtx",
    "LTSH",
    "VDMX",
    "hdmx",
    "cmap",
    "fpgm",
    "prep",
    "cvt ",
    "loca",
    "glyf",
    "kern",
    "name",
    "post",
    "gasp",
    "BASE",
    "COLR",
    "PCLT",
    "CPAL",
    "EBDT",
    "EBLC",
    "GDEF",
    "GPOS",
    "GSUB",
    "HVAR",
    "JSTF",
    "MATH",
    "MERG",
    "MVAR",
    "STAT",
    "avar",
    "cvar",
    "fvar",
    "gvar",
    "meta",
    "vhea",
    "vmtx",
    "DSIG",
)

_ORDER_RANK = {tag: index for index, tag in enumerate(dict.fromkeys(CANONICAL_TABLE_ORDER))}
_UNKNOWN_RANK = len(_ORDER_RANK) + 1


def _sort_key(tag: str) -> tuple[int, str]:
    return (_ORDER_RANK[tag], "") if tag in _ORDER_RANK else (_UNKNOWN_RANK, tag)


def canonical_tables(tables: Sequence[TableEntry]) -> list[TableEntry]:
    """Sort tables into the reference tools' physical write order."""
    return sorted(tables, key=lambda e: _sort_key(e.tag))


def _plan(tables: Sequence[TableEntry]) -> tuple[list[TableEntry], list[int], dict[str, int]]:
    """Return ``(ordered tables, offsets, per-tag checksums)``."""
    ordered = canonical_tables(tables)
    _check_tag_all(ordered)
    offsets: list[int] = []
    cursor = 12 + 16 * len(ordered)
    for entry in ordered:
        offsets.append(cursor)
        cursor += len(entry.data) + _pad4(len(entry.data))
    checksums = {e.tag: _table_checksum(e.tag, e.data) for e in ordered}
    return ordered, offsets, checksums


def _directory_blob(face: Face, tables: Sequence[TableEntry], offsets, checksums) -> bytes:
    """Directory blob *including* the offset-table header, sorted by tag."""
    out = bytearray(
        struct.pack(">4sHHHH", face.version, len(tables), *_search_params(len(tables)))
    )
    for entry, offset in sorted(zip(tables, offsets), key=lambda pair: pair[0].tag):
        out += struct.pack(
            ">4sIII",
            _check_tag(entry.tag),
            checksums[entry.tag],
            offset,
            len(entry.data),
        )
    return bytes(out)


def build_sfnt(face: Face, *, recalc_master: bool = True) -> bytes:
    """Assemble a standalone font file from a :class:`Face`.

    ``recalc_master`` recomputes ``head.checkSumAdjustment``; pass ``False``
    when the payload already carries a valid value.
    """
    tables, offsets, checksums = _plan(face.tables)

    if recalc_master and any(e.tag == _HEAD for e in tables):
        blob = _directory_blob(face, tables, offsets, checksums)
        adjustment = (
            MASTER_CHECKSUM_MAGIC - (sum(checksums.values()) + sfnt_checksum(blob))
        ) & 0xFFFFFFFF
        tables = [
            TableEntry(e.tag, _patch_adjustment(e.data, adjustment)) if e.tag == _HEAD else e
            for e in tables
        ]

    body = b"".join(e.data + b"\x00" * _pad4(len(e.data)) for e in tables)
    return _directory_blob(face, tables, offsets, checksums) + body


def _patch_adjustment(head_payload: bytes, adjustment: int) -> bytes:
    if len(head_payload) < 12:
        return head_payload
    return head_payload[:8] + struct.pack(">I", adjustment) + head_payload[12:]


def read_table(data: bytes, tag: str) -> bytes | None:
    """Return one table payload, or ``None`` when absent."""
    return split_sfnt(data).table(tag)


def replace_table(data: bytes, tag: str, payload: bytes) -> bytes:
    """Return ``data`` with ``tag`` replaced (or appended) by ``payload``.

    Everything else -- table order, table bytes -- stays untouched, which is
    what ``ttx -m`` does for a single-table modification file.
    """
    return build_sfnt(split_sfnt(data).with_table(tag, payload))


def drop_table(data: bytes, tag: str) -> bytes:
    face = split_sfnt(data)
    kept = tuple(e for e in face.tables if e.tag != tag)
    if len(kept) == len(face.tables):
        return data
    return build_sfnt(Face(face.version, kept))


# --------------------------------------------------------------------------
# TTC collections
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TtcHeader:
    version: bytes = b"\x00\x01\x00\x00"
    dsig_offset: int = 0

    @property
    def minor(self) -> int:
        return struct.unpack(">H", self.version[2:4])[0]


def split_ttc(data: bytes) -> tuple[TtcHeader, list[Face]]:
    """Split a font collection into ``(header, [Face, ...])``.

    Shared tables are materialised for every face that references them, so each
    returned :class:`Face` is a fully standalone font.  Per-face ``sfnt``
    versions are preserved, so an ``OTTO`` collection stays ``OTTO``.
    """
    if data[:4] != Magic.TTCF:
        raise SfntError("not a TTC collection")
    header, _num_fonts, offsets = _parse_ttc_header(data)
    faces = [split_sfnt(data, base=offset) for offset in offsets]
    return header, faces


def _parse_ttc_header(data: bytes) -> tuple[TtcHeader, int, list[int]]:
    if len(data) < 16:
        raise SfntError("truncated TTC header")
    (major, minor) = struct.unpack(">HH", data[4:8])
    if (major, minor) not in ((1, 0), (2, 0)):
        raise SfntError(f"unsupported TTC version {major}.{minor}")
    num_fonts, = struct.unpack(">I", data[8:12])
    if num_fonts == 0 or num_fonts > 4096:
        raise SfntError(f"implausible font count {num_fonts}")
    offsets = list(struct.unpack(f">{num_fonts}I", data[12 : 12 + 4 * num_fonts]))
    dsig = 0
    if minor >= 1 and len(data) >= 16 + 4 * num_fonts:
        dsig, = struct.unpack(">I", data[12 + 4 * num_fonts : 16 + 4 * num_fonts])
    return TtcHeader(struct.pack(">HH", major, minor), dsig), num_fonts, offsets


def build_ttc(
    faces: Sequence[Face],
    header: TtcHeader | None = None,
    *,
    recalc_master: bool = False,
) -> bytes:
    """Assemble a font collection, sharing identical ``(tag, payload)`` pairs.

    Faces must already be in their final form -- materialise them with
    :func:`build_sfnt` (or ``recalc_master=True``) first, because that is where
    the reference tools compute each face's ``head.checkSumAdjustment``.
    """
    if not faces:
        raise SfntError("cannot build a TTC without fonts")
    header = header or TtcHeader()

    faces = [
        sfnt_materialise(face, recalc_master)
        for face in faces
    ]

    # 1. one slot per distinct (tag, payload); slot order = alphabetical per
    #    face, face by face -- exactly how UniteTTC lays the collection out
    slot_of: dict[tuple[str, bytes], int] = {}
    slots: list[bytes] = []
    slot_checksums: list[int] = []
    font_slots: list[list[tuple[str, int]]] = []
    for face in faces:
        _check_tag_all(face.tables)
        per_face: list[tuple[str, int]] = []
        for entry in sorted(face.tables, key=lambda e: e.tag):
            key = (entry.tag, entry.data)
            slot = slot_of.get(key)
            if slot is None:
                slot = len(slots)
                slot_of[key] = slot
                slots.append(entry.data)
                slot_checksums.append(_table_checksum(entry.tag, entry.data))
            per_face.append((entry.tag, slot))
        font_slots.append(per_face)

    num_fonts = len(faces)
    has_dsig = header.minor >= 1
    dir_start = 12 + 4 * num_fonts + (4 if has_dsig else 0)
    directories_size = sum(12 + 16 * len(face.tables) for face in faces)
    body_start = dir_start + directories_size

    slot_offsets: list[int] = []
    cursor = body_start
    for payload in slots:
        slot_offsets.append(cursor)
        cursor += len(payload) + _pad4(len(payload))

    # 2. per face directory, sorted by tag
    directories: list[bytes] = []
    for face, per_face in zip(faces, font_slots):
        num_tables = len(per_face)
        search_range, entry_selector, range_shift = _search_params(num_tables)
        blob = bytearray(
            struct.pack(
                ">4sHHHH", face.version, num_tables, search_range, entry_selector, range_shift
            )
        )
        for tag, slot in sorted(per_face, key=lambda pair: pair[0]):
            blob += struct.pack(
                ">4sIII",
                _check_tag(tag),
                slot_checksums[slot],
                slot_offsets[slot],
                len(slots[slot]),
            )
        directories.append(bytes(blob))

    out = bytearray(Magic.TTCF + header.version)
    out += struct.pack(">I", num_fonts)
    out += struct.pack(f">{num_fonts}I", *_font_offsets(directories, dir_start))
    if has_dsig:
        out += struct.pack(">I", header.dsig_offset)
    out += b"".join(directories)
    for payload in slots:
        out += payload
        out += b"\x00" * _pad4(len(payload))
    return bytes(out)


def _font_offsets(directories: Sequence[bytes], dir_start: int) -> list[int]:
    offsets: list[int] = []
    cursor = dir_start
    for blob in directories:
        offsets.append(cursor)
        cursor += len(blob)
    return offsets


def sfnt_materialise(face: Face, recalc_master: bool = True) -> Face:
    """Return ``face`` in canonical table order, optionally re-checksummed."""
    face = Face(face.version, tuple(canonical_tables(face.tables)))
    return recalc_master_face(face) if recalc_master else face


def recalc_master_face(face: Face) -> Face:
    """Return ``face`` with a freshly computed ``head.checkSumAdjustment``."""
    if face.table(_HEAD) is None:
        return face
    tables, offsets, checksums = _plan(face.tables)
    blob = _directory_blob(face, tables, offsets, checksums)
    adjustment = (MASTER_CHECKSUM_MAGIC - (sum(checksums.values()) + sfnt_checksum(blob))) & 0xFFFFFFFF
    return Face(
        face.version,
        tuple(
            TableEntry(e.tag, _patch_adjustment(e.data, adjustment)) if e.tag == _HEAD else e
            for e in tables
        ),
    )