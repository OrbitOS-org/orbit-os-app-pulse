import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "Pulse"))

from lib import instances  # noqa: E402
from lib.instances import InstanceLock  # noqa: E402


@unittest.skipIf(instances.fcntl is None, "file locks are tested on Linux (the device)")
class InstanceLockTest(unittest.TestCase):
    def test_second_instance_waits_until_the_first_releases(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = InstanceLock(d), InstanceLock(d)
            stop = threading.Event()
            self.assertTrue(first.acquire(stop))
            waited = []
            got = []
            t = threading.Thread(target=lambda: got.append(second.acquire(stop, lambda: waited.append(1), poll_s=0.05)))
            t.start()
            t.join(0.3)
            self.assertEqual((waited, got), ([1], []))
            first.release()
            t.join(2)
            self.assertEqual(got, [True])
            second.release()

    def test_stop_while_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = InstanceLock(d), InstanceLock(d)
            stop = threading.Event()
            first.acquire(stop)
            stop.set()
            self.assertFalse(second.acquire(stop))
            first.release()


class NoLockOnWindowsTest(unittest.TestCase):
    def test_acquire_without_fcntl(self):
        with tempfile.TemporaryDirectory() as d:
            saved, instances.fcntl = instances.fcntl, None
            try:
                self.assertTrue(InstanceLock(d).acquire(threading.Event()))
            finally:
                instances.fcntl = saved


if __name__ == "__main__":
    unittest.main()
