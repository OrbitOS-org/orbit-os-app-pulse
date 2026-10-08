"""One Pulse at a time.

Orbit OS can start an app twice when it is updated, and an older process can
outlive the update. Pulse owns a database and a Launcher route, so at start-up
the newest Pulse process stops the older ones. They are easy to find: every
process of this app runs as the app's own Linux user. Nothing here starts a
program; it only reads /proc and signals processes of the same user.
"""
from __future__ import annotations

import os
import signal
import time


def _start_key(proc: str, pid: int) -> tuple[int, int] | None:
    """(start time, pid) of a live process; None if it is gone or a zombie."""
    try:
        with open(os.path.join(proc, str(pid), "stat"), encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except OSError:
        return None
    rest = stat[stat.rfind(")") + 2 :].split()
    if rest[0] == "Z":
        return None
    return int(rest[19]), pid  # field 22: start time in clock ticks since boot


def older_instances(proc: str, me: int, uid: int, entry: str = "main.py") -> list[int]:
    """Processes of this user running our entry point that started before this one."""
    mine = _start_key(proc, me)
    if mine is None:
        return []
    found = []
    for name in os.listdir(proc):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        try:
            if os.stat(os.path.join(proc, name)).st_uid != uid:
                continue
            with open(os.path.join(proc, name, "cmdline"), "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if not any(a.endswith(entry.encode()) for a in args):
            continue
        key = _start_key(proc, pid)
        if key is not None and key < mine:
            found.append(pid)
    return found


def stop_older_instances(proc: str = "/proc", wait_s: float = 10.0, kill=os.kill) -> list[int]:
    """Stops older Pulse processes (SIGTERM, then SIGKILL) and returns their pids."""
    pids = older_instances(proc, os.getpid(), os.getuid())
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # let them flush and unregister before this instance registers with the Launcher
    deadline = time.monotonic() + wait_s
    left = list(pids)
    while left and time.monotonic() < deadline:
        time.sleep(0.2)
        left = [p for p in left if _start_key(proc, p) is not None]
    for pid in left:
        try:
            kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    return pids
