"""Pulse sampling: one {series key: value} dict per sample.

A key is "<metric>" or "<metric>@<label>", where the label is a CPU core, a
thermal zone, a network interface, a mount point or an app. Keys are the same
whether a value comes from the Orbit OS API or from /proc and /sys.

- Orbit OS API (on the device, or from a computer with the device in Developer Mode):
  SystemService.GetMetrics for memory, load, uptime, CPU frequency, SoC temperature,
  disk "/", disk I/O and network counters; PackageManagerService for the apps and
  their process ids. GetMetrics is a snapshot the system refreshes every 20 s.
- On the device only: /proc and /sys, for what that snapshot does not give: CPU over
  the exact interval, every thermal zone, interface and mount point, swap, and CPU
  and memory per app. Nothing here starts another program.
"""
from __future__ import annotations

import os
import threading
import time

import grpc
from logger import Logger

LOG_TAG = "collector"

# File systems worth a disk usage series; the rest are virtual (proc, tmpfs, cgroup…).
DISK_FS = {"ext2", "ext3", "ext4", "vfat", "exfat", "f2fs", "btrfs", "xfs", "ntfs", "ntfs3", "fuseblk"}


class Collector:
    def __init__(self, client, on_device: bool, proc: str = "/proc", sys: str = "/sys") -> None:
        self._client = client
        self.on_device = on_device
        self._proc = proc
        self._sys = sys
        self._prev: dict[str, tuple[float, dict[str, float]]] = {}
        self._failing: set[str] = set()
        self._info: dict | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, float] = {}
        self._latest_ts = 0
        self._apps: list[dict] = []
        try:
            self._clk_tck = os.sysconf("SC_CLK_TCK")
            self._page_size = os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, ValueError, OSError):
            self._clk_tck, self._page_size = 100, 4096
        self._ncpu = os.cpu_count() or 1

    # ── public ─────────────────────────────────────────────────────────────

    def info(self) -> dict:
        """Static facts about the device (asked once)."""
        if self._info is not None:
            return self._info
        s = self._client.system_manager
        info: dict = {}
        for name, call in (
            ("hardware", s.get_hardware_model),
            ("cpu_model", s.get_cpu_model),
            ("cpu_cores", s.get_cpu_cores),
            ("total_ram", s.get_total_ram),
            ("architecture", s.get_architecture),
            ("os", lambda: f"{s.get_os_name()} {s.get_os_version()}".strip()),
            ("api", s.get_api_version_info),
        ):
            try:
                info[name] = call()
            except grpc.RpcError as e:
                Logger.warn(LOG_TAG, f"device info {name}: {_rpc_error(e)}")
        self._info = info
        return info

    def sample(self) -> dict[str, float]:
        values: dict[str, float] = {}
        self._run("metrics", self._from_api, values)
        if self.on_device:
            self._run("cpu", self._cpu, values)
            self._run("thermal", self._thermal, values)
            self._run("network", self._network, values)
            self._run("disks", self._disks, values)
            self._run("swap", self._swap, values)
        self._run("apps", self._apps_usage, values)
        with self._lock:
            self._latest = values
            self._latest_ts = int(time.time())
        return values

    def latest(self) -> tuple[int, dict[str, float]]:
        with self._lock:
            return self._latest_ts, dict(self._latest)

    def apps(self) -> list[dict]:
        with self._lock:
            return list(self._apps)

    # ── helpers ────────────────────────────────────────────────────────────

    def _run(self, source: str, fn, values: dict) -> None:
        """Runs one reader; logs a failure once, and again when it recovers."""
        try:
            fn(values)
        except (grpc.RpcError, OSError, ValueError, IndexError) as e:
            if source not in self._failing:
                self._failing.add(source)
                msg = _rpc_error(e) if isinstance(e, grpc.RpcError) else str(e)
                Logger.warn(LOG_TAG, f"{source}: {msg}")
            return
        if source in self._failing:
            self._failing.discard(source)
            Logger.info(LOG_TAG, f"{source}: reading again")

    def _rates(self, name: str, clock: float, counters: dict[str, float]) -> dict[str, float]:
        """Per-second rates against the previous reading of the same counters."""
        prev = self._prev.get(name)
        self._prev[name] = (clock, counters)
        if prev is None:
            return {}
        dt = clock - prev[0]
        if dt <= 0:
            return {}  # same snapshot, or the counters restarted (reboot)
        old = prev[1]
        return {k: (v - old[k]) / dt for k, v in counters.items() if k in old and v >= old[k]}

    def _read(self, *parts: str) -> str:
        with open(os.path.join(*parts), encoding="utf-8", errors="replace") as f:
            return f.read()

    # ── Orbit OS API ───────────────────────────────────────────────────────

    def _from_api(self, values: dict) -> None:
        m = self._client.system_manager.get_metrics().metrics
        vm = m.virtual_memory
        values["mem.used_pct"] = vm.percent or m.memory_usage
        values["mem.used_bytes"] = vm.used
        values["mem.available_bytes"] = vm.available
        values["mem.cached_bytes"] = vm.cached + vm.buffers
        load = list(m.sys_uptime.load_average)
        if len(load) == 3:
            values["load.1m"], values["load.5m"], values["load.15m"] = load
        if m.sys_uptime.uptime:
            values["sys.uptime_s"] = m.sys_uptime.uptime
        if m.cpu_freq.current:
            values["cpu.freq_mhz"] = m.cpu_freq.current
        if m.sensors_temperatures:
            values["temp.soc_c"] = m.soc_thermal
        for mount, du in m.disk_usage.items():
            values[f"disk.used_pct@{mount}"] = du.percent
            values[f"disk.used_bytes@{mount}"] = du.used
        if not self.on_device:
            # on the device /proc/stat gives the exact interval instead
            values["cpu.total_pct"] = m.cpu_usage
            for i, pct in enumerate(m.cpu_core_percent):
                values[f"cpu.core_pct@{i}"] = pct
        # counters → rates, timed by the snapshot's own uptime (it changes every 20 s)
        counters = {
            "disk.read_bps": m.disk_io_counters.read_bytes,
            "disk.write_bps": m.disk_io_counters.write_bytes,
            "cpu.ctx_switches_ps": m.cpu_stats.ctx_switches,
            "cpu.interrupts_ps": m.cpu_stats.interrupts,
        }
        if not self.on_device:
            # the API sums every interface, loopback included
            counters["net.rx_bps@all"] = m.network.bytes_recv
            counters["net.tx_bps@all"] = m.network.bytes_sent
        values.update(self._rates("api", m.sys_uptime.uptime, counters))

    # ── /proc and /sys (on the device) ─────────────────────────────────────

    def _cpu(self, values: dict) -> None:
        counters: dict[str, float] = {}
        for line in self._read(self._proc, "stat").splitlines():
            if not line.startswith("cpu"):
                break
            name, *fields = line.split()
            # user nice system idle iowait irq softirq steal (guest time is already in user)
            t = [int(x) for x in fields[:8]]
            label = "all" if name == "cpu" else name[3:]
            counters[f"{label}.total"] = sum(t)
            counters[f"{label}.idle"] = t[3] + t[4]
            counters[f"{label}.iowait"] = t[4]
        prev = self._prev.get("cpu")
        self._prev["cpu"] = (time.monotonic(), counters)
        if prev is None:
            return
        old = prev[1]

        def busy(label: str) -> float | None:
            d_total = counters[f"{label}.total"] - old.get(f"{label}.total", 0)
            d_idle = counters[f"{label}.idle"] - old.get(f"{label}.idle", 0)
            if f"{label}.total" not in old or d_total <= 0:
                return None
            return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

        for key in counters:
            label, kind = key.rsplit(".", 1)
            if kind != "total":
                continue
            pct = busy(label)
            if pct is None:
                continue
            if label == "all":
                values["cpu.total_pct"] = pct
                d_total = counters["all.total"] - old["all.total"]
                values["cpu.iowait_pct"] = 100.0 * (counters["all.iowait"] - old["all.iowait"]) / d_total
            else:
                values[f"cpu.core_pct@{label}"] = pct

    def _thermal(self, values: dict) -> None:
        base = os.path.join(self._sys, "class", "thermal")
        try:
            zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
        except FileNotFoundError:
            return
        seen: dict[str, int] = {}
        for zone in zones:
            try:
                kind = self._read(base, zone, "type").strip() or zone
                milli = int(self._read(base, zone, "temp").strip())
            except (OSError, ValueError):
                continue
            seen[kind] = seen.get(kind, 0) + 1
            label = kind if seen[kind] == 1 else f"{kind}-{seen[kind]}"
            values[f"temp.zone_c@{label}"] = milli / 1000.0

    def _network(self, values: dict) -> None:
        counters: dict[str, float] = {}
        for line in self._read(self._proc, "net", "dev").splitlines()[2:]:
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            f = data.split()
            counters[f"net.rx_bps@{iface}"] = int(f[0])
            counters[f"net.tx_bps@{iface}"] = int(f[8])
        rates = self._rates("net", time.monotonic(), counters)
        values.update(rates)
        if rates:
            values["net.rx_bps@all"] = sum(v for k, v in rates.items() if k.startswith("net.rx_bps@"))
            values["net.tx_bps@all"] = sum(v for k, v in rates.items() if k.startswith("net.tx_bps@"))

    def _disks(self, values: dict) -> None:
        by_device: dict[str, str] = {}
        for line in self._read(self._proc, "mounts").splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] not in DISK_FS:
                continue
            device, mount = parts[0], _unescape(parts[1])
            # a device mounted twice (bind mounts) counts once, at its shortest path
            if device not in by_device or len(mount) < len(by_device[device]):
                by_device[device] = mount
        for mount in sorted(by_device.values()):
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            if used + avail <= 0:
                continue
            values[f"disk.used_pct@{mount}"] = 100.0 * used / (used + avail)
            values[f"disk.used_bytes@{mount}"] = used

    def _swap(self, values: dict) -> None:
        info = {}
        for line in self._read(self._proc, "meminfo").splitlines():
            name, _, rest = line.partition(":")
            if name in ("SwapTotal", "SwapFree"):
                info[name] = int(rest.split()[0]) * 1024
        total = info.get("SwapTotal", 0)
        if total > 0:
            values["mem.swap_used_pct"] = 100.0 * (total - info.get("SwapFree", 0)) / total

    def _apps_usage(self, values: dict) -> None:
        packages = self._client.package_manager.list_installed_packages()
        rows = [
            {
                "package_id": p.package_id,
                "name": p.display_name or p.package_id,
                "version": p.version,
                "running": p.is_running,
                "pid": p.process_id,
                "uptime_s": p.uptime_seconds,
            }
            for p in packages
        ]
        values["apps.running"] = sum(1 for r in rows if r["running"])
        if self.on_device:
            self._per_app(rows, values)
        with self._lock:
            self._apps = rows

    def _per_app(self, rows: list[dict], values: dict) -> None:
        """CPU and memory per app: every process in the app's session (apps start with setsid)."""
        sessions = {r["pid"]: r for r in rows if r["running"] and r["pid"] > 0}
        ticks: dict[int, int] = {}
        rss: dict[int, int] = {}
        for entry in os.listdir(self._proc):
            if not entry.isdigit():
                continue
            try:
                stat = self._read(self._proc, entry, "stat")
            except OSError:
                continue  # the process ended meanwhile
            rest = stat[stat.rfind(")") + 2 :].split()
            session = int(rest[3])
            if session in sessions:
                ticks[session] = ticks.get(session, 0) + int(rest[11]) + int(rest[12])
                rss[session] = rss.get(session, 0) + int(rest[21]) * self._page_size
        counters = {sessions[pid]["package_id"]: t for pid, t in ticks.items()}
        rates = self._rates("apps", time.monotonic(), counters)
        for pid, row in sessions.items():
            pkg = row["package_id"]
            if pid in rss:
                row["mem_bytes"] = rss[pid]
                values[f"app.mem_bytes@{pkg}"] = rss[pid]
            if pkg in rates:
                # share of the whole CPU (all cores), like the CPU chart
                pct = 100.0 * rates[pkg] / self._clk_tck / self._ncpu
                row["cpu_pct"] = round(pct, 2)
                values[f"app.cpu_pct@{pkg}"] = pct


def _unescape(path: str) -> str:
    """/proc/mounts writes spaces, tabs, newlines and backslashes as octal escapes."""
    for code, ch in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        path = path.replace(code, ch)
    return path


def _rpc_error(e: grpc.RpcError) -> str:
    try:
        return f"{e.code().name}: {e.details()}"
    except Exception:
        return str(e)
