import contextlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ham_cloud_udp_bridge.protocol import IgnoredPacket, parse_packet, to_adif
from ham_cloud_udp_bridge.service import Bridge as RealBridge, UploadError, normalise_url, request_json, tls_context, upload
from ham_cloud_udp_bridge.__main__ import make_server


class MemorySecrets:
    def __init__(self):
        self.values = {}
    def read(self):
        return dict(self.values)
    def write(self, values):
        self.values = dict(values)


_STORES = {}
def Bridge(directory):
    return RealBridge(directory, secret_store=_STORES.setdefault(str(directory), MemorySecrets()))


PACKET = b'''<?xml version="1.0" encoding="utf-8"?>
<contactinfo><app>SDR-Control</app><timestamp>2026-09-23 10:23:45</timestamp>
<call>ZL1TEST</call><mycall>N0CALL</mycall><txfreq>1425000</txfreq>
<rxfreq>1425000</rxfreq><mode>USB</mode><snt>59</snt><rcv>57</rcv>
<gridsquare>RF73</gridsquare><name>Test &amp; Example</name><comment>Local test only</comment>
<power>50</power><ID>test-guid</ID></contactinfo>'''


def wrapped_packet():
    payload = to_adif(parse_packet(PACKET)[0]).encode()
    return b"<command:3>Log<parameters:" + str(len(payload)).encode() + b">" + payload


def until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError("Timed out waiting for bridge state")


@contextlib.contextmanager
def fake_cloudlog(status=201, response=None, redirect=False):
    requests = []
    response = {"status": "created", "imported_count": 1, "messages": []} if response is None else response

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            payload = ({"status": "valid", "rights": "rw"} if "check_auth" in self.path else
                       [{"station_id": "7", "station_profile_name": "Home", "station_callsign": "N0CALL"}])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(302 if redirect else status)
            if redirect:
                self.send_header("Location", "/secret-must-not-follow")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ProtocolTests(unittest.TestCase):
    def test_sdr_control_command_envelope(self):
        expected = parse_packet(PACKET)[0]
        fields, protocol, _ = parse_packet(wrapped_packet())
        self.assertEqual(fields, expected)
        self.assertEqual(protocol, "SDR-Control ADIF")
        self.assertNotIn("COMMAND", fields)
        self.assertNotIn("PARAMETERS", fields)
        upper_band = wrapped_packet().replace(b">20m<", b">20M<")
        self.assertEqual(parse_packet(upper_band)[0]["BAND"], "20m")

    def test_command_envelope_rejects_truncation_and_extra_data(self):
        packet = wrapped_packet()
        for invalid in (packet[:-5], packet + b"EXTRA", packet.replace(b"<parameters:", b"<other:")):
            with self.subTest(packet=invalid[:40]), self.assertRaises(ValueError):
                parse_packet(invalid)
        with self.assertRaises(IgnoredPacket):
            parse_packet(packet.replace(b"<command:3>Log", b"<command:6>Delete"))

    def test_documented_n1mm_frequency_and_reports(self):
        fields, protocol, _ = parse_packet(PACKET)
        self.assertEqual(protocol, "N1MM")
        self.assertEqual(fields["FREQ"], "14.25")
        self.assertEqual(fields["BAND"], "20m")
        self.assertEqual(fields["MODE"], "SSB")
        self.assertEqual(fields["SUBMODE"], "USB")
        self.assertEqual(fields["RST_RCVD"], "57")
        self.assertEqual(fields["RX_PWR"], "50")
        self.assertNotIn("TX_PWR", fields)
        self.assertEqual(fields["TIME_ON"], "102345")
        self.assertEqual(fields["NAME"], "Test & Example")

    def test_split_frequency(self):
        fields, _, _ = parse_packet(PACKET.replace(b"<rxfreq>1425000", b"<rxfreq>14590000"))
        self.assertEqual(fields["FREQ_RX"], "145.9")
        self.assertEqual(fields["BAND_RX"], "2m")

    def test_ft4_and_utc_conversion(self):
        fields, _, _ = parse_packet(PACKET.replace(b">USB<", b">FT4<").replace(b"2026-09-23 10:23:45", b"2026-09-24T01:23:45+12:00"))
        self.assertEqual((fields["MODE"], fields["SUBMODE"]), ("MFSK", "FT4"))
        self.assertEqual(fields["TIME_ON"], "132345")
        self.assertEqual(fields["QSO_DATE"], "20260923")

    def test_adif_roundtrip_preserves_fields_and_embedded_tags(self):
        fields, _, _ = parse_packet(PACKET)
        fields["COMMENT"] = "Contains <CALL:4>FAKE and café"
        actual, protocol, _ = parse_packet(to_adif(fields).encode())
        self.assertEqual(actual, fields)
        self.assertEqual(protocol, "ADIF")

    def test_header_and_adif_time_padding(self):
        packet = b"Exported by a logger\n<ADIF_VER:5>3.1.4<EOH><CALL:6>ZL1ABC<QSO_DATE:8>20260923<TIME_ON:4>1230<MODE:2>CW<BAND:3>20m<EOR>"
        fields, _, _ = parse_packet(packet)
        self.assertEqual(fields["TIME_ON"], "123000")
        self.assertNotIn("ADIF_VER", fields)

    def test_non_qso_messages_never_imported(self):
        for root in ("RadioInfo", "lookupinfo", "contactreplace", "contactdelete"):
            with self.subTest(root=root), self.assertRaises(IgnoredPacket):
                parse_packet(PACKET.replace(b"contactinfo", root.encode()))

    def test_invalid_data_rejected(self):
        variants = [b"not xml", b"\xad\xbc\xcb\xda\x00", PACKET.replace(b"<call>ZL1TEST</call>", b""),
                    PACKET.replace(b"2026-09-23", b"2026-99-99"), PACKET.replace(b"1425000", b"NaN"),
                    b'<!DOCTYPE x [<!ENTITY y "x">]><contactinfo/>',
                    b"<CALL:100>SHORT<EOR>"]
        for packet in variants:
            with self.subTest(packet=packet[:25]), self.assertRaises(ValueError):
                parse_packet(packet)

    def test_one_qso_per_adif_datagram(self):
        adif = to_adif(parse_packet(PACKET)[0]).encode()
        with self.assertRaisesRegex(ValueError, "one QSO"):
            parse_packet(adif + adif)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = Bridge(self.tmp.name)

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def configure(self, url):
        self.bridge.save_config({"cloudlog_url": url, "station_id": "7", "api_key": "test-secret", "uploads_enabled": True})

    def test_capture_mode_never_creates_job(self):
        self.bridge.receive(PACKET, "127.0.0.1:50000")
        state = self.bridge.snapshot()
        self.assertEqual(state["events"][0]["status"], "captured")
        self.assertEqual(state["jobs"], [])
        with fake_cloudlog() as (url, requests):
            self.configure(url)
            time.sleep(0.1)
            self.assertEqual(requests, [])

    def test_wrapped_test_packet_stays_out_of_outbox(self):
        self.bridge.receive(wrapped_packet(), "test")
        self.assertEqual(self.bridge.snapshot()["events"][0]["status"], "captured")
        with fake_cloudlog() as (url, requests):
            self.configure(url)
            self.assertEqual(self.bridge.snapshot()["jobs"], [])
            self.assertEqual(requests, [])
            # Only a newly received broadcast is queued after enabling uploads.
            self.bridge.receive(wrapped_packet(), "test")
            until(lambda: self.bridge.snapshot()["counts"].get("uploaded") == 1)
            self.assertEqual(len(requests), 1)
            self.assertNotIn("<COMMAND", requests[0][1]["string"])

    def test_udp_to_http_end_to_end_and_duplicate(self):
        with fake_cloudlog() as (url, requests), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            self.configure(url)
            # Let the OS choose an available port for this isolated integration test.
            self.bridge.config["udp_port"] = 0
            self.bridge.start_listener()
            address = self.bridge.sock.getsockname()
            sender.sendto(PACKET, address)
            until(lambda: self.bridge.snapshot()["counts"].get("uploaded") == 1)
            sender.sendto(PACKET, address)
            until(lambda: len(self.bridge.snapshot()["events"]) == 2)
            self.assertEqual(len(requests), 1)
            path, body = requests[0]
            self.assertEqual(path, "/index.php/api/qso")
            self.assertEqual(body["station_profile_id"], "7")
            self.assertEqual(body["key"], "test-secret")
            self.assertIn("<FREQ:5>14.25", body["string"])
            self.assertNotIn("test-secret", json.dumps(self.bridge.snapshot()))
            self.bridge.stop_listener()
            self.assertFalse(self.bridge.snapshot()["listening"])

    def test_port_conflict_reported(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.bind(("127.0.0.1", 0))
            self.bridge.save_config({"udp_port": other.getsockname()[1]})
            with self.assertRaisesRegex(ValueError, "Cannot bind"):
                self.bridge.start_listener()
            self.assertFalse(self.bridge.snapshot()["listening"])

    def test_listener_settings_locked_while_running(self):
        self.bridge.config["udp_port"] = 0
        self.bridge.start_listener()
        with self.assertRaisesRegex(ValueError, "Stop the listener"):
            self.bridge.save_config({"udp_port": 2238})

    def test_retry_then_success_and_restart_persistence(self):
        with fake_cloudlog(status=503) as (url, _):
            self.configure(url)
            self.bridge.receive(PACKET, "test")
            until(lambda: self.bridge.snapshot()["counts"].get("retry") == 1)
            job = self.bridge.snapshot()["jobs"][0]
            self.assertEqual(job["attempts"], 1)
            self.bridge.save_config({"uploads_enabled": False})
            self.bridge.close()
            self.bridge = Bridge(self.tmp.name)
            self.assertEqual(self.bridge.snapshot()["jobs"][0]["status"], "retry")
            with patch("ham_cloud_udp_bridge.service.upload", return_value=("uploaded", "Confirmed")):
                self.bridge.retry(job["id"])
                self.bridge.save_config({"uploads_enabled": True})
                until(lambda: self.bridge.snapshot()["counts"].get("uploaded") == 1)

    def test_permanent_failure_and_changed_contact_review(self):
        with fake_cloudlog(status=401, response={"status":"failed","reason":"Bad test-secret"}) as (url, requests):
            self.configure(url)
            self.bridge.receive(PACKET, "test")
            until(lambda: self.bridge.snapshot()["counts"].get("failed") == 1)
            self.bridge.receive(PACKET.replace(b"Local test only", b"Changed note"), "test")
            state = self.bridge.snapshot()
            self.assertEqual(len(requests), 1)
            self.assertEqual(state["events"][0]["status"], "review")
            self.assertNotIn("test-secret", json.dumps(state))

    def test_destination_changes_do_not_redirect_queued_contacts(self):
        with fake_cloudlog(status=503) as (url, _):
            self.configure(url)
            self.bridge.receive(PACKET, "test")
            until(lambda: self.bridge.snapshot()["counts"].get("retry") == 1)
            self.bridge.save_config({"station_id": "8"})
            self.assertEqual(self.bridge.snapshot()["held"], 1)
            with self.assertRaisesRegex(ValueError, "original URL"):
                self.bridge.retry(1)

    def test_key_retention_clear_and_private_permissions(self):
        self.bridge.save_config({"api_key": "secret"})
        self.bridge.save_config({"api_key": ""})
        self.assertTrue(self.bridge.snapshot()["config"]["has_api_key"])
        self.assertEqual(self.bridge.config_path.stat().st_mode & 0o777, 0o600)
        self.bridge.save_config({"clear_api_key": True})
        self.assertFalse(self.bridge.snapshot()["config"]["has_api_key"])

    def test_second_instance_cannot_share_outbox(self):
        with self.assertRaisesRegex(ValueError, "already using"):
            Bridge(self.tmp.name)

    def test_station_discovery_without_writing_qso(self):
        with fake_cloudlog() as (url, requests):
            self.configure(url)
            self.assertEqual(self.bridge.stations()[0]["station_id"], "7")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(isinstance(x, str) for x in requests))


class TransportTests(unittest.TestCase):
    def test_macos_tls_loads_system_roots_and_keeps_verification(self):
        # An empty default trust store reproduces this python.org installation.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch("ham_cloud_udp_bridge.service.ssl.create_default_context", return_value=context), \
                patch("ham_cloud_udp_bridge.service.sys.platform", "darwin"), \
                patch.dict("os.environ", {}, clear=True):
            result = tls_context()
        self.assertIs(result, context)
        self.assertTrue(result.check_hostname)
        self.assertEqual(result.verify_mode, ssl.CERT_REQUIRED)
        if Path('/etc/ssl/cert.pem').is_file():
            self.assertGreater(result.cert_store_stats()['x509_ca'], 0)

    def test_explicit_certificate_override_is_respected(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch("ham_cloud_udp_bridge.service.ssl.create_default_context", return_value=context), \
                patch("ham_cloud_udp_bridge.service.sys.platform", "darwin"), \
                patch.dict("os.environ", {"SSL_CERT_FILE": "/custom/roots.pem"}, clear=True):
            self.assertEqual(tls_context().cert_store_stats()['x509_ca'], 0)

    def test_network_errors_are_specific_and_never_expose_key(self):
        cases = [(ssl.SSLCertVerificationError(1, 'test-secret'), 'certificate', False),
                 (ssl.SSLError(1, 'test-secret'), 'TLS setup', False),
                 (socket.gaierror(-2, 'test-secret'), 'resolved', True),
                 (TimeoutError('test-secret'), 'timed out', True),
                 (ConnectionRefusedError('test-secret'), 'refused', True)]
        for reason, expected, retry in cases:
            with self.subTest(expected=expected), patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = urllib.error.URLError(reason)
                with self.assertRaises(UploadError) as error:
                    request_json('http://example.test/api/test-secret', 'test-secret')
                self.assertIn(expected, str(error.exception))
                self.assertNotIn('test-secret', str(error.exception))
                self.assertEqual(error.exception.retry, retry)

    def test_success_requires_application_response(self):
        cases = [({"status":"created", "imported_count":1}, "uploaded"),
                 ({"status":"created", "imported_count":0, "messages":["Duplicate"]}, "duplicate"),
                 ({"status":"created"}, "accepted")]
        for response, expected in cases:
            with self.subTest(expected=expected), fake_cloudlog(response=response) as (url, _):
                result = upload({"api_key":"secret"}, {"target":url,"station":"7","adif":"<EOR>"})
                self.assertEqual(result[0], expected)
        with fake_cloudlog(response={"status":"created", "imported_count":0, "messages":["Invalid mode"]}) as (url, _):
            with self.assertRaises(UploadError):
                upload({"api_key":"secret"}, {"target":url,"station":"7","adif":"<EOR>"})

    def test_does_not_follow_redirects_with_key(self):
        with fake_cloudlog(redirect=True) as (url, requests):
            with self.assertRaises(UploadError):
                request_json(url, "secret", {"key":"secret"})
            self.assertEqual(len(requests), 1)

    def test_url_validation_and_subdirectory(self):
        self.assertEqual(normalise_url("https://example.test/cloudlog/index.php/"), "https://example.test/cloudlog")
        for url in ("file:///tmp/file", "https://user:pass@example.test", "https://example.test/?key=secret"):
            with self.assertRaises(ValueError):
                normalise_url(url)


class HTTPTests(unittest.TestCase):
    def test_local_api_secret_and_origin_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge(directory)
            server = make_server(bridge, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(root + "/api/state") as response:
                    state = json.load(response)
                headers = {"Content-Type":"application/json", "X-Bridge-Token":state["token"]}
                request = urllib.request.Request(root + "/api/config", data=json.dumps({"api_key":"top-secret"}).encode(), headers=headers)
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                with urllib.request.urlopen(root + "/api/state") as response:
                    self.assertNotIn(b"top-secret", response.read())
                for override in ({"Origin":"https://evil.example"}, {"Host":"evil.example"}, {"X-Bridge-Token":"wrong"}):
                    with self.subTest(override=override):
                        request = urllib.request.Request(root + "/api/start", data=b"{}", headers={**headers, **override})
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            urllib.request.urlopen(request)
                        self.assertEqual(error.exception.code, 403)
                        error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                bridge.close()


if __name__ == "__main__":
    unittest.main()
