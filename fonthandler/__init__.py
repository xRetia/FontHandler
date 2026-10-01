"""FontHandler — PyQt6 font GaspHack / replacement toolchain.

Layering:

    sfnt.py        raw sfnt/TTC table engine (byte-exact)
    gasp.py        GaspHack patch + TTC split/merge (AllUniteTTC equivalent)
    config.py      whitelist, defaults, persisted settings
    acl.py         TrustedInstaller owner / SDDL snapshot & restore
    registry.py    HKLM access (injectable) + pending-rename queue
    replace.py     hot-replace / reboot-queue engine (injectable FileOps)
    backup.py      zip backup packages + restore
    pipeline.py    high-level workflows, Qt-free
    ui/            PyQt6 front end (Worker in ui/common.py is the signal bus)
"""

from __future__ import annotations

__version__ = "2.0.0"
__author__ = "FontHandler"

__all__ = [
    "sfnt",
    "gasp",
    "config",
    "acl",
    "registry",
    "session",
    "replace",
    "backup",
    "pipeline",
    "fontcache",
    "livefont",
    "systeminfo",
    "elevation",
]
