"""Offline tests for collector freshness, without validator or Telegram."""
import os
import sqlite3
import tempfile
import unittest

from collector_health import collector_health


class CollectorHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, "uptime.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, ok INTEGER)")
        con.commit()
        con.close()

    def sample(self, ts):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO samples VALUES (?, 1)", (ts,))
        con.commit()
        con.close()

    def test_fresh(self):
        self.sample(1000)
        code, payload = collector_health(self.db, now=1180)
        self.assertEqual((code, payload["status"]), (200, "MONITOR_OK"))

    def test_stale_not_validator_offline(self):
        self.sample(1000)
        code, payload = collector_health(self.db, now=1181)
        self.assertEqual((code, payload["status"]), (503, "MONITOR_STALE"))
        self.assertEqual(payload["sample_age_seconds"], 181)

    def test_empty(self):
        code, payload = collector_health(self.db, now=1181)
        self.assertEqual((code, payload["status"]), (503, "MONITOR_NO_DATA"))

    def test_missing_database_does_not_create_it(self):
        path = os.path.join(self.temp.name, "missing.db")
        code, payload = collector_health(path, now=1181)
        self.assertEqual((code, payload["status"]), (503, "MONITOR_UNAVAILABLE"))
        self.assertFalse(os.path.exists(path))

    def test_future_sample(self):
        self.sample(1200)
        code, payload = collector_health(self.db, now=1181)
        self.assertEqual((code, payload["status"]), (503, "MONITOR_CLOCK_SKEW"))


if __name__ == "__main__":
    unittest.main()
