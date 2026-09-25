# Setup and troubleshooting

## CloudLog

Use the base address of your CloudLog installation, including any subdirectory, without `/index.php/api/qso`. Create an active read/write API key in CloudLog and save it in the bridge. Use **Check key and load stations** to verify permission and choose a station profile, then save again. A numeric station profile ID can also be entered manually.

Disable any other automatic upload path to the same CloudLog destination, including SDR-Control's built-in integration, if it would send the same QSO twice.

CloudLog may forward QSOs to other services according to its own configuration. This bridge reports the CloudLog API response; it cannot confirm those onward deliveries.

## Club Log

You need four values:

| GUI field | Value |
| --- | --- |
| Club Log email address | Registered account email, not a callsign |
| Your Club Log callsign | Exact callsign registered to that account, including portable suffixes |
| Club Log application password | Generate under Club Log → Settings → App Passwords |
| Developer API key for this bridge | Key issued by Club Log for HAM Cloud UDP Bridge |

The password generated on the **App Passwords** page is not a developer API key. The numeric **Key #** displayed on that page is an identifier, not either credential.

Developer keys are requested via [Club Log's request form](https://clublog.org/requestapikey.php). This repository does not include a key or imply approval. If applying, identify the project, its repository URL and use of `realtime.php` for new QSOs and `putlogs.php` for delayed batches. Do not reuse another application's key or commit an issued key to this repository.

Enter the four values, enable **Enable Club Log uploads**, and save. The main automatic-upload setting must also be enabled. Leave saved password/key fields blank to keep their existing Keychain values. To remove them, disable Club Log and select the relevant removal checkboxes.

Club Log's documentation shows a 40-character hexadecimal developer key. The UI warns about other formats but does not reject them solely on that basis. It trims leading/trailing whitespace; it does not silently alter internal characters.

A station callsign supplied by the packet must match the configured Club Log callsign. If the packet omits it, the configured callsign is used.

### Rejected uploads / HTTP 403

The bridge pauses Club Log after an authentication rejection. CloudLog continues. A generic nginx HTML response does not identify which field was rejected and does not prove a firewall ban.

Check the developer key, email, application password and registered callsign. [Club Log's diagnostic page](https://clublog.org/check.php) can show recent rejected requests. After correcting and saving credentials, retry only the failed Club Log delivery. Do not repeatedly submit rejected requests; see [Club Log's guidance](https://clublog.freshdesk.com/support/solutions/articles/3000118794-help-club-log-is-blocking-my-ip-address).

### Backlogs and retries

Fresh individual requests are spaced by at least three seconds. Multiple due contacts, or a delivery waiting more than five minutes, use an ADIF batch merge. Up to 500 due records are included in one batch, with at least five minutes between batch submissions. New deliveries can wait during that interval.

A successful batch submission means Club Log accepted the file for later processing, not that every QSO was imported. Verify its processing in Club Log. Any batch failure pauses Club Log. Correct authentication failures by changing the credentials. For other failures, resolve the error, select **Resume Club Log**, then retry failed deliveries. The batch interval still applies.

## Ignoring rebroadcasts

Enable **Ignore rebroadcasts** under Uploads and save. The option starts off for backwards compatibility. When enabled, it skips contacts already in the bridge's delivery history for the current destinations, including packets with added name or location details. It also skips previously unseen contacts older than **Maximum QSO age**, initially 15 minutes. Both destinations are skipped, and the incoming packet is recorded as **Ignored**, with a reason. Existing queued jobs and retries are unaffected. Capture-only mode still captures all valid packets.

The age uses UTC `QSO_DATE_OFF` / `TIME_OFF` when available, otherwise `QSO_DATE` / `TIME_ON`. An end time without an end date uses the start date, allowing a midnight crossing. A malformed end timestamp is reported as an invalid packet.

This is a heuristic: the supported Log packets do not identify whether SDR-Control is adding a new QSO or rebroadcasting an old one. An unseen rebroadcast within the age limit can still be uploaded. Conversely, a delayed new QSO or a long contact without an end time can be ignored. Keep the Mac and logger clocks accurate, increase the age limit if needed, and switch the filter off before deliberately rebroadcasting missed contacts. Ignored packets are not automatically released when the filter is disabled.

To stop Club Log jobs accumulating, clear **Enable Club Log uploads** and save. This prevents new Club Log jobs and pauses existing ones without affecting CloudLog. Re-enabling it resumes existing jobs but does not backfill packets received while it was off. An upload already in progress may finish.

## macOS Keychain

The app uses native Security-framework calls; it does not pass secrets to the `security` command or any subprocess. macOS may prompt for permission for your Python executable to access the item. Only allow access if you recognise the Python installation and this app. The app never falls back to plaintext storage if access fails.

If startup or saving fails with a Keychain status code, unlock your login Keychain in **Keychain Access**, then restart or try saving again. A different Python installation can trigger a new access prompt. Do not paste credentials or Keychain exports into GitHub issues.

## UDP listener

`127.0.0.1` accepts traffic from this Mac. If receiving from another trusted device, bind to an appropriate local interface address and configure that device's destination accordingly. The web UI remains restricted to loopback. UDP packets are unauthenticated, so do not expose the listener to an untrusted network.

Port 2237 may already be used by another radio application. Select an unused port in both sender and bridge if binding fails. The bridge uses an exclusive socket.

## TLS and network errors

The default CloudLog URL is empty; enter your own HTTPS address. Club Log always uses `https://clublog.org`. Both transports refuse redirects. Fix the CloudLog base URL if the server redirects to a login page or another host.

On macOS the app loads the system `/etc/ssl/cert.pem` trust bundle as well as Python's defaults, unless `SSL_CERT_FILE` or `SSL_CERT_DIR` explicitly overrides trust. Certificate and hostname verification remain enabled. Check server certificates and Python's installation if TLS errors persist. Do not disable verification.

## ADIF conversion

N1MM frequencies use 10 Hz units and are converted to MHz. USB/LSB become SSB with the corresponding submode, FT4 becomes MFSK/FT4, and supported PSK variants use mode/submode. N1MM `power` represents the contacted station's power and maps to `RX_PWR`.

Plain and wrapped ADIF preserve supplied fields. Only one QSO per datagram is accepted. SDR-Control envelope lengths are checked. Missing station information cannot be reconstructed from UDP; CloudLog's selected station profile supplies its own defaults.
