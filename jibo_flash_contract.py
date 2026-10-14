"""Frozen v1 wire contract and standard-library/host Ed25519 verifier.

No network or USB access. The local canonical schema is loaded at import.
Release assets pin the production public key and toolkit compatibility version.
Never accept keys from the remote catalog.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from jibo_flash_schema import CONTRACT, validate_shape, check_schema

ASSETS = Path(__file__).resolve().parent / 'assets' / 'flash-images'
TOOLKIT_VERSION = "1.0.0"
CAPABILITIES = frozenset(CONTRACT['capabilities'])
PROFILES = frozenset(CONTRACT['profiles'])
STRATEGIES = frozenset(CONTRACT['strategies'])
INDEX_MAX = CONTRACT['limits']['index_envelope_bytes']
ENVELOPE_MAX = CONTRACT['limits']['manifest_envelope_bytes']
ARCHIVE_MAX = CONTRACT['limits']['archive_bytes']
UNPACKED_MAX = CONTRACT['limits']['unpacked_bytes']
ROLES = {'rootfs': 'rootfs.ext4', 'services': 'services.ext4',
         'skills': 'skills.ext4', 'var': 'var.ext4'}
ID_RE = re.compile(r'[a-z0-9][a-z0-9.-]{0,95}\Z')
HASH_RE = re.compile(r'[0-9a-f]{64}\Z')
VERSION_RE = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


class ContractError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(message, code='signature'):
    raise ContractError(code, message)


def strict_json(raw, maximum=ENVELOPE_MAX):
    if not isinstance(raw, bytes) or len(raw) > maximum:
        fail('JSON exceeds the contract byte bound')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('Duplicate JSON key')
            result[key] = value
        return result

    def no_float(value):
        fail('Non-integer JSON number is not allowed')

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_float=no_float, parse_constant=no_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError('signature', 'Invalid bounded UTF-8 JSON') from exc

    def bounded(item, depth=0):
        if depth > 16:
            fail('JSON nesting exceeds contract bound')
        if isinstance(item, str):
            if len(item) > maximum or any(ord(c) < 32 or 127 <= ord(c) <= 159 or
                                       0xD800 <= ord(c) <= 0xDFFF for c in item):
                fail('Unsafe or oversized JSON text')
        elif isinstance(item, dict):
            if len(item) > 256:
                fail('Too many JSON fields')
            for key, child in item.items():
                bounded(key, depth+1)
                bounded(child, depth+1)
        elif isinstance(item, list):
            if len(item) > 256:
                fail('Too many JSON array entries')
            for child in item:
                bounded(child, depth+1)
        elif type(item) is int and abs(item) > 2**63-1:
            fail('JSON integer exceeds contract bound')
    bounded(value)
    return value


def trusted_keys():
    trust = strict_json((ASSETS / 'trust.json').read_bytes(), 16384)
    if trust.get('schema') != 1 or trust.get('test_only') is not False:
        fail('Hosted trust is TEST-ONLY; a reviewed release key is required', 'dependency')
    keys = trust.get('keys')
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 8:
        fail('Invalid pinned trust key set', 'dependency')
    result = {}
    for key, filename in keys.items():
        if not ID_RE.fullmatch(key) or not isinstance(filename, str) or \
                not re.fullmatch(r'[a-zA-Z0-9.-]+\.pem', filename):
            fail('Invalid pinned public-key filename', 'dependency')
        path = ASSETS / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            fail('Invalid pinned public key', 'dependency')
        result[key] = path.read_bytes()
    return result


def _python_verifier():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        return None

    def verify(public, message, signature):
        try:
            key = serialization.load_pem_public_key(public)
            if not isinstance(key, Ed25519PublicKey):
                return False
            key.verify(signature, message)
            return True
        except (ValueError, TypeError, InvalidSignature):
            return False
    return verify


def _openssl_verify(executable, public, message, signature):
    with tempfile.TemporaryDirectory(prefix='jibo-flash-verify-') as temp:
        root = Path(temp)
        for name, raw in (('public.pem', public), ('payload', message), ('signature', signature)):
            path = root / name
            path.write_bytes(raw)
            path.chmod(0o600)
        try:
            result = subprocess.run([executable, 'pkeyutl', '-verify', '-pubin', '-rawin',
                                     '-inkey', str(root / 'public.pem'), '-in', str(root / 'payload'),
                                     '-sigfile', str(root / 'signature')],
                                    capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0


def _openssl_verifier():
    # RFC8032 section 7.1 TEST 2: positive capability probe, not version guessing.
    public_der = bytes.fromhex('302a300506032b6570032100' +
                              '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c')
    public = b'-----BEGIN PUBLIC KEY-----\n' + base64.b64encode(public_der) + b'\n-----END PUBLIC KEY-----\n'
    signature = bytes.fromhex('92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0'
                              '85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00')
    candidates = [shutil.which('openssl'), '/opt/homebrew/opt/openssl@3/bin/openssl',
                  '/usr/local/opt/openssl@3/bin/openssl', '/opt/local/bin/openssl']
    for executable in dict.fromkeys(candidates):
        if executable and Path(executable).is_file() and \
                _openssl_verify(executable, public, b'\x72', signature):
            return lambda key, msg, sig: _openssl_verify(executable, key, msg, sig)
    return None


def verify_envelope(raw, document=None):
    maximum = CONTRACT['limits'][document + '_envelope_bytes'] if document else ENVELOPE_MAX
    envelope = strict_json(raw, maximum)
    schema(envelope, 'envelope')
    if not isinstance(envelope, dict) or set(envelope) != {'schema', 'key_id', 'payload', 'signature'} or \
            type(envelope.get('schema')) is not int or envelope['schema'] != 1:
        fail('Unsupported signed envelope')
    keys = trusted_keys()
    key_id = envelope['key_id']
    if not isinstance(key_id, str) or key_id not in keys:
        fail('Unknown pinned key id')
    try:
        payload = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ContractError('signature', 'Invalid envelope base64') from exc
    if base64.b64encode(payload).decode('ascii') != envelope['payload'] or base64.b64encode(signature).decode('ascii') != envelope['signature']:
        fail('Noncanonical envelope base64')
    if not payload or len(payload) > ENVELOPE_MAX or len(signature) != 64:
        fail('Invalid signed payload/signature size')
    verifier = _python_verifier() or _openssl_verifier()
    if verifier is None:
        fail('Ed25519 verifier unavailable; install modern OpenSSL or cryptography', 'dependency')
    if not verifier(keys[key_id], payload, signature):
        fail('Ed25519 signature verification failed')
    if document and len(payload) > CONTRACT['limits'][document + '_payload_bytes']:
        fail('Signed payload exceeds document bound')
    result = strict_json(payload)
    if document and (not isinstance(result, dict) or result.get('kind') != CONTRACT['kinds'][document]):
        fail('Signed document kind does not match verification domain')
    return result


def integer(value, maximum, field):
    if type(value) is not int or not 0 < value <= maximum:
        fail('Invalid integer ' + field)
    return value


def text(value, field, maximum=256):
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        fail('Invalid text ' + field)
    return value


def image_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        fail('Invalid image id')
    return value


def digest(value):
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        fail('Invalid SHA-256')
    return value


def schema(value, document, *, origin=None):
    try:
        return validate_shape(value, document, origin=origin)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ContractError('signature', 'Canonical ' + document + ' schema refused: ' + str(exc)) from exc


def utc(value):
    if type(value) is not int or not 0 <= value <= CONTRACT['definitions']['timestamp']['maximum']:
        fail('Invalid integer Unix UTC timestamp')
    return value


def compatibility(row):
    if row['profile'] not in PROFILES or row['strategy'] not in STRATEGIES:
        return 'Unsupported hosted profile or strategy'
    required = row.get('required_capabilities')
    if not isinstance(required, list) or any(not isinstance(c, str) for c in required):
        fail('Invalid required capabilities')
    minimum = row.get('minimum_toolkit_version')
    try:
        check_schema(minimum, CONTRACT['definitions']['semver'])
    except (ValueError, TypeError):
        fail('Invalid minimum toolkit release version')
    if not set(required).issubset(CAPABILITIES):
        return 'Toolkit upgrade required: unsupported capability'
    current = VERSION_RE.fullmatch(TOOLKIT_VERSION) if isinstance(TOOLKIT_VERSION, str) else None
    # Only a reviewed stable toolkit can run. Stable X.Y.Z satisfies X.Y.Z-pre.
    if not current or tuple(map(int, current.groups())) < tuple(map(int, minimum.split('-')[0].split('.'))):
        return 'Toolkit upgrade required: released version not assigned or too old'
    return None


def validate_identity(row):
    if not isinstance(row, dict):
        fail('Unsupported artifact identity')
    image_id(row.get('id'))
    for field in ('base_release', 'profile', 'strategy'):
        text(row.get(field), field)
    try:
        check_schema(row.get('components'), CONTRACT['definitions']['components'])
    except (ValueError, TypeError) as exc:
        raise ContractError('signature', 'Invalid component tuple') from exc


def validate_members(row):
    validate_identity(row)
    try:
        for field in ('members', 'var_policies', 'official_base', 'sources', 'recipe', 'gate_receipts'):
            check_schema(row.get(field), CONTRACT['fields']['metadata'][field])
    except (ValueError, TypeError) as exc:
        raise ContractError('signature', 'Invalid canonical member/provenance contract: ' + str(exc)) from exc
    members = row['members']
    if {m['role'] for m in members} != set(ROLES):
        fail('Member roles must be unique and complete')
    total = 0
    for member in members:
        expected = 'phoenix-' + row['id'] + '/flash_jibo/output/images/' + ROLES[member['role']]
        if member['name'] != expected:
            fail('Member name does not match immutable image id/role')
        geometry = member['geometry']
        if member['size'] % 512 or member['size'] != geometry['block_size'] * geometry['block_count']:
            fail('Member geometry/size mismatch')
        total += member['size']
    if total > UNPACKED_MAX:
        fail('Unpacked filesystem size exceeds bound')
    if row['base_release'] != row['official_base']['release']:
        fail('Official base release conflict')
    if row['profile'] not in PROFILES or row['strategy'] not in STRATEGIES:
        fail('Unsupported hosted profile or strategy', 'incompatible')
    return total


def validate_metadata(row):
    schema(row, 'metadata')
    validate_members(row)
    return row


def validate_manifest(row, selection, *, origin=None):
    schema(row, 'manifest', origin=origin)
    total = validate_members(row)
    for field in set(CONTRACT['fields']['manifest']) & set(CONTRACT['fields']['image']):
        if row[field] != selection.get(field):
            fail('Manifest disagrees with catalog: ' + field)
    if row['unpacked_size'] != total:
        fail('Manifest understates or conflicts with member workspace sizes')
    return row


def hash_file(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


import tarfile


def verify_archive(source, manifest=None):
    """Authenticate embedded metadata and exact member bytes without extraction.

    Returning None means genuinely legacy, not malformed/unsigned Phoenix. A
    signed hosted archive may contain directory headers but no extra payloads,
    links, hooks, device nodes, duplicate paths or unbounded member geometry.
    """
    opener = {'fileobj': source} if hasattr(source, 'read') else {'name': source}
    with tarfile.open(mode='r:*', **opener) as archive:
        entries = []
        for entry in archive:
            if manifest is not None and (len(entries) >= 32 or entry.size > ENVELOPE_MAX + manifest['unpacked_size']):
                fail('Hosted archive header exceeds member/count bound')
            entries.append(entry)
        markers = [m for m in entries if PurePosixPath(m.name).name == 'phoenix-flash.json']
        if not markers:
            if manifest is not None:
                fail('Hosted archive is missing signed Phoenix metadata')
            return None
        if len(markers) != 1 or not markers[0].isfile() or not 0 < markers[0].size <= ENVELOPE_MAX:
            fail('Invalid or duplicate Phoenix metadata member')
        raw = archive.extractfile(markers[0]).read(ENVELOPE_MAX + 1)
        metadata = validate_metadata(verify_envelope(raw, 'metadata'))
        expected_marker = 'phoenix-' + metadata['id'] + '/phoenix-flash.json'
        if markers[0].name != expected_marker:
            fail('Phoenix metadata path disagrees with signed image id')
        if manifest is not None:
            for field in set(CONTRACT['fields']['metadata']) - {'kind'}:
                if metadata.get(field) != manifest.get(field):
                    fail('Embedded metadata disagrees with signed manifest: ' + field)
            if hashlib.sha256(raw).hexdigest() != manifest['embedded_metadata_sha256']:
                fail('Embedded metadata hash disagrees with signed manifest', 'hash')
        allowed = {m['name']: m for m in metadata['members']}
        seen = set()
        if len(entries) > 32:
            fail('Hosted archive has too many members')
        for member in entries:
            if member.name in seen or member.name.startswith('/') or '..' in PurePosixPath(member.name).parts or \
                    '\\' in member.name or '%' in member.name:
                fail('Unsafe or duplicate hosted archive member')
            seen.add(member.name)
            if member.name == expected_marker:
                continue
            if member.isdir():
                if not any(name.startswith(member.name.rstrip('/') + '/') for name in (*allowed, expected_marker)):
                    fail('Unexpected hosted archive directory')
                continue
            expected = allowed.get(member.name)
            if not expected or not member.isfile() or member.size != expected['size']:
                fail('Unexpected hosted archive member/type/size')
            result = hashlib.sha256()
            remaining = member.size
            with archive.extractfile(member) as stream:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        fail('Truncated hosted archive member', 'hash')
                    result.update(chunk)
                    remaining -= len(chunk)
            if result.hexdigest() != expected['sha256']:
                fail('Hosted filesystem member SHA-256 mismatch', 'hash')
        if not set(allowed).issubset(seen):
            fail('Missing signed hosted filesystem member')
        return {**metadata, '_metadata_sha256': hashlib.sha256(raw).hexdigest()}


def workspace_bytes(manifest, var_capacity=524288000):
    total = validate_members(manifest)
    sizes = {member['role']: member['size'] for member in manifest['members']}
    largest = max(sizes.values())
    # In addition to extraction/copies, rollback and readback, keep-calibration
    # may create distinct candidates for BOTH root slots and the skills image.
    calibration_copies = 2 * sizes['rootfs'] + sizes['skills']
    return max(manifest.get('workspace_bytes', 0),
               2 * total + 3 * var_capacity + max(largest, var_capacity) +
               calibration_copies + 1024**3)
