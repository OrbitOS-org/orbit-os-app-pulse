"""Pulse time-series store: one SQLite file in the app's data folder.

Three tiers, each kept for its own retention time:

    raw      every sample                   (default 48 hours)
    agg_5m   5-minute average, min and max  (default 30 days)
    agg_1h   hourly average, min and max    (default 2 years)

Samples are buffered in memory and written in one transaction per flush, to
spare the SD card. WAL mode keeps the file consistent if the app is killed
(Orbit OS stops apps with SIGKILL): at most the unflushed samples are lost.
Aggregation runs from a watermark kept in the database, so after a restart it
resumes exactly where it stopped.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time

DB_FILE = "pulse.db"

TIERS = ("raw", "agg_5m", "agg_1h")
BUCKET_S = {"agg_5m": 300, "agg_1h": 3600}
TIER_NAMES = {"raw": "raw", "agg_5m": "5m", "agg_1h": "1h"}

# A sample buffer that cannot be written (disk full, I/O error) stops growing here.
MAX_BUFFERED_ROWS = 200_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id  INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS raw (
    series_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg_5m (
    series_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg_1h (
    series_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_ROLLUP_SQL = {
    "agg_5m": (
        "INSERT OR REPLACE INTO agg_5m (series_id, ts, avg, min, max, n) "
        "SELECT series_id, (ts / 300) * 300 AS b, AVG(value), MIN(value), MAX(value), COUNT(*) "
        "FROM raw WHERE series_id = ? AND ts >= ? AND ts < ? GROUP BY b"
    ),
    "agg_1h": (
        "INSERT OR REPLACE INTO agg_1h (series_id, ts, avg, min, max, n) "
        "SELECT series_id, (ts / 3600) * 3600 AS b, SUM(avg * n) / SUM(n), MIN(min), MAX(max), SUM(n) "
        "FROM agg_5m WHERE series_id = ? AND ts >= ? AND ts < ? GROUP BY b"
    ),
}
_ROLLUP_SOURCE = {"agg_5m": "raw", "agg_1h": "agg_5m"}


def retention_seconds(cfg: dict) -> dict[str, int]:
    return {
        "raw": cfg["retention_raw_h"] * 3600,
        "agg_5m": cfg["retention_5m_d"] * 86400,
        "agg_1h": cfg["retention_1h_d"] * 86400,
    }


def pick_tier(t_from: int, t_to: int, now: int, raw_step: int, retention: dict[str, int]) -> str:
    """The finest tier that still holds data from t_from, without reading a huge number of rows."""
    span = max(1, t_to - t_from)
    if t_from >= now - retention["raw"] and span / max(1, raw_step) <= 20_000:
        return "raw"
    if t_from >= now - retention["agg_5m"] and span / 300 <= 20_000:
        return "agg_5m"
    return "agg_1h"


class Store:
    def __init__(self, data_dir: str, clock=time.time) -> None:
        self.path = os.path.join(data_dir, DB_FILE)
        self._clock = clock
        self._buf: list[tuple[int, str, float]] = []
        self._buf_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._w = self._connect()
        self._init_schema()
        self._r = self._connect()
        self._ids: dict[str, int] = dict(
            (k, i) for i, k in self._w.execute("SELECT id, key FROM series")
        )

    # ── setup ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_schema(self) -> None:
        # auto_vacuum only takes effect before the first table is created
        self._w.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._w.execute("PRAGMA journal_mode = WAL")
        self._w.executescript(_SCHEMA)

    def close(self) -> None:
        with self._write_lock:
            self._w.close()
        with self._read_lock:
            self._r.close()

    # ── writing ────────────────────────────────────────────────────────────

    def add(self, ts: int, values: dict[str, float]) -> None:
        rows = [
            (int(ts), key, float(v))
            for key, v in values.items()
            if v is not None and math.isfinite(v)
        ]
        with self._buf_lock:
            self._buf.extend(rows)
            if len(self._buf) > MAX_BUFFERED_ROWS:
                del self._buf[: len(self._buf) - MAX_BUFFERED_ROWS]

    def pending(self) -> int:
        with self._buf_lock:
            return len(self._buf)

    def flush(self) -> int:
        """Writes the buffered samples in one transaction; returns how many."""
        with self._buf_lock:
            rows, self._buf = self._buf, []
        if not rows:
            return 0
        with self._write_lock:
            try:
                self._w.execute("BEGIN")
                for key in {k for _, k, _ in rows}:
                    if key not in self._ids:
                        cur = self._w.execute("INSERT INTO series (key) VALUES (?)", (key,))
                        self._ids[key] = cur.lastrowid
                self._w.executemany(
                    "INSERT OR REPLACE INTO raw (series_id, ts, value) VALUES (?, ?, ?)",
                    [(self._ids[k], ts, v) for ts, k, v in rows],
                )
                self._w.execute("COMMIT")
            except sqlite3.Error:
                self._rollback()
                # forget ids created in the failed transaction, keep the rows for the next flush
                self._ids = dict((k, i) for i, k in self._w.execute("SELECT id, key FROM series"))
                with self._buf_lock:
                    self._buf[:0] = rows
                raise
        return len(rows)

    def _rollback(self) -> None:
        try:
            self._w.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _meta(self, key: str) -> int | None:
        row = self._w.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else int(row[0])

    def _set_meta(self, key: str, value: int) -> None:
        self._w.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, int(value)))

    def aggregate(self, now: float | None = None) -> None:
        """Rolls closed buckets up: raw → 5 minutes → 1 hour."""
        now = self._clock() if now is None else now
        with self._write_lock:
            for dst in ("agg_5m", "agg_1h"):
                self._rollup(dst, int(now))

    def _rollup(self, dst: str, now: int) -> None:
        size = BUCKET_S[dst]
        until = now // size * size  # buckets that start before this one are closed
        src = _ROLLUP_SOURCE[dst]
        done_key = f"done_{dst}"
        done = self._meta(done_key)
        if done is None:
            first = self._w.execute(f"SELECT MIN(ts) FROM {src}").fetchone()[0]
            if first is None:
                return  # nothing recorded yet
            done = int(first) // size * size
        if done > until:
            # the clock went back (set by hand, or wrong before NTP): restart from now
            self._set_meta(done_key, until)
            return
        if until == done:
            return
        try:
            self._w.execute("BEGIN")
            for sid in self._ids.values():
                self._w.execute(_ROLLUP_SQL[dst], (sid, done, until))
            self._set_meta(done_key, until)
            self._w.execute("COMMIT")
        except sqlite3.Error:
            self._rollback()
            raise

    # ── retention and size ─────────────────────────────────────────────────

    def used_bytes(self) -> int:
        page_size = self._w.execute("PRAGMA page_size").fetchone()[0]
        pages = self._w.execute("PRAGMA page_count").fetchone()[0]
        free = self._w.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * page_size

    def prune(self, retention: dict[str, int], max_bytes: int, now: float | None = None) -> dict:
        """Deletes data older than each tier's retention, then trims the oldest data
        while the database is over max_bytes. Returns what it did."""
        now = int(self._clock() if now is None else now)
        report = {"deleted": 0, "trimmed": False}
        with self._write_lock:
            try:
                self._w.execute("BEGIN")
                for tier in TIERS:
                    cutoff = now - retention[tier]
                    for sid in self._ids.values():
                        cur = self._w.execute(
                            f"DELETE FROM {tier} WHERE series_id = ? AND ts < ?", (sid, cutoff)
                        )
                        report["deleted"] += cur.rowcount
                self._w.execute("COMMIT")
                rounds = 0
                while self.used_bytes() > max_bytes and rounds < 10:
                    # over the limit: drop the oldest tenth of every tier, then measure again
                    self._w.execute("BEGIN")
                    for tier in TIERS:
                        lo, hi = self._w.execute(f"SELECT MIN(ts), MAX(ts) FROM {tier}").fetchone()
                        if lo is None:
                            continue
                        cutoff = lo + max((hi - lo) // 10, BUCKET_S.get(tier, 60))
                        cur = self._w.execute(f"DELETE FROM {tier} WHERE ts < ?", (cutoff,))
                        report["deleted"] += cur.rowcount
                    self._w.execute("COMMIT")
                    report["trimmed"] = True
                    rounds += 1
            except sqlite3.Error:
                self._rollback()
                raise
            # the pragma frees one page per step: read it to the end
            self._w.execute("PRAGMA incremental_vacuum").fetchall()
            self._w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return report

    # ── reading (web server threads) ───────────────────────────────────────

    def keys(self) -> list[str]:
        with self._read_lock:
            return [k for (k,) in self._r.execute("SELECT key FROM series ORDER BY key")]

    def query(
        self,
        keys: list[str],
        t_from: int,
        t_to: int,
        *,
        raw_step: int,
        retention: dict[str, int],
        max_points: int = 600,
        now: float | None = None,
    ) -> dict:
        """Points between t_from and t_to as [bucket_ts, avg, min, max], at most about max_points per key."""
        now = int(self._clock() if now is None else now)
        tier = pick_tier(t_from, t_to, now, raw_step, retention)
        res = raw_step if tier == "raw" else BUCKET_S[tier]
        span = max(1, t_to - t_from)
        step = max(res, math.ceil(span / max(1, max_points) / res) * res)
        if tier == "raw":
            sql = (
                "SELECT (ts / ?) * ? AS b, AVG(value), MIN(value), MAX(value) FROM raw "
                "WHERE series_id = ? AND ts >= ? AND ts <= ? GROUP BY b ORDER BY b"
            )
        else:
            sql = (
                f"SELECT (ts / ?) * ? AS b, SUM(avg * n) / SUM(n), MIN(min), MAX(max) FROM {tier} "
                "WHERE series_id = ? AND ts >= ? AND ts <= ? GROUP BY b ORDER BY b"
            )
        series: dict[str, list] = {}
        with self._read_lock:
            for key in keys:
                row = self._r.execute("SELECT id FROM series WHERE key = ?", (key,)).fetchone()
                if row is None:
                    series[key] = []
                    continue
                series[key] = [
                    [b, _round(a), _round(lo), _round(hi)]
                    for b, a, lo, hi in self._r.execute(sql, (step, step, row[0], t_from, t_to))
                ]
        return {"tier": TIER_NAMES[tier], "step": step, "from": t_from, "to": t_to, "series": series}

    def stats(self) -> dict:
        """Size of the database and how much each tier holds."""
        with self._read_lock:
            page_size = self._r.execute("PRAGMA page_size").fetchone()[0]
            pages = self._r.execute("PRAGMA page_count").fetchone()[0]
            free = self._r.execute("PRAGMA freelist_count").fetchone()[0]
            tiers = {}
            for tier in TIERS:
                rows = self._r.execute(f"SELECT COUNT(*) FROM {tier}").fetchone()[0]
                oldest = self._r.execute(
                    f"SELECT MIN(m) FROM (SELECT (SELECT MIN(ts) FROM {tier} WHERE series_id = s.id) AS m FROM series s)"
                ).fetchone()[0]
                tiers[TIER_NAMES[tier]] = {"rows": rows, "oldest": oldest}
            n_series = self._r.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        wal = self.path + "-wal"
        return {
            "file_bytes": _size(self.path) + _size(wal),
            "used_bytes": (pages - free) * page_size,
            "series": n_series,
            "tiers": tiers,
            "pending": self.pending(),
        }


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
