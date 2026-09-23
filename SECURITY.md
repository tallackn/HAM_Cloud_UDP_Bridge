# Security

## Credentials

CloudLog and Club Log API keys and the Club Log application password are stored as one generic-password item in the user's macOS Keychain, under the service name **HAM Cloud UDP Bridge**. A random profile identifier in `config.json` selects the item. Each data directory receives a separate profile unless its configuration is explicitly copied.

The implementation calls Apple's `SecItemCopyMatching`, `SecItemAdd`, `SecItemUpdate` and `SecItemDelete` APIs directly. There is no shell command containing a secret and no plaintext fallback. Keychain writes update the credential bundle atomically. Failed settings-file writes attempt to restore the prior Keychain bundle.

This is an unsigned Python console app. macOS controls Keychain access for the Python executable; the app does not have the identity isolation of a separately signed, sandboxed native application. Credentials necessarily exist in process memory while running. Keychain protects storage at rest, not a compromised user account or process.

Saved credentials are never returned by the browser settings endpoint. Known raw and URL-encoded secrets are redacted from upload messages and packet views. Do not intentionally put credentials in radio log fields. QSO records are private operational data, not encrypted application secrets.

## Local interface and transport

- Web UI binds to `127.0.0.1` only, over HTTP.
- Requests validate Host and Origin; mutations require a random session token.
- The UI contains no externally loaded scripts, styles or analytics.
- HTTPS verifies certificates and hostnames. Redirects are refused to avoid forwarding credentials.
- CloudLog may be configured with HTTP for local installations; use HTTPS for remote servers.
- Club Log uses a fixed HTTPS host and pauses after authentication rejection.
- UDP is unauthenticated. Keep the listener on loopback unless its network is trusted.

Loopback and the session token do not protect against malicious software running as the same macOS user. CloudLog's documented station-discovery API includes its key in the request path, so the server may retain it in access logs.

## Migration and backups

The former CloudLog UDP Bridge stored credentials in `config.json`. Migration verifies a Keychain write before replacing that file with non-secret settings. Known `backup-before-clublog-*/config.json` files within the migrated data directory are also stripped of credential fields. Migration failure retains original settings rather than discarding credentials.

Time Machine snapshots, external backups, old distributions and unrelated copies are outside this migration. Consider credential rotation if a plaintext copy was shared or exposed. File replacement is not a guarantee of forensic erasure on SSDs or historical snapshots.

The local data directory uses mode `0700` and configuration/database files use `0600`. Never commit local databases, settings, Keychain exports, packet captures or real-QSO screenshots.

## Reporting a vulnerability

Do not put passwords, API keys, QSO databases or personal logs into public issues. Use the repository's private vulnerability-reporting option if available. Otherwise open an issue containing only a high-level request for a private reporting channel, without exploit details or sensitive attachments. Do not assume private reporting has been enabled until GitHub offers it.
