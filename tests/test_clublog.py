import contextlib
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse

from ham_cloud_udp_bridge.clublog import explain_error, upload_clublog
from ham_cloud_udp_bridge.service import UploadError
from tests.test_bridge import Bridge, PACKET, fake_cloudlog, until

CONFIG = {'cloudlog_url': 'http://127.0.0.1:1', 'api_key': 'cloud-secret', 'station_id': '7', 'uploads_enabled': True,
          'clublog_enabled': True, 'clublog_email': 'radio@example.test', 'clublog_callsign': 'N0CALL',
          'clublog_password': 'app&secret+pass', 'clublog_api_key': 'developer-secret'}


@contextlib.contextmanager
def fake_clublog(status=200, reason='QSO OK', body='OK', redirect=False):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            if self.headers['Content-Type'].startswith('multipart'):
                mail = BytesParser(policy=default).parsebytes(('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + data)
                fields = {part.get_param('name', header='Content-Disposition'): part.get_payload(decode=True).decode() for part in mail.iter_parts()}
            else:
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()}
            requests.append((self.path, fields))
            self.send_response(302 if redirect else status, reason)
            if redirect:
                self.send_header('Location', '/must-not-follow')
            self.end_headers()
            self.wfile.write(body.encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    with patch('ham_cloud_udp_bridge.clublog.URL', f'http://127.0.0.1:{server.server_port}'):
        try:
            yield requests
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class ClubLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = Bridge(self.tmp.name)
        # Drive the workers deterministically without contacting real services.
        self.bridge.closed.set()
        self.bridge.wake.set()
        self.bridge.worker.join()
        self.bridge.clublog_worker.join()
        self.bridge.save_config(CONFIG)

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def receive(self, packet=PACKET):
        self.bridge.receive(packet, 'local-test')

    def allow_next(self):
        self.bridge.db.execute("DELETE FROM service_state WHERE name='clublog_next'")
        self.bridge.db.execute('UPDATE jobs SET next_try=0')
        self.bridge.db.commit()

    def test_dual_records_duplicate_and_independent_retry(self):
        self.receive()
        with patch('ham_cloud_udp_bridge.service.upload', return_value=('uploaded','CloudLog receipt')) as cloud, fake_clublog(status=503, body='Maintenance') as club:
            self.assertTrue(self.bridge.process_one())
            self.assertTrue(self.bridge.process_one('clublog'))
            self.receive()
            self.assertEqual(len(self.bridge.snapshot()['jobs']), 2)
            self.assertEqual(self.bridge.snapshot()['service_counts']['cloudlog'], {'uploaded': 1})
            self.assertEqual(self.bridge.snapshot()['service_counts']['clublog'], {'retry': 1})
            self.assertEqual(cloud.call_count, 1)
            self.assertEqual(len(club), 1)
        self.allow_next()
        with fake_clublog() as calls:
            self.bridge.process_one('clublog')
            self.assertEqual(calls[0][0], '/realtime.php')
            self.assertEqual(calls[0][1]['password'], CONFIG['clublog_password'])
            self.assertEqual(calls[0][1]['api'], CONFIG['clublog_api_key'])
            self.assertEqual(calls[0][1]['callsign'], 'N0CALL')
            self.assertNotIn('key', calls[0][1])
            self.assertEqual(self.bridge.snapshot()['counts']['uploaded'], 2)
        state = self.bridge.snapshot()
        self.assertEqual(state['events'][0]['clublog_status'], 'uploaded')
        for secret in ('api_key', 'clublog_password', 'clublog_api_key'):
            self.assertNotIn(CONFIG[secret], json.dumps(state))
            self.assertNotIn(secret, state['config'])

    def test_disabled_destinations_do_not_backfill(self):
        self.bridge.save_config({'uploads_enabled': False})
        self.receive()
        self.bridge.save_config({'uploads_enabled': True, 'clublog_enabled': False})
        self.receive()
        self.bridge.save_config({'clublog_enabled': True})
        self.assertEqual(len(self.bridge.snapshot()['jobs']), 1)
        self.assertFalse(self.bridge.process_one('clublog'))
        self.receive()  # Explicit rebroadcast can queue the missing destination only.
        self.assertEqual(len(self.bridge.snapshot()['jobs']), 2)

    def test_403_blocks_all_clublog_until_credentials_change(self):
        self.receive()
        body = 'Bad ' + CONFIG['clublog_password'] + ' ' + urllib.parse.quote_plus(CONFIG['clublog_api_key'])
        with fake_clublog(status=403, body=body) as calls:
            self.bridge.process_one('clublog')
            self.allow_next()
            self.receive(PACKET.replace(b'ZL1TEST', b'ZL1NEXT'))
            self.assertFalse(self.bridge.process_one('clublog'))
            self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ValueError, 'credentials'):
            self.bridge.resume_clublog()
        self.bridge.save_config({'clublog_password': ''})
        self.assertTrue(self.bridge.snapshot()['clublog_block'])
        self.bridge.close()
        self.bridge = Bridge(self.tmp.name)
        self.bridge.closed.set(); self.bridge.wake.set()
        self.bridge.worker.join(); self.bridge.clublog_worker.join()
        self.assertTrue(self.bridge.snapshot()['clublog_block'])
        self.bridge.save_config({'clublog_password': 'corrected-password'})
        self.assertIsNone(self.bridge.snapshot()['clublog_block'])
        self.assertNotIn(CONFIG['clublog_password'], json.dumps(self.bridge.snapshot()))

    def test_backlog_uses_merge_batch_and_reports_acceptance(self):
        self.receive()
        self.receive(PACKET.replace(b'ZL1TEST', b'ZL1NEXT'))
        with fake_clublog(reason='OK', body='Upload accepted') as calls:
            self.bridge.process_one('clublog')
            self.assertEqual(len(calls), 1)
            path, fields = calls[0]
            self.assertEqual(path, '/putlogs.php')
            self.assertNotIn('clear', fields)
            self.assertEqual(fields['file'].count('<EOR>'), 2)
            self.assertEqual(self.bridge.snapshot()['service_counts']['clublog'], {'accepted': 2})
            self.receive(PACKET.replace(b'ZL1TEST', b'ZL1THIRD'))
            self.assertFalse(self.bridge.process_one('clublog'))  # Throttled.

    def test_batch_failure_pauses_until_explicit_resume(self):
        self.receive()
        self.bridge.db.execute("UPDATE jobs SET created=created-600 WHERE service='clublog'")
        with fake_clublog(status=500, body='Maintenance'):
            self.bridge.process_one('clublog')
        self.assertFalse(self.bridge.snapshot()['clublog_block']['auth'])
        self.bridge.resume_clublog()
        self.assertIsNone(self.bridge.snapshot()['clublog_block'])

    def test_destination_and_callsign_safety(self):
        self.receive()
        self.bridge.save_config({'clublog_email': 'other@example.test'})
        self.assertEqual(self.bridge.snapshot()['held'], 1)
        self.assertFalse(self.bridge.process_one('clublog'))
        self.bridge.save_config({'clublog_email': CONFIG['clublog_email'], 'clublog_callsign': 'ZL2WRONG'})
        self.receive()
        job = self.bridge.snapshot()['jobs'][0]
        self.assertEqual(job['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'callsign'):
            self.bridge.retry(job['id'])
        self.assertFalse(self.bridge.process_one('clublog'))

    def test_secret_retention_validation_and_clear(self):
        self.bridge.save_config({'clublog_password':'', 'clublog_api_key':''})
        self.assertEqual(self.bridge.config['clublog_password'], CONFIG['clublog_password'])
        with self.assertRaisesRegex(ValueError, 'email'):
            self.bridge.save_config({'clublog_email': 'N0CALL'})
        with self.assertRaisesRegex(ValueError, 'application password'):
            self.bridge.save_config({'clear_clublog_password': True})
        self.bridge.save_config({'clublog_enabled': False, 'clear_clublog_password': True, 'clear_clublog_api_key': True})
        self.assertFalse(self.bridge.snapshot()['config']['has_clublog_password'])
        self.assertFalse(self.bridge.snapshot()['config']['has_clublog_api_key'])

    def test_generic_403_explained_without_claiming_wrong_password(self):
        html = '<html><head><title>403 Forbidden</title></head><body>nginx</body></html>'
        with fake_clublog(status=403, body=html):
            with self.assertRaises(UploadError) as error:
                upload_clublog(CONFIG, [{'station': 'N0CALL', 'adif': '<EOR>'}])
        self.assertTrue(error.exception.pause)
        self.assertIn('without an API explanation', str(error.exception))
        self.assertNotIn('<html>', str(error.exception))
        self.assertEqual(explain_error('Club Log HTTP 403: Bad API key'), 'Club Log HTTP 403: Bad API key')
        self.assertTrue(self.bridge.snapshot()['config']['clublog_key_format_warning'])
        self.bridge.save_config({'clublog_api_key': 'a' * 40})
        self.assertFalse(self.bridge.snapshot()['config']['clublog_key_format_warning'])

    def test_real_time_response_variants_and_redirect(self):
        job = {'station':'N0CALL', 'adif':'<EOR>'}
        for reason, body, expected in [('QSO OK', '', 'uploaded'), ('QSO Duplicate', 'Already known', 'duplicate'), ('QSO Modified', 'Adjusted DXCC', 'uploaded'), ('OK', 'OK', 'uploaded')]:
            with self.subTest(reason=reason), fake_clublog(reason=reason, body=body):
                self.assertEqual(upload_clublog(CONFIG, [job])[0], expected)
        with fake_clublog(reason='OK', body='<html>Login</html>'):
            with self.assertRaisesRegex(UploadError, 'Unrecognised'):
                upload_clublog(CONFIG, [job])
        with fake_clublog(redirect=True) as calls:
            with self.assertRaises(UploadError):
                upload_clublog(CONFIG, [job])
            self.assertEqual(len(calls), 1)


class IntegrationTests(unittest.TestCase):
    def test_udp_to_both_services_with_slow_cloudlog(self):
        with tempfile.TemporaryDirectory() as directory, fake_cloudlog() as (url, cloud), fake_clublog() as club:
            bridge = Bridge(directory)
            release = threading.Event()
            from ham_cloud_udp_bridge.service import upload
            def slow_upload(config, job):
                release.wait(3)
                return upload(config, job)
            try:
                bridge.save_config({**CONFIG, 'cloudlog_url': url})
                bridge.config['udp_port'] = 0
                with patch('ham_cloud_udp_bridge.service.upload', side_effect=slow_upload):
                    bridge.start_listener()
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                        sender.sendto(PACKET, bridge.sock.getsockname())
                    until(lambda: bridge.snapshot()['service_counts']['clublog'].get('uploaded') == 1)
                    self.assertEqual(len(cloud), 0)
                    release.set()
                    until(lambda: bridge.snapshot()['counts'].get('uploaded') == 2)
                    self.assertEqual(len(club), 1)
                    self.assertEqual(len(cloud), 1)
            finally:
                release.set()
                bridge.close()

    def test_existing_database_migration_and_identity_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = {k: CONFIG[k] for k in ('api_key', 'station_id')}
            (path/'config.json').write_text(json.dumps(config))
            db = sqlite3.connect(path/'bridge.sqlite3')
            db.executescript('''CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, identity TEXT UNIQUE NOT NULL,
                call TEXT NOT NULL, adif TEXT NOT NULL, target TEXT NOT NULL, station TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0);
                CREATE TABLE events (id INTEGER PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
                raw TEXT NOT NULL, protocol TEXT NOT NULL, call TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, job_id INTEGER);''')
            db.execute("INSERT INTO jobs VALUES(1,0,'unchanged','ZL1TEST','<EOR>','https://example.test','7','uploaded','Done',1,0)")
            db.commit(); db.close()
            bridge = Bridge(directory)
            try:
                state = bridge.snapshot()
                self.assertFalse(state['config']['clublog_enabled'])
                self.assertEqual(state['jobs'][0]['identity'], 'unchanged')
                self.assertEqual(state['jobs'][0]['service'], 'cloudlog')
                self.assertEqual(state['jobs'][0]['status'], 'uploaded')
                self.assertTrue(state['config']['has_api_key'])
            finally:
                bridge.close()
