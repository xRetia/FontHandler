import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.sandbox import Sandbox
from fonthandler import acl, backup, gasp, pipeline, registry, session

sb = Sandbox()
try:
    names = ["msyh.ttc", "msjh.ttc", "msyi.ttf", "Deng.ttf", "simhei.ttf",
             "malgun.ttf", "YuGothR.ttc", "msgothic.ttc"]
    sb.populate(names)
    print("sandbox:", sb.root)

    # --- generate gasp into source -------------------------------------
    ctx = sb.context()
    rep = gasp.run_batch(sb.fonts_dir, sb.source_dir, files=names,
                         progress=lambda n, s: None)
    print(f"gasp batch: {rep.total} files, hacked={rep.hacked}, unchanged={rep.unchanged}")

    # --- scan -----------------------------------------------------------
    rows = pipeline.scan_targets(ctx)
    print(f"scan: {len(rows)} rows, needs-replace={sum(1 for r in rows if not r.identical)}")

    # --- replace (with auto backup) -------------------------------------
    ctx.settings.backup_before_replace = True
    results = pipeline.run_replace(ctx, names=names)
    for r in results:
        print(f"  {r.name:<18} {r.status.value:<16} {r.message}")

    # verify bytes now match source
    ok = all((sb.fonts_dir / n).read_bytes() == (sb.source_dir / n).read_bytes() for n in names)
    print("post-replace bytes match:", ok)

    # verify backup created
    pkgs = backup.list_backups(sb.backup_dir)
    print("backups:", len(pkgs), "count:", pkgs[0].count if pkgs else 0)

    # --- idempotency ----------------------------------------------------
    results2 = pipeline.run_replace(ctx, names=names, backup_first=False)
    print("second run all identical:",
          all(r.status.is_skip for r in results2))

    # --- locked file -> reboot queue ------------------------------------
    locked = sb.fonts_dir / "msyh.ttc"
    sb.file_ops.locked = {locked.resolve()}
    from fonthandler.replace import ReplaceOptions
    res = sb.engine().replace_one("msyh.ttc", ReplaceOptions(queue_on_lock=True), force=True)
    print("locked replace:", res.status.value, "| sandbox queue:", len(sb.file_ops.reboot_queue))
    print("registry pending:", len(session.read_pending(sb.registry)))
    sb.file_ops.locked = set()

    # --- acl snapshot / restore -----------------------------------------
    target = sb.fonts_dir / "msyi.ttf"
    snap = acl.snapshot(target, sb.security)
    print("snapshot owner:", snap.owner_sid[:20], "inherited:", snap.inherited)
    sb.security.set_owner(target, "S-1-5-32-544")
    acl.restore(snap, sb.security)
    print("restored owner ok:", sb.security.get_owner(target) == snap.owner_sid)

    # --- pending queue round trip ---------------------------------------
    entry = registry.PendingEntry("\\??\\C:\\Windows\\Fonts\\msyi.ttf.new",
                                 "\\??\\C:\\Windows\\Fonts\\msyi.ttf")
    session.add_pending(entry, sb.registry)
    print("pending after add:", len(session.read_pending(sb.registry)))
    session.clear_our_pending(sb.registry)
    print("pending after clear ours:", len(session.read_pending(sb.registry)))

    print("\nALL SANDBOX CHECKS DONE")
finally:
    sb.cleanup()