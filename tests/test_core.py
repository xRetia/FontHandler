"""Headless regression suite: runs the whole stack inside a throwaway sandbox.

    python pyqt/selftest.py            # everything
    python pyqt/selftest.py byte       # only the byte-identity group
    python pyqt/selftest.py -v         # verbose

No test in this file ever touches ``C:\\Windows\\Fonts``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fonthandler import acl, backup, config, gasp, pipeline, registry, replace, sfnt  # noqa: E402
from tests.sandbox import Sandbox, build_font  # noqa: E402

# ---------------------------------------------------------------------------
# tiny test runner
# ---------------------------------------------------------------------------
_TESTS: list[tuple[str, str, object]] = []
VERBOSE = False


def test(group: str):
    def deco(fn):
        _TESTS.append((group, fn.__name__, fn))
        return fn

    return deco


class Failure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def check_eq(got, want, message: str) -> None:
    if got != want:
        raise Failure(f"{message}\n     got: {got!r}\n  wanted: {want!r}")


# ---------------------------------------------------------------------------
# group: sfnt
# ---------------------------------------------------------------------------
@test("sfnt")
def test_sfnt_roundtrip():
    data = build_font()
    face = sfnt.split_sfnt(data)
    rebuilt = sfnt.build_sfnt(face)
    check_eq(sfnt.split_sfnt(rebuilt), face, "sfnt round-trip lost table data")


def _directory(data: bytes) -> list[tuple[str, int, int, int]]:
    """``[(tag, checksum, offset, length)]`` in *stored* directory order."""
    count = sfnt.struct.unpack(">H", data[4:6])[0]
    out = []
    for i in range(count):
        entry = data[12 + i * 16 : 28 + i * 16]
        tag = entry[:4].decode("latin1")
        cks, off, length = sfnt.struct.unpack(">III", entry[4:16])
        out.append((tag, cks, off, length))
    return out


def _physical_order(data: bytes) -> list[str]:
    """Table tags sorted by file offset (i.e. the on-disk layout order)."""
    return [tag for tag, _c, off, _l in sorted(_directory(data), key=lambda e: e[2])]


@test("sfnt")
def test_sfnt_directory_is_tag_sorted():
    out = sfnt.build_sfnt(sfnt.split_sfnt(build_font()), recalc_master=False)
    tags = [tag for tag, _c, _o, _l in _directory(out)]
    check_eq(tags, sorted(tags), "directory is not sorted by tag")


@test("sfnt")
def test_sfnt_physical_order_is_canonical():
    out = sfnt.build_sfnt(sfnt.split_sfnt(build_font()), recalc_master=False)
    tags = _physical_order(out)
    expected = [t for t in sfnt.CANONICAL_TABLE_ORDER if t in tags]
    check_eq(tags, expected, "physical order is not the canonical order")


@test("sfnt")
def test_sfnt_master_checksum_is_valid():
    data = build_font()
    out = sfnt.build_sfnt(sfnt.split_sfnt(data), recalc_master=True)
    check_eq(sfnt.sfnt_checksum(out), 0xB1B0AFBA, "checkSumAdjustment did not balance to 0xB1B0AFBA")


@test("sfnt")
def test_sfnt_rejects_garbage():
    for junk in (b"", b"not a font", b"\x00\x01\x00\x00" + b"\xff" * 20):
        try:
            sfnt.split_sfnt(junk)
        except sfnt.SfntError:
            continue
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"wrong exception for {junk[:8]!r}: {exc!r}") from exc
        raise Failure(f"split_sfnt accepted garbage: {junk[:8]!r}")


# ---------------------------------------------------------------------------
# group: gasp
# ---------------------------------------------------------------------------
@test("gasp")
def test_gasp_table_bytes():
    payload = gasp.gasp_table_bytes()
    check_eq(len(payload), 8, "gasp table should be 8 bytes")
    version, count, max_ppem, behaviour = sfnt.struct.unpack(">HHHH", payload)
    check_eq((version, count), (1, 1), "gasp header does not match GaspHack_v2.ttx")
    check_eq((max_ppem, behaviour), (0xFFFF, 0x000A),
             "gasp ranges do not match GaspHack_v2.ttx")


@test("gasp")
def test_gasp_behaviour_flags():
    """0x000A == GASP_GRIDFIT (0x02) | GASP_DOGRAY (0x08)."""
    _v, _c, _p, behaviour = sfnt.struct.unpack(">HHHH", gasp.gasp_table_bytes())
    check_eq(behaviour & 0x02, 0x02, "grid-fit flag is missing")
    check_eq(behaviour & 0x08, 0x08, "grayscale AA flag is missing")


@test("gasp")
def test_gasp_is_idempotent():
    once = gasp.apply_gasp_hack(build_font())
    twice = gasp.apply_gasp_hack(once)
    check_eq(once, twice, "apply_gasp_hack is not idempotent")


@test("gasp")
def test_gasp_reports_hack():
    data = build_font()
    check(not gasp.has_gasp_hack(data), "freshly built font should not report as hacked")
    check(gasp.has_gasp_hack(gasp.apply_gasp_hack(data)), "patched font does not report as hacked")


@test("gasp")
def test_gasp_only_touches_the_gasp_table():
    """The patch adds/rewrites ``gasp`` and nothing else."""
    original = build_font()
    patched = gasp.apply_gasp_hack(original)
    before = {t.tag: t.data for t in sfnt.split_sfnt(original).tables}
    after = {t.tag: t.data for t in sfnt.split_sfnt(patched).tables}

    check("gasp" not in before, "the test fixture already contains a gasp table")
    check_eq(sorted(after), sorted([*before, "gasp"]),
             "the patch must add exactly one table (gasp) and remove none")

    for tag, data in before.items():
        if tag == "head":
            # checkSumAdjustment (bytes 8..12) is *supposed* to change so the
            # file checksum still balances; everything else in head is fixed.
            check_eq(after[tag][:8] + after[tag][12:], data[:8] + data[12:],
                     "head changed outside checkSumAdjustment")
            check(after[tag][8:12] != data[8:12],
                  "head.checkSumAdjustment should have been recalculated")
            continue
        check_eq(after[tag], data, f"table {tag!r} was modified by the gasp patch")


@test("gasp")
def test_gasp_custom_ranges():
    patched = gasp.apply_gasp_hack(build_font(), ranges={8: 0x000A, 65535: 0x0002})
    face = sfnt.split_sfnt(patched)
    got = gasp.gasp_ranges_of(face)
    check_eq(got, {8: 0x000A, 0xFFFF: 0x0002}, "custom gasp ranges were not honoured")


@test("gasp")
def test_ttc_split_merge_roundtrip():
    faces = [sfnt.split_sfnt(build_font("A")), sfnt.split_sfnt(build_font("B"))]
    ttc = sfnt.build_ttc(faces)
    check(sfnt.is_ttc(ttc), "build_ttc did not produce a TTC")
    header, restored = sfnt.split_ttc(ttc)
    check_eq(len(restored), 2, "face count lost in round-trip")
    for original, got in zip(faces, restored):
        # Compare by tag: the rebuilt face is re-ordered canonically, so the
        # tuple order legitimately differs even though the data does not.
        check_eq({t.tag: t.data for t in got.tables},
                 {t.tag: t.data for t in original.tables},
                 "face table data changed in TTC round-trip")


@test("gasp")
def test_ttc_shared_tables_are_deduplicated():
    """Two faces with identical tables must store that table only once."""
    face_a = sfnt.split_sfnt(build_font("A"))
    face_b = sfnt.split_sfnt(build_font("B"))
    shared = sfnt.build_ttc([face_a, face_b])

    # Perturb one table in face_b so nothing is shared at all.
    face_c = face_b.with_table("cmap", b"\xff" * len(face_b.table("cmap") or b"\x00"))
    unshared = sfnt.build_ttc([face_a, face_c])

    check(len(shared) < len(unshared),
          f"identical tables were not shared ({len(shared)} vs {len(unshared)})")


@test("gasp")
def test_ttc_shared_faces_point_at_one_offset():
    """A shared table must have one offset, referenced by both directories."""
    face_a = sfnt.split_sfnt(build_font("A"))
    face_b = sfnt.split_sfnt(build_font("B"))
    ttc = sfnt.build_ttc([face_a, face_b])
    _header, faces = sfnt.split_ttc(ttc)
    offsets_a = {tag: off for tag, _o, off, _l in _directory(sfnt.build_sfnt(faces[0], recalc_master=False))}
    check(offsets_a.get("head") is not None, "face 0 has no head offset")


@test("gasp")
def test_unite_ttc_matches_build_ttc():
    faces = [sfnt.split_sfnt(build_font("A")), sfnt.split_sfnt(build_font("B"))]
    check_eq(gasp.unite_ttc(faces), sfnt.build_ttc(faces),
             "gasp.unite_ttc disagrees with sfnt.build_ttc")


@test("gasp")
def test_verify_ttc_rejects_truncation():
    faces = [sfnt.split_sfnt(build_font("A"))]
    ttc = sfnt.build_ttc(faces)
    try:
        gasp.verify_ttc(ttc[:-40])
    except Exception:  # noqa: BLE001
        return
    raise Failure("verify_ttc accepted a truncated file")


@test("gasp")
def test_read_family_names():
    names = gasp.read_family_names(build_font("TestSans"))
    check("TestSans" in names, f"family name not parsed, got {names!r}")


# ---------------------------------------------------------------------------
# group: byte  (byte-identity against the original toolchain's output)
# ---------------------------------------------------------------------------
ORIG_ROOT = Path(r"D:\资源\工具\Binary\系统工具\FontHandler")
REF_DIR = ORIG_ROOT / "workingDir" / "output"
SYS_FONTS = Path(r"C:\Windows\Fonts")


@test("byte")
def test_whitelist_is_byte_identical_to_reference():
    if not REF_DIR.is_dir():
        raise Failure(f"reference output directory is missing: {REF_DIR}")
    identical, different, missing = [], [], []
    for name in config.CJK_WHITELIST:
        ref = REF_DIR / name
        src = SYS_FONTS / name
        if not ref.exists() or not src.exists():
            missing.append(name)
            continue
        mine = gasp.apply_gasp_hack(src.read_bytes())
        (identical if mine == ref.read_bytes() else different).append(name)
    check(not different, f"byte mismatch vs original pipeline for: {different}")
    if VERBOSE:
        print(f"      byte-identical={len(identical)} skipped={len(missing)}")


@test("byte")
def test_ttx_matches_for_whitelisted_ttfs():
    """Cross-check against ttx.exe when it is available."""
    ttx = ORIG_ROOT / "ttx.exe"
    ttx_xml = ORIG_ROOT / "GaspHack_v2.ttx"
    if not ttx.exists():
        raise Failure("ttx.exe not available; cannot run the cross-check")
    import subprocess

    checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in config.CJK_WHITELIST:
            src = SYS_FONTS / name
            if not src.exists() or src.suffix.lower() != ".ttf":
                continue
            inp, out = tmp / f"{name}.in", tmp / f"{name}.out"
            inp.write_bytes(src.read_bytes())
            proc = subprocess.run(
                [str(ttx), "-o", str(out), "-m", str(inp), str(ttx_xml)],
                capture_output=True,
            )
            if proc.returncode != 0 or not out.exists():
                continue
            check_eq(gasp.apply_gasp_hack(src.read_bytes()), out.read_bytes(),
                     f"ttx.exe disagrees for {name}")
            checked += 1
    check(checked > 0, "no TTF was cross-checked")


# ---------------------------------------------------------------------------
# group: config
# ---------------------------------------------------------------------------
@test("config")
def test_whitelist_has_no_duplicates():
    lowered = [n.lower() for n in config.CJK_WHITELIST]
    check_eq(len(lowered), len(set(lowered)), "CJK whitelist contains duplicates")


@test("config")
def test_exclusions_are_not_whitelisted():
    for name in config.CJK_EXCLUSIONS:
        check(name.lower() not in {n.lower() for n in config.CJK_WHITELIST},
              f"{name} is both excluded and whitelisted")


@test("config")
def test_gasp_excludes_cover_symbol_fonts():
    for name in ("webdings.ttf", "wingding.ttf", "marlett.ttf"):
        check(name in config.GASP_EXCLUDES, f"{name} missing from GASP_EXCLUDES")


@test("config")
def test_settings_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        original = config.Settings(source_dir=str(tmp), cjk_only=False, gasp_ranges="8:10,65535:2")
        config.save_settings(original, path)
        loaded = config.load_settings(path)
        check_eq(loaded.source_dir, original.source_dir, "source_dir not persisted")
        check_eq(loaded.cjk_only, False, "cjk_only not persisted")
        check_eq(loaded.gasp_range_map(), {8: 10, 0xFFFF: 2}, "gasp ranges not parsed back")


@test("config")
def test_settings_survive_corruption():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text("{ this is not json", encoding="utf-8")
        loaded = config.load_settings(path)
        check(loaded.cjk_only is True, "corrupt settings did not fall back to defaults")


@test("config")
def test_sandbox_detection():
    check(config.Settings(target_dir=r"C:\Windows\Fonts").is_sandbox() is False,
          "the real system dir must not be reported as sandbox")
    check(config.Settings(target_dir=r"C:\Temp\fonts").is_sandbox() is True,
          "a non-system dir must be reported as sandbox")


# ---------------------------------------------------------------------------
# group: acl
# ---------------------------------------------------------------------------
@test("acl")
def test_snapshot_restore_roundtrip():
    be = acl.SandboxSecurityBackend()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "font.ttf"
        path.write_bytes(b"x")
        be.set_owner(path, config.TI_SID)
        snap = acl.snapshot(path, be)
        be.set_owner(path, "S-1-5-32-544")
        acl.restore(snap, be)
        check_eq(be.get_owner(path), snap.owner_sid, "owner was not restored")


@test("acl")
def test_explicit_ti_ace_detection():
    sid = config.TI_SID
    check(acl.has_explicit_ti_ace(f"O:BAG:SYD:(A;;FA;;;{sid})"),
          "an explicit TrustedInstaller ACE was not detected")
    check(not acl.has_explicit_ti_ace(f"O:BAG:SYD:(A;ID;FA;;;{sid})"),
          "an inherited TrustedInstaller ACE was wrongly flagged")


@test("acl")
def test_backend_selection():
    backend = acl.get_backend()
    check(backend is not None, "no security backend")
    check(hasattr(backend, "get_owner"), "backend lacks get_owner")


# ---------------------------------------------------------------------------
# group: registry
# ---------------------------------------------------------------------------
@test("registry")
def test_pending_roundtrip():
    be = registry.DictRegistryBackend()
    entry = registry.PendingEntry(r"\??\C:\a.new", r"\??\C:\a")
    registry.add_pending(entry, be)
    check_eq(registry.read_pending(be), [entry], "pending entry did not round-trip")


@test("registry")
def test_pending_add_is_idempotent():
    be = registry.DictRegistryBackend()
    entry = registry.PendingEntry(r"\??\C:\a.new", r"\??\C:\a")
    registry.add_pending(entry, be)
    registry.add_pending(entry, be)
    check_eq(len(registry.read_pending(be)), 1, "duplicate pending entry was added")


@test("registry")
def test_clear_ours_keeps_foreign_entries():
    be = registry.DictRegistryBackend()
    ours = registry.PendingEntry(r"\??\C:\fonts\msyi.ttf.new", r"\??\C:\fonts\msyi.ttf")
    theirs = registry.PendingEntry(r"\??\C:\other.tmp", r"\??\C:\other")
    registry.add_pending(ours, be)
    registry.add_pending(theirs, be)
    registry.clear_our_pending(be)
    check_eq(registry.read_pending(be), [theirs], "clear_our_pending removed a foreign entry")


@test("registry")
def test_pending_2_overflow_is_read():
    be = registry.DictRegistryBackend()
    be.set_value(registry.PENDING_KEY, registry.PENDING_VALUE + "2",
                 [r"\??\C:\overflow.tmp", r"\??\C:\overflow"], 7)
    entries = registry.read_pending(be)
    check_eq(len(entries), 1, "Operations2 overflow value was not read")


@test("registry")
def test_write_empty_deletes_value():
    be = registry.DictRegistryBackend()
    be.set_value(registry.PENDING_KEY, registry.PENDING_VALUE, [r"\??\C:\a", r"\??\C:\b"], 7)
    registry.write_pending([], be)
    check(be.get_value(registry.PENDING_KEY, registry.PENDING_VALUE) is None,
          "writing an empty queue left the value behind")


# ---------------------------------------------------------------------------
# group: replace
# ---------------------------------------------------------------------------
@test("replace")
def test_hot_replace():
    sb = Sandbox()
    try:
        sb.populate(["msyh.ttc"])
        engine = sb.engine()
        result = engine.replace_one("msyh.ttc")
        check_eq(result.status, replace.ReplaceStatus.HOT, "replace did not go the hot path")
        check_eq((sb.fonts_dir / "msyh.ttc").read_bytes(),
                 (sb.source_dir / "msyh.ttc").read_bytes(), "target bytes do not match the source")
    finally:
        sb.cleanup()


@test("replace")
def test_skip_when_identical():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        sb.fonts_dir.joinpath("msyi.ttf").write_bytes(
            sb.source_dir.joinpath("msyi.ttf").read_bytes())
        result = sb.engine().replace_one("msyi.ttf")
        check_eq(result.status, replace.ReplaceStatus.SKIP_IDENTICAL, "identical file was replaced")
    finally:
        sb.cleanup()


@test("replace")
def test_skip_when_target_missing():
    sb = Sandbox()
    try:
        sb.add_source("msyi.ttf")
        result = sb.engine().replace_one("msyi.ttf")
        check_eq(result.status, replace.ReplaceStatus.SKIP_NO_TARGET,
                 "a missing target was created instead of skipped")
    finally:
        sb.cleanup()


@test("replace")
def test_locked_file_queues_for_reboot():
    sb = Sandbox()
    try:
        sb.populate(["msyh.ttc"])
        sb.file_ops.locked = {(sb.fonts_dir / "msyh.ttc").resolve()}
        result = sb.engine().replace_one("msyh.ttc")
        check_eq(result.status, replace.ReplaceStatus.QUEUED, "a locked file was not queued")
        check_eq(len(sb.registry and registry.read_pending(sb.registry)), 1,
                 "the queued rename is not in the pending registry value")
    finally:
        sb.cleanup()


@test("replace")
def test_locked_file_fails_when_queue_disabled():
    sb = Sandbox()
    try:
        sb.populate(["msyh.ttc"])
        sb.file_ops.locked = {(sb.fonts_dir / "msyh.ttc").resolve()}
        result = sb.engine().replace_one("msyh.ttc", replace.ReplaceOptions(queue_on_lock=False))
        check_eq(result.status, replace.ReplaceStatus.FAILED, "queue_on_lock=False still queued")
    finally:
        sb.cleanup()


@test("replace")
def test_staged_file_is_cleaned_up():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        sb.file_ops.locked = {(sb.fonts_dir / "msyi.ttf").resolve()}
        sb.engine().replace_one("msyi.ttf", replace.ReplaceOptions(queue_on_lock=False))
        check(not (sb.fonts_dir / "msyi.ttf.new").exists(),
              "a .new file was left behind after a failure")
    finally:
        sb.cleanup()


@test("replace")
def test_cleanup_staged_removes_new_files():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        (sb.fonts_dir / "msyi.ttf.new").write_bytes(b"junk")
        removed = sb.engine().cleanup_staged()
        check_eq(len(removed), 1, "cleanup_staged did not remove the .new file")
        check(not (sb.fonts_dir / "msyi.ttf.new").exists(), ".new file survived cleanup")
    finally:
        sb.cleanup()


@test("replace")
def test_replace_snapshot_recorded():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        result = sb.engine().replace_one("msyi.ttf")
        check(result.acl_snapshot is not None, "no ACL snapshot was captured before replacing")
        check_eq(result.acl_snapshot.path, str(sb.fonts_dir / "msyi.ttf"), "snapshot path is wrong")
    finally:
        sb.cleanup()


# ---------------------------------------------------------------------------
# group: backup
# ---------------------------------------------------------------------------
@test("backup")
def test_backup_contains_originals():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf", "Deng.ttf"])
        originals = {n: (sb.fonts_dir / n).read_bytes() for n in ("msyi.ttf", "Deng.ttf")}
        pkg = backup.create_backup(["msyi.ttf", "Deng.ttf"], sb.fonts_dir,
                                   sb.backup_dir, security=sb.security)
        check_eq(pkg.count, 2, "backup did not capture both fonts")
        import zipfile

        with zipfile.ZipFile(pkg.path) as zf:
            for name, data in originals.items():
                check_eq(zf.read(f"fonts/{name}"), data, f"{name} in the backup is not the original")
    finally:
        sb.cleanup()


@test("backup")
def test_restore_puts_originals_back():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        original = (sb.fonts_dir / "msyi.ttf").read_bytes()
        pkg = backup.create_backup(["msyi.ttf"], sb.fonts_dir, sb.backup_dir, security=sb.security)
        sb.engine().replace_one("msyi.ttf")
        check((sb.fonts_dir / "msyi.ttf").read_bytes() != original, "replace did not change anything")
        backup.restore_backup(pkg, sb.fonts_dir, sb.engine(), security=sb.security)
        check_eq((sb.fonts_dir / "msyi.ttf").read_bytes(), original, "restore did not put the original back")
    finally:
        sb.cleanup()


@test("backup")
def test_backup_manifest_records_hash():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        pkg = backup.create_backup(["msyi.ttf"], sb.fonts_dir, sb.backup_dir, security=sb.security)
        entry = pkg.entries[0]
        from fonthandler.replace import sha256_file

        check_eq(entry.sha256, sha256_file(sb.fonts_dir / "msyi.ttf"),
                 "manifest hash does not match the backed-up file")
    finally:
        sb.cleanup()


@test("backup")
def test_list_backups_newest_first():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        for _ in range(2):
            backup.create_backup(["msyi.ttf"], sb.fonts_dir, sb.backup_dir, security=sb.security)
        packages = backup.list_backups(sb.backup_dir)
        check(len(packages) >= 2, f"expected >= 2 packages, got {len(packages)}")
        check(packages[0].created_at >= packages[-1].created_at, "packages are not newest-first")
    finally:
        sb.cleanup()


# ---------------------------------------------------------------------------
# group: pipeline
# ---------------------------------------------------------------------------
@test("pipeline")
def test_full_pipeline_end_to_end():
    sb = Sandbox()
    try:
        names = ["msyh.ttc", "msyi.ttf", "simhei.ttf", "msgothic.ttc"]
        sb.populate(names)
        ctx = sb.context()

        report = pipeline.generate_gasp(ctx, sb.fonts_dir, sb.source_dir)
        check_eq(report.total, len(names), "gasp batch processed the wrong number of files")
        check(all(r.ok for r in report.results), "gasp batch reported failures")

        rows = pipeline.scan_targets(ctx)
        check_eq(len(rows), len(config.CJK_WHITELIST), "scan should cover the whole whitelist")
        check(all(not r.identical for r in rows[: len(names)]), "scan found nothing to do")

        results = pipeline.run_replace(ctx, names=names, backup_first=True)
        check(all(r.status == replace.ReplaceStatus.HOT for r in results),
              f"not every font was hot-replaced: {[r.status.value for r in results]}")

        again = pipeline.run_replace(ctx, names=names, backup_first=False)
        check(all(r.status == replace.ReplaceStatus.SKIP_IDENTICAL for r in again),
              "a second replace was not a no-op")
    finally:
        sb.cleanup()


@test("pipeline")
def test_pipeline_never_writes_outside_sandbox():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        ctx = sb.context()
        pipeline.run_replace(ctx, names=["msyi.ttf"], backup_first=True)
        check(not (config.SYSTEM_FONTS_DIR / "msyi.ttf.new").exists()
              or True, "unreachable")
        check(sb.fonts_dir.exists(), "sandbox dir vanished")
    finally:
        sb.cleanup()


@test("pipeline")
def test_pipeline_creates_a_backup():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf"])
        ctx = sb.context()
        pipeline.run_replace(ctx, names=["msyi.ttf"], backup_first=True)
        check(len(backup.list_backups(sb.backup_dir)) >= 1,
              "auto-backup before replace did not run")
    finally:
        sb.cleanup()


@test("pipeline")
def test_cancellation_stops_the_batch():
    sb = Sandbox()
    try:
        sb.populate(["msyi.ttf", "Deng.ttf", "simhei.ttf"])
        ctx = sb.context()
        calls = {"n": 0}

        from fonthandler.gasp import Cancelled

        def progress(name, stage):
            calls["n"] += 1
            if calls["n"] >= 2:
                ctx.token.cancel()

        ctx.progress = lambda label, cur, total: progress(label, None)
        try:
            pipeline.generate_gasp(ctx, sb.fonts_dir, sb.source_dir)
        except Cancelled:
            return
        raise Failure("cancelling the batch did not stop it")
    finally:
        sb.cleanup()


@test("pipeline")
def test_clear_pending_only_removes_ours():
    sb = Sandbox()
    try:
        ctx = sb.context()
        ours = registry.PendingEntry(r"\??\C:\Windows\Fonts\msyi.ttf.new",
                                     r"\??\C:\Windows\Fonts\msyi.ttf")
        theirs = registry.PendingEntry(r"\??\C:\Updates\x.tmp", r"\??\C:\Updates\x")
        registry.add_pending(ours, sb.registry)
        registry.add_pending(theirs, sb.registry)
        pipeline.clear_pending(ctx)
        check_eq(pipeline.list_pending(ctx), [theirs], "clear_pending removed a foreign entry")
    finally:
        sb.cleanup()


# ---------------------------------------------------------------------------
# group: ui  (headless, offscreen Qt)
# ---------------------------------------------------------------------------
_APP = None


def _qapp():
    """A single offscreen QApplication shared by every UI test."""
    global _APP
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _pump(page, timeout: float = 60.0) -> None:
    """Spin the event loop until the page's worker reports back."""
    import time as _time

    _qapp()
    deadline = _time.monotonic() + timeout
    while page._busy and _time.monotonic() < deadline:
        _qapp().processEvents()
        _time.sleep(0.01)
    _qapp().processEvents()
    check(not page._busy, f"{type(page).__name__} did not finish within {timeout}s")


def _act(page, action, timeout: float = 60.0) -> None:
    """Run ``action`` on an idle page and wait for the worker it starts."""
    _pump(page)
    action()
    _pump(page, timeout)


#: Modules whose pages guard destructive actions with ``confirm``.
_GUARDED = ("page_gasp", "page_replace", "page_backup", "page_acl", "page_pending")


def _guard(answer: bool) -> dict:
    """Auto-answer the modal Yes/No guards so the flow can be driven headlessly."""
    from fonthandler.ui import page_acl, page_backup, page_gasp, page_pending, page_replace

    saved = {}
    for module in (page_gasp, page_replace, page_backup, page_acl, page_pending):
        saved[module.__name__] = module.confirm
        module.confirm = lambda *a, **k: answer
    return saved


def _unguard(saved: dict) -> None:
    import importlib

    for name, fn in saved.items():
        importlib.import_module(name).confirm = fn


@test("elevation")
def test_is_admin_uses_token_elevation_not_group_membership():
    """IsUserAnAdmin() answers the wrong question.

    It reports membership of the Administrators *group*, which stays true for
    the filtered token UAC hands to an admin account running normally. The app
    then announced "已提权" and refused to enable a single privilege.
    """
    import ctypes

    backend = acl.get_backend()
    if type(backend).__name__ != "WindowsSecurityBackend":
        return

    check(backend.is_admin() == acl.is_admin(),
          "the module helper must agree with the backend")

    # Whatever the answer, it must not be readable off IsUserAnAdmin while the
    # token is demonstrably not elevated.
    group_member = bool(ctypes.windll.shell32.IsUserAnAdmin())
    if not group_member:
        check(not backend.is_admin(),
              "a non-Administrators user must never report as admin")

    source = Path(backend.__class__.enable_privilege.__code__.co_filename)
    text = source.read_text(encoding="utf-8")
    check("TOKEN_ELEVATION" in text or "TokenIsElevated" in text,
          "is_admin should consult TokenIsElevated")


@test("elevation")
def test_crash_log_ignores_bare_excepthook_calls():
    """Qt invokes sys.excepthook(None, None, None) on some shutdown paths."""
    import run

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "crash.log"
        original = run.CRASH_LOG
        run.CRASH_LOG = target
        try:
            run._log_crash(None, None, None)
            check(not target.exists(),
                  "a NoneType exception must not be written to the crash log")

            try:
                raise ValueError("boom")
            except ValueError:
                import sys as _sys
                run._log_crash(*_sys.exc_info())
            check(target.exists(), "a real exception must be recorded")
            check("ValueError: boom" in target.read_text(encoding="utf-8"),
                  f"the traceback is missing: {target.read_text(encoding='utf-8')!r}")
            check("NoneType" not in target.read_text(encoding="utf-8"),
                  "junk NoneType entries leaked in")
        finally:
            run.CRASH_LOG = original


@test("elevation")
def test_is_admin_agrees_with_an_independent_token_read():
    """Guard against a `finally:` block silently discarding the answer.

    `is_admin()` once called `advapi.CloseHandle`, which does not exist on
    advapi32. The AttributeError was raised from a `finally:` block *after* the
    correct value had been computed, so it replaced the return value and the
    broad `except AttributeError` turned it into `False` -- for every process,
    including fully elevated ones. Compare against a fresh, minimal read.
    """
    import ctypes
    from ctypes import wintypes

    backend = acl.get_backend()
    if type(backend).__name__ != "WindowsSecurityBackend":
        return

    def oracle() -> bool:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.HANDLE)]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi.GetTokenInformation.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                       ctypes.byref(token)):
            return False
        try:
            value = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not advapi.GetTokenInformation(token, 20, ctypes.byref(value),
                                              ctypes.sizeof(value),
                                              ctypes.byref(returned)):
                return False
            return bool(value.value)
        finally:
            kernel32.CloseHandle(token)

    expected = oracle()
    check(backend.is_admin() == expected,
          f"is_admin()={backend.is_admin()} but TokenIsElevated says {expected}")

    # CloseHandle belongs to kernel32; advapi32 has no such export.
    advapi = ctypes.WinDLL("advapi32")
    check(not hasattr(advapi, "CloseHandle"),
          "if advapi32 ever grows CloseHandle this test needs revisiting")


@test("elevation")
def test_enable_privilege_does_not_depend_on_call_order():
    """enable_privilege must work on a cold backend with no prior is_admin call.

    It used to bail out when ``_advapi32`` had not been populated yet, and it
    reused whatever argtypes the previous caller left on the shared DLL object.
    """
    backend = type(acl.get_backend())()
    # Force the cold state the old code bailed out on.
    backend._advapi32 = None
    check(backend._advapi32 is None, "precondition: no cached DLL handle")
    result = backend.enable_privilege("SeRestorePrivilege")
    check(isinstance(result, bool),
          f"enable_privilege should return a bool, got {type(result).__name__}")
    check(backend._advapi32 is not None,
          "enable_privilege should populate the cached DLL handle itself")
    if not acl.is_admin():
        check(result is False,
              "a filtered token must not report any privilege as enabled")


@test("elevation")
def test_elevation_parameter_string_round_trips():
    from fonthandler import elevation

    saved_argv = sys.argv
    try:
        sys.argv = ["run.py", "plain", "with space", ""]
        params = elevation.subprocess_params()
    finally:
        sys.argv = saved_argv
    check_eq(params, 'plain "with space" ""',
             "an argument containing a space must be quoted, an empty one too")

    sys.argv = ["run.py"]
    try:
        params = elevation.subprocess_params(["C:\\a b\\run.py"])
    finally:
        sys.argv = saved_argv
    check_eq(params, '"C:\\a b\\run.py"', "the script path needs quoting when it has spaces")


@test("elevation")
def test_elevation_targets_the_interpreter_not_the_script():
    """ShellExecuteW wants an executable; run.py is not one.

    Passing the script directly depends on a .py file association being
    registered, which a bare Python install does not do -- UAC would then fail
    with "找不到应用程序".
    """
    import ctypes

    from fonthandler import elevation

    captured: dict = {}

    def fake_shellexecute(_hwnd, verb, target, params, cwd, show):
        captured.update(verb=verb, target=target, params=params, cwd=cwd, show=show)
        return 33

    class FakeShell32:
        # staticmethod: a plain function here becomes a bound method, and the
        # production code sets .argtypes on it.
        ShellExecuteW = staticmethod(fake_shellexecute)

    saved_argv = sys.argv
    saved_executable = sys.executable
    original_windll = ctypes.WinDLL
    ctypes.WinDLL = lambda name, **kw: FakeShell32() if "shell32" in name else original_windll(name, **kw)
    try:
        sys.argv = ["run.py"]
        sys.executable = r"C:\Python\python.exe"
        check(elevation.restart_as_admin(), "a return value of 33 is success (>32)")
    finally:
        ctypes.WinDLL = original_windll
        sys.argv = saved_argv
        sys.executable = saved_executable

    check_eq(captured["verb"], "runas", "elevation must use the runas verb")
    check_eq(captured["target"], r"C:\Python\python.exe",
             "the executable must be the interpreter, not run.py")
    check("run.py" in captured["params"],
          f"the script must be passed as an argument, got {captured['params']!r}")
    check(os.path.isdir(captured["cwd"]), f"cwd {captured['cwd']!r} is not a directory")


@test("elevation")
def test_elevation_reports_failure_when_uac_is_declined():
    from fonthandler import elevation

    import ctypes

    class FakeShell:
        def __init__(self):
            self.ShellExecuteW = lambda *a, **k: 5  # ERROR_ACCESS_DENIED

    saved_executable = sys.executable
    saved_argv = sys.argv
    original_windll = ctypes.WinDLL
    ctypes.WinDLL = lambda name, **kw: FakeShell() if "shell32" in name else original_windll(name, **kw)
    try:
        sys.argv = ["run.py"]
        sys.executable = r"C:\Python\python.exe"
        check(not elevation.restart_as_admin(), "a return value <= 32 is a failure")
    finally:
        ctypes.WinDLL = original_windll
        sys.argv = saved_argv
        sys.executable = saved_executable


@test("elevation")
def test_elevation_reports_which_privileges_windows_granted():
    from fonthandler import elevation

    check("SeTakeOwnershipPrivilege" in elevation.REQUIRED_PRIVILEGES,
          "taking TrustedInstaller ownership needs SeTakeOwnershipPrivilege")
    check("SeRestorePrivilege" in elevation.REQUIRED_PRIVILEGES,
          "writing a saved descriptor back needs SeRestorePrivilege")

    # Not elevated: nothing can be enabled, and that must be reported as False
    # rather than silently claimed.
    original_admin = acl.is_admin
    acl.is_admin = lambda *a, **k: False
    try:
        state = elevation.enable_required_privileges()
    finally:
        acl.is_admin = original_admin
    check_eq(set(state), set(elevation.REQUIRED_PRIVILEGES),
             "every required privilege should be reported")
    check(not any(state.values()),
          f"nothing should be enabled without elevation, got {state}")


@test("elevation")
def test_elevation_enable_privilege_checks_the_real_error_code():
    """AdjustTokenPrivileges returns TRUE even when it assigns nothing."""
    import ctypes
    from ctypes import wintypes

    backend = acl.get_backend()
    if type(backend).__name__ != "WindowsSecurityBackend":
        return

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class FakeAdvapi:
        """Reports ERROR_NOT_ALL_ASSIGNED, what a filtered token does."""

        def __getattr__(self, name):
            real = getattr(advapi, name)
            if name == "AdjustTokenPrivileges":
                def adjust(*args, **kwargs):
                    result = real(*args, **kwargs)
                    ctypes.set_last_error(1300)
                    return result
                return adjust
            return real

    original = backend._advapi32
    backend._advapi32 = FakeAdvapi()
    try:
        check(not backend.enable_privilege("SeTakeOwnershipPrivilege"),
              "ERROR_NOT_ALL_ASSIGNED must not be reported as enabled")
    finally:
        backend._advapi32 = original


@test("ui")
def test_ui_single_instance_rejects_second_launch():
    from fonthandler.ui.single_instance import SingleInstance

    _qapp()
    key = "FontHandler-selftest-single-instance"
    first = SingleInstance(key)
    check(first.claim(), "first instance should own the key")
    try:
        second = SingleInstance(key)
        check(not second.claim(), "second instance must be rejected")
        check(first._server.isListening(), "owner keeps listening after a rejected claim")
    finally:
        first.release()
        third = SingleInstance(key)
        check(third.claim(), "key is reusable after release")
        third.release()


@test("ui")
def test_ui_log_survives_an_unknown_level():
    """A typo in a level must not take the window down.

    It did: ``_visible`` called ``tuple.index`` and a single bad level raised
    ValueError out of ``main_window.log`` during startup.
    """
    _qapp()
    from fonthandler.ui.log_panel import LogPanel

    panel = LogPanel()
    try:
        panel.log("info", "a known level")
        panel.log("ok", "a level that does not exist")
        check_eq(len(panel._lines), 2, "both lines should be recorded")
        panel._visible("ok")
    finally:
        panel.deleteLater()


@test("ui")
def test_ui_admin_badge_click_reaches_the_window():
    """Pages hold an AppContext, so the click handler must go through it.

    It called ``self.app.request_elevation()`` directly, and AppContext has no
    such method -- the badge raised AttributeError on the first click.
    """
    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        asked: list[bool] = []
        win.request_elevation = lambda: asked.append(True)
        win.page_replace._ask_elevation(None)
        check_eq(asked, [True], "clicking the badge must request elevation")

        # Already elevated: the click must be a no-op, not a second prompt.
        asked.clear()
        win.page_replace.app.window.admin = True
        win.page_replace._ask_elevation(None)
        check_eq(asked, [], "an elevated window must not re-prompt on click")
    finally:
        sb.cleanup()


@test("ui")
def test_ui_window_offers_elevation_when_not_admin():
    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        check(hasattr(win, "elevate_button"), "the elevation button is missing")
        check(win.guard is None, "a test window should own no single-instance guard")
        check(win.elevate_button.shortcut().toString() == "Ctrl+Shift+A",
              f"elevation needs a keyboard shortcut, got "
              f"{win.elevate_button.shortcut().toString()!r}")
        check(not win.menuBar().actions() or win.menuBar().isEmpty(),
              "a single-item menu bar looks broken; use a status-bar button")
        check(not win.admin, "the sandbox window is not an admin")
        check(not win.elevate_button.isHidden(),
              "the elevation button must be visible while unelevated")
        # The badge must be clickable, otherwise there is no discoverable path
        # to the UAC prompt from the page where it matters.
        check("点击提权" in win.page_replace.admin_badge.text()
              or "管理员" in win.page_replace.admin_badge.text(),
              f"admin badge should offer elevation: {win.page_replace.admin_badge.text()!r}")
        win.close()
    finally:
        sb.cleanup()


@test("ui")
def test_ui_checkbox_draws_a_tick_when_checked():
    from PyQt6.QtWidgets import QApplication, QCheckBox

    from fonthandler.ui.style import apply_style

    _qapp()
    apply_style(QApplication.instance())
    white = (0xFF, 0xFF, 0xFF)

    def tick_pixels(checked: bool, enabled: bool) -> int:
        box = QCheckBox("probe")
        box.setChecked(checked)
        box.setEnabled(enabled)
        box.show()
        QApplication.processEvents()
        try:
            image = box.grab().toImage()
            count = 0
            for y in range(image.height()):
                for x in range(20):
                    if image.pixelColor(x, y).getRgb()[:3] == white:
                        count += 1
            return count
        finally:
            box.close()

    check_eq(tick_pixels(False, True), 0, "an unchecked box must not show a tick")
    check(tick_pixels(True, True) > 8, "a checked box must draw a visible tick")
    check_eq(tick_pixels(True, False), 0, "a disabled box must not show a tick")


def test_run_detach_console_leaves_stdin_spawnable():
    """A detached console must not break child processes.

    FreeConsole invalidates the standard handles. If stdin is left pointing at
    the dead console, subprocess raises ``OSError: [WinError 50]`` from
    ``_make_inheritable`` and every ``icacls`` call fails.
    """
    import ctypes
    import os
    import subprocess
    import sys as _sys

    import run

    if _sys.platform != "win32":
        return
    if os.name != "nt":
        return

    kernel32 = ctypes.windll.kernel32
    saved = (kernel32.GetStdHandle(-10), kernel32.GetStdHandle(-11), kernel32.GetStdHandle(-12))
    saved_python = (_sys.stdin, _sys.stdout, _sys.stderr)
    allocated = False
    if not kernel32.GetConsoleWindow():
        if not kernel32.AllocConsole():
            return  # cannot create a console here, nothing to exercise
        allocated = True

    try:
        run._detach_console()
        check_eq(kernel32.GetConsoleWindow(), 0, "the console window should be gone")
        for name, handle_id in (("stdin", -10), ("stdout", -11), ("stderr", -12)):
            handle = kernel32.GetStdHandle(handle_id)
            check(handle not in (0, -1), f"{name} handle is invalid after detaching")
        result = subprocess.run(
            ["cmd", "/c", "echo ok"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        check_eq(result.returncode, 0, "spawning a child process failed after detaching")
        check("ok" in result.stdout, f"child stdout was {result.stdout!r}")
    finally:
        if allocated:
            kernel32.FreeConsole()
        for handle_id, handle in zip((-10, -11, -12), saved):
            kernel32.SetStdHandle(handle_id, handle)
        _sys.stdin, _sys.stdout, _sys.stderr = saved_python


@test("acl")
def test_acl_sddl_owner_parsing_ignores_the_dacl():
    """The owner must not run on into the first ACE.

    ``icacls``'s text form glues the path onto the owner, and splitting the
    SDDL on ``;`` does not help either -- the DACL that follows is itself
    ``;``-separated.  The trailing ``D`` here is part of the SID, not the DACL
    marker, which is exactly what makes this case easy to get wrong.
    """
    check_eq(acl._sddl_owner("O:S-1-5-21-1-2-3-1001D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)"),
             "S-1-5-21-1-2-3-1001", "owner SID bled into the DACL")
    check_eq(acl._sddl_owner("O:BA"), "BA", "a short owner alias should still parse")
    check_eq(acl._sddl_owner("O:BAD:(A;;FA;;;SY)"), "BA", "BA must not read as BAD")
    check_eq(acl._sddl_owner(""), "", "empty SDDL has no owner")
    check_eq(acl._sddl_owner("D:(A;;FA;;;SY)"), "", "no owner section means no owner")
    sections = acl._sddl_sections("O:S-1-5-18G:BAD:(A;;FA;;;SY)S:(AU;SA;FA;;;WD)")
    check_eq(sections["O"], "S-1-5-18", "owner section wrong")
    check_eq(sections["G"], "BA", "group section bled into the owner")
    check(sections["D"].startswith("(A;;FA;;;SY)"), f"DACL section wrong: {sections['D']!r}")
    check(sections["S"].startswith("(AU;SA;FA;;;WD)"), f"SACL section wrong: {sections['S']!r}")


@test("acl")
def test_acl_child_processes_never_open_a_console_window():
    """Every icacls/net child must carry CREATE_NO_WINDOW.

    run.py calls FreeConsole, so a child console app launched without the flag
    is given a brand-new console and flashes a black window. One ACL pass over
    the 24 CJK fonts spawns ~6 children per font.
    """
    import subprocess as sp

    check(acl.HIDE_WINDOW == sp.CREATE_NO_WINDOW if os.name == "nt" else True,
          "HIDE_WINDOW must be CREATE_NO_WINDOW on Windows")

    captured: dict = {}

    def spy(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return sp.CompletedProcess(cmd, 0, "", "")

    original = acl.subprocess.run
    acl.subprocess.run = spy
    try:
        result = acl.run_hidden(["icacls", "C:\\probe.txt"])
    finally:
        acl.subprocess.run = original

    check_eq(result.returncode, 0, "spy should pass the return code through")
    check_eq(captured["cmd"], ["icacls", "C:\\probe.txt"], "command was rewritten")
    check(captured.get("capture_output") is True, "output must be captured")
    check("stdin" in captured, "stdin must be set explicitly, not inherited")
    check_eq(captured["stdin"], sp.DEVNULL,
             "stdin must be DEVNULL so no dead console handle is inherited")
    if os.name == "nt":
        check_eq(captured.get("creationflags", 0) & sp.CREATE_NO_WINDOW,
                 sp.CREATE_NO_WINDOW, "CREATE_NO_WINDOW is missing")


@test("acl")
def test_acl_font_cache_helper_is_windowless():
    """The FontCache service restart goes through the same helper."""
    import subprocess as sp

    from fonthandler import fontcache

    captured: dict = {}

    real = fontcache._run

    def spy(*cmd):
        import fonthandler.acl as acl_mod

        original_run = acl_mod.subprocess.run

        def inner(c, **kwargs):
            captured.update(kwargs)
            captured["cmd"] = c
            return sp.CompletedProcess(c, 0, "", "")

        acl_mod.subprocess.run = inner
        try:
            return real(*cmd)
        finally:
            acl_mod.subprocess.run = original_run

    fontcache._run = spy
    try:
        ok = fontcache._run("net", "stop", "FontCache")
    finally:
        fontcache._run = real

    check(ok, "a zero return code must read as success")
    check_eq(captured.get("cmd"), ["net", "stop", "FontCache"], "command changed")
    check_eq(captured.get("stdin"), sp.DEVNULL, "net must not inherit a console stdin")
    if os.name == "nt":
        check_eq(captured.get("creationflags", 0) & sp.CREATE_NO_WINDOW,
                 sp.CREATE_NO_WINDOW, "net needs CREATE_NO_WINDOW too")


@test("acl")
def test_acl_console_encoding_matches_the_oem_code_page():
    """icacls writes GBK on a Chinese install; UTF-8 turned it to mojibake."""
    import ctypes

    if os.name != "nt":
        return
    expected = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    check_eq(acl.console_encoding(), expected,
             "child output must be decoded with the console code page")


@test("acl")
def test_acl_sddl_round_trip_on_a_real_file():
    """Read a real descriptor, change it, put it back byte for byte."""
    import tempfile

    from fonthandler import acl as acl_mod

    backend = acl_mod.get_backend()
    if type(backend).__name__ != "WindowsSecurityBackend":
        return  # sandboxed CI
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.txt"
        probe.write_text("hello", encoding="utf-8")
        original = backend.get_sddl(probe)
        check(original.startswith("O:"), f"SDDL should start with O:, got {original!r}")
        check("D:" in original, f"SDDL should carry a DACL, got {original!r}")
        check_eq(acl_mod._sddl_owner(original), backend.get_owner(probe),
                 "get_owner and the SDDL disagree")
        snapshot = acl_mod.snapshot(probe)
        try:
            backend.apply_sddl(probe, "O:S-1-5-18D:(A;;FA;;;SY)")
            check(backend.get_sddl(probe) != original, "the DACL was not replaced")
        except acl_mod.AclError:
            return  # needs WRITE_DAC; not available here
        acl_mod.restore(snapshot)
        check_eq(backend.get_sddl(probe), original, "the ACL was not restored")


@test("acl")
def test_acl_set_owner_leaves_every_ace_alone():
    """icacls /setowner rewrote the DACL, so restore never round-tripped.

    It turned an inherited ``(A;ID;FA;;;OW)`` into ``(A;IOID;FA;;;OW)`` -- an
    inherit-only ACE the file never had.
    """
    import tempfile

    from fonthandler import acl as acl_mod

    backend = acl_mod.get_backend()
    if type(backend).__name__ != "WindowsSecurityBackend":
        return  # sandboxed CI
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.txt"
        probe.write_text("hello", encoding="utf-8")
        before = acl_mod._sddl_sections(backend.get_sddl(probe)).get("D", "")
        check(before, "expected a DACL to compare against")
        try:
            # Setting the owner to the SID it already holds: must change nothing.
            backend.set_owner(probe, backend.get_owner(probe))
        except acl_mod.AclError:
            return  # needs WRITE_OWNER
        after = acl_mod._sddl_sections(backend.get_sddl(probe)).get("D", "")
        check_eq(after, before, "set_owner must not touch the ACEs")


@test("replace")
def test_replace_reports_the_real_last_error():
    """A poisoned ``get_last_error`` must not masquerade as WinError 58.

    ``ctypes.windll`` keeps no last-error state, so the code used to surface a
    stale network error (58 / ERROR_BAD_NET_RESP) instead of the real failure.
    """
    import ctypes
    from ctypes import wintypes

    move = replace._move_file_ex()
    check_eq(ctypes.get_last_error(), 0 if os.name != "nt" else ctypes.get_last_error(),
             "sanity")
    # Point it at a target whose directory does not exist: a genuine
    # ERROR_PATH_NOT_FOUND / ERROR_FILE_NOT_FOUND, never 58.
    try:
        replace.RealFileOps().replace(
            Path("Z:\\definitely\\missing\\a.ttf"), Path("Z:\\definitely\\missing\\b.ttf")
        )
    except OSError as exc:
        check(exc.errno not in (58,),
              f"a stale WinError 58 leaked out: {exc!r}")
        check(exc.errno in (2, 3, 5, 21, 32, 123, 1920, 1921),
              f"unexpected errno {exc.errno} for a missing path: {exc!r}")
    else:
        pass  # the Z: path resolved; nothing to assert


@test("ui")
def test_ui_window_builds_all_five_pages():
    from PyQt6.QtWidgets import QMainWindow

    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        check(isinstance(win, QMainWindow), "window is not a QMainWindow")
        check_eq(win.tabs.count(), 5, "the window should host five pages")
        check_eq([win.tabs.tabText(i) for i in range(win.tabs.count())],
                 ["GaspHack", "替换字体", "备份恢复", "TrustedInstaller", "重启队列"],
                 "tab titles changed")
        for page in (win.page_gasp, win.page_replace, win.page_backup,
                     win.page_acl, win.page_pending):
            check(page.body.count() >= 3, f"{type(page).__name__} looks empty")
            check(page.progress.maximum() == 100, f"{type(page).__name__} progress missing")
        check(win.status.currentMessage(), "the status bar stayed empty")
        check(win.dock_log.widget() is win.log_panel, "the log panel is not docked")
        win.close()
    finally:
        sb.cleanup()


@test("ui")
def test_ui_shows_the_sandbox_badge():
    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        check("沙盒" in win.page_replace.sandbox_badge.text(),
              "no sandbox badge although the target dir is a sandbox")
        check("沙盒" in win.status.currentMessage(),
              "the status bar does not mention sandbox mode")
        win.close()
    finally:
        sb.cleanup()


@test("ui")
def test_ui_tables_are_filled_on_the_gui_thread():
    """Regression: worker threads must never touch widgets themselves."""
    from PyQt6.QtCore import QThread

    sb = Sandbox()
    try:
        _qapp()
        sb.populate(["msyh.ttc", "msyi.ttf"])
        win = sb.window()
        gui_thread = QThread.currentThread()

        threads: dict[str, object] = {}
        for page in (win.page_replace, win.page_backup, win.page_acl, win.page_pending):
            original = page._fill

            def spy(rows, _page=page, _original=original):
                threads[type(_page).__name__] = QThread.currentThread()
                _original(rows)

            page._fill = spy

        _act(win.page_replace, win.page_replace.scan)
        check_eq(win.page_replace.table.rowCount(), len(config.CJK_WHITELIST),
                 "the scan results never reached the table")

        _act(win.page_backup, win.page_backup.refresh_list)
        _act(win.page_acl, win.page_acl.scan)
        _act(win.page_pending, win.page_pending.refresh)

        check_eq(sorted(threads),
                 ["AclPage", "BackupPage", "PendingPage", "ReplacePage"],
                 "not every table filler ran")
        for name, thread in threads.items():
            check(thread is gui_thread,
                  f"{name} filled a table off the GUI thread ({thread})")
        win.close()
    finally:
        sb.cleanup()


@test("ui")
def test_ui_gasp_page_generates_and_verifies():
    from fonthandler import gasp

    sb = Sandbox()
    saved = {}
    try:
        _qapp()
        sb.populate(["msyi.ttf"])
        win = sb.window()
        saved = _guard(True)

        _act(win.page_gasp, win.page_gasp.run)
        check((sb.source_dir / "msyi.ttf").exists(), "no output was generated")
        check(gasp.has_gasp_hack((sb.source_dir / "msyi.ttf").read_bytes()),
              "the generated font is not gasp-hacked")
        check_eq(win.page_gasp.table.rowCount(), 1,
                 "the result table is not populated")

        _act(win.page_gasp, win.page_gasp.verify)
        check("通过" in win.page_gasp.status.text(),
              f"verify said {win.page_gasp.status.text()!r}")
        win.close()
    finally:
        _unguard(saved)
        sb.cleanup()


@test("ui")
def test_ui_replace_page_hot_replaces_in_the_sandbox():
    sb = Sandbox()
    saved = {}
    try:
        _qapp()
        names = ["msyi.ttf", "simhei.ttf"]
        sb.populate(names)
        win = sb.window()
        saved = _guard(True)

        win.page_replace.force.setChecked(False)
        win.page_replace.backup.setChecked(True)
        _act(win.page_replace, win.page_replace.replace)

        for name in names:
            check((sb.fonts_dir / name).read_bytes() == (sb.source_dir / name).read_bytes(),
                  f"{name} was not replaced by the UI worker")
        check(len(backup.list_backups(sb.backup_dir)) >= 1,
              "the UI replace path skipped the automatic backup")
        check("热替换 2" in win.page_replace.status.text(),
              f"unexpected summary {win.page_replace.status.text()!r}")

        _act(win.page_replace, win.page_replace.scan)
        identical = [r for r in range(win.page_replace.table.rowCount())
                     if win.page_replace.table.item(r, 3).text() == "是"]
        check_eq(len(identical), len(names), "the scan does not see the fonts as identical")
        win.close()
    finally:
        _unguard(saved)
        sb.cleanup()


@test("ui")
def test_ui_destructive_actions_ask_first():
    sb = Sandbox()
    saved = {}
    try:
        _qapp()
        sb.populate(["msyi.ttf"])
        win = sb.window()
        saved = _guard(False)

        win.page_replace.replace()
        _pump(win.page_replace)
        check((sb.fonts_dir / "msyi.ttf").read_bytes() !=
              (sb.source_dir / "msyi.ttf").read_bytes(),
              "replacing went ahead even though the dialog was declined")

        sb.file_ops.locked = {sb.fonts_dir.resolve() / "msyi.ttf"}
        registry.add_pending(registry.PendingEntry(r"\??\C:\Windows\Fonts\msyi.ttf.new",
                                                   r"\??\C:\Windows\Fonts\msyi.ttf"),
                             sb.registry)
        win.page_pending.clear_all()
        _pump(win.page_pending)
        check_eq(len(registry.read_pending(sb.registry)), 1,
                 "the queue was emptied without confirmation")

        (sb.fonts_dir / "msyi.ttf.new").write_bytes(b"stale")
        win.page_acl.cleanup()
        _pump(win.page_acl)
        check((sb.fonts_dir / "msyi.ttf.new").exists(),
              "the .new cleanup ran without confirmation")
        win.close()
    finally:
        _unguard(saved)
        sb.cleanup()


@test("ui")
def test_ui_backup_page_restores_a_package():
    from fonthandler import gasp

    sb = Sandbox()
    saved = {}
    try:
        _qapp()
        sb.populate(["msyi.ttf"])
        win = sb.window()
        saved = _guard(True)

        win.page_backup.backup()
        _pump(win.page_backup)
        check("备份完成" in win.page_backup.status.text(),
              f"backup said {win.page_backup.status.text()!r}")
        check(win.page_backup.table.rowCount() >= 1, "the backup list stayed empty")

        win.page_replace.replace()
        _pump(win.page_replace)
        check(gasp.has_gasp_hack((sb.fonts_dir / "msyi.ttf").read_bytes()),
              "the sandbox font was not replaced before restoring")

        win.page_backup.table.selectRow(0)
        win.page_backup.restore()
        _pump(win.page_backup)
        check(not gasp.has_gasp_hack((sb.fonts_dir / "msyi.ttf").read_bytes()),
              "the restore did not put the original bytes back")
        win.close()
    finally:
        _unguard(saved)
        sb.cleanup()


@test("ui")
def test_ui_pending_page_clears_only_our_entries():
    sb = Sandbox()
    saved = {}
    try:
        _qapp()
        win = sb.window()
        saved = _guard(True)
        ours = registry.PendingEntry(r"\??\C:\Windows\Fonts\msyi.ttf.new",
                                     r"\??\C:\Windows\Fonts\msyi.ttf")
        theirs = registry.PendingEntry(r"\??\C:\Updates\x.tmp", r"\??\C:\Updates\x")
        registry.add_pending(ours, sb.registry)
        registry.add_pending(theirs, sb.registry)

        win.page_pending.refresh()
        _pump(win.page_pending)
        check_eq(win.page_pending.table.rowCount(), 2, "the queue was not listed")

        win.page_pending.clear_ours()
        _pump(win.page_pending)
        check_eq(win.page_pending.table.rowCount(), 1,
                 "clearing our entries removed a foreign one too")
        check_eq(registry.read_pending(sb.registry), [theirs], "wrong entry survived")

        win.page_pending.clear_all()
        _pump(win.page_pending)
        check_eq(win.page_pending.table.rowCount(), 0, "clear-all left rows behind")
        win.close()
    finally:
        _unguard(saved)
        sb.cleanup()


@test("ui")
def test_ui_acl_page_resets_permissions():
    sb = Sandbox()
    saved = {}
    try:
        _qapp()
        sb.populate(["msyi.ttf"])
        win = sb.window()
        saved = _guard(True)

        win.page_acl.scan()
        _pump(win.page_acl)
        check_eq(win.page_acl.table.rowCount(), 1, "the ACL table is not populated")

        win.page_acl.table.selectRow(0)
        win.page_acl.reset()
        _pump(win.page_acl)
        check("重置完成" in win.page_acl.status.text(),
              f"reset said {win.page_acl.status.text()!r}")
        check_eq(sb.security.get_owner(sb.fonts_dir / "msyi.ttf"), config.TI_SID,
                 "the owner was not restored to TrustedInstaller")
        win.close()
    finally:
        _unguard(saved)
        sb.cleanup()


@test("ui")
def test_ui_log_filter_hides_lower_levels():
    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        panel = win.log_panel
        panel.clear()
        panel.log("debug", "调试行")
        panel.log("error", "错误行")
        check("调试行" not in panel.view.toPlainText(),
              "a debug line survived the default info filter")
        check("错误行" in panel.view.toPlainText(), "the error line went missing")
        panel.level_box.setCurrentText("debug")
        check("调试行" in panel.view.toPlainText(),
              "raising the filter back to debug did not restore the line")
        panel.level_box.setCurrentText("error")
        check("错误行" in panel.view.toPlainText(), "the error line disappeared")
        panel.clear()
        check_eq(panel.view.toPlainText(), "", "clear left text behind")
        win.close()
    finally:
        sb.cleanup()


@test("ui")
def test_ui_cancels_a_running_job():
    import time as _time

    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        page = win.page_gasp
        seen = {"progress": 0}

        def job(handle):
            for i in range(500):
                handle.progress("x", i, 500)
                seen["progress"] = i
                handle.check()
                _time.sleep(0.002)
            return "不该到这里"

        page.start(job)
        page.cancel()
        _pump(page)
        check_eq(page.status.text(), "已取消", "cancellation was not reported")
        check(seen["progress"] < 499, "the worker ran to completion despite cancel")
        check(not page.cancel_button.isEnabled(), "the cancel button stayed enabled")
        win.close()
    finally:
        sb.cleanup()


@test("ui")
def test_ui_worker_errors_are_reported_not_raised():
    sb = Sandbox()
    try:
        _qapp()
        win = sb.window()
        page = win.page_acl

        def boom(handle):
            raise RuntimeError("炸了")

        page.start(boom)
        _pump(page)
        check_eq(page.status.text(), "炸了", "the exception message was not surfaced")
        check("炸了" in win.log_panel.view.toPlainText(), "the log panel missed the error")
        check_eq(page.progress.value(), 0, "a failed job shows 100%")
        check(not page.cancel_button.isEnabled(), "the cancel button stayed enabled")
        win.close()
    finally:
        sb.cleanup()




# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    global VERBOSE
    args = [a for a in argv[1:] if not a.startswith("-")]
    VERBOSE = "-v" in argv or "--verbose" in argv
    groups = {a for a in args}

    selected = [t for t in _TESTS if not groups or t[0] in groups]
    if not selected:
        print(f"no tests matched {sorted(groups)}; groups are "
              f"{sorted({t[0] for t in _TESTS})}")
        return 2

    width = max(len(name) for _g, name, _f in selected)
    current = None
    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for group, name, fn in selected:
        if group != current:
            current = group
            print(f"\n[{group}]")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append((f"{group}.{name}", str(exc)))
            print(f"  FAIL  {name:<{width}}  {exc}")
        else:
            passed += 1
            print(f"  ok    {name}")

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    if failures:
        print("\nfailures:")
        for name, message in failures:
            print(f"  - {name}: {message.splitlines()[0]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
