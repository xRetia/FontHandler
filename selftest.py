"""Headless self-test entry point.

    python selftest.py           # all groups
    python selftest.py sfnt gasp # selected groups
    python selftest.py -v        # verbose

Nothing here touches ``C:\\Windows\\Fonts``; every destructive operation runs
inside a temporary sandbox directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tests.test_core import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv))
