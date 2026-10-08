import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "Pulse"))

from lib.config import Config, ConfigError, defaults, validate  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(validate({"interval_s": "60", "retention_raw_h": 24.0}), {"interval_s": 60, "retention_raw_h": 24})
        for bad in ({"interval_s": 5}, {"interval_s": 30.5}, {"interval_s": True}, {"interval_s": "x"}, {"max_db_mb": None}):
            with self.assertRaises(ConfigError):
                validate(bad)
        self.assertEqual(validate({"unknown": 1}), {})

    def test_file_created_updated_and_repaired(self):
        with tempfile.TemporaryDirectory() as d:
            c = Config(d)
            self.assertEqual(c.as_dict(), defaults())
            c.update({"interval_s": 60})
            self.assertEqual(Config(d).get("interval_s"), 60)
            path = os.path.join(d, "config.json")
            with open(path, "w") as f:
                json.dump({"interval_s": 9999, "max_db_mb": 50}, f)
            c = Config(d)
            self.assertEqual(c.get("interval_s"), defaults()["interval_s"])  # invalid → default
            self.assertEqual(c.get("max_db_mb"), 50)
            with open(path, "w") as f:
                f.write("{broken")
            self.assertEqual(Config(d).as_dict(), defaults())
            self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
