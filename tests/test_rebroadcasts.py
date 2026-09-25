"""Filtering tests use fixed UTC times and never contact live services."""
import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import patch

from ham_cloud_udp_bridge.protocol import parse_packet, to_adif
from tests.test_bridge import Bridge, PACKET
from tests.test_clublog import CONFIG

NOW = dt.datetime(2026, 9, 23, 10, 30, tzinfo=dt.timezone.utc).timestamp()


class RebroadcastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = Bridge(self.tmp.name)
        self.stop_workers()
        self.bridge.save_config({**CONFIG, 'ignore_rebroadcasts': True})
        self.clock = patch('ham_cloud_udp_bridge.service.time.time', return_value=NOW)
        self.clock.start()

    def stop_workers(self):
        self.bridge.closed.set()
        self.bridge.wake.set()
        self.bridge.worker.join()
        self.bridge.clublog_worker.join()

    def tearDown(self):
        self.clock.stop()
        self.bridge.close()
        self.tmp.cleanup()

    def receive(self, **changes):
        fields = parse_packet(PACKET)[0]
        fields.update(changes)
        self.bridge.receive(to_adif(fields).encode(), 'test')
        return self.bridge.snapshot()

    def test_fresh_contact_queues_both_destinations(self):
        state = self.receive()
        self.assertEqual(state['service_counts'], {'cloudlog': {'queued': 1}, 'clublog': {'queued': 1}})

    def test_known_repeat_and_enriched_contact_create_no_more_jobs(self):
        self.receive()
        for changes in ({}, {'NAME': 'Updated name', 'QTH': 'Updated location'}):
            state = self.receive(**changes)
            self.assertEqual(len(state['jobs']), 2)
            self.assertEqual(state['events'][0]['status'], 'ignored')
            self.assertIsNone(state['events'][0]['job_id'])
            self.assertIsNone(state['events'][0]['clublog_job_id'])
            self.assertIn('already recorded', state['events'][0]['detail'])

    def test_previously_unseen_old_contact_is_ignored_for_both(self):
        state = self.receive(QSO_DATE='20260920')
        self.assertEqual(state['jobs'], [])
        self.assertEqual(state['events'][0]['status'], 'ignored')
        self.assertIn('15 minutes', state['events'][0]['detail'])
        self.bridge.save_config({'ignore_rebroadcasts': False})
        self.assertEqual(self.bridge.snapshot()['jobs'], [])  # No automatic release.
        self.assertEqual(len(self.receive(QSO_DATE='20260920')['jobs']), 2)

    def test_age_limit_boundary_and_custom_limit(self):
        self.assertEqual(self.receive(TIME_ON='101459')['events'][0]['status'], 'ignored')
        self.assertEqual(self.receive(TIME_ON='101500')['events'][0]['status'], 'queued')
        self.bridge.save_config({'rebroadcast_minutes': '30'})
        self.assertEqual(self.receive(TIME_ON='100000')['events'][0]['status'], 'queued')

    def test_long_contact_uses_end_time_instead_of_start_time(self):
        state = self.receive(TIME_ON='080000', TIME_OFF='1029', QSO_DATE_OFF='20260923')
        self.assertEqual(state['events'][0]['status'], 'queued')

    def test_midnight_end_time_without_date(self):
        midnight = dt.datetime(2026, 9, 24, 0, 5, tzinfo=dt.timezone.utc).timestamp()
        with patch('ham_cloud_udp_bridge.service.time.time', return_value=midnight):
            state = self.receive(TIME_ON='230000', TIME_OFF='000100')
        self.assertEqual(state['events'][0]['status'], 'queued')

    def test_explicit_end_date_and_invalid_end_times(self):
        for changes in ({'TIME_OFF': 'nope'}, {'TIME_OFF': '102900', 'QSO_DATE_OFF': '20261399'},
                        {'TIME_OFF': '080000', 'QSO_DATE_OFF': '20260923'}):
            with self.subTest(changes=changes):
                state = self.receive(**changes)
                self.assertEqual(state['events'][0]['status'], 'invalid')
                self.assertEqual(state['jobs'], [])
        state = self.receive(QSO_DATE='20260922', TIME_ON='235000',
                             TIME_OFF='102900', QSO_DATE_OFF='20260923')
        self.assertEqual(state['events'][0]['status'], 'queued')

    def test_capture_mode_remains_capture_only(self):
        self.bridge.save_config({'uploads_enabled': False})
        state = self.receive(QSO_DATE='20260920')
        self.assertEqual(state['events'][0]['status'], 'captured')
        self.assertEqual(state['jobs'], [])

    def test_disabled_filter_keeps_existing_review_behaviour(self):
        self.bridge.save_config({'ignore_rebroadcasts': False})
        self.receive(QSO_DATE='20260920')
        state = self.receive(QSO_DATE='20260920', NAME='Updated name')
        self.assertEqual(state['events'][0]['status'], 'review')
        self.assertEqual(len(state['jobs']), 2)

    def test_settings_validation_and_persistence(self):
        for config in ({'ignore_rebroadcasts': 'true'}, {'rebroadcast_minutes': True},
                       {'rebroadcast_minutes': 1.5}, {'rebroadcast_minutes': 0},
                       {'rebroadcast_minutes': 1441}, {'rebroadcast_minutes': 'x'}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.bridge.save_config(config)
        self.receive()
        self.bridge.save_config({'uploads_enabled': False, 'rebroadcast_minutes': 25})
        self.bridge.close()
        self.bridge = Bridge(self.tmp.name)
        self.stop_workers()
        self.assertTrue(self.bridge.config['ignore_rebroadcasts'])
        self.assertEqual(self.bridge.config['rebroadcast_minutes'], 25)
        self.bridge.save_config({'uploads_enabled': True})
        self.assertEqual(self.receive()['events'][0]['status'], 'ignored')
        saved = json.loads(self.bridge.config_path.read_text())
        self.assertTrue(saved['ignore_rebroadcasts'])
        self.assertNotIn('api_key', saved)

    def test_clublog_off_stops_new_jobs_and_existing_uploads(self):
        self.receive()
        self.bridge.save_config({'clublog_enabled': False})
        state = self.receive(CALL='ZL1NEXT')
        self.assertEqual(state['service_counts']['clublog'], {'queued': 1})
        self.assertEqual(state['service_counts']['cloudlog'], {'queued': 2})
        with patch('ham_cloud_udp_bridge.service.upload_clublog') as send:
            self.assertFalse(self.bridge.process_one('clublog'))
            send.assert_not_called()
        self.bridge.save_config({'clublog_enabled': True})
        self.assertEqual(self.bridge.snapshot()['service_counts']['clublog'], {'queued': 1})
        # Re-enabling Club Log must not make an enriched known contact a new upload.
        self.assertEqual(self.receive(CALL='ZL1NEXT', NAME='Updated')['events'][0]['status'], 'ignored')
        self.assertEqual(self.bridge.snapshot()['service_counts']['clublog'], {'queued': 1})
