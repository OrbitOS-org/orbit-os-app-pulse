"""Pulse web page and JSON API.

The server listens on 127.0.0.1 only. On the device the page is registered with
the Orbit OS Launcher, which serves it at http://<device>/pulse behind the device
login, so nothing is reachable from the network directly.

The port is the first free one in 50000-60000 (the range reserved for app web
pages), starting at the preferred port and moving to the next one when a port
is taken or the Launcher refuses it.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from logger import Logger

from lib.config import ConfigError
from lib.store import retention_seconds

LOG_TAG = "web"

PORT_MIN, PORT_MAX = 50000, 60000

MAX_RANGE_S = 10 * 366 * 86400
MAX_POINTS = 2000
MAX_BODY = 4096

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class NoFreePort(RuntimeError):
    pass


class App:
    """What the request handlers read: set up once by main.py."""

    def __init__(self, *, store, collector, config, web_dir: str, version: str, mode: str) -> None:
        self.store = store
        self.collector = collector
        self.config = config
        self.web_dir = web_dir
        self.version = version
        self.mode = mode
        self.route = ""
        self.started = int(time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "Pulse"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        pass  # one line per request would flood the device log

    # ── responses ──────────────────────────────────────────────────────────

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # ── routing ────────────────────────────────────────────────────────────

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        path = parts.path or "/"
        # the Launcher strips the route; accept it anyway, for a direct request
        route = self.app.route
        if route and (path == route or path.startswith(route + "/")):
            path = path[len(route) :] or "/"
        return path, parse_qs(parts.query)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path, q = self._path()
        try:
            if path == "/health":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            elif path in STATIC:
                self._static(*STATIC[path])
            elif path == "/api/info":
                self._info()
            elif path == "/api/now":
                ts, values = self.app.collector.latest()
                self._json({"ts": ts, "values": values})
            elif path == "/api/keys":
                self._json({"keys": self.app.store.keys()})
            elif path == "/api/series":
                self._series(q)
            elif path == "/api/apps":
                self._json({"apps": self.app.collector.apps()})
            elif path == "/api/config":
                self._json(self.app.config.as_dict())
            elif path == "/api/stats":
                self._json(self.app.store.stats())
            elif path == "/api/export.csv":
                self._export(q)
            else:
                self._error(404, "not found")
        except BadRequest as e:
            self._error(400, str(e))
        except Exception as e:  # keep serving; report it
            Logger.error(LOG_TAG, f"GET {path}: {e!r}")
            self._error(500, "internal error")

    def do_POST(self) -> None:
        path, _ = self._path()
        try:
            if path != "/api/config":
                self._error(404, "not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._error(400, "expected a small JSON body")
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._error(400, "invalid JSON")
                return
            if not isinstance(body, dict):
                self._error(400, "expected a JSON object")
                return
            try:
                values = self.app.config.update(body)
            except ConfigError as e:
                self._error(400, str(e))
                return
            Logger.info(LOG_TAG, f"settings changed: {json.dumps(values, separators=(',', ':'))}")
            self._json(values)
        except Exception as e:
            Logger.error(LOG_TAG, f"POST {path}: {e!r}")
            self._error(500, "internal error")

    # ── handlers ───────────────────────────────────────────────────────────

    def _static(self, name: str, ctype: str) -> None:
        with open(os.path.join(self.app.web_dir, name), "rb") as f:
            body = f.read()
        if name == "index.html":
            # a new version must not reuse the browser's cached script and styles
            body = body.replace(b"{{version}}", self.app.version.encode())
        self._send(200, body, ctype)

    def _info(self) -> None:
        a = self.app
        self._json(
            {
                "name": "Pulse",
                "version": a.version,
                "mode": a.mode,
                "route": a.route,
                "started": a.started,
                "device": a.collector.info(),
                "config": a.config.as_dict(),
            }
        )

    def _range(self, q: dict) -> tuple[int, int]:
        now = int(time.time())
        try:
            if "from" in q:
                t_from = int(q["from"][0])
                t_to = int(q.get("to", [now])[0])
            else:
                t_to = now
                t_from = now - int(q.get("range", ["3600"])[0])
        except ValueError:
            raise BadRequest("from, to and range are whole seconds") from None
        if t_to <= t_from or t_to - t_from > MAX_RANGE_S:
            raise BadRequest("invalid time range")
        return t_from, t_to

    def _keys(self, q: dict) -> list[str]:
        keys = [k for k in q.get("key", []) if k]
        prefixes = [p for p in q.get("prefix", []) if p]
        if prefixes:
            keys += [k for k in self.app.store.keys() if any(k.startswith(p) for p in prefixes) and k not in keys]
        if not keys:
            raise BadRequest("give at least one key or prefix")
        return keys[:64]

    def _query(self, q: dict, max_points: int) -> dict:
        t_from, t_to = self._range(q)
        cfg = self.app.config.as_dict()
        return self.app.store.query(
            self._keys(q),
            t_from,
            t_to,
            raw_step=cfg["interval_s"],
            retention=retention_seconds(cfg),
            max_points=max_points,
        )

    def _series(self, q: dict) -> None:
        try:
            points = int(q.get("points", ["600"])[0])
        except ValueError:
            raise BadRequest("points is a whole number") from None
        self._json(self._query(q, max(10, min(MAX_POINTS, points))))

    def _export(self, q: dict) -> None:
        result = self._query(q, max_points=100_000)
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["time_utc", "unix_ts", "key", "avg", "min", "max"])
        for key, points in result["series"].items():
            for ts, avg, lo, hi in points:
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), ts, key, avg, lo, hi])
        name = time.strftime("pulse-%Y%m%d-%H%M%S.csv", time.gmtime())
        self._send(
            200,
            out.getvalue().encode(),
            "text/csv; charset=utf-8",
            {"Content-Disposition": f'attachment; filename="{name}"'},
        )


class BadRequest(ValueError):
    pass


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket take a port in use: keep it for Linux only.
    allow_reuse_address = os.name != "nt"


class WebServer:
    def __init__(self, app: App) -> None:
        self.app = app
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.addr = ""

    def start(self, preferred_port: int, register=None) -> str:
        """Listens on the first usable port from preferred_port on, wrapping around the
        range; register(addr) -> bool lets the Launcher accept or refuse each one."""
        span = PORT_MAX - PORT_MIN + 1
        for i in range(span):
            port = PORT_MIN + (preferred_port - PORT_MIN + i) % span
            try:
                httpd = _HTTPServer(("127.0.0.1", port), Handler)
            except OSError:
                continue  # taken: try the next port
            httpd.app = self.app  # type: ignore[attr-defined]
            addr = f"127.0.0.1:{port}"
            try:
                accepted = register is None or register(addr)
            except BaseException:
                httpd.server_close()
                raise
            if not accepted:
                httpd.server_close()
                continue  # refused by the Launcher: try the next port
            self._httpd = httpd
            self.addr = addr
            self._thread = threading.Thread(target=httpd.serve_forever, name="web", daemon=True)
            self._thread.start()
            return addr
        raise NoFreePort(f"no usable web port in {PORT_MIN}-{PORT_MAX}")

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
