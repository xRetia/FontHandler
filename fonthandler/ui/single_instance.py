"""Single-instance guard.

Every launch used to open another copy of the main window, so repeatedly
double-clicking the launcher stacked windows up.  ``QLocalServer`` gives us a
per-user named socket: the first process listens on it, later ones fail to
connect and exit instead of drawing a second window.
"""

from __future__ import annotations

from PyQt6.QtNetwork import QLocalServer, QLocalSocket

__all__ = ["SingleInstance"]

_PROBE_MS = 300


class SingleInstance:
    """Claim a named local socket for the lifetime of the process."""

    def __init__(self, key: str) -> None:
        self.key = key
        self._server = QLocalServer()

    def claim(self) -> bool:
        """True when this process owns the key, False when another one does."""
        probe = QLocalSocket()
        probe.connectToServer(self.key)
        alive = probe.waitForConnected(_PROBE_MS)
        probe.abort()
        if alive:
            return False
        # The socket file outlives a crashed process; clear the stale one.
        QLocalServer.removeServer(self.key)
        return self._server.listen(self.key)

    def release(self) -> None:
        self._server.close()
        QLocalServer.removeServer(self.key)