"""Closed canonical schema and cross-document bindings; signed negative inputs."""
import base64
import copy
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import patch

import pytest
import jibo_dfu as dfu
import jibo_flash_contract as c
import jibo_hosted_updates as h
import jibo_updates as u
from test_hosted_catalog import HostedFixture


@pytest.fixture(scope='module')
def canonical():
    fixture = HostedFixture()
    fixture.setUp()
    try:
        raw = fixture.server.files[fixture.row['url'].removeprefix(fixture.server.origin)]
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            marker = next(m for m in archive if m.name.endswith('/phoenix-flash.json'))
            fixture.metadata = c.verify_envelope(archive.extractfile(marker).read(), 'metadata')
        yield fixture
    finally:
        fixture.doCleanups()


def admit(fixture, document, value):
    envelope = fixture.server.envelope(value)
    if document == 'index':
        return h._index(envelope, None)
    if document == 'manifest':
        return c.validate_manifest(c.verify_envelope(envelope, 'manifest'), fixture.row, origin=fixture.server.origin)
    return c.validate_metadata(c.verify_envelope(envelope, 'metadata'))


def document_value(fixture, document):
    return copy.deepcopy(getattr(fixture, 'index' if document == 'index' else 'manifest' if document == 'manifest' else 'metadata'))


MISSING = [(document, field) for document in ('index', 'manifest', 'metadata')
           for field in c.CONTRACT['fields'][document]]


@pytest.mark.parametrize('document,field', MISSING)
def test_every_required_field_is_required(canonical, document, field):
    value = document_value(canonical, document)
    del value[field]
    with pytest.raises(c.ContractError):
        admit(canonical, document, value)


@pytest.mark.parametrize('document', ['index', 'manifest', 'metadata'])
@pytest.mark.parametrize('schema', [0, 2, True, '1', None])
def test_wrong_schema_version_is_never_coerced(canonical, document, schema):
    value = document_value(canonical, document)
    value['schema'] = schema
    with pytest.raises(c.ContractError):
        admit(canonical, document, value)


@pytest.mark.parametrize('document', ['index', 'manifest', 'metadata'])
@pytest.mark.parametrize('kind', ['phoenix-full-flash-v1', 'phoenix-flash-index-v1',
                                  'phoenix-flash-metadata-v1', 'phoenix-flash-manifest-v1'])
def test_signature_domain_cannot_be_swapped(canonical, document, kind):
    value = document_value(canonical, document)
    if kind == c.CONTRACT['kinds'][document]:
        assert admit(canonical, document, value) == value
    else:
        value['kind'] = kind
        with pytest.raises(c.ContractError):
            admit(canonical, document, value)


@pytest.mark.parametrize('document', ['index', 'manifest', 'metadata'])
def test_unknown_fields_are_refused(canonical, document):
    value = document_value(canonical, document)
    value['unexpected'] = True
    with pytest.raises(c.ContractError):
        admit(canonical, document, value)


@pytest.mark.parametrize('field', sorted(set(c.CONTRACT['fields']['manifest']) & set(c.CONTRACT['fields']['image'])))
def test_all_index_manifest_shared_fields_are_bound(canonical, field):
    selection = copy.deepcopy(canonical.row)
    selection[field] = None  # Keep signed manifest valid; mismatch the selected signed row.
    with pytest.raises(c.ContractError):
        c.validate_manifest(canonical.manifest, selection, origin=canonical.server.origin)


@pytest.mark.parametrize('change', ['legacy-kind', 'legacy-policy', 'version-added', 'duplicate-role', 'member-name',
    'member-hash', 'member-size', 'geometry-count', 'geometry-extra', 'member-extra', 'base-conflict', 'missing-source', 'empty-gates'])
def test_malformed_embedded_contract_refused(canonical, change):
    value = copy.deepcopy(canonical.metadata)
    if change == 'legacy-kind': value['kind'] = 'phoenix-full-flash-v1'
    elif change == 'legacy-policy': value['var_policies'] = ['preserve']
    elif change == 'version-added': value['version'] = 'invented-version'
    elif change == 'duplicate-role': value['members'][1] = copy.deepcopy(value['members'][0])
    elif change == 'member-name': value['members'][0]['name'] = '../rootfs.ext4'
    elif change == 'member-hash': value['members'][0]['sha256'] = 'A' * 64
    elif change == 'member-size': value['members'][0]['size'] = True
    elif change == 'geometry-count': value['members'][0]['geometry']['block_count'] += 1
    elif change == 'geometry-extra': value['members'][0]['geometry']['extra'] = 1
    elif change == 'member-extra': value['members'][0]['extra'] = 1
    elif change == 'base-conflict': value['official_base']['release'] = 'wrong-base'
    elif change == 'missing-source': del value['sources']['be_commit']
    elif change == 'empty-gates': value['gate_receipts'] = {}
    with pytest.raises(c.ContractError):
        admit(canonical, 'metadata', value)


@pytest.mark.parametrize('minimum', ['1', 'v1.0.0', '01.0.0', 1, True, None, '1.0.0+wrong'])
def test_malformed_minimum_toolkit_version_refused(canonical, minimum):
    value = copy.deepcopy(canonical.index)
    value['images'][0]['minimum_toolkit_version'] = minimum
    with pytest.raises(c.ContractError):
        admit(canonical, 'index', value)


@pytest.mark.parametrize('version', [None, '0.9.9', '1.0.0-dev', 'garbage'])
def test_unassigned_old_or_unreleased_toolkit_is_not_installable(canonical, version):
    with patch.object(c, 'TOOLKIT_VERSION', version):
        assert c.compatibility(canonical.row) is not None


@pytest.mark.parametrize('change', ['signature', 'duplicate-json', 'float-json', 'noncanonical-base64', 'key-id', 'envelope-version'])
def test_malformed_envelope_refused(canonical, change):
    raw = canonical.server.envelope(canonical.metadata)
    value = json.loads(raw)
    if change == 'signature': value['signature'] = base64.b64encode(bytes(64)).decode()
    elif change == 'key-id': value['key_id'] = 'attacker-key'
    elif change == 'envelope-version': value['schema'] = True
    elif change == 'noncanonical-base64': value['payload'] += '='
    elif change == 'duplicate-json':
        raw = canonical.server.envelope(b'{"schema":1,"schema":1}')
    elif change == 'float-json':
        raw = canonical.server.envelope(b'{"schema":1.0}')
    if change not in ('duplicate-json', 'float-json'):
        raw = json.dumps(value).encode()
    with pytest.raises(c.ContractError):
        c.verify_envelope(raw, 'metadata')


@pytest.mark.parametrize('change', ['tampered-member', 'tampered-marker', 'missing-marker', 'duplicate-marker',
    'extra-file', 'link', 'traversal', 'missing-member', 'wrong-kind', 'wrong-version'])
def test_bad_archives_never_reach_usb(canonical, tmp_path, change):
    raw = canonical.server.files[canonical.row['url'].removeprefix(canonical.server.origin)]
    path = tmp_path / 'bad.tar.bz2'
    with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(path, 'w:bz2') as output:
        for member in source:
            data = source.extractfile(member).read()
            marker = member.name.endswith('/phoenix-flash.json')
            if change == 'missing-marker' and marker: continue
            if change == 'missing-member' and member.name.endswith('rootfs.ext4'): continue
            if change == 'tampered-member' and not marker: data = b'x' * len(data)
            if change == 'tampered-marker' and marker: data = b'{}'
            if change in ('wrong-kind', 'wrong-version') and marker:
                metadata = copy.deepcopy(canonical.metadata)
                metadata['kind' if change == 'wrong-kind' else 'schema'] = 'phoenix-full-flash-v1' if change == 'wrong-kind' else 2
                data = canonical.server.envelope(metadata)
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
            if change == 'duplicate-marker' and marker:
                output.addfile(member, io.BytesIO(data))
        if change in ('extra-file', 'link', 'traversal'):
            member = tarfile.TarInfo('../escape' if change == 'traversal' else 'extra')
            if change == 'link':
                member.type, member.linkname = tarfile.SYMTYPE, '/etc/passwd'
            output.addfile(member)
    # A signed manifest makes even a missing marker a hard failure, not legacy.
    with pytest.raises(c.ContractError):
        c.verify_archive(path, canonical.manifest)
    if change != 'missing-marker':
        with patch.object(dfu, 'devices', side_effect=AssertionError('bad archive reached USB')):
            with pytest.raises((u.UpdateError, dfu.DfuError)):
                dfu.flash_update(path, True)


def test_maximum_valid_catalog_keeps_local_provenance_readable(canonical, tmp_path):
    index = copy.deepcopy(canonical.index)
    for i in range(65):
        row = copy.deepcopy(canonical.row)
        row.update(id='synthetic-older-' + str(i), notes='n' * 1800)
        row['url'] = canonical.server.origin + '/flash/images/' + row['id'] + '/' + row['sha256'] + '.tar.bz2'
        row['manifest_url'] = canonical.server.origin + '/flash/manifests/' + row['id'] + '.json'
        index['images'].append(row)
    raw = canonical.server.envelope(index)
    assert len(raw) <= c.INDEX_MAX
    original = canonical.server.files['/flash/index.json']
    canonical.server.files['/flash/index.json'] = raw
    try:
        path = h.download_image('latest', dest_dir=tmp_path / 'download')
        assert Path(path + '.provenance.json').stat().st_size > c.ENVELOPE_MAX
        assert h.verify_local_image(path)['image'].id == canonical.row['id']
    finally:
        canonical.server.files['/flash/index.json'] = original
