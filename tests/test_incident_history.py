"""Offline fixtures only: no validator, network or production database access."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('watcher_web', Path(__file__).resolve().parents[1] / 'web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def sample(ts, status='online'):
    return (ts, int(status == 'online'), status, 0 if status == 'degraded' else 5, 100, 98)


class HistoryTests(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(web.build_incidents([sample(100), sample(160)], 170), [])

    def test_recovery(self):
        result = web.build_incidents([sample(100, 'offline'), sample(160, 'offline'), sample(220)], 230)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['kind'], 'metrics_unavailable')
        self.assertEqual(result[0]['duration_seconds'], 120)
        self.assertEqual(result[0]['ending'], 'recovered')

    def test_stale_failure_does_not_extend_downtime(self):
        failure, gap = web.build_incidents([sample(100, 'offline'), sample(160, 'offline')], 1000)
        self.assertEqual(failure['duration_seconds'], 60)
        self.assertEqual(failure['ending'], 'unknown')
        self.assertEqual(failure['recovered'], '—')
        self.assertEqual(gap['kind'], 'observation_gap')
        self.assertTrue(gap['active'])

    def test_gap_between_failures_splits_incident(self):
        events = web.build_incidents([sample(100, 'offline'), sample(500, 'offline'), sample(560)], 570)
        self.assertEqual([e['ending'] for e in events], ['unknown', 'observations_resumed', 'recovered'])
        self.assertEqual(events[2]['duration_seconds'], 60)

    def test_healthy_after_gap_does_not_claim_failure_recovery(self):
        events = web.build_incidents([sample(100, 'offline'), sample(500)], 510)
        self.assertFalse(any(e['ending'] == 'recovered' for e in events))

    def test_symptoms_are_distinct(self):
        rows = [sample(100, 'offline'), sample(160, 'degraded'), (220, 0, 'degraded', None, None, None), sample(280)]
        events = web.build_incidents(rows, 290)
        self.assertEqual([e['kind'] for e in events], ['metrics_unavailable', 'no_peers', 'metrics_incomplete'])
        self.assertEqual(events[0]['ending'], 'symptom_changed')

    def test_stale_healthy_and_empty(self):
        self.assertEqual(web.build_incidents([], 500), [])
        events = web.build_incidents([sample(100)], 500)
        self.assertEqual(events[0]['kind'], 'observation_gap')
        self.assertEqual(web.build_incidents([sample(100)], 280), [])

    def test_read_only_database_and_old_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'samples.db')
            con = sqlite3.connect(path)
            con.execute('CREATE TABLE samples(ts INTEGER PRIMARY KEY, ok INTEGER, status TEXT, peers INTEGER, best INTEGER, finalized INTEGER)')
            con.execute('INSERT INTO samples VALUES(?,?,?,?,?,?)', sample(100))
            con.commit(); con.close()
            before = Path(path).read_bytes()
            with patch.object(web, 'DB', path), patch.object(web.time, 'time', return_value=3000000):
                events = web.incident_rows()
            self.assertEqual(events[0]['kind'], 'observation_gap')
            self.assertEqual(events[0]['duration_seconds'], 2592000)
            self.assertEqual(Path(path).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
