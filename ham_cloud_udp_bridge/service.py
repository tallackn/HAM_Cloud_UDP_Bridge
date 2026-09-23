"""Durable outbox, exclusive UDP listener and CloudLog transport."""

import hashlib
import fcntl
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .keychain import KeychainError, KeychainStore

from .clublog import SECRETS, credentials, explain_error, redact, upload_clublog

from .protocol import IgnoredPacket, parse_packet, to_adif

DEFAULTS = {"udp_host": "127.0.0.1", "udp_port": 2237,
            "cloudlog_url": "", "api_key": "",
            "station_id": "", "uploads_enabled": False,
            "clublog_enabled": False, "clublog_email": "", "clublog_callsign": "",
            "clublog_password": "", "clublog_api_key": ""}


def normalise_url(value):
    value = str(value).strip().rstrip("/")
    url = urllib.parse.urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("CloudLog URL must begin with http:// or https://")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("Use the CloudLog base URL without credentials, query or fragment")
    _ = url.port  # Validate the optional port.
    for suffix in ("/index.php/api/qso", "/api/qso", "/index.php"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UploadError(Exception):
    def __init__(self, message, retry=False, pause=False, auth=False):
        super().__init__(message)
        self.retry = retry
        self.pause = pause
        self.auth = auth


def tls_context():
    """Use verified TLS, including macOS's CA bundle for python.org installs."""
    context = ssl.create_default_context()
    # python.org builds can point at a nonexistent cert.pem until their separate
    # certificate installer is run. macOS already supplies a maintained CA file.
    # Honour explicit trust-store overrides rather than broadening them.
    if (sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE")
            and not os.environ.get("SSL_CERT_DIR") and Path("/etc/ssl/cert.pem").is_file()):
        context.load_verify_locations(cafile="/etc/ssl/cert.pem")
    return context


def request_json(url, key, body=None):
    # Do not forward a key through redirects to a login page or different host.
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                    headers={"Content-Type": "application/json", "Accept": "application/json",
                                             "User-Agent": "HAM-Cloud-UDP-Bridge/1.2"})
    try:
        handlers = [NoRedirect]
        if urllib.parse.urlsplit(url).scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=tls_context()))
        with urllib.request.build_opener(*handlers).open(request, timeout=15) as response:
            raw = response.read(1024 * 1024)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise UploadError("Server returned non-JSON data. Check the CloudLog base URL.") from exc
    except urllib.error.HTTPError as exc:
        # Never log response bodies or URLs that might echo a secret.
        detail = ""
        try:
            payload = json.loads(exc.read(65536))
            if isinstance(payload, dict):
                detail = str(payload.get("reason", ""))[:240].replace(key, "[redacted]") if key else ""
        except (ValueError, UnicodeDecodeError):
            pass
        finally:
            exc.close()
        hint = detail or ("Check URL, API key and station profile." if exc.code < 500 else "CloudLog server error.")
        raise UploadError(f"HTTP {exc.code}: {hint}", exc.code == 429 or exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise UploadError("CloudLog's TLS certificate could not be verified. Check the server certificate and local trusted certificate bundle.") from exc
        if isinstance(reason, ssl.SSLError):
            raise UploadError("The secure connection to CloudLog failed during TLS setup. Check the server's HTTPS configuration and local certificates.") from exc
        if isinstance(reason, socket.gaierror):
            raise UploadError("The CloudLog server name could not be resolved. Check the server URL and your DNS or internet connection.", True) from exc
        if isinstance(reason, TimeoutError):
            raise UploadError("The CloudLog connection timed out. Check your internet connection and server availability.", True) from exc
        if isinstance(reason, ConnectionRefusedError):
            raise UploadError("CloudLog refused the connection. Check the server URL and port.", True) from exc
        raise UploadError("Could not connect to CloudLog. Check your internet connection and server availability.", True) from exc


def upload(config, job):
    result = request_json(job["target"] + "/index.php/api/qso", config["api_key"],
                          {"key": config["api_key"], "station_profile_id": job["station"],
                           "type": "adif", "string": job["adif"]})
    if not isinstance(result, dict) or result.get("status") != "created":
        reason = str(result.get("reason", "Unrecognised API response")) if isinstance(result, dict) else "Unrecognised API response"
        raise UploadError(reason.replace(config["api_key"], "[redacted]")[:300])
    count = result.get("imported_count")
    if count in (1, "1"):
        return "uploaded", "CloudLog confirmed 1 QSO imported"
    # Older releases do not return an import count. Do not claim a confirmed insertion.
    if count is None:
        return "accepted", "CloudLog accepted the request; this server did not return an import count"
    messages = json.dumps(result.get("messages", []), ensure_ascii=False)
    if "duplicate" in messages.lower() or "dupe" in messages.lower():
        return "duplicate", "CloudLog reports this QSO already exists"
    raise UploadError(("CloudLog imported no QSO: " + messages).replace(config["api_key"], "[redacted]")[:400])


class Bridge:
    def __init__(self, directory, secret_store=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.instance_lock = open(self.directory / ".instance.lock", "a")
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.instance_lock.close()
            raise ValueError("Another bridge is already using this data folder. Open its browser window instead.") from exc
        self.lock = threading.RLock()
        self.control = threading.Lock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.sock = None
        self.listener = None
        self.listener_error = ""
        self.config = dict(DEFAULTS)
        self.config_path = self.directory / "config.json"
        try:
            if self.config_path.exists():
                self.config.update(json.loads(self.config_path.read_text()))
            self.config['keychain_id'] = str(uuid.UUID(self.config['keychain_id'])) if self.config.get('keychain_id') else str(uuid.uuid4())
            self.secret_store = secret_store if secret_store is not None else KeychainStore(self.config['keychain_id'])
            saved = self.secret_store.read()
            legacy = {key: self.config[key] for key in SECRETS if self.config.get(key)}
            if legacy:
                migrated = {**saved, **legacy}
                self.secret_store.write(migrated)
                if self.secret_store.read() != migrated:
                    raise KeychainError('Keychain migration could not be verified; original settings have been retained')
                saved = migrated
            for key in SECRETS:
                self.config[key] = saved.get(key, '')
            self._write_public_config(self.config)
            # Earlier versions made local backups with plaintext config files.
            # Scrub only those known bridge backups, never arbitrary user files.
            for backup in self.directory.glob('backup-before-clublog-*/config.json'):
                previous = json.loads(backup.read_text())
                if any(key in previous for key in SECRETS):
                    self._write_public_config(previous, backup)
        except Exception:
            self.instance_lock.close()
            raise
        self.db = sqlite3.connect(self.directory / "bridge.sqlite3", check_same_thread=False)
        os.chmod(self.directory / "bridge.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, identity TEXT UNIQUE NOT NULL,
                call TEXT NOT NULL, adif TEXT NOT NULL, target TEXT NOT NULL, station TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
                raw TEXT NOT NULL, protocol TEXT NOT NULL, call TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, job_id INTEGER);
            UPDATE jobs SET status='retry', detail='Interrupted upload; will retry with duplicate checking'
                WHERE status='uploading';
        """)
        # Additive migration preserves all existing CloudLog identities and history.
        if 'service' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN service TEXT NOT NULL DEFAULT 'cloudlog'")
        if 'clublog_job_id' not in {r[1] for r in self.db.execute('PRAGMA table_info(events)')}:
            self.db.execute('ALTER TABLE events ADD COLUMN clublog_job_id INTEGER')
        self.db.execute("CREATE TABLE IF NOT EXISTS service_state (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self.clublog_worker = threading.Thread(target=self._work, args=('clublog',), name='clublog-uploader', daemon=True)
        self.clublog_worker.start()
        self.worker = threading.Thread(target=self._work, name="cloudlog-uploader", daemon=True)
        self.worker.start()

    def _write_public_config(self, config, destination=None):
        destination = destination or self.config_path
        temporary = destination.with_suffix('.tmp')
        public = {key: value for key, value in config.items() if key not in SECRETS}
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'w') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(public, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def save_config(self, incoming):
        with self.control, self.lock:
            config = dict(self.config)
            for key in DEFAULTS:
                if key in incoming:
                    if key in SECRETS and incoming[key] == "":
                        continue  # Blank means retain the stored key.
                    config[key] = incoming[key]
            for secret in SECRETS:
                if incoming.get('clear_' + secret) is True:
                    config[secret] = ''
                if not isinstance(config[secret], str) or len(config[secret]) > 512:
                    raise ValueError('Invalid credential')
                config[secret] = config[secret].strip()
            if type(config['clublog_enabled']) is not bool:
                raise ValueError('Invalid Club Log upload setting')
            for name in ('clublog_email', 'clublog_callsign'):
                if not isinstance(config[name], str) or len(config[name]) > 254:
                    raise ValueError('Invalid Club Log account setting')
                config[name] = config[name].strip()
            config['clublog_email'] = config['clublog_email'].lower()
            config['clublog_callsign'] = config['clublog_callsign'].upper()
            if config['clublog_email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', config['clublog_email']):
                raise ValueError('Enter your Club Log email address, not a callsign')
            if config['clublog_callsign'] and not re.fullmatch(r'[A-Z0-9]+(?:/[A-Z0-9]+)*', config['clublog_callsign']):
                raise ValueError('Invalid Club Log callsign')
            if config['clublog_enabled'] and not all(credentials(config)):
                raise ValueError('Set the Club Log email, callsign, application password and developer API key before enabling it')
            try:
                ipaddress.IPv4Address(config["udp_host"])
            except (ValueError, TypeError) as exc:
                raise ValueError("Listener IP must be an IPv4 address, e.g. 127.0.0.1") from exc
            config["udp_port"] = int(config["udp_port"])
            if not 1 <= config["udp_port"] <= 65535:
                raise ValueError("UDP port must be between 1 and 65535")
            config["cloudlog_url"] = normalise_url(config["cloudlog_url"]) if config["cloudlog_url"] else ""
            config["station_id"] = str(config["station_id"]).strip()
            if config["station_id"] and (not config["station_id"].isdigit() or int(config["station_id"]) < 1):
                raise ValueError("Station profile ID must be a positive number")
            if type(config["uploads_enabled"]) is not bool:
                raise ValueError("Invalid upload setting")
            if not isinstance(config["api_key"], str) or len(config["api_key"]) > 512:
                raise ValueError("Invalid API key")
            config["api_key"] = config["api_key"].strip()
            if config["uploads_enabled"] and (not config["api_key"] or not config["station_id"] or not config["cloudlog_url"]):
                raise ValueError("Set the CloudLog URL, API key and station profile ID before enabling uploads")
            if self.sock and (config["udp_host"], config["udp_port"]) != (self.config["udp_host"], self.config["udp_port"]):
                raise ValueError("Stop the listener before changing its IP or port")
            old_secrets = {key: self.config[key] for key in SECRETS if self.config[key]}
            new_secrets = {key: config[key] for key in SECRETS if config[key]}
            changed = old_secrets != new_secrets
            if changed:
                self.secret_store.write(new_secrets)
            try:
                self._write_public_config(config)
            except OSError:
                if changed:
                    try:
                        self.secret_store.write(old_secrets)
                    except KeychainError:
                        raise KeychainError('Settings could not be saved and Keychain rollback failed. Restart the bridge and check credentials before uploading.') from None
                raise
            if credentials(config) != credentials(self.config):
                self.db.execute("DELETE FROM service_state WHERE name='clublog_block'")
                self.db.commit()
            self.config = config
            self.wake.set()

    def start_listener(self):
        with self.control, self.lock:
            if self.sock:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Exclusive bind: sharing with WSJT-X can silently divert datagrams.
                sock.bind((self.config["udp_host"], self.config["udp_port"]))
                sock.settimeout(0.3)
            except OSError as exc:
                sock.close()
                self.listener_error = f"Cannot bind UDP listener: {exc}"
                raise ValueError(self.listener_error) from exc
            self.sock = sock
            self.listener_error = ""
            self.listener = threading.Thread(target=self._listen, args=(sock,), name="udp-listener", daemon=True)
            self.listener.start()

    def stop_listener(self):
        with self.control:
            with self.lock:
                sock, listener = self.sock, self.listener
                self.sock = None
            if sock:
                sock.close()
            if listener:
                listener.join(timeout=2)

    def _listen(self, sock):
        while not self.closed.is_set():
            with self.lock:
                if self.sock is not sock:
                    break
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                with self.lock:
                    if self.sock is sock:
                        self.listener_error = f"UDP receive failed: {exc}"
                        self.sock = None
                        sock.close()
                break
            try:
                self.receive(data, f"{address[0]}:{address[1]}")
            except Exception:
                # A storage fault must not leave a dead thread labelled 'listening'.
                with self.lock:
                    self.listener_error = "Could not store the received packet. Check disk space and restart the listener."
                    if self.sock is sock:
                        self.sock = None
                    sock.close()
                break

    def _queue(self, fields, adif, service):
        config = self.config
        target, station = self.destination(config, service)
        identity = [target, station, fields.get('STATION_CALLSIGN', ''), fields['CALL'],
                    fields['QSO_DATE'], fields['TIME_ON'], fields.get('BAND', fields.get('FREQ', '')),
                    fields['MODE'], fields.get('SUBMODE', '')]
        if service == 'clublog':
            identity.insert(0, service)
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        old = self.db.execute('SELECT * FROM jobs WHERE identity=?', (fingerprint,)).fetchone()
        if old:
            if old['adif'] == adif:
                return old['id'], 'duplicate', 'Already recorded; not queued again'
            return old['id'], 'review', 'Changed QSO matches an existing upload; review changes manually'
        status, detail = 'queued', 'Waiting for upload'
        if service == 'clublog' and fields.get('STATION_CALLSIGN', '').upper() not in ('', station):
            status, detail = 'failed', 'Packet station callsign differs from the configured Club Log callsign; correct the destination before rebroadcasting'
        job_id = self.db.execute("""INSERT INTO jobs
            (created,identity,call,adif,target,station,status,detail,service) VALUES (?,?,?,?,?,?,?,?,?)""",
            (time.time(), fingerprint, fields['CALL'], adif, target, station, status, detail, service)).lastrowid
        return job_id, status, detail

    @staticmethod
    def destination(config, service):
        if service == 'clublog':
            return config['clublog_email'], config['clublog_callsign']
        return config['cloudlog_url'], config['station_id']

    def receive(self, data, source):
        protocol, call, job_id, clublog_job_id = 'Unknown', '', None, None
        # One transaction links both destinations and the incoming event before
        # either worker can send. Destination settings cannot change mid-packet.
        with self.lock:
            try:
                fields, protocol, source_id = parse_packet(data)
                call, adif = fields['CALL'], to_adif(fields)
                if not self.config['uploads_enabled']:
                    status, detail = 'captured', 'Capture only: not queued or uploaded'
                else:
                    job_id, status, detail = self._queue(fields, adif, 'cloudlog')
                    if self.config['clublog_enabled']:
                        clublog_job_id, club_status, club_detail = self._queue(fields, adif, 'clublog')
                        if club_status == 'review':
                            status, detail = club_status, club_detail
            except IgnoredPacket as exc:
                status, detail = 'ignored', str(exc)
            except (ValueError, UnicodeDecodeError) as exc:
                status, detail = 'invalid', str(exc)
            self.db.execute("""INSERT INTO events
                (created,source,raw,protocol,call,status,detail,job_id,clublog_job_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                (time.time(), source, redact(data.decode('utf-8', errors='replace'), self.config),
                 protocol, call, status, redact(detail, self.config), job_id, clublog_job_id))
            self.db.execute('DELETE FROM events WHERE id <= (SELECT MAX(id)-1000 FROM events)')
            self.db.commit()
        self.wake.set()

    def _work(self, service='cloudlog'):
        while not self.closed.is_set():
            self.wake.clear()
            try:
                worked = self.process_one(service)
            except Exception:
                with self.lock:
                    self.listener_error = 'Upload worker encountered an error. Restart the app after checking disk space.'
                worked = False
            if not worked:
                self.wake.wait(0.5)

    def _state(self, name, default=None):
        row = self.db.execute('SELECT value FROM service_state WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_state(self, name, value):
        self.db.execute('INSERT OR REPLACE INTO service_state VALUES (?,?)', (name, json.dumps(value)))

    def process_one(self, service='cloudlog'):
        with self.lock:
            config = dict(self.config)
            if not config['uploads_enabled']:
                return False
            if service == 'clublog':
                if not config['clublog_enabled'] or not all(credentials(config)) or self._state('clublog_block'):
                    return False
                if self._state('clublog_next', 0) > time.time():
                    return False
            elif not config['api_key'] or not config['station_id']:
                return False
            target, station = self.destination(config, service)
            rows = self.db.execute("""SELECT * FROM jobs WHERE status IN ('queued','retry')
                AND next_try <= ? AND target=? AND station=? AND service=? ORDER BY id LIMIT 500""",
                (time.time(), target, station, service)).fetchall()
            if not rows:
                return False
            batch = service == 'clublog' and (len(rows) > 1 or time.time() - rows[0]['created'] > 300)
            jobs = [dict(row) for row in (rows if batch else rows[:1])]
            label = 'Club Log' if service == 'clublog' else 'CloudLog'
            for job in jobs:
                job['attempts'] += 1
                self.db.execute("UPDATE jobs SET status='uploading',attempts=?,detail=? WHERE id=?",
                                (job['attempts'], 'Sending to ' + label, job['id']))
            if service == 'clublog':
                # Persist throttling across restarts. Backlogs use the batch API.
                self._set_state('clublog_next', time.time() + (300 if batch else 3))
            self.db.commit()
        error = None
        try:
            status, detail = upload_clublog(config, jobs, batch) if service == 'clublog' else upload(config, jobs[0])
        except UploadError as exc:
            error = exc
            status, detail = 'failed', str(exc)
        except Exception:
            status, detail = 'failed', 'Unexpected upload response. Inspect ' + label + ' before retrying.'
        with self.lock:
            if error and error.pause and service == 'clublog':
                # A request started with old credentials must not block corrected ones.
                if credentials(config) == credentials(self.config):
                    self._set_state('clublog_block', {'detail': redact(detail, config), 'auth': error.auth})
            for job in jobs:
                job_status, job_detail = status, redact(detail, config)
                delay = min(300, 5 * 2 ** min(job['attempts'] - 1, 6))
                if error and error.retry and job['attempts'] < 5:
                    job_status = 'retry'
                    job_detail += f' Retrying in {delay} seconds.'
                self.db.execute('UPDATE jobs SET status=?,detail=?,next_try=? WHERE id=?',
                                (job_status, job_detail, time.time() + delay, job['id']))
            self.db.commit()
        return True

    def resume_clublog(self):
        with self.lock:
            block = self._state('clublog_block')
            if block and block['auth']:
                raise ValueError('Update the rejected Club Log credentials before resuming')
            self.db.execute("DELETE FROM service_state WHERE name='clublog_block'")
            self.db.commit()
        self.wake.set()

    def retry(self, job_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['status'] not in ('failed', 'retry'):
                raise ValueError('Only failed or waiting uploads can be retried')
            if (row['target'], row['station']) != self.destination(self.config, row['service']):
                raise ValueError("Restore this upload's original URL/account and station profile/callsign before retrying")
            if row['service'] == 'clublog':
                if self._state('clublog_block'):
                    raise ValueError('Resolve the Club Log pause before retrying')
                fields, _, _ = parse_packet(row['adif'].encode())
                if fields.get('STATION_CALLSIGN', '').upper() not in ('', row['station']):
                    raise ValueError('Correct the Club Log callsign and rebroadcast this QSO')
            self.db.execute("UPDATE jobs SET status='queued', attempts=0, next_try=0, detail='Manual retry requested' WHERE id=?", (job_id,))
            self.db.commit()
        self.wake.set()

    def stations(self):
        with self.lock:
            config = dict(self.config)
        if not config["api_key"] or not config["cloudlog_url"]:
            raise ValueError("Save your CloudLog URL and API key first")
        suffix = urllib.parse.quote(config["api_key"], safe="")
        root = config["cloudlog_url"] + "/index.php/api/"
        rights = request_json(root + "check_auth/" + suffix, config["api_key"])
        if not isinstance(rights, dict) or rights.get("status") != "valid" or rights.get("rights") != "rw":
            raise ValueError("CloudLog requires an active read/write API key")
        stations = request_json(root + "station_info/" + suffix, config["api_key"])
        if not isinstance(stations, list):
            raise ValueError("CloudLog did not return a station list")
        return stations

    def snapshot(self):
        with self.lock:
            config = {key: value for key, value in self.config.items() if key not in (*SECRETS, "keychain_id")}
            for name in SECRETS:
                config['has_' + name] = bool(self.config[name])
            config['clublog_key_format_warning'] = bool(self.config['clublog_api_key']) and not bool(
                re.fullmatch(r'[0-9a-fA-F]{40}', self.config['clublog_api_key']))
            jobs = [dict(row) for row in self.db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 200')]
            counts = {row['status']: row['n'] for row in self.db.execute('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}
            service_counts = {name: {} for name in ('cloudlog', 'clublog')}
            for row in self.db.execute('SELECT service,status,COUNT(*) n FROM jobs GROUP BY service,status'):
                service_counts[row['service']][row['status']] = row['n']
            events = [dict(row) for row in self.db.execute("""SELECT e.*,
                j.status upload_status, j.detail upload_detail,
                c.status clublog_status, c.detail clublog_detail FROM events e
                LEFT JOIN jobs j ON e.job_id=j.id LEFT JOIN jobs c ON e.clublog_job_id=c.id
                ORDER BY e.id DESC LIMIT 100""")]
            held = 0
            for name in ('cloudlog', 'clublog'):
                target, station = self.destination(self.config, name)
                held += self.db.execute("""SELECT COUNT(*) FROM jobs WHERE service=?
                    AND status IN ('queued','retry','failed') AND (target!=? OR station!=?)""",
                    (name, target, station)).fetchone()[0]
            for job in jobs:
                if job['service'] == 'clublog':
                    job['detail'] = explain_error(job['detail'])
            for event in events:
                if event.get('clublog_detail'):
                    event['clublog_detail'] = explain_error(event['clublog_detail'])
            block = self._state('clublog_block')
            if block:
                block['detail'] = explain_error(block['detail'])
            for item in jobs + events:
                for key, value in item.items():
                    if isinstance(value, str):
                        item[key] = redact(value, self.config)
            return {'config': config, 'listening': self.sock is not None,
                    'listener_error': self.listener_error, 'events': events, 'jobs': jobs,
                    'counts': counts, 'service_counts': service_counts, 'held': held,
                    'clublog_block': block}

    def close(self):
        self.stop_listener()
        self.closed.set()
        self.wake.set()
        self.clublog_worker.join()
        self.worker.join()  # Allow an in-flight request to finish before closing SQLite.
        with self.lock:
            self.db.close()
            self.instance_lock.close()
