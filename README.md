# HAM Cloud UDP Bridge

A MacOS console app with local web UI that allows for N1MM or Log4OM UDP Broadcast Logs to be uploaded to CloudLog and ClubLog.

Receive a newly logged QSO over UDP, save it to a durable local outbox, and follow each destination's upload status in your browser. Designed for **SDR-Control on macOS**. The browser interface runs at **http://127.0.0.1:8765**.

## Features

- N1MM `contactinfo` XML, single-record ADIF, and SDR-Control's wrapped Log4OM/ADIF broadcasts.
- Independent CloudLog and Club Log uploads, status, duplicate suppression and retries.
- Start/stop UDP reception and configure destinations in a local web interface.
- Capture-only mode for testing without uploading a QSO.
- Optional rebroadcast filtering using delivery history and a configurable QSO age limit.
- SQLite outbox that survives restarts, with received packets and outgoing ADIF visible for inspection.
- API keys and passwords stored in **macOS Keychain**, using Apple's Security framework directly.
- No third-party Python dependencies, external web assets or telemetry.

**Club Log requires both an application password and a developer API key issued for this software.** An App Password alone is insufficient. No developer key is bundled. Publication does not establish Club Log approval or guarantee a key will be issued. See [Club Log setup](docs/SETUP.md#club-log).

## Requirements

- macOS with an accessible, unlocked user Keychain.
- Python **3.10 or newer**, available as `python3`. Install from [python.org](https://www.python.org/downloads/macos/) if needed.
- SDR-Control or another sender producing a supported UDP log format.
- CloudLog server with a read/write API key and station profile. Club Log is optional.

This is a Python console application, not a signed or notarised macOS `.app`. The service must remain running while receiving QSOs. It does not start at login automatically.

## Start

Download this repository using **Code → Download ZIP**, extract it, then double-click **Start HAM Cloud UDP Bridge.command**. If Finder cannot launch the script, open Terminal in the extracted directory and run:

```sh
python3 -m ham_cloud_udp_bridge --open
```

Keep the Terminal window open. Press **Control-C** to stop the service. To use another browser port:

```sh
python3 -m ham_cloud_udp_bridge --port 8766 --open
```

The browser always binds to loopback. Its port is separate from the UDP listener port.

## First setup

1. Open the local UI and leave **Upload new QSOs automatically** off.
2. Set the UDP listener IP and port. Defaults are **127.0.0.1:2237**.
3. Enter your CloudLog base URL and read/write API key, then save. No server URL is supplied by default.
4. Select **Check key and load stations**, choose your station profile, then save again.
5. In SDR-Control, open **Settings → External Software**, enable UDP log submission, choose **N1MM or Log4OM**, and enter the same IP and port.
6. Start the bridge's listener. Use SDR-Control's **Test** button and inspect the captured packet.
7. Enable automatic uploads and save when ready to log real QSOs. Configure Club Log separately if needed.

A packet received in capture-only mode is **never released automatically later**. To upload a captured real QSO, enable uploads and explicitly rebroadcast it from the sender. Do not send test QSOs while automatic uploads are enabled.

See [detailed setup and troubleshooting](docs/SETUP.md) for credentials, server settings and errors.

## Delivery behaviour

Each enabled destination receives its own outbox record. A failed or slow Club Log upload does not block CloudLog. Retrying one destination does not resubmit a successful delivery to the other.

| Status | Meaning |
| --- | --- |
| Ignored | A non-QSO message or a packet skipped by the rebroadcast filter |
| Captured only | Displayed locally, with no upload job created |
| Queued / Uploading | Waiting or currently submitting |
| Uploaded | The service confirmed the QSO was stored |
| Duplicate | The service reports the QSO already exists |
| Accepted; verify | Request accepted, but insertion not confirmed; inspect the remote logbook |
| Retry scheduled | A transient failure will be retried |
| Failed | Requires attention and a manual retry after correction |
| Review changes | A changed record matches an existing identity; edit the remote logbook manually |

- **Stopping the listener stops reception.** Disable automatic uploads to pause both outboxes. A request already in progress may finish.
- Club Log can be switched off separately. Existing Club Log jobs pause, and new QSOs are sent only to CloudLog. Re-enabling resumes existing jobs but does not backfill older packets.
- New single QSOs use Club Log's real-time API. Backlogs use its ADIF merge API and show **Accepted; verify** until you check them remotely. The bridge never requests replacement or clearing of an existing Club Log logbook.
- Network failures and transient real-time errors retry up to five total attempts. Club Log authentication failures stop further Club Log requests until credentials change. Batch failures also pause Club Log to respect its API rules.
- Delivery records retain their original server/account and station/callsign. Changing settings holds unmatched jobs rather than redirecting them.
- **Ignore rebroadcasts** skips known contacts and contacts older than the configured age limit (initially 15 minutes), using the end time if supplied, otherwise the start time. It is an optional heuristic, not a reliable protocol flag. See [filter behaviour and limitations](docs/SETUP.md#ignoring-rebroadcasts).
- Local duplicate detection includes destination, station, callsign, UTC date/time, band, mode and submode. Delivery is not guaranteed exactly once: a lost response can follow a successful remote import.
- The confirmed-deliveries counter counts destinations, not unique QSOs. One QSO delivered to two services adds two.

## Limits

UDP provides no delivery acknowledgement. Packets sent while the listener or Mac is stopped or asleep are missed. Turn off **Ignore rebroadcasts** before deliberately rebroadcasting missed real QSOs from the sender.

This is a **new-QSO importer**, not a two-way synchroniser. Edits and deletions must be applied in each remote logbook. N1MM `contactreplace`, `contactdelete` and radio-status packets are recorded but not imported. WSJT-X binary UDP is not supported.

The latest 1,000 incoming events are retained, with 100 displayed. Upload jobs remain for duplicate suppression, with 200 displayed. They are not automatically pruned. CloudLog must be configured to use automatic uploads; a Club Log-only mode is not provided.

## Local data and Keychain

Settings and QSO history reside in:

```text
~/Library/Application Support/HAM Cloud UDP Bridge/
    config.json       # Non-secret settings and Keychain profile identifier
    bridge.sqlite3    # QSO outbox and received events
```

Credentials are stored in a macOS generic-password Keychain item with service name **HAM Cloud UDP Bridge**. Settings files contain no API keys or application passwords. The app holds credentials in memory while running and transmits them only to the configured service. See [SECURITY.md](SECURITY.md) for the security model, Keychain prompts, backups and reporting.

**Upgrade from CloudLog UDP Bridge:** stop the old service first. On a normal launch, the app renames the old data directory, preserves the outbox, migrates saved credentials to Keychain and removes plaintext credentials from the active settings and known legacy bridge backup settings. If Keychain migration fails, the app stops and retains the original settings. External backups and historical copies cannot be scrubbed automatically.

## Development and verification

```sh
python3 -m unittest discover -v
python3 tools/build_release.py
```

Tests use temporary folders, in-memory secret stores and local fake HTTP services. They do not send QSOs to real accounts or access your Keychain. The release builder uses an explicit source-file allowlist and excludes settings, databases and workspace material.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [CHANGELOG.md](CHANGELOG.md) for changes. Club Log's live upload remains unverified until an appropriate developer key is issued and a real QSO is successfully accepted. Local automated tests are not evidence of Club Log approval.

## Protocol references

- [SDR-Control for Mac manual](https://documents.roskosch.de/sdr-control-mac/)
- [N1MM UDP broadcast specification](https://n1mmwp.hamdocs.com/appendices/external-udp-broadcasts/)
- [CloudLog QSO API implementation](https://github.com/magicbug/Cloudlog/blob/master/application/controllers/Api.php)
- [Club Log real-time API](https://clublog.freshdesk.com/support/solutions/articles/54906-how-to-upload-qsos-in-real-time)
- [Club Log batch upload API](https://clublog.freshdesk.com/support/solutions/articles/54905)

This project is independent of SDR-Control, CloudLog and Club Log.

## Licence

[MIT](LICENSE), copyright 2026 Nathan Tallack.
