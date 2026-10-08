import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "Pulse"))

from lib.instances import older_instances  # noqa: E402


def proc_entry(proc, pid, start, cmd, state="S"):
    d = os.path.join(proc, str(pid))
    os.makedirs(d)
    rest = [state] + ["0"] * 18 + [str(start)] + ["0"] * 10
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} (python3) " + " ".join(rest) + "\n")
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(cmd) + b"\0")


class InstancesTest(unittest.TestCase):
    def test_only_older_pulse_processes_of_this_user(self):
        with tempfile.TemporaryDirectory() as proc:
            main = [b"python3", b"bin/main.py"]
            proc_entry(proc, 100, 500, main)               # me
            proc_entry(proc, 90, 400, main)                # older Pulse: stop
            proc_entry(proc, 95, 400, main, state="Z")     # already gone (zombie)
            proc_entry(proc, 110, 600, main)               # newer: it will stop me, not the reverse
            proc_entry(proc, 80, 300, [b"python3", b"other.py"])  # not Pulse
            proc_entry(proc, 99, 500, main)                # same tick, lower pid: older
            uid = os.stat(os.path.join(proc, "100")).st_uid
            self.assertEqual(sorted(older_instances(proc, 100, uid)), [90, 99])
            self.assertEqual(older_instances(proc, 100, uid + 1), [])  # other users are never touched


if __name__ == "__main__":
    unittest.main()
