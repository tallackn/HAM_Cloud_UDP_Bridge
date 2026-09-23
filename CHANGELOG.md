# Changelog

## 1.2.0

- Renamed the application and Python package to HAM Cloud UDP Bridge.
- Added native macOS Keychain credential storage and migration from legacy settings.
- Preserved existing QSO history, destination identities and paused uploads during upgrade.
- Removed station-specific defaults and prepared public documentation and packaging.
- Added credential-migration and failure-path tests.

## 1.1.0 — local development

- Added optional independent Club Log delivery, real-time and backlog endpoints, and durable rejection pauses.
- Added separate destination results and clearer credential guidance.

## 1.0.0 — local development

- Initial SDR-Control UDP listener, CloudLog uploader and local browser interface.
- Added durable outbox, capture-only mode, duplicate suppression and macOS TLS trust handling.
