"""The toolkit is licensed GPL-3.0-only; the license must be present and must travel with every build."""
import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# sha256 of the canonical text published at https://www.gnu.org/licenses/gpl-3.0.txt
GPL3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"


class LicenseTests(unittest.TestCase):
    def test_license_is_the_unmodified_gpl_3_text(self):
        data = (ROOT / "LICENSE").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), GPL3_SHA256)

    def test_notice_states_gpl_3_only_and_keeps_third_party_terms(self):
        notice = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
        self.assertIn("GPL-3.0-only", notice)
        self.assertIn("Copyright (C) 2026 Paskooter and contributors", notice)
        for path in ("vendor/jibo-ram-dfu-v1-source.tar.gz", "firmware/*.patch",
                     "patches/shofel2-dfu-entry.patch", "assets/boot-network-status/startup-view.js"):
            self.assertIn(path, notice, path)
        for listed in re.findall(r"`((?:vendor|firmware|patches|assets)/[^`*]+)`", notice):
            if not listed.endswith("/"):
                self.assertTrue((ROOT / listed).exists(), listed)

    def test_readme_names_the_license(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## License", 1)
        self.assertEqual(len(section), 2, "README needs a License section")
        self.assertIn("GPL-3.0", section[1])
        self.assertIn("(LICENSE)", section[1])
        self.assertIn("(NOTICE.md)", section[1])


if __name__ == "__main__":
    unittest.main()
