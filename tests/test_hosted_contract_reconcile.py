"""Canonical publisher wire admission (no legacy schema aliases)."""
from pathlib import Path
import jibo_flash_contract as c
import jibo_hosted_updates as h
import jibo_tui
import jibo_updates as u
from test_hosted_catalog import HostedFixture


class CanonicalAdmissionTests(HostedFixture):
    def test_canonical_index_download_archive_and_first_choice(self):
        catalog = h.fetch_catalog(cache_dir=self.cache)
        items, choices = jibo_tui.hosted_chooser_items(catalog, (), [Path('official.tar.bz2')])
        assert items[0].key == 'hosted:' + self.row['id']
        assert items[0].enabled
        path = h.download_image(self.row['id'], dest_dir=self.cache.parent / 'downloads')
        package = u.validate_package(path)
        assert package.phoenix is not None
        assert package.phoenix['kind'] == 'phoenix-flash-metadata-v1'
        assert package.phoenix['var_policies'] == ['keep-calibration', 'without-calibration']
        assert 'version' not in package.phoenix
        assert package.version == self.row['id']  # local-only display uses signed identity, not guessed release
        assert h.local_hosted_images(self.cache.parent / 'downloads')[0].id == self.row['id']

    def test_canonical_metadata_alone_is_admitted(self):
        path = self.cache.parent / 'canonical.tar.bz2'
        path.write_bytes(self.server.files[self.row['url'].removeprefix(self.server.origin)])
        assert c.verify_archive(path)['kind'] == 'phoenix-flash-metadata-v1'
