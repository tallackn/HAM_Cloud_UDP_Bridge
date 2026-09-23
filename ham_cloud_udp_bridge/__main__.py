"""Run with python3 -m ham_cloud_udp_bridge."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import urllib.parse
import webbrowser

from .service import Bridge, UploadError


def make_server(bridge, port):
    token = secrets.token_urlsafe(32)
    static = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never put credentials or request paths into console logs.

        def respond(self, status, data, content_type="application/json"):
            payload = json.dumps(data).encode() if content_type == "application/json" else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def allowed(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in hosts:
                self.respond(403, {"error": "Local access only"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in hosts}:
                self.respond(403, {"error": "Cross-origin requests are not allowed"})
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.respond(403, {"error": "Cross-site requests are not allowed"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/state":
                self.respond(200, {**bridge.snapshot(), "token": token})
            elif path in ("/", "/app.js", "/style.css"):
                name = "index.html" if path == "/" else path[1:]
                types = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
                self.respond(200, (static / name).read_bytes(), types[name])
            elif path == "/favicon.ico":
                self.respond(204, b"", "image/x-icon")
            else:
                self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed():
                return
            if not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), token):
                self.respond(403, {"error": "Reload the page to refresh your local session"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, {"error": "Expected application/json"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError("Invalid request size")
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/api/config":
                    bridge.save_config(body)
                elif self.path == "/api/start":
                    bridge.start_listener()
                elif self.path == "/api/stop":
                    bridge.stop_listener()
                elif self.path == "/api/retry":
                    bridge.retry(int(body["id"]))
                elif self.path == "/api/clublog/resume":
                    bridge.resume_clublog()
                elif self.path == "/api/stations":
                    self.respond(200, {"stations": bridge.stations()})
                    return
                else:
                    self.respond(404, {"error": "Not found"})
                    return
                self.respond(200, {"ok": True})
            except (ValueError, TypeError, KeyError, UploadError) as exc:
                self.respond(400, {"error": str(exc)})
            except OSError:
                self.respond(500, {"error": "Local storage or network error. Check available disk space and permissions."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = False
    return server


def default_directory():
    root = Path.home() / 'Library/Application Support'
    destination = root / 'HAM Cloud UDP Bridge'
    legacy = root / 'CloudLog UDP Bridge'
    if legacy.exists() and not destination.exists():
        # Refuse to rename a directory that an old bridge still owns.
        import fcntl
        with open(legacy / '.instance.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('Stop the old CloudLog UDP Bridge before upgrading') from None
            legacy.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description="HAM Cloud UDP Bridge for CloudLog and Club Log")
    parser.add_argument("--port", type=int, default=8765, help="Local browser interface port (default: 8765)")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--open", action="store_true", help="Open the browser interface")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        bridge = Bridge(args.data_dir or default_directory())
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Cannot start the bridge: {exc}\n")
    try:
        server = make_server(bridge, args.port)
    except OSError as exc:
        bridge.close()
        parser.exit(1, f"Cannot open the local web interface: {exc}\nTry a different --port.\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"HAM Cloud UDP Bridge: {url}\nListener is stopped. Configure and start it in your browser.\nPress Control-C to quit.", flush=True)
    if args.open:
        webbrowser.open(url)

    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        bridge.close()


if __name__ == "__main__":
    main()
