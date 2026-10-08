"""One active Pulse at a time.

Orbit OS can start an app twice when it updates it, and the extra process can
outlive later updates (a known Orbit OS issue). Pulse owns a database and a
Launcher route, so only the process that holds a lock file in the data folder
works; an extra one waits, idle, until the lock is free.

Neither process exits to solve it: Orbit OS would take the extra process's
exit for the app's own, show the app as stopped or start it once more.
"""
from __future__ import annotations

import os
import threading

LOCK_FILE = "pulse.lock"

try:
    import fcntl
except ImportError:  # Windows: development only, one process anyway
    fcntl = None


class InstanceLock:
    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, LOCK_FILE)
        self._f = None

    def acquire(self, stop: threading.Event, on_wait=None, poll_s: float = 2.0) -> bool:
        """Takes the lock, waiting while another Pulse holds it; False if stopped first."""
        if fcntl is None:
            return True
        self._f = open(self.path, "a+")
        waiting = False
        while not stop.is_set():
            try:
                fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if not waiting and on_wait is not None:
                    on_wait()
                waiting = True
                stop.wait(poll_s)
        self._f.close()
        self._f = None
        return False

    def release(self) -> None:
        if self._f is not None:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            self._f = None
