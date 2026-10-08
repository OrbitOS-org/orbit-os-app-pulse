# Pulse — records the device's CPU, memory, temperature, disk and network use
# over time and shows it in the Orbit OS Launcher. Created with Orbit Studio.
import argparse
import os
import signal
import sqlite3
import sys
import threading
import time

import grpc
import metadata
from client import Client
from logger import Logger

from lib.collector import Collector
from lib.config import FIELDS, Config
from lib.instances import stop_older_instances
from lib.server import App, NoFreePort, WebServer
from lib.store import Store, retention_seconds

LOG_TAG = "main"

APP_DIR = os.path.dirname(os.path.abspath(__file__))

APP_ROUTE = "/pulse"
PREFERRED_PORT = 50480
# Present when the app runs on the device (Client.connect then uses it).
DEVICE_SOCKET = "/run/gravity/ipc/system_server.sock"

FLUSH_EVERY_S = 60        # write the buffered samples to the database
MAINTAIN_EVERY_S = 3600   # apply retention and the size limit
LAUNCHER_CHECK_S = 300    # the Launcher keeps registrations in memory: check ours is there
# Before this date the clock is not set yet (no RTC, NTP not synced): don't record.
VALID_CLOCK = 1_700_000_000

ERROR_ALREADY_EXISTS = 4  # types.ErrorCode


class RouteTaken(RuntimeError):
    pass


class Launcher:
    """Registers the page with the Orbit OS Launcher and keeps it registered."""

    ROUTES = (APP_ROUTE, f"{APP_ROUTE}-2", f"{APP_ROUTE}-3")

    def __init__(self, hub) -> None:
        self._hub = hub
        self.route = ""
        self.addr = ""

    def register(self, addr: str) -> bool:
        """Registers addr under the first free route; False makes the server try another port."""
        for route in self.ROUTES:
            try:
                resp = self._hub.register_web_ui(addr, route)
            except grpc.RpcError as e:
                Logger.warn(LOG_TAG, f"Launcher refused {addr}: {e.code().name} {e.details()}")
                return False
            code = resp.error.code
            if code == 0:
                self.route, self.addr = route, addr
                return True
            if code == ERROR_ALREADY_EXISTS:
                Logger.warn(LOG_TAG, f"Launcher route {route} is taken: {resp.error.message}")
                continue
            Logger.warn(LOG_TAG, f"Launcher refused {addr}: {resp.error.message or code}")
            return False
        raise RouteTaken(f"routes {', '.join(self.ROUTES)} are all taken")

    def check(self) -> None:
        """Registers again if the registration is gone (e.g. the system service restarted)."""
        try:
            services = self._hub.list_services().services
        except grpc.RpcError as e:
            Logger.warn(LOG_TAG, f"Launcher check: {e.code().name}")
            return
        port = int(self.addr.rsplit(":", 1)[1])
        for s in services:
            if s.port == port and any(r.path == self.route for r in s.routes):
                return
        Logger.warn(LOG_TAG, f"registration {self.route} is gone: registering again")
        try:
            if not self.register(self.addr):
                Logger.error(LOG_TAG, "could not register the page again")
        except RouteTaken as e:
            Logger.error(LOG_TAG, str(e))

    def unregister(self) -> None:
        try:
            self._hub.unregister_service()
        except grpc.RpcError:
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pulse: device metrics over time.")
    p.add_argument(
        "--host",
        default="",
        help="Development only: address of a device in Developer Mode, to run Pulse on this "
        "computer against it. Leave it out on the device.",
    )
    p.add_argument(
        "--data-dir",
        default="",
        help="Where pulse.db and config.json live (default: the app's data folder on the "
        "device, .pulse-data at the project root in development).",
    )
    lo, hi = FIELDS["interval_s"][1:]
    p.add_argument("--interval", type=int, default=0, help=f"Seconds between samples, {lo}-{hi} (overrides the setting).")
    p.add_argument("--port", type=int, default=PREFERRED_PORT, help="Preferred web port, 50000-60000.")
    args = p.parse_args()
    if args.interval and not lo <= args.interval <= hi:
        p.error(f"--interval must be between {lo} and {hi}")
    return args


def log_database(store: Store) -> None:
    s = store.stats()
    t = s["tiers"]
    oldest = min((v["oldest"] for v in t.values() if v["oldest"]), default=None)
    since = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(oldest)) if oldest else "no data yet"
    Logger.info(
        LOG_TAG,
        f"database: {s['used_bytes'] / 1e6:.1f} MB, {s['series']} series, rows raw={t['raw']['rows']} "
        f"5m={t['5m']['rows']} 1h={t['1h']['rows']}, since {since}",
    )


def run(stop: threading.Event, collector: Collector, store: Store, config: Config, launcher, interval_override: int) -> None:
    now = time.monotonic()
    next_flush = now + FLUSH_EVERY_S
    next_maintain = now + 60  # first clean-up a minute after start
    next_check = now + LAUNCHER_CHECK_S
    clock_warned = False
    while not stop.is_set():
        started = time.monotonic()
        values = collector.sample()
        wall = time.time()
        if wall >= VALID_CLOCK:
            store.add(int(wall), values)
        elif not clock_warned:
            Logger.warn(LOG_TAG, "the clock is not set yet: samples are not recorded until it is")
            clock_warned = True

        if started >= next_flush:
            try:
                store.flush()
                store.aggregate()
            except sqlite3.Error as e:
                Logger.error(LOG_TAG, f"database write: {e}")
            next_flush = started + FLUSH_EVERY_S

        if started >= next_maintain:
            cfg = config.as_dict()
            try:
                report = store.prune(retention_seconds(cfg), cfg["max_db_mb"] * 1024 * 1024)
                if report["trimmed"]:
                    Logger.warn(LOG_TAG, f"database over {cfg['max_db_mb']} MB: oldest data removed")
                if report["deleted"]:
                    log_database(store)
            except sqlite3.Error as e:
                Logger.error(LOG_TAG, f"database clean-up: {e}")
            next_maintain = started + MAINTAIN_EVERY_S

        if launcher is not None and started >= next_check:
            launcher.check()
            next_check = started + LAUNCHER_CHECK_S

        interval = interval_override or config.get("interval_s")
        stop.wait(max(1.0, interval - (time.monotonic() - started)))


def main() -> int:
    args = parse_args()

    # metadata.json sits next to this script; Orbit Run may start it from another folder.
    manifest = metadata.must_parse_app_manifest_json(os.path.join(APP_DIR, "metadata.json"))
    meta = metadata.build(manifest)
    Logger.init(meta.name, "INFO", True)

    on_device = not args.host and os.path.exists(DEVICE_SOCKET)
    if not on_device and not args.host:
        Logger.fatal(LOG_TAG, "not running on an Orbit OS device: give --host <device address> (Developer Mode)")
        return 2
    mode = "device" if on_device else "remote"
    Logger.info(LOG_TAG, f"Starting {meta.name} {meta.version} ({mode})")
    manifest.print_info()

    # On the device the working directory is the app's data folder: it survives
    # restarts, app updates and Orbit OS updates, and is removed on uninstall.
    if args.data_dir:
        data_dir = args.data_dir
    elif on_device:
        data_dir = os.getcwd()
    else:
        data_dir = os.path.join(APP_DIR, "..", "..", ".pulse-data")
    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)

    if on_device:
        # an update can leave an older Pulse running: one database, one instance
        stopped = stop_older_instances()
        if stopped:
            Logger.warn(LOG_TAG, f"stopped {len(stopped)} older Pulse process(es): {stopped}")

    config = Config(data_dir)
    store = Store(data_dir)
    log_database(store)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    with Client.connect(args.host) as client:
        collector = Collector(client, on_device)
        info = collector.info()
        Logger.info(LOG_TAG, f"device: {info.get('hardware', '?')}, API {info.get('api', '?')}")

        app = App(
            store=store,
            collector=collector,
            config=config,
            web_dir=os.path.join(APP_DIR, "web"),
            version=meta.version,
            mode=mode,
        )
        web = WebServer(app)
        launcher = Launcher(client.app_hub_manager) if on_device else None
        try:
            addr = web.start(args.port, launcher.register if launcher else None)
            if launcher is not None:
                app.route = launcher.route
                Logger.info(LOG_TAG, f"page registered with the Launcher at {launcher.route} ({addr})")
            else:
                Logger.info(LOG_TAG, f"page at http://{addr}/ (this computer only)")
        except (NoFreePort, RouteTaken) as e:
            launcher = None
            Logger.error(LOG_TAG, f"no web page: {e}. Recording continues.")

        try:
            run(stop, collector, store, config, launcher, args.interval)
        finally:
            Logger.info(LOG_TAG, "Stopping")
            try:
                store.flush()
                store.aggregate()
            except sqlite3.Error as e:
                Logger.error(LOG_TAG, f"database write: {e}")
            if launcher is not None:
                launcher.unregister()
            web.stop()
            store.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError as e:
        Logger.fatal(LOG_TAG, f"certificate not found — {e}")
        print("  Place ca.crt (and optionally client.crt + client.key) in certs/grpc/", file=sys.stderr)
        sys.exit(1)
    except grpc.RpcError as e:
        Logger.fatal(LOG_TAG, f"{e.code().name} — {e.details()}")
        sys.exit(1)
