"""Calibration fixtures are synthetic; never copy robot data into these tests."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

VECTOR = json.loads((Path(__file__).parent / "fixtures/cal-v3-vectors.json").read_text())
FILES = {key: base64.b64decode(value, validate=True) for key, value in VECTOR["files_b64"].items()}


class CalibrationVectorTests(unittest.TestCase):
    def test_frozen_v3_vectors(self):
        self.assertIsNotNone(importlib.util.find_spec("jibo_calibration"), "frozen v3 API is missing")
        import jibo_calibration as cal
        for key, data in FILES.items():
            self.assertEqual(hashlib.sha256(data).hexdigest(), VECTOR["file_sha256"][key])
        self.assertEqual(cal.canonical_bundle(FILES), VECTOR["canonical_bundle_utf8"].encode("ascii"))
        self.assertEqual(cal.candidate_sha256(FILES), VECTOR["candidate_sha256"])
        self.assertEqual(cal.robot_key_from_cid(VECTOR["cid_serial"]), VECTOR["robot_key"])


if __name__ == "__main__":
    unittest.main()
