# Contributing

Use Python 3.10+ and keep the runtime free of third-party dependencies unless there is a clear reason to add one. The production app requires macOS Keychain. Unit tests inject an in-memory credential store and can run without Keychain.

```sh
python3 -m unittest discover -v
python3 tools/build_release.py
```

Run an isolated development instance with:

```sh
python3 -m ham_cloud_udp_bridge --data-dir /tmp/ham-cloud-bridge-dev --port 8766
```

This still uses a separate macOS Keychain profile. Do not use production credentials or send synthetic QSOs to real services. An isolated directory does not isolate the UDP port: choose a different port if your normal bridge is listening.

## Structure

- `protocol.py`: N1MM/ADIF parsing and conversion.
- `service.py`: settings, migration, UDP reception, durable outboxes and workers.
- `clublog.py`: Club Log transport, response handling and redaction.
- `keychain.py`: native macOS credential storage.
- `__main__.py`: loopback HTTP API and command-line launcher.
- `static/`: dependency-free browser interface.
- `tests/`: local transport, persistence, security-boundary and migration tests.

Include meaningful tests for parser, transport, retry and migration changes. Preserve existing destination identities and capture-only behaviour. Verify UI changes in a browser and never equate fake-server tests with successful live service uploads.

The GitHub workflow tests supported Python versions. It does not use real credentials or send external QSOs. Native Keychain integration should additionally be checked on macOS with a temporary, uniquely named item that is removed after the test.

Keep generated archives out of source commits. The release builder deliberately includes only named documentation and source folders. Check its output before publishing a release. Do not add runtime files or private project material to the allowlist.
