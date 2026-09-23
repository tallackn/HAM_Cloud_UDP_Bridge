"""Credential persistence, migration and failure-path tests (no real secrets)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ham_cloud_udp_bridge.keychain import KeychainError
from ham_cloud_udp_bridge.service import Bridge
from ham_cloud_udp_bridge.__main__ import default_directory
from tests.test_bridge import MemorySecrets


class KeychainIntegrationTests(unittest.TestCase):
    def test_legacy_migration_scrubs_settings_and_known_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            original = {'api_key':'legacy-cloud-key', 'clublog_password':'legacy-password',
                        'clublog_api_key':'legacy-club-key', 'udp_port':2237}
            (path/'config.json').write_text(json.dumps(original))
            backup = path/'backup-before-clublog-20260101'
            backup.mkdir()
            (backup/'config.json').write_text(json.dumps(original))
            secrets = MemorySecrets()
            bridge = Bridge(path, secrets)
            try:
                profile = bridge.config['keychain_id']
                self.assertEqual(secrets.read()['api_key'], original['api_key'])
                for file in (path/'config.json', backup/'config.json'):
                    content = file.read_text()
                    self.assertNotIn('legacy-', content)
                    self.assertNotIn('api_key', content)
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertNotIn('legacy-', json.dumps(bridge.snapshot()))
            finally:
                bridge.close()
            reopened = Bridge(path, secrets)
            try:
                self.assertEqual(reopened.config['api_key'], original['api_key'])
                self.assertEqual(reopened.config['keychain_id'], profile)
            finally:
                reopened.close()

    def test_keychain_failure_preserves_original_migration_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            original = '{"api_key": "keep-this-until-migration-succeeds"}'
            path.write_text(original)
            store = MemorySecrets()
            with patch.object(store, 'write', side_effect=KeychainError('Keychain locked')):
                with self.assertRaisesRegex(KeychainError, 'locked'):
                    Bridge(directory, store)
            self.assertEqual(path.read_text(), original)
            # Constructor failure released the exclusive lock.
            bridge = Bridge(directory, store)
            bridge.close()

    def test_save_and_clear_use_keychain_without_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemorySecrets()
            bridge = Bridge(directory, store)
            try:
                bridge.save_config({'api_key':'new-private-secret'})
                self.assertEqual(store.read(), {'api_key':'new-private-secret'})
                self.assertNotIn('new-private-secret', bridge.config_path.read_text())
                bridge.save_config({'api_key':''})
                self.assertTrue(bridge.snapshot()['config']['has_api_key'])
                bridge.save_config({'clear_api_key':True})
                self.assertEqual(store.read(), {})
            finally:
                bridge.close()

    def test_failed_keychain_save_does_not_change_public_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemorySecrets()
            bridge = Bridge(directory, store)
            try:
                original = bridge.config_path.read_text()
                with patch.object(store, 'write', side_effect=KeychainError('Denied')):
                    with self.assertRaises(KeychainError):
                        bridge.save_config({'api_key':'not-saved', 'udp_port':2238})
                self.assertEqual(bridge.config_path.read_text(), original)
                self.assertEqual(bridge.config['api_key'], '')
            finally:
                bridge.close()

    def test_failed_file_save_rolls_back_keychain(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemorySecrets()
            bridge = Bridge(directory, store)
            try:
                bridge.save_config({'api_key':'old-secret'})
                with patch.object(bridge, '_write_public_config', side_effect=OSError('Disk full')):
                    with self.assertRaises(OSError):
                        bridge.save_config({'api_key':'new-secret'})
                self.assertEqual(store.read(), {'api_key':'old-secret'})
                self.assertEqual(bridge.config['api_key'], 'old-secret')
            finally:
                bridge.close()

    def test_rename_migration_refuses_running_old_app(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            legacy = Path(directory)/'Library/Application Support/CloudLog UDP Bridge'
            legacy.mkdir(parents=True)
            with (legacy/'.instance.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'Stop the old'):
                    default_directory()
            new = default_directory()
            self.assertEqual(new.name, 'HAM Cloud UDP Bridge')
            self.assertTrue(new.exists())
            self.assertFalse(legacy.exists())
