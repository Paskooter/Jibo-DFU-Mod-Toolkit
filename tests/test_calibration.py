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


class CalibrationGrammarTests(unittest.TestCase):
    def setUp(self):
        import jibo_calibration
        self.cal = jibo_calibration

    def reject(self, key, data):
        with self.assertRaises(self.cal.CalibrationError):
            self.cal.validate_item(key, data)

    def test_all_supported_grammars_return_exact_bytes(self):
        extra = {
            "touchscreen": b"1 0 -3 0 +1 4 65536 1280 720\n",
            "factory_record": b'{"imu":{"timedate":"20261013010203","passfail":"PASS"},'
                              b'"camera":{"timedate":"","passfail":"False"}}\n',
        }
        for key, data in {**FILES, **extra}.items():
            with self.subTest(key=key):
                self.assertIs(self.cal.validate_item(key, data), data)
        for data in (b"1 0 0 0 1 0 65536", b"1 0 0 0 1 0 65536\n"):
            self.assertEqual(self.cal.validate_item("touchscreen", data), data)
        quoted = b'{"mic1": -1e-2, "mic2": 0, "mic3": 2.5, "mic4": 4, "mic5": 5, "mic6": 6}\n'
        self.assertEqual(self.cal.validate_item("audio", quoted), quoted)
        camera = json.loads(FILES["left"])
        camera["version"] = "1.0.0"
        camera["scale_x"], camera["scale_y"] = 250000, -250000
        self.cal.validate_item("left", json.dumps(camera).encode())

    def test_audio_rejects_duplicates_missing_keys_and_non_number_tokens(self):
        for data in (
            FILES["audio"].replace(b"mic6:", b"mic1:"),
            FILES["audio"].replace(b"mic6: 1.5, ", b""),
            FILES["audio"].replace(b"1.5", b"NaN"),
            FILES["audio"].replace(b"1.5", b"Infinity"),
            FILES["audio"].replace(b"1.5", b"1e999"),
            FILES["audio"].replace(b"1.5", b"true"),
            FILES["audio"].replace(b"1.5", b"01.5"),
            FILES["audio"].replace(b"1.5", b"+1.5"),
            FILES["audio"].replace(b"1.5", b'"secret"'),
            FILES["audio"].replace(b", ", b",  "),
            FILES["audio"].replace(b": ", b":\t"),
            FILES["audio"] + b"\n",
            FILES["audio"] + b"junk",
            b'{"mic1": 1, "mic1": 2, "mic2": 2, "mic3": 3, "mic4": 4, "mic5": 5, "mic6": 6}',
        ):
            with self.subTest(data=data):
                self.reject("audio", data)

    def test_json_shapes_constants_duplicates_and_encoding_are_strict(self):
        for key, data in FILES.items():
            for bad in (b"\xef\xbb\xbf" + data, b"\xff" + data, data + b"junk"):
                self.reject(key, bad)
        for key in ("left", "right"):
            camera = json.loads(FILES[key])
            for field, value in (("camera_id", True), ("camera_id", 9), ("version", "secret"),
                                 ("image_width", "1280"), ("image_height", 1),
                                 ("frame_name", "secret"), ("distortion_precompute_filename", "/etc/secret"),
                                 ("k1", "1"), ("k2", None), ("scale_x", False), ("k3", float("nan"))):
                self.reject(key, json.dumps({**camera, field: value}).encode())
            self.reject(key, json.dumps({**camera, "private": "secret"}).encode())
            self.reject(key, FILES[key].replace(b'"camera_id":', b'"camera_id": 1, "camera_id":', 1))
        self.reject("inter_camera", b'{"Transform":[1,2]}')
        self.reject("inter_camera", json.dumps({"Transform": [True] * 16}).encode())
        self.reject("inter_camera", json.dumps({"Transform": [float("inf")] * 16}).encode())
        self.reject("imu", FILES["imu"].replace(b'"x_axis": 0.01', b'"x_axis": 0.01, "x_axis": 1'))
        self.reject("imu", FILES["imu"].replace(b'"x_axis": 0.01', b'"x_axis": true'))
        self.reject("imu", FILES["imu"].replace(b'"gyro_bias"', b'"private"'))
        self.reject("identity", b'{}')

    def test_each_item_enforces_its_byte_cap(self):
        for item in self.cal.ITEMS:
            self.reject(item.key, b" " * (item.cap + 1))
        self.reject("audio", "not bytes")

    def test_touchscreen_and_factory_record_reject_arbitrary_text(self):
        for data in (b"1 2 3 4 5 6", b"1 2 3 4 5 6 7 8", b"1 2 3 4 5 6 7 8 9 10",
                     b"1 2 3 4 5 6 7 junk", b"1\t2 3 4 5 6 7", b"1  2 3 4 5 6 7",
                     b"1 2 3 4 5 6 7\n\n", b"1.0 2 3 4 5 6 7"):
            self.reject("touchscreen", data)
        record = {"imu": {"timedate": "20261013010203", "passfail": "PASS"},
                  "camera": {"timedate": "", "passfail": ""}}
        for field, value in (("timedate", "20261399000000"), ("timedate", "private"),
                             ("passfail", "private"), ("passfail", True)):
            bad = {**record, "imu": {**record["imu"], field: value}}
            self.reject("factory_record", json.dumps(bad).encode())
        self.reject("factory_record", json.dumps({**record, "serial": "private"}).encode())

    def test_cid_is_not_normalized_and_stock_comparison_is_per_pinned_item(self):
        for cid in ("0" * 32, "A" * 32, VECTOR["cid_serial"] + "\n", " " + VECTOR["cid_serial"],
                    "1" * 31, "g" * 32, None):
            with self.assertRaises(self.cal.CalibrationError):
                self.cal.robot_key_from_cid(cid)
        self.assertEqual(self.cal.stock_equivalent(FILES), {"left": False, "right": False, "inter_camera": False})
        with self.assertRaises(self.cal.CalibrationError):
            self.cal.canonical_bundle({"identity": b"private"})


if __name__ == "__main__":
    unittest.main()
