"""HTTP contract tests using synthetic samples and a loopback-only test server."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('web_api', Path(__file__).resolve().parents[1] / 'web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / 'uptime.db'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE samples(ts INTEGER PRIMARY KEY, ok INTEGER, status TEXT, peers INTEGER, best INTEGER, finalized INTEGER, latency_ms INTEGER, error TEXT)')
        con.executemany('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)', [
            (900, 0, 'offline', None, None, None, 5, 'unreachable'),
            (960, 1, 'online', 12, 100, 98, 5, None)])
        con.commit(); con.close()
        self.events = root / 'events.json'
        self.events.write_text(json.dumps({'events': [{'started': 970, 'ended': 980, 'recovered': True, 'type': 'CPU load', 'peak_container_cpu_pct': 90}]}))
        (root / 'favicon.svg').write_text('<svg/>')
        self.patches = [patch.object(web, 'DB', str(self.db)), patch.object(web, 'STATIC_DIR', root),
                        patch.object(web.time, 'time', return_value=1000),
                        patch.dict(web.os.environ, {'ORBINUM_EVENTS_FILE': str(self.events)})]
        for p in self.patches: p.start()
        self.server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def get(self, path, method='GET'):
        return urllib.request.urlopen(urllib.request.Request(self.url + path, method=method))

    def test_status_preserves_stress_and_metrics_events_without_writes(self):
        before = self.db.read_bytes()
        with self.get('/api/status') as r:
            self.assertEqual(r.status, 200)
            data = json.load(r)
        self.assertEqual(data['ui_state'], 'online')
        self.assertEqual(data['peers'], 12)
        self.assertEqual(len(data['timeline']), 96)
        self.assertEqual(set(data['windows']), {'24h', '7d', '30d', 'all'})
        self.assertEqual([x['kind'] for x in data['incidents']], ['stress', 'metrics_unavailable'])
        self.assertEqual(data['incidents'][1]['ending'], 'recovered')
        self.assertTrue(data['incidents'][0]['details'])
        self.assertEqual(self.db.read_bytes(), before)

    def test_stale_status_and_unknown_recovery(self):
        with patch.object(web.time, 'time', return_value=2000):
            with self.get('/api/status') as r: data = json.load(r)
        self.assertEqual(data['ui_state'], 'stale')
        gap = next(x for x in data['incidents'] if x['kind'] == 'observation_gap')
        self.assertEqual(gap['recovered'], '—')
        self.assertTrue(gap['active'])

    def test_optional_events_can_be_missing_or_invalid(self):
        for contents in (None, '{bad json'):
            if contents is None: self.events.unlink()
            else: self.events.write_text(contents)
            with self.get('/api/status') as r: data = json.load(r)
            self.assertEqual(len(data['incidents']), 1)

    def test_health_head_and_page(self):
        with self.get('/health') as r: self.assertEqual(r.read(), b'ok\n')
        with self.get('/', 'HEAD') as r:
            self.assertGreater(int(r.headers['Content-Length']), 0)
            self.assertEqual(r.read(), b'')
        with self.get('/') as r:
            page = r.read().decode()
            self.assertIn('incidentBox', page)
            self.assertNotIn('__VALIDATOR__', page)
            self.assertIn('gaps in observations leave recovery unknown', page)

    def test_static_and_traversal(self):
        with self.get('/favicon.svg') as r: self.assertEqual(r.read(), b'<svg/>')
        for path in ('/static/../uptime.db', '/static/nested/file', '/missing'):
            with self.assertRaises(urllib.error.HTTPError) as exc: self.get(path)
            self.assertEqual(exc.exception.code, 404)
            exc.exception.close()

    def test_missing_database_does_not_create_one(self):
        missing = str(Path(self.tmp.name) / 'missing.db')
        with patch.object(web, 'DB', missing):
            with self.assertRaises(urllib.error.HTTPError) as exc: self.get('/api/status')
            self.assertEqual(exc.exception.code, 500)
            exc.exception.close()
        self.assertFalse(Path(missing).exists())


if __name__ == '__main__': unittest.main()
