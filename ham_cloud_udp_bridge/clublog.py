"""Club Log's real-time and backlog upload interfaces."""
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

URL = 'https://clublog.org'
SECRETS = ('api_key', 'clublog_api_key', 'clublog_password')


def redact(text, config):
    text = str(text)
    for name in SECRETS:
        value = config.get(name, '')
        if value:
            for variant in (value, urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value)):
                text = text.replace(variant, '[redacted]')
    return text


def explain_error(message):
    """Generic web-server HTML gives no evidence about which credential failed."""
    if '403' in message and re.search(r'<(?:html|head|body)\b', message, re.I):
        return ('Club Log refused the request (HTTP 403) without an API explanation. '
                'Check the developer API key, account email, application password and callsign. '
                'Club Log uploads are paused to prevent repeated rejected requests.')
    return message


def credentials(config):
    return tuple(config[name] for name in ('clublog_email', 'clublog_callsign', 'clublog_password', 'clublog_api_key'))


def upload_clublog(config, jobs, batch=False):
    from .service import NoRedirect, UploadError, tls_context
    fields = {'email': config['clublog_email'], 'password': config['clublog_password'],
              'callsign': jobs[0]['station'], 'api': config['clublog_api_key']}
    if batch:
        boundary = 'Bridge' + uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="bridge.adi"\r\nContent-Type: application/octet-stream\r\n\r\n')
        parts.append('HAM Cloud UDP Bridge\n<ADIF_VER:5>3.1.4<EOH>\n' + '\n'.join(j['adif'] for j in jobs))
        parts.append(f'\r\n--{boundary}--\r\n')
        data = ''.join(parts).encode('utf-8')
        content_type = 'multipart/form-data; boundary=' + boundary
        path = '/putlogs.php'
    else:
        fields['adif'] = jobs[0]['adif']
        data = urllib.parse.urlencode(fields).encode('utf-8')
        content_type, path = 'application/x-www-form-urlencoded', '/realtime.php'
    request = urllib.request.Request(URL + path, data=data, headers={
        'Content-Type': content_type, 'User-Agent': 'HAM-Cloud-UDP-Bridge/1.3'})
    try:
        with urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=tls_context())).open(request, timeout=15) as response:
            status = response.status
            reason = str(response.reason)
            message = response.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        message = redact(exc.read(65536).decode('utf-8', errors='replace'), config)[:400]
        exc.close()
        raise UploadError(explain_error(f'Club Log HTTP {exc.code}: {message or "Request rejected"}'),
                          retry=not batch and (exc.code == 429 or exc.code >= 500),
                          pause=batch or exc.code in (401, 403), auth=exc.code in (401, 403)) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLError):
            raise UploadError('Club Log TLS verification or connection failed. Check local certificates.') from exc
        message = 'Club Log connection timed out.' if isinstance(reason, (TimeoutError, socket.timeout)) else 'Could not connect to Club Log. Check your internet connection.'
        raise UploadError(message, retry=not batch, pause=batch) from exc
    message = redact(message, config).strip()[:400]
    if status != 200:
        raise UploadError(f'Unexpected Club Log HTTP {status}: {message}', pause=True)
    if batch:
        return 'accepted', 'Club Log accepted the ADIF batch for processing; verify it in Club Log. ' + message
    # Club Log documents these phrases as HTTP reason/status text. Some
    # implementations return OK in the body instead. Arbitrary HTML is not success.
    feedback = reason + '\n' + message
    if re.search(r'\b(?:QSO\s+)?Duplicate\b', feedback, re.I):
        return 'duplicate', 'Club Log reports this QSO already exists. ' + message
    if re.search(r'\bQSO\s+(?:OK|Modified)\b', feedback, re.I) or re.match(r'^OK(?:\b|$)', message):
        return 'uploaded', 'Club Log confirmed receipt. ' + message
    raise UploadError('Unrecognised Club Log response; verify the QSO before retrying. ' + message)
