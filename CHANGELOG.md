# Changelog

## 1.3.0

- Added an optional rebroadcast filter using existing delivery history and a configurable QSO age limit.
- Recorded filtered packets as ignored without creating jobs for either destination.
- Clarified that disabling Club Log prevents new queue entries and pauses existing deliveries.
- Added tests for repeat packets, old contacts, end times, settings persistence and destination controls.

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
