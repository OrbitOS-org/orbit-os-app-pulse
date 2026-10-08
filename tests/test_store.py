import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "Pulse"))

from lib.store import Store, pick_tier, retention_seconds  # noqa: E402

CFG = {"interval_s": 30, "retention_raw_h": 48, "retention_5m_d": 30, "retention_1h_d": 730, "max_db_mb": 200}
T0 = 1_780_000_000 // 3600 * 3600  # on the hour, so bucket counts are easy to check


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = T0
        self.store = Store(self.tmp.name, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fill(self, start, end, step=30, value=lambda t: float(t % 100)):
        for t in range(start, end, step):
            self.store.add(t, {"cpu.total_pct": value(t), "mem.used_pct": 50.0})

    def test_flush_writes_and_skips_bad_values(self):
        self.store.add(T0, {"a": 1.0, "b": float("nan"), "c": None, "d": float("inf")})
        self.assertEqual(self.store.flush(), 1)
        self.assertEqual(self.store.keys(), ["a"])
        self.assertEqual(self.store.flush(), 0)

    def test_rollup_5m_and_1h(self):
        self.fill(T0, T0 + 7200, value=lambda t: 10.0 if (t - T0) < 3600 else 30.0)
        self.store.flush()
        self.store.aggregate(now=T0 + 7200)
        st = self.store.stats()
        self.assertEqual(st["tiers"]["5m"]["rows"], 2 * 24)  # 2 series x 24 buckets
        self.assertEqual(st["tiers"]["1h"]["rows"], 2 * 2)
        r = self.store.query(["cpu.total_pct"], T0, T0 + 7200, raw_step=30,
                             retention={"raw": 0, "agg_5m": 0, "agg_1h": 10**9}, now=T0 + 7200)
        self.assertEqual(r["tier"], "1h")
        pts = r["series"]["cpu.total_pct"]
        self.assertEqual([p[1] for p in pts], [10.0, 30.0])

    def test_rollup_resumes_after_restart(self):
        self.fill(T0, T0 + 600)
        self.store.flush()
        self.store.aggregate(now=T0 + 600)
        self.store.close()
        # a new process: the watermark comes from the database
        self.store = Store(self.tmp.name, clock=lambda: self.now)
        self.fill(T0 + 600, T0 + 1200)
        self.store.flush()
        self.store.aggregate(now=T0 + 1200)
        self.assertEqual(self.store.stats()["tiers"]["5m"]["rows"], 2 * 4)

    def test_open_bucket_is_not_aggregated(self):
        self.fill(T0, T0 + 450)
        self.store.flush()
        self.store.aggregate(now=T0 + 450)  # the bucket at T0+300 is still open
        self.assertEqual(self.store.stats()["tiers"]["5m"]["rows"], 2)

    def test_clock_going_back_does_not_break_rollup(self):
        self.fill(T0, T0 + 900)
        self.store.flush()
        self.store.aggregate(now=T0 + 900)
        self.store.aggregate(now=T0 - 86400)  # clock set back a day
        self.fill(T0 + 900, T0 + 1500)
        self.store.flush()
        self.store.aggregate(now=T0 + 1500)
        self.assertEqual(self.store.stats()["tiers"]["5m"]["rows"], 2 * 5)

    def test_retention(self):
        self.fill(T0, T0 + 3 * 3600)
        self.store.flush()
        self.store.aggregate(now=T0 + 3 * 3600)
        cfg = dict(CFG, retention_raw_h=1)
        report = self.store.prune(retention_seconds(cfg), 10**9, now=T0 + 3 * 3600)
        self.assertGreater(report["deleted"], 0)
        self.assertFalse(report["trimmed"])
        oldest = self.store.stats()["tiers"]["raw"]["oldest"]
        self.assertGreaterEqual(oldest, T0 + 2 * 3600)
        # the averages are still there
        self.assertEqual(self.store.stats()["tiers"]["5m"]["oldest"], T0)

    def test_size_limit_trims_oldest(self):
        for t in range(T0, T0 + 86400, 30):
            self.store.add(t, {f"k{i}": float(i) for i in range(20)})
        self.store.flush()
        before = self.store.used_bytes()
        report = self.store.prune(retention_seconds(CFG), before // 2, now=T0 + 86400)
        self.assertTrue(report["trimmed"])
        self.assertLessEqual(self.store.used_bytes(), before // 2)
        self.assertGreater(self.store.stats()["tiers"]["raw"]["oldest"], T0)

    def test_query_downsamples(self):
        self.fill(T0, T0 + 3600)
        self.store.flush()
        r = self.store.query(["cpu.total_pct", "missing"], T0, T0 + 3600, raw_step=30,
                             retention=retention_seconds(CFG), max_points=10, now=T0 + 3600)
        self.assertEqual(r["tier"], "raw")
        self.assertEqual(r["step"], 360)
        self.assertEqual(len(r["series"]["cpu.total_pct"]), 10)
        self.assertEqual(r["series"]["missing"], [])
        for b, avg, lo, hi in r["series"]["cpu.total_pct"]:
            self.assertLessEqual(lo, avg)
            self.assertLessEqual(avg, hi)


class PickTierTest(unittest.TestCase):
    def test_finest_tier_holding_the_range(self):
        ret = retention_seconds(CFG)
        now = T0
        self.assertEqual(pick_tier(now - 3600, now, now, 30, ret), "raw")
        self.assertEqual(pick_tier(now - 7 * 86400, now, now, 30, ret), "agg_5m")
        self.assertEqual(pick_tier(now - 365 * 86400, now, now, 30, ret), "agg_1h")


if __name__ == "__main__":
    unittest.main()
