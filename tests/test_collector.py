import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "Pulse"))

from lib import collector as collector_mod  # noqa: E402
from lib.collector import Collector, _unescape  # noqa: E402


def metrics(uptime, read_bytes=0, recv=0):
    m = NS(
        virtual_memory=NS(percent=40.0, used=4_000, available=6_000, cached=500, buffers=100),
        memory_usage=40.0,
        sys_uptime=NS(uptime=uptime, load_average=[0.5, 0.4, 0.3]),
        cpu_freq=NS(current=1500.0),
        sensors_temperatures=1,
        soc_thermal=45.5,
        disk_usage={"/": NS(percent=35.0, used=5_000)},
        cpu_usage=12.5,
        cpu_core_percent=[10.0, 15.0],
        disk_io_counters=NS(read_bytes=read_bytes, write_bytes=0),
        cpu_stats=NS(ctx_switches=0, interrupts=0),
        network=NS(bytes_recv=recv, bytes_sent=0),
    )
    return NS(metrics=m)


def package(pid, running=True):
    return NS(package_id="org.example.app", display_name="Example", version="1.0.0",
              is_running=running, process_id=pid, uptime_seconds=60)


class FakeClient:
    def __init__(self):
        self.snapshots = []
        self.packages = [package(100)]
        self.system_manager = NS(get_metrics=lambda: self.snapshots.pop(0))
        self.package_manager = NS(list_installed_packages=lambda: self.packages)


class ProcFixture:
    """A minimal /proc and /sys tree."""

    def __init__(self, root):
        self.proc = os.path.join(root, "proc")
        self.sys = os.path.join(root, "sys")
        os.makedirs(os.path.join(self.proc, "net"))
        zone = os.path.join(self.sys, "class", "thermal", "thermal_zone0")
        os.makedirs(zone)
        self.write(os.path.join(zone, "type"), "cpu-thermal\n")
        self.write(os.path.join(zone, "temp"), "47250\n")
        self.write(os.path.join(self.proc, "meminfo"), "MemTotal: 8000 kB\nSwapTotal: 1000 kB\nSwapFree: 750 kB\n")
        self.write(os.path.join(self.proc, "mounts"),
                   "/dev/root / ext4 rw 0 0\nproc /proc proc rw 0 0\n/dev/root /bind\\040dir ext4 rw 0 0\n")

    @staticmethod
    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    def tick(self, cpu, cpu0, rx, app_ticks):
        self.write(os.path.join(self.proc, "stat"),
                   f"cpu  {cpu}\ncpu0 {cpu0}\nintr 1 2 3\n")
        self.write(os.path.join(self.proc, "net", "dev"),
                   "Inter-|   Receive\n face |bytes\n"
                   f"    lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n"
                   f"  eth0: {rx} 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0\n")
        for pid, session, ticks in ((100, 100, app_ticks), (101, 100, app_ticks), (200, 200, 5)):
            d = os.path.join(self.proc, str(pid))
            os.makedirs(d, exist_ok=True)
            rest = ["S", "1", str(session), str(session)] + ["0"] * 7 + [str(ticks), "0"] + ["0"] * 8 + ["10"]
            self.write(os.path.join(d, "stat"), f"{pid} (name with) paren) " + " ".join(rest) + "\n")


class CollectorTest(unittest.TestCase):
    def test_remote_mode_uses_the_api(self):
        client = FakeClient()
        client.snapshots = [metrics(100, read_bytes=0), metrics(120, read_bytes=2000)]
        c = Collector(client, on_device=False)
        first = c.sample()
        self.assertEqual(first["cpu.total_pct"], 12.5)
        self.assertEqual(first["cpu.core_pct@1"], 15.0)
        self.assertEqual(first["temp.soc_c"], 45.5)
        self.assertEqual(first["disk.used_pct@/"], 35.0)
        self.assertNotIn("disk.read_bps", first)  # no previous reading yet
        second = c.sample()
        self.assertEqual(second["disk.read_bps"], 100.0)  # 2000 bytes over 20 s of snapshot time
        self.assertEqual(second["apps.running"], 1)
        self.assertNotIn("app.cpu_pct@org.example.app", second)

    def test_same_snapshot_gives_no_rate(self):
        client = FakeClient()
        client.snapshots = [metrics(100, read_bytes=0), metrics(100, read_bytes=0)]
        c = Collector(client, on_device=False)
        c.sample()
        self.assertNotIn("disk.read_bps", c.sample())

    def test_unchanged_snapshot_repeats_the_last_rate(self):
        client = FakeClient()
        client.snapshots = [metrics(100, read_bytes=0), metrics(120, read_bytes=2000), metrics(120, read_bytes=2000)]
        c = Collector(client, on_device=False)
        c.sample()
        self.assertEqual(c.sample()["disk.read_bps"], 100.0)
        self.assertEqual(c.sample()["disk.read_bps"], 100.0)

    def test_failures_are_reported_once(self):
        client = FakeClient()
        client.system_manager = NS(get_metrics=mock.Mock(side_effect=OSError("down")))
        c = Collector(client, on_device=False)
        with mock.patch.object(collector_mod.Logger, "warn") as warn:
            c.sample()
            c.sample()
        self.assertEqual(warn.call_count, 1)

    def test_device_mode_reads_proc_and_sys(self):
        with tempfile.TemporaryDirectory() as root:
            fx = ProcFixture(root)
            client = FakeClient()
            client.snapshots = [metrics(100), metrics(130)]
            c = Collector(client, on_device=True, proc=fx.proc, sys=fx.sys)
            c._clk_tck, c._ncpu, c._page_size = 100, 4, 4096
            clock = iter([10.0, 10.0, 10.0, 40.0, 40.0, 40.0])
            with mock.patch.object(collector_mod.time, "monotonic", lambda: next(clock)), \
                    mock.patch.object(collector_mod.os, "statvfs", lambda p: NS(f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=1000), create=True):
                fx.tick("100 0 100 800 0 0 0 0", "50 0 50 400 0 0 0 0", rx=1000, app_ticks=10)
                c.sample()
                fx.tick("200 0 200 1400 200 0 0 0", "100 0 100 700 100 0 0 0", rx=4000, app_ticks=40)
                v = c.sample()
        # CPU over the interval: busy = total - idle, where idle includes iowait
        self.assertAlmostEqual(v["cpu.total_pct"], 100 * (1000 - 800) / 1000)
        self.assertAlmostEqual(v["cpu.iowait_pct"], 20.0)
        self.assertIn("cpu.core_pct@0", v)
        self.assertEqual(v["temp.zone_c@cpu-thermal"], 47.25)
        self.assertEqual(v["net.rx_bps@eth0"], 100.0)  # 3000 bytes in 30 s
        self.assertEqual(v["net.rx_bps@all"], 100.0)
        self.assertNotIn("net.rx_bps@lo", v)
        self.assertAlmostEqual(v["mem.swap_used_pct"], 25.0)
        self.assertAlmostEqual(v["disk.used_pct@/"], 100 * 60 / 90)
        self.assertNotIn("disk.used_pct@/bind dir", v)  # same device, counted once
        # app: two processes in the session, 30 ticks each in 30 s → 2 ticks/s → 0.5 % of 4 cores
        self.assertAlmostEqual(v["app.cpu_pct@org.example.app"], 0.5)
        self.assertEqual(v["app.mem_bytes@org.example.app"], 2 * 10 * 4096)
        self.assertEqual(c.apps()[0]["cpu_pct"], 0.5)

    def test_unescape(self):
        self.assertEqual(_unescape("/mnt/my\\040disk"), "/mnt/my disk")


if __name__ == "__main__":
    unittest.main()
