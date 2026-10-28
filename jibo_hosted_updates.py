"""Authenticated hosted Phoenix images. No USB and no import-time I/O.

The GUI and TUI build on this module's public API, so keep its names and
data classes stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event

INDEX_URL = 'https://jibo.io/flash/index.json'


@dataclass(frozen=True)
class HostedImage:
    id: str
    version: str
    base_release: str
    components: dict
    size: int
    sha256: str
    profile: str
    strategy: str
    released_at: str
    notes: str
    offered: bool
    is_latest: bool
    compatible: bool
    incompatible_reason: str | None
    downloaded_path: str | None


@dataclass(frozen=True)
class Catalog:
    images: tuple
    latest_id: str | None
    fetched_at: str
    from_cache: bool
    error: str | None


@dataclass(frozen=True)
class Progress:
    phase: str
    bytes: int
    total: int | None
    percent: float | None
    rate: float | None
    eta: float | None
    message: str


class CancelToken:
    def __init__(self):
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class HostedError(Exception):
    code: str

    def __init__(self, code, message):
        self.code = code
        # Keep a non-empty detail so the chooser can explain the failure instead
        # of showing only its generic "jibo.io answered with an error" sentence.
        super().__init__(message or 'Hosted operation failed with code {!r} and no further detail'.format(code))


def fetch_catalog(*, cache_dir=None, timeout=15.0, cancel=None) -> Catalog:
    """Fetch an authenticated catalog; cache fallback only for transport failures.

    Invalid authentication/freshness never falls back to a previously valid list.
    HostedError is intentionally raised for callers to keep local choices usable.
    """
    cache = Path(cache_dir or _default_cache())
    _cancel(cancel)
    try:
        cache = _directory(cache)
        with _lock(cache / 'index.lock'):
            raw = _fetch(INDEX_URL, c.INDEX_MAX, timeout, cancel)
            payload = _index(raw, cache)
            _atomic(cache / 'index.json', raw)
            _atomic(cache / 'sequence.json', _json({'sequence': payload['sequence']}))
            return _catalog(payload, False, None, cache.parent)
    except HostedError as exc:
        if exc.code not in ('offline', 'http', 'rate_limited'):
            raise
        try:
            return cached_catalog(cache_dir=cache, error=f'{exc.code}: {exc}')
        except HostedError:
            raise exc
    except c.ContractError as exc:
        raise HostedError(exc.code, str(exc)) from exc
    except OSError as exc:
        raise _disk_error(cache, exc) from exc


def cached_catalog(*, cache_dir=None, error=None) -> Catalog:
    """Additive explicit offline API; performs zero network or USB calls."""
    cache = Path(cache_dir or _default_cache())
    try:
        cache = _directory(cache)
        raw = _read(cache / 'index.json', c.INDEX_MAX)
        payload = _index(raw, cache)
        return _catalog(payload, True, error, cache.parent)
    except c.ContractError as exc:
        raise HostedError(exc.code, str(exc)) from exc
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise HostedError('offline', 'No usable authenticated offline catalog') from exc
        raise _disk_error(cache, exc) from exc


def download_image(image_id, *, dest_dir=None, progress=None, cancel=None) -> str:
    """Resolve, download/resume and authenticate an archive; never touch USB."""
    try:
        dest = _directory(dest_dir or default_updates_dir())
        return _download(image_id, dest, progress, cancel)
    except (HostedError, c.ContractError, OSError, ValueError) as exc:
        if isinstance(exc, HostedError):
            error = exc
        elif isinstance(exc, c.ContractError):
            error = HostedError(exc.code, str(exc))
        else:
            error = _disk_error(dest_dir or default_updates_dir(), exc)
        _progress(progress, 'canceled' if error.code == 'canceled' else 'error', 0, None, str(error))
        raise error from exc


def local_hosted_images(dest_dir=None) -> list:
    """Rehash and authenticate completed provenance, even when fully offline.

    Expired indexes cannot assert latest; immutable signed completed artifacts
    remain usable. Invalid/missing receipts are omitted, never promoted.
    """
    if dest_dir is None:
        paths = dict.fromkeys((default_updates_dir(), Path.cwd() / 'updates'))
        found = {image.downloaded_path: image for folder in paths for image in local_hosted_images(folder)}
        return list(found.values())
    dest = _safe_path(dest_dir)
    if not dest.is_dir():
        return []
    result = []
    for receipt in sorted(dest.glob('phoenix-*.tar.bz2.provenance.json')):
        path = receipt.with_name(receipt.name.removesuffix('.provenance.json'))
        try:
            row, manifest, _ = _local(path)
            result.append(_dto(row, False, str(path)))
        except (HostedError, c.ContractError, OSError, ValueError):
            continue
    return result


def verify_local_image(path):
    """Additive revalidation API for the destructive backend/CLI (no network)."""
    try:
        row, manifest, metadata = _local(_safe_path(path))
        if reason := c.compatibility(row):
            raise HostedError('incompatible', reason)
        return {'image': _dto(row, False, str(_safe_path(path))), 'manifest': manifest,
                'metadata': metadata, 'archive_sha256': row['sha256']}
    except c.ContractError as exc:
        raise HostedError(exc.code, str(exc)) from exc


def hosted_var_policies(image) -> list:
    return [('keep-calibration', True, ''), ('without-calibration', True, '')]


# Private transport seam is injected only by offline tests; never a CLI override.
ORIGIN = 'https://jibo.io'

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
import socket
from pathlib import Path
import re
import stat
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import jibo_flash_contract as c


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')


def default_updates_dir():
    """Persistent per-user image data, independent of the launch directory."""
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        configured = os.environ.get('XDG_DATA_HOME', '')
        base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / '.local' / 'share'
    return base / 'jibo-dfu' / 'updates'


def _default_cache():
    return default_updates_dir() / '.hosted'


def _cancel(token):
    if token is not None and token.cancelled:
        raise HostedError('canceled', 'Hosted operation canceled; no robot action performed')


def _safe_path(path):
    path = Path(path).expanduser().absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise HostedError('disk', f'Folder: {path}. Hosted paths cannot contain symbolic links')
    return path


def _disk_error(path, error):
    return HostedError('disk', f'Folder: {Path(path).expanduser().absolute()}. {error}')


def _directory(path):
    path = _safe_path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError('Hosted destination is not a directory')
    except OSError as exc:
        raise _disk_error(path, exc) from exc
    return path


def _read(path, maximum):
    path = _safe_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise HostedError('disk', f'Folder: {path.parent}. Unsafe or oversized hosted receipt')
        return stream.read(maximum + 1)


def _atomic(path, raw):
    path = _safe_path(path)
    temporary = path.with_name(path.name + '.tmp-' + os.urandom(8).hex())
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise _disk_error(path.parent, exc) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise _disk_error(path.parent, exc) from exc


@contextmanager
def _lock(path):
    path = _safe_path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise HostedError('disk', f'Folder: {path.parent}. Hosted target is locked by another operation; stale locks require review') from exc
    except OSError as exc:
        raise _disk_error(path.parent, exc) from exc
    try:
        try:
            os.write(fd, str(os.getpid()).encode('ascii'))
        except OSError as exc:
            raise _disk_error(path.parent, exc) from exc
        yield
    finally:
        os.close(fd)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise _disk_error(path.parent, exc) from exc


def _url(url):
    if not isinstance(url, str):
        raise HostedError('signature', 'Hosted URL must be text')
    parsed = urllib.parse.urlsplit(url)
    origins = c.CONTRACT['image_origins'] if ORIGIN == c.CONTRACT['origin'] and parsed.path.startswith('/flash/images/') else (ORIGIN,)
    if not any(url.startswith(origin + '/') for origin in origins) or parsed.username or \
            parsed.password or '?' in url or '#' in url or '%' in parsed.path or \
            not parsed.path.startswith('/flash/') or any(part in ('..', '.') for part in parsed.path.split('/')) or \
            not re.fullmatch(r'/[a-zA-Z0-9._/-]+', parsed.path) or '//' in parsed.path:
        raise HostedError('signature', 'Hosted URL/redirect must remain on the pinned HTTPS flash origin')
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl)  # Refuse before urllib opens the target.
        source, target = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (source.scheme, source.netloc) != (target.scheme, target.netloc) or \
                source.path.startswith('/flash/images/') and not target.path.startswith('/flash/images/'):
            raise HostedError('signature', 'Hosted redirect must remain on the request origin and image route')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTP_ATTEMPTS = 3
_RETRY_WAIT_MAX = 5.0


def _retry_delay(value, attempt):
    """Honor valid Retry-After seconds/date; otherwise use bounded backoff."""
    from email.utils import parsedate_to_datetime
    if value is not None:
        if len(value) > 128:
            return _RETRY_WAIT_MAX + 1  # Refuse, never shorten a huge server wait.
        value = value.strip()
        if value.isascii() and value.isdecimal():
            return int(value)
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(0.0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    return min(_RETRY_WAIT_MAX, 0.25 * 2**attempt)


def _retry_wait(delay, cancel):
    end = time.monotonic() + delay
    while True:
        _cancel(cancel)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


_CA_BUNDLE = Path(__file__).resolve().parent / 'assets' / 'ca' / 'cacert.pem'


def _exception_id(error):
    # Raw urllib/proxy/OS exception text can contain credentials, URLs or server
    # response bytes. Keep the type and numeric errno, not that untrusted text.
    number = getattr(error, 'errno', None)
    return type(error).__name__ + (f' (errno {number})' if isinstance(number, int) else '')


def _load_ca_file(context, path):
    try:
        context.load_verify_locations(cafile=str(path))
    except (OSError, ValueError) as exc:
        reason = ('missing' if isinstance(exc, FileNotFoundError) else
                  'not readable' if isinstance(exc, PermissionError) else 'invalid or unreadable')
        raise HostedError('dependency', f'CA certificate file is {reason}: {path}. '
                          'Reinstall this toolkit copy (source installs: check certifi). ' +
                          _exception_id(exc)) from exc


def _tls_context():
    """Use bundled public roots, not the build Mac's Homebrew certificate path.

    System/explicit OpenSSL roots remain available for managed networks. No
    verification bypass, network bootstrap or external transport is used.
    """
    try:
        context = ssl._create_default_https_context()
    except (OSError, ValueError) as exc:
        raise HostedError('dependency', 'Could not load the system TLS certificates. '
                          'Check SSL_CERT_FILE / SSL_CERT_DIR. ' + _exception_id(exc)) from exc
    if getattr(sys, 'frozen', False) or _CA_BUNDLE.is_file():
        _load_ca_file(context, _CA_BUNDLE)
    else:
        # Source installs can use certifi too; normal system trust still works
        # when that optional package is absent. Frozen apps never use its path.
        try:
            import certifi
        except ImportError:
            pass
        else:
            _load_ca_file(context, certifi.where())
    return context


def _deadline_opener(deadline):
    """Apply the same receive budget before status/headers are parsed."""
    class Response(http.client.HTTPResponse):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            raw = self.fp.raw
            setattr(raw, '_sock', _DeadlineSocket(getattr(raw, '_sock'), deadline))

    class HTTPConnection(http.client.HTTPConnection):
        response_class = Response

    class HTTPSConnection(http.client.HTTPSConnection):
        response_class = Response

    def remaining(req):
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError('Hosted request deadline exceeded')
        req.timeout = budget

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            remaining(req)
            return self.do_open(HTTPConnection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            remaining(req)
            return self.do_open(HTTPSConnection, req, context=getattr(self, '_context'))

    return urllib.request.build_opener(_Redirect(), HTTPHandler(), HTTPSHandler(context=_tls_context()))


def _cloudflare_page(body):
    body = body.lower()
    return any(marker in body for marker in (b'cf-error-details', b'cf-chl-',
        b'attention required! | cloudflare', b'cloudflare ray id'))


def _http_detail(response, deadline):
    status = response.code
    blocked = response.headers.get('cf-mitigated', '').lower() == 'challenge'
    if not blocked:
        try:
            blocked = _cloudflare_page(_metadata_read(response.fp, 8192, deadline))
        except (HostedError, OSError, http.client.HTTPException):
            pass  # Diagnostic inspection must never replace the known HTTP status.
    return f'HTTP {status}.' + (' Cloudflare blocked this request.' if blocked else '')


def _transport_error(error, stage, url=None):
    """Classify the actual failure, without exposing proxy credentials."""
    seen = set()
    while isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException) and id(error) not in seen:
        seen.add(id(error))
        error = error.reason
    target = urllib.parse.urlsplit(url or INDEX_URL).hostname or 'the image host'
    code = 'offline'
    if isinstance(error, ssl.SSLCertVerificationError):
        code = 'tls'
        verify_code = getattr(error, 'verify_code', None)
        # OpenSSL verification codes are stable and safer than raw server text.
        reasons = {9: 'certificate not yet valid', 10: 'certificate expired',
                   18: 'self-signed certificate', 19: 'self-signed certificate chain',
                   20: 'issuer certificate missing', 21: 'issuer chain incomplete',
                   62: 'hostname mismatch', 64: 'IP address mismatch'}
        reason = reasons.get(verify_code, 'certificate not trusted') if isinstance(verify_code, int) else 'certificate not trusted'
        detail = (f'TLS certificate verification failed for {target}: {reason}' +
                  (f' (verify code {verify_code})' if isinstance(verify_code, int) else '') +
                  '. Check the computer clock and any HTTPS-inspecting VPN/proxy. '
                  'Do not disable certificate verification.')
    elif isinstance(error, ssl.SSLError):
        code, detail = 'tls', f'TLS handshake failed for {target}. Check the VPN/proxy and TLS configuration.'
    elif isinstance(error, socket.gaierror):
        detail = f'DNS lookup failed for {target} or its configured proxy. Check DNS and the network connection.'
    elif isinstance(error, TimeoutError):
        detail = f'Timed out while {stage} {target}. Check the connection/VPN/proxy and try again.'
    elif isinstance(error, PermissionError):
        detail = 'Network access was denied on this computer. Check firewall or sandbox permissions.'
    elif isinstance(error, http.client.RemoteDisconnected):
        detail = f'The server or proxy closed the connection to {target} before replying.'
    elif isinstance(error, ConnectionResetError):
        detail = f'Connection reset while {stage} {target}.'
    elif isinstance(error, ConnectionRefusedError):
        detail = f'Connection refused by {target} or its configured proxy.'
    elif getattr(error, 'errno', None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL):
        detail = f'Network route unavailable for {target} or its proxy. Check the network, including IPv4/IPv6 routing.'
    elif isinstance(error, http.client.HTTPException):
        detail = f'Invalid HTTP response while {stage} {target}.'
    elif isinstance(error, ValueError):
        code, detail = 'proxy', 'Invalid proxy or URL configuration on this computer. Check the proxy address and port.'
    elif isinstance(error, urllib.error.URLError) and str(error.reason).startswith('unknown url type:'):
        code, detail = 'proxy', 'Unsupported proxy protocol. This transport requires an HTTP/HTTPS proxy, not SOCKS.'
    elif isinstance(error, OSError) and (match := re.match(r'Tunnel connection failed: (\d{3})\b', str(error))):
        code, detail = 'proxy', f'The configured proxy refused the HTTPS tunnel (HTTP {match[1]}). Check proxy access/authentication.'
    else:
        detail = f'Network request failed while {stage} {target}. Check the connection and proxy configuration.'
    return HostedError(code, detail + ' ' + _exception_id(error))


def _open(url, timeout, headers=None, cancel=None):
    _url(url)
    request = urllib.request.Request(url, headers={
        'Accept-Encoding': 'identity', 'User-Agent': 'Jibo-DFU-Toolkit/hosted-flash-v1',
        **(headers or {})})
    deadline = time.monotonic() + timeout
    for attempt in range(_HTTP_ATTEMPTS):
        _cancel(cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HostedError('offline', 'Hosted request deadline exceeded')
        try:
            response = _deadline_opener(deadline).open(request, timeout=remaining)
            if response.headers.get('cf-mitigated', '').lower() == 'challenge':
                status = response.status
                response.close()
                raise HostedError('http', f'HTTP {status}. Cloudflare blocked this request.')
            if response.headers.get('Content-Encoding', 'identity') != 'identity':
                response.close()
                raise HostedError('http', 'Unexpected HTTP content encoding')
            # Opening/retry deadlines must not become total archive durations.
            # Metadata installs its own outer deadline before consuming bodies.
            if response.fp is not None:
                raw = response.fp.raw
                sock = raw._sock
                if isinstance(sock, _DeadlineSocket):
                    raw._sock = sock._socket
                    raw._sock.settimeout(timeout)
            return response
        except urllib.error.HTTPError as exc:
            delay = _retry_delay(exc.headers.get('Retry-After'), attempt)
            detail = _http_detail(exc, deadline)
            exc.close()
            code = 'rate_limited' if exc.code == 429 else 'http'
            if exc.code not in (429, 503) or attempt + 1 == _HTTP_ATTEMPTS or \
                    delay > _RETRY_WAIT_MAX or delay >= deadline - time.monotonic():
                raise HostedError(code, detail) from exc
            _retry_wait(delay, cancel)
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError, ValueError) as exc:
            raise _transport_error(exc, 'connecting to', url) from exc
    raise HostedError('http', 'Hosted HTTP retry limit exceeded')


class _DeadlineSocket:
    """Keep every receive, including HTTP chunk framing, in one budget."""
    def __init__(self, sock, deadline):
        self._socket = sock
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._socket, name)

    def recv_into(self, *args):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Hosted metadata overall deadline exceeded')
        self._socket.settimeout(remaining)
        return self._socket.recv_into(*args)


def _metadata_read(response, amount, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HostedError('offline', 'Hosted metadata overall deadline exceeded')
    if response.fp is None:
        return b''
    try:
        # urllib's HTTP/HTTPS responses use a buffered SocketIO stream. Bound
        # every raw receive as well as read1: chunk-size/trailer parsing may
        # make several receives before a single read1 returns.
        raw = response.fp.raw
        if not isinstance(raw._sock, _DeadlineSocket):
            raw._sock = _DeadlineSocket(raw._sock, deadline)
        chunk = response.read1(amount)
    except (TimeoutError, http.client.HTTPException, OSError) as exc:
        raise _transport_error(exc, 'reading metadata from') from exc
    if time.monotonic() >= deadline:
        raise HostedError('offline', 'Hosted metadata overall deadline exceeded')
    return chunk


def _fetch(url, maximum, timeout, cancel, expected=None):
    _cancel(cancel)
    deadline = time.monotonic() + timeout
    with _open(url, deadline - time.monotonic(), cancel=cancel) as response:
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > maximum or
                                   expected is not None and int(length) != expected):
            raise HostedError('http', 'HTTP length exceeds or conflicts with signed bound')
        chunks = []
        size = 0
        while True:
            _cancel(cancel)
            chunk = _metadata_read(response, min(65536, maximum + 1 - size), deadline)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise HostedError('http', 'HTTP metadata exceeds signed bound')
            chunks.append(chunk)
        if expected is not None and size != expected or length is not None and size != int(length):
            raise HostedError('http', 'Hosted metadata was truncated')
        raw = b''.join(chunks)
        if raw.lstrip().startswith(b'<') and _cloudflare_page(raw[:8192]):
            raise HostedError('http', f'HTTP {response.status}. Cloudflare blocked this request.')
        return raw


def _index(raw, cache, *, freshness=True):
    payload = c.schema(c.verify_envelope(raw, 'index'), 'index', origin=ORIGIN)
    if not isinstance(payload, dict) or type(payload.get('schema')) is not int or payload.get('schema') != 1 or \
            payload.get('kind') != 'phoenix-flash-index-v1':
        c.fail('Unsupported hosted index')
    sequence = c.integer(payload.get('sequence'), 2**63-1, 'sequence')
    now = time.time()
    generated = c.utc(payload.get('generated_at'))
    expiry = c.utc(payload.get('expires_at'))
    if not generated < expiry <= generated + c.CONTRACT['limits']['index_expiry_seconds'] or generated > now + 300:
        c.fail('Invalid index freshness interval', 'expired')
    if freshness and (now >= expiry or now - generated > 86400):
        c.fail('Hosted catalog is expired', 'expired')
    watermark = cache / 'sequence.json' if cache else None
    if watermark and watermark.exists():
        state = c.strict_json(_read(watermark, 256), 256)
        prior = c.integer(state.get('sequence'), 2**63-1, 'cached sequence')
        if sequence < prior:
            c.fail('Hosted catalog sequence rollback rejected')
    rows = payload.get('images')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        c.fail('Invalid bounded image list')
    seen = set()
    latest = payload.get('latest_image_id')
    for row in rows:
        c.validate_identity(row)
        if row['id'] in seen:
            c.fail('Duplicate catalog image id')
        seen.add(row['id'])
        c.integer(row.get('size'), c.ARCHIVE_MAX, 'archive size')
        c.digest(row.get('sha256'))
        c.integer(row.get('manifest_size'), c.ENVELOPE_MAX, 'manifest size')
        c.digest(row.get('manifest_sha256'))
        for field in ('url', 'manifest_url'):
            _url(row.get(field))
        if c.utc(row.get('released_at')) > now + c.CONTRACT['limits']['future_clock_skew_seconds']:
            c.fail('Future image release')
        image_origins = c.CONTRACT['image_origins'] if ORIGIN == c.CONTRACT['origin'] else (ORIGIN,)
        if row['url'] not in {origin + '/flash/images/' + row['id'] + '/' + row['sha256'] + '.tar.bz2' for origin in image_origins} or row['manifest_url'] != ORIGIN + '/flash/manifests/' + row['id'] + '.json':
            c.fail('URL disagrees with signed image id/hash')
        c.text(row.get('notes'), 'notes', 2048)
        c.text(row.get('acceptance_scope'), 'acceptance scope', 2048)
        if type(row.get('offered')) is not bool:
            c.fail('offered must be a JSON boolean')
        c.compatibility(row)
    if latest not in seen or not next(row['offered'] for row in rows if row['id'] == latest):
        c.fail('Latest must identify exactly one offered row')
    return payload


def _dto(row, latest=False, path=None):
    reason = c.compatibility(row)
    return HostedImage(row['id'], row['version'], row['base_release'], dict(row['components']),
                       row['size'], row['sha256'], row['profile'], row['strategy'],
                       datetime.fromtimestamp(row['released_at'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                       row['notes'], row['offered'], latest,
                       reason is None, reason, path)


def _catalog(payload, cached, error, dest):
    locals_by_hash = {image.sha256: image.downloaded_path for image in local_hosted_images(dest)}
    rows = tuple(_dto(row, row['id'] == payload['latest_image_id'], locals_by_hash.get(row['sha256']))
                 for row in payload['images'])
    return Catalog(rows, payload['latest_image_id'], datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   cached, error)


import base64
import hashlib
import http.client
import shutil
import tarfile


def _progress(callback, phase, size, total, message, started=None):
    if callback is None:
        return
    elapsed = time.monotonic() - started if started is not None else 0
    rate = size / elapsed if elapsed > 0 and size > 0 else None
    callback(Progress(phase, size, total, 100 * size / total if total else None,
                      rate, (total - size) / rate if total is not None and rate else None, message))


def _filename(row):
    return f'phoenix-{row["id"]}-{row["sha256"]}.tar.bz2'


def _provenance(path):
    return path.with_name(path.name + '.provenance.json')


def _local(path):
    path = _safe_path(path)
    if not path.is_file() or path.stat().st_size > c.ARCHIVE_MAX:
        raise HostedError('disk', f'Folder: {path.parent}. Completed hosted archive missing or unsafe')
    receipt_max = 2 * c.ENVELOPE_MAX + c.INDEX_MAX
    receipt = c.strict_json(_read(_provenance(path), receipt_max), receipt_max)
    if not isinstance(receipt, dict) or receipt.get('schema') != 1:
        raise HostedError('signature', 'Unsupported local hosted provenance')
    try:
        index_raw = base64.b64decode(receipt['index'], validate=True)
        manifest_raw = base64.b64decode(receipt['manifest'], validate=True)
    except (ValueError, TypeError, KeyError) as exc:
        raise HostedError('signature', 'Invalid local provenance envelope encoding') from exc
    # Immutable artifact use is independent of offer expiry and watermark. No latest claim.
    index = _index(index_raw, None, freshness=False)
    image_id = receipt.get('id')
    row = next((r for r in index['images'] if r['id'] == image_id), None)
    if row is None:
        raise HostedError('signature', 'Local provenance image id missing from signed index')
    if len(manifest_raw) != row['manifest_size'] or hashlib.sha256(manifest_raw).hexdigest() != row['manifest_sha256']:
        raise HostedError('hash', 'Local signed manifest does not match authenticated catalog pin')
    manifest = c.validate_manifest(c.verify_envelope(manifest_raw, 'manifest'), row, origin=ORIGIN)
    try:
        metadata = c.verify_archive(path, manifest)
    except tarfile.TarError as exc:
        raise HostedError('hash', 'The saved image is damaged. Download it again.') from exc
    return row, manifest, metadata


_CHECKPOINT_BYTES = 8 * 1024 * 1024
_CHECKPOINT_SECONDS = 2.0


def _download(image_id, dest, progress, cancel):
    _cancel(cancel)
    _progress(progress, 'index', 0, None, 'Authenticating hosted catalog')
    cache = dest / '.hosted'
    fetch_catalog(cache_dir=cache, cancel=cancel)
    index_raw = _read(cache / 'index.json', c.INDEX_MAX)
    index = _index(index_raw, cache)
    wanted = index['latest_image_id'] if image_id == 'latest' else c.image_id(image_id)
    row = next((r for r in index['images'] if r['id'] == wanted), None)
    if row is None or not row['offered']:
        raise HostedError('incompatible', 'Selected immutable image is not offered; no alternate was selected')
    if reason := c.compatibility(row):
        raise HostedError('incompatible', reason)
    final = _safe_path(dest / _filename(row))
    with _lock(final.with_name(final.name + '.lock')):
        if final.exists():
            with c.operation(progress, cancel):
                _local(final)  # Never overwrite a pre-existing user archive.
            _progress(progress, 'done', 0, None, 'Getting ready for your review')
            return str(final)
        _progress(progress, 'manifest', 0, row['manifest_size'], 'Authenticating signed artifact manifest')
        manifest_raw = _fetch(row['manifest_url'], c.ENVELOPE_MAX, 15, cancel, row['manifest_size'])
        if hashlib.sha256(manifest_raw).hexdigest() != row['manifest_sha256']:
            raise HostedError('hash', 'Artifact manifest does not match signed catalog pin')
        manifest = c.validate_manifest(c.verify_envelope(manifest_raw, 'manifest'), row, origin=ORIGIN)
        partial = _safe_path(final.with_name(final.name + '.part'))
        receipt_path = _safe_path(partial.with_name(partial.name + '.json'))
        pin = {'schema': 1, 'id': row['id'], 'sha256': row['sha256'], 'size': row['size'],
               'manifest_sha256': row['manifest_sha256']}
        state = dict(pin)
        offset = 0
        digest = hashlib.sha256()
        if partial.exists():
            state = c.strict_json(_read(receipt_path, 8192), 8192)
            if any(state.get(key) != value for key, value in pin.items()) or \
                    not partial.is_file() or partial.stat().st_size > row['size']:
                raise HostedError('disk', f'Folder: {dest}. Partial receipt disagrees with immutable selected image')
            offset = partial.stat().st_size
            with partial.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    _cancel(cancel)
                    digest.update(chunk)
            if state.get('bytes') != offset or state.get('partial_sha256') != digest.hexdigest():
                raise HostedError('hash', 'Existing partial bytes do not match their durable receipt')
        required = row['size'] - offset + c.workspace_bytes(manifest)
        if shutil.disk_usage(dest).free < required:
            raise HostedError('disk', f'Folder: {dest}. Hosted download/preparation requires {required} free bytes on destination volume')
        if offset < row['size']:
            headers = {'Range': f'bytes={offset}-'} if offset else {}
            if offset and state.get('etag'):
                headers['If-Range'] = state['etag']
            started = time.monotonic()
            with _open(row['url'], 30, headers, cancel=cancel) as response:
                if response.status == 206:
                    expected_range = f'bytes {offset}-{row["size"]-1}/{row["size"]}'
                    if response.headers.get('Content-Range') != expected_range:
                        raise HostedError('http', 'Content-Range disagrees with selected signed archive')
                elif response.status == 200:
                    offset = 0  # Range ignored/validator changed: RESTART, never append.
                    digest = hashlib.sha256()
                else:
                    raise HostedError('http', 'Unsupported archive HTTP status')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) != row['size'] - offset):
                    raise HostedError('http', 'Archive HTTP length disagrees with signed size')
                etag = response.headers.get('ETag')
                if etag is not None and (len(etag) > 256 or any(ord(ch) < 32 for ch in etag)):
                    raise HostedError('http', 'Unsafe server validator')
                flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                flags |= os.O_APPEND if offset else os.O_TRUNC
                with os.fdopen(os.open(partial, flags, 0o600), 'wb') as output:
                    if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                        raise HostedError('disk', f'Folder: {dest}. Partial output must be a regular file')
                    checkpoint_bytes = offset
                    checkpoint_time = time.monotonic()
                    def checkpoint():
                        nonlocal checkpoint_bytes, checkpoint_time
                        output.flush()
                        os.fsync(output.fileno())
                        _atomic(receipt_path, _json({**pin, 'bytes': offset,
                            'partial_sha256': digest.hexdigest(), 'etag': etag}))
                        checkpoint_bytes = offset
                        checkpoint_time = time.monotonic()
                    checkpoint()
                    try:
                        while True:
                            _cancel(cancel)
                            try:
                                chunk = response.read(min(65536, row['size'] + 1 - offset))
                            except (TimeoutError, http.client.HTTPException, OSError) as exc:
                                raise _transport_error(exc, 'reading the image from', row['url']) from exc
                            if not chunk:
                                break
                            if offset + len(chunk) > row['size']:
                                raise HostedError('hash', 'Archive exceeds its signed size')
                            output.write(chunk)
                            digest.update(chunk)
                            offset += len(chunk)
                            if offset - checkpoint_bytes >= _CHECKPOINT_BYTES or \
                                    time.monotonic() - checkpoint_time >= _CHECKPOINT_SECONDS:
                                checkpoint()
                            _progress(progress, 'download', offset, row['size'], 'Downloading authenticated Phoenix archive', started)
                    finally:
                        # EOF, cancellation and exceptions all publish the last durable batch.
                        checkpoint()
        _cancel(cancel)
        _progress(progress, 'verify', 0, None, 'Checking the image')
        if offset != row['size']:
            raise HostedError('http', 'Archive was truncated; partial retained for exact-ID resume')
        if digest.hexdigest() != row['sha256']:
            quarantine = partial.with_name(partial.name + '.bad-hash-' + os.urandom(4).hex())
            os.rename(partial, quarantine)
            raise HostedError('hash', 'Archive SHA-256 mismatch; bytes quarantined and never usable')
        # Receipt first, then an exclusive hardlink publishes without overwriting
        # a local archive created during the transfer. Both are same filesystem.
        _atomic(_provenance(final), _json({'schema': 1, 'id': wanted,
                'index': base64.b64encode(index_raw).decode('ascii'),
                'manifest': base64.b64encode(manifest_raw).decode('ascii')}))
        os.link(partial, final)
        partial.unlink()
        receipt_path.unlink(missing_ok=True)
        try:
            with c.operation(progress, cancel):
                c.verify_archive(final, manifest)
        except BaseException as exc:
            # Never leave a completed-looking file after a failed/canceled check.
            final.rename(final.with_name(final.name + '.bad-check-' + os.urandom(4).hex()))
            _provenance(final).unlink(missing_ok=True)
            if isinstance(exc, tarfile.TarError):
                raise HostedError('hash', 'The downloaded image is damaged.') from exc
            raise
        _cancel(cancel)
        _progress(progress, 'done', 0, None, 'Getting ready for your review')
        return str(final)


@dataclass
class FlashDownloadSession:
    directory: Path
    complete: bool = False
    interrupted: bool = False


@contextmanager
def flash_download_session(image_id, *, dest_dir=None):
    """Own only this flash's working files; keep interrupted transfers for resume."""
    image_id = c.image_id(image_id)
    root = _directory(Path(dest_dir or default_updates_dir()) / '.hosted-flash')
    directory = _safe_path(root / image_id)
    marker = directory / 'flash-download-owner.json'
    owner = _json({'schema': 1, 'kind': 'jibo-flash-download', 'id': image_id})
    with _lock(root / (image_id + '.lock')):
        if directory.exists():
            if not directory.is_dir() or not marker.is_file() or _read(marker, 1024) != owner:
                raise HostedError('disk', f'Folder: {directory}. Flash working folder is not owned by this toolkit; nothing was removed')
        else:
            _directory(directory)
            _atomic(marker, owner)
        session = FlashDownloadSession(directory)
        try:
            yield session
        finally:
            resumable = (session.interrupted and not session.complete and
                         any(not path.is_symlink() and path.is_file()
                             for path in directory.glob('phoenix-*.tar.bz2.part')))
            if not resumable:
                # This directory never contains robot backups or user packages.
                _safe_path(directory)
                if _read(marker, 1024) != owner:
                    raise HostedError('disk', f'Folder: {directory}. Flash working folder ownership changed; nothing was removed')
                shutil.rmtree(directory)
