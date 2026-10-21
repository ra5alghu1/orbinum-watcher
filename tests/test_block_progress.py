import contextlib
import io
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import monitor


class BlockProgressTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.execute('CREATE TABLE samples (ts INTEGER, status TEXT, peers INTEGER, best INTEGER, finalized INTEGER)')
        self.addCleanup(self.con.close)

    def sample(self, ts, best=100, finalized=98, status='online', peers=10):
        self.con.execute('INSERT INTO samples VALUES (?,?,?,?,?)', (ts, status, peers, best, finalized))

    def result(self, ts=300, best=100, finalized=98):
        with patch.object(monitor, 'STALL_SECONDS', 300), patch.object(monitor, 'PROGRESS_MAX_GAP', 180):
            return monitor.stalled_blocks(self.con, ts, best, finalized)

    def test_threshold_and_recovery_independent(self):
        for ts in range(0, 300, 60): self.sample(ts)
        self.assertEqual(self.result(299), [])
        self.assertEqual(self.result(), ['best', 'finalized'])
        self.assertEqual(self.result(best=101), ['finalized'])
        self.assertEqual(self.result(finalized=99), ['best'])
        self.assertEqual(self.result(best=101, finalized=99), [])

    def test_missing_observations_reset_evidence(self):
        self.sample(0)
        self.sample(60)
        self.assertEqual(self.result(), [])

    def test_failed_or_incomplete_sample_resets_evidence(self):
        for changes in ({'status': 'offline'}, {'finalized': None}, {'peers': 0}):
            with self.subTest(changes=changes):
                self.con.execute('DELETE FROM samples')
                self.sample(0)
                self.sample(60, **changes)
                for ts in (120, 180, 240): self.sample(ts)
                self.assertEqual(self.result(), [])

    def test_short_gap_and_existing_degradation(self):
        for ts in (0, 60, 180, 240): self.sample(ts, status='degraded')
        self.assertEqual(self.result(), ['best', 'finalized'])

    def test_counter_reset_and_first_sample(self):
        self.assertEqual(self.result(), [])
        for ts in range(0, 300, 60): self.sample(ts)
        self.assertEqual(self.result(best=1, finalized=0), [])

    def test_check_persists_stall_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, 'DB', directory + '/samples.db'), patch.object(monitor, 'STALL_SECONDS', 120):
            def metrics(best, finalized):
                response = io.BytesIO((f'substrate_block_height{{status="best"}} {best}\nsubstrate_block_height{{status="finalized"}} {finalized}\nsubstrate_sub_libp2p_peers_count 10\n').encode())
                return response
            for ts, best, finalized, expected in ((0,100,98,'online'), (60,100,98,'online'), (120,100,98,'degraded'), (180,101,99,'online')):
                with patch.object(monitor.time, 'time', return_value=ts), patch.object(monitor.urllib.request, 'urlopen', return_value=metrics(best, finalized)), contextlib.redirect_stdout(io.StringIO()):
                    monitor.check()
                con = monitor.connect()
                row = con.execute('SELECT status,error FROM samples WHERE ts=?', (ts,)).fetchone()
                con.close()
                self.assertEqual(row[0], expected)
                if expected == 'degraded': self.assertIn('best, finalized', row[1])
