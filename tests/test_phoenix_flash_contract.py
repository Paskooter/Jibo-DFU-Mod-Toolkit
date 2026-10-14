from pathlib import Path
import unittest
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
import jibo_updates as u
import jibo_flash_contract as c


class PhoenixContractTests(HostedFixture):
    def test_replaced_path_does_not_replace_authenticated_descriptor(self):
        import os
        path = self.cache.parent / 'synthetic.tar.bz2'
        path.write_bytes(self.server.files[self.row['url'].removeprefix(self.server.origin)])
        package = u.validate_package(path)
        metadata = package.phoenix
        assert metadata is not None
        original = c.verify_archive
        calls = []

        def verify_then_replace(source, *args, **kwargs):
            result = original(source, *args, **kwargs)
            self.assertTrue(hasattr(source, 'fileno'))
            replacement = path.with_name('replacement')
            replacement.write_bytes(b'SYNTHETIC INVALID REPLACEMENT')
            os.replace(replacement, path)
            calls.append(True)
            return result

        with patch.object(c, 'verify_archive', side_effect=verify_then_replace):
            with u._package_images(package, workdir=self.cache.parent) as extracted:
                for member in metadata['members']:
                    self.assertEqual(c.hash_file(extracted[c.ROLES[member['role']]]),
                                     member['sha256'])
        self.assertEqual(calls, [True])

    def test_extracted_marked_directory_cannot_bypass_marker_policy(self):
        import tarfile
        path = self.cache.parent / 'synthetic.tar.bz2'
        path.write_bytes(self.server.files[self.row['url'].removeprefix(self.server.origin)])
        extracted = self.cache.parent / 'extracted'
        extracted.mkdir()
        with tarfile.open(path) as archive:
            archive.extractall(extracted, filter='data')
        package = u.validate_package(extracted)
        self.assertEqual(package.phoenix['id'], self.row['id'])
        marker = extracted / ('phoenix-' + self.row['id']) / 'phoenix-flash.json'
        marker.write_bytes(b'{"kind":"unsigned-phoenix"}')
        with self.assertRaises(u.UpdateError):
            u.validate_package(extracted)

    def test_optional_signed_metadata_authenticated_member_set(self):
        path = self.cache.parent / 'synthetic.tar.bz2'
        path.write_bytes(self.server.files[self.row['url'].removeprefix(self.server.origin)])
        package = u.validate_package(path)
        self.assertEqual(package.phoenix['id'], self.row['id'])
        self.assertEqual(package.version, self.row['id'])

    def test_manifest_member_sum_understatement_and_fresh_policy_refused(self):
        manifest = dict(self.manifest)
        manifest['unpacked_size'] = 1
        with self.assertRaises(c.ContractError):
            c.validate_manifest(manifest, self.row)
        manifest['unpacked_size'] = self.manifest['unpacked_size']
        manifest['var_policies'] = ['preserve', 'fresh-with-calibration']
        with self.assertRaises(c.ContractError):
            c.validate_manifest(manifest, self.row)

    def test_mutated_member_and_unsigned_metadata_cannot_be_legacy_package(self):
        import io
        import tarfile
        archive = self.server.files[self.row['url'].removeprefix(self.server.origin)]
        for change in ('member', 'unsigned'):
            with self.subTest(change=change):
                path = self.cache.parent / (change + '.tar.bz2')
                with tarfile.open(fileobj=io.BytesIO(archive), mode='r:bz2') as source, \
                        tarfile.open(path, 'w:bz2') as output:
                    for member in source:
                        raw = source.extractfile(member).read()
                        if change == 'member' and member.name.endswith('skills.ext4'):
                            raw = b'x' * len(raw)
                        if change == 'unsigned' and member.name.endswith('phoenix-flash.json'):
                            raw = b'{"kind":"phoenix-full-flash-v1"}'
                            member.size = len(raw)
                        output.addfile(member, io.BytesIO(raw))
                with self.assertRaises(u.UpdateError):
                    u.validate_package(path)


if __name__ == '__main__':
    unittest.main()
