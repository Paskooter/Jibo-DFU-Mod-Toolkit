"""Pure, exact-byte calibration helpers. No USB, network or robot writes.

Only the frozen v3 profile is eligible for sharing. Private backup images and
unknown formats remain untouched. Format validation is not factory provenance
or a claim that measurements are mechanically safe.
"""
from dataclasses import dataclass
from datetime import datetime
import math
import base64
import hashlib
import json
import re
from typing import NoReturn
from pathlib import Path, PurePosixPath
import tempfile

import jibo_images as images

PROFILE = "jibo-factory-calibration-v3"


@dataclass(frozen=True)
class ItemSpec:
    key: str
    partition: str
    image_path: str
    presence: str
    cap: int


ITEMS = (
    ItemSpec("left", "var", "/jibo/lps/CameraModelParamsL.json", "required", 2048),
    ItemSpec("right", "var", "/jibo/lps/CameraModelParamsR.json", "required", 2048),
    ItemSpec("inter_camera", "var", "/jibo/lps/InterCameraTransform.json", "required", 2048),
    ItemSpec("imu", "var", "/jibo/imu/imu-cal.json", "expected", 512),
    ItemSpec("audio", "var", "/jibo/audio/audio_calibration.json", "expected", 256),
    ItemSpec("touchscreen", "rootfs", "/etc/pointercal", "conditional", 128),
    ItemSpec("factory_record", "skills", "/mfg/calibration_manifest.json", "conditional", 512),
)


class CalibrationError(Exception):
    """Unsupported calibration, without echoing private file contents."""


_SPECS = {item.key: item for item in ITEMS}
_CAMERA_FIELDS = frozenset((
    "camera_id", "version", "frame_name", "distortion_precompute_filename",
    "image_width", "image_height", "center_x", "center_y", "scale_x", "scale_y",
    "focal_length_x", "focal_length_y", "k1", "k2", "k3", "k4", "k5", "skew",
))
_NUMBER = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_STOCK_HASHES = {
    "left": "72a1fbdef047873c67167cf416555981b25faaafed0848b9718abd0d9a85dee7",
    "right": "cd0ede42243fbf7cb883b6aa79d44667823cb9b2ddc2c0cbda1016a39654672d",
    "inter_camera": "26ef25c2fabe6313803ce9b91b1b03c8274cd5764be4849a6b3ad324233ca810",
}


def _unsupported() -> NoReturn:
    raise CalibrationError("Kept local (unsupported format).")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _unsupported()
        result[key] = value
    return result


def _json(text):
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=lambda _value: _unsupported())
    except (ValueError, RecursionError):
        _unsupported()


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        _unsupported()


def _number(value):
    if type(value) not in (int, float):
        _unsupported()
    try:
        if not math.isfinite(value):
            _unsupported()
    except OverflowError:
        _unsupported()


def _camera(key, value):
    _keys(value, _CAMERA_FIELDS)
    side, camera_id = ("left_eye", 1) if key == "left" else ("right_eye", 0)
    for field, expected in (("camera_id", camera_id), ("image_width", 1280), ("image_height", 720)):
        if type(value[field]) is not int or value[field] != expected:
            _unsupported()
    if value["frame_name"] != side or value["distortion_precompute_filename"] != \
            "/var/jibo/lps/distortionPrecomputeFile_{}_{}".format(side, camera_id):
        _unsupported()
    # 0.1 is the frozen v3 vector; 1.0.0 is the distributed stock template.
    if value["version"] not in ("0.1", "1.0.0"):
        _unsupported()
    for field in _CAMERA_FIELDS - {"camera_id", "image_width", "image_height", "frame_name",
                                  "distortion_precompute_filename", "version"}:
        _number(value[field])
        if abs(value[field]) > 1000000:
            _unsupported()
    if not (0 <= value["center_x"] < 1280 and 0 <= value["center_y"] < 720 and
            value["focal_length_x"] > 0 and value["focal_length_y"] > 0):
        _unsupported()


def _audio(text):
    # Shell echo removed quotes from historical factory output. Recognize only
    # these tokens, never eval, general JS/Python, or a quote-repair serializer.
    for quoted in (False, True):
        key = r'"mic[1-6]"' if quoted else r"mic[1-6]"
        entry = key + r": " + _NUMBER
        if re.fullmatch(r"\{" + entry + r"(?:, " + entry + r"){5}\}\n?", text):
            tokens = re.findall(r"(" + key + r"): (" + _NUMBER + r")", text)
            if {name.strip('"') for name, _value in tokens} != {"mic" + str(i) for i in range(1, 7)}:
                _unsupported()
            for _name, number in tokens:
                _number(_json(number))
            return
    _unsupported()


def _factory_record(value):
    # Profile pin: manufacturing/automation_scripts@6ea44333f8707d140050d864c80ff1ceb96614bb
    # src/calibration_file_check.py:19,99-114,178 (empty and 12-digit demo);
    # src/mainboard_test.py:35,408-410 (False initializer, IMU updates);
    # src/camera_test.py:377-381; src/jibo_test_core.py:433;
    # src/jibo_test_shared.py:17,26-28 (14-digit production date, PASS/FAIL).
    _keys(value, ("imu", "camera"))
    for record in value.values():
        _keys(record, ("timedate", "passfail"))
        date, result = record["timedate"], record["passfail"]
        if not isinstance(date, str) or result not in ("", "False", "PASS", "FAIL"):
            _unsupported()
        if date:
            if not re.fullmatch(r"(?:[0-9]{12}|[0-9]{14})", date):
                _unsupported()
            try:
                datetime.strptime(date, "%Y%m%d%H%M%S" if len(date) == 14 else "%y%m%d%H%M%S")
            except ValueError:
                _unsupported()


def validate_item(key: str, data: bytes) -> bytes:
    spec = _SPECS.get(key)
    if spec is None or not isinstance(data, bytes) or not data or len(data) > spec.cap:
        _unsupported()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _unsupported()
    if text.startswith("\ufeff"):
        _unsupported()
    if key == "audio":
        _audio(text)
    elif key == "touchscreen":
        integer = r"[+-]?[0-9]+"
        if not re.fullmatch(integer + r"(?: " + integer + r"){6}(?: " + integer + r"){0,2}\n?", text) or \
                len(text.rstrip("\n").split(" ")) not in (7, 9):
            _unsupported()
    else:
        value = _json(text)
        if key in ("left", "right"):
            _camera(key, value)
        elif key == "inter_camera":
            _keys(value, ("Transform",))
            if not isinstance(value["Transform"], list) or len(value["Transform"]) != 16:
                _unsupported()
            for number in value["Transform"]:
                _number(number)
        elif key == "imu":
            _keys(value, ("icm20608",))
            _keys(value["icm20608"], ("accel_bias", "gyro_bias"))
            for bias in value["icm20608"].values():
                _keys(bias, ("x_axis", "y_axis", "z_axis"))
                for number in bias.values():
                    _number(number)
        elif key == "factory_record":
            _factory_record(value)
    return data


@dataclass
class ExtractResult:
    files: dict[str, bytes]
    status: dict[str, str]


def _image_stat(image, path):
    output = images._run_debugfs(image, "stat " + path, allow_missing=True)
    if re.search(r"File not found|ext2_lookup", output, re.IGNORECASE):
        return None
    kind = re.search(r"\bType:\s+(\w+)", output)
    size = re.search(r"\bSize:\s+(\d+)", output)
    if not kind or not size:
        raise CalibrationError("Could not inspect the calibration partition image.")
    return kind.group(1).lower(), int(size.group(1))


def _read_item(image, spec, destination):
    """Check EVERY inode on the fixed path before dumping; cap before reading."""
    for parent in reversed(PurePosixPath(spec.image_path).parents):
        stat = _image_stat(image, str(parent))
        if stat is None:
            return None, "absent"
        if stat[0] != "directory":
            return None, "kept_local_unsupported"
    stat = _image_stat(image, spec.image_path)
    if stat is None:
        return None, "absent"
    if stat[0] != "regular" or stat[1] > spec.cap:
        return None, "kept_local_unsupported"
    data = images._extract(image, spec.image_path, destination)
    if data is None or len(data) != stat[1]:
        raise CalibrationError("Calibration extraction did not match the inode size.")
    try:
        validate_item(spec.key, data)
    except CalibrationError:
        return None, "kept_local_unsupported"
    return data, "present"


def extract(var_image, rootfs_image=None, skills_image=None) -> ExtractResult:
    """Read only fixed profile paths from unmounted images; never repair them."""
    partitions = {"var": var_image, "rootfs": rootfs_image, "skills": skills_image}
    try:
        for partition, image in partitions.items():
            if image is None:
                if partition == "var":
                    raise CalibrationError("A var partition image is required.")
                continue
            image = Path(image)
            if image.is_symlink() or not image.is_file():
                raise CalibrationError("Calibration partition images must be regular files.")
            partitions[partition] = image
        files, status = {}, {}
        with tempfile.TemporaryDirectory(prefix="jibo-calibration-") as temp:
            for spec in ITEMS:
                image = partitions[spec.partition]
                if image is None:
                    status[spec.key] = "not_checked"
                    continue
                data, status[spec.key] = _read_item(image, spec, Path(temp) / spec.key)
                if data is not None:
                    files[spec.key] = data
        return ExtractResult(files, status)
    except (images.ImageError, OSError, TypeError, ValueError) as exc:
        raise CalibrationError("Could not safely extract the calibration partition image.") from exc


def stock_equivalent(files) -> dict[str, bool]:
    """Only the three items with pinned stock hashes are comparable."""
    return {key: hashlib.sha256(files[key]).hexdigest() == digest
            for key, digest in _STOCK_HASHES.items() if key in files}


def canonical_bundle(files: dict[str, bytes]) -> bytes:
    if not isinstance(files, dict) or any(key not in {item.key for item in ITEMS} for key in files):
        raise CalibrationError("Unknown calibration item.")
    if any(not isinstance(data, bytes) for data in files.values()):
        raise CalibrationError("Calibration items must be exact bytes.")
    return json.dumps({"files": {key: base64.b64encode(data).decode("ascii")
                                for key, data in files.items()}},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def candidate_sha256(files: dict[str, bytes]) -> str:
    return hashlib.sha256(canonical_bundle(files)).hexdigest()


def robot_key_from_cid(cid_serial: str) -> str:
    if not isinstance(cid_serial, str) or not re.fullmatch(r"[0-9a-f]{32}", cid_serial) or \
            cid_serial == "0" * 32:
        raise CalibrationError("A nonzero, lowercase 32-hex eMMC CID is required.")
    return hashlib.sha256(b"jibo-calibration:emmc-cid:v1\x00" + cid_serial.encode("ascii")).hexdigest()
