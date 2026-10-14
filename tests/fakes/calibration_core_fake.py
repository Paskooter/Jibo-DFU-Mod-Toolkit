"""Frozen API fake for synthetic bytes ONLY; never used by production."""
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

PROFILE = "jibo-factory-calibration-v3"
VECTOR = json.loads(Path(__file__).with_name("cal_v3_vectors.json").read_text())
FILES = {key: base64.b64decode(value, validate=True) for key, value in VECTOR["files_b64"].items()}

@dataclass(frozen=True)
class ItemSpec:
    key: str
    partition: str
    image_path: str
    presence: str
    cap: int

ITEMS = tuple(ItemSpec(k, p, path, presence, cap) for k, p, path, presence, cap in (
    ("left", "var", "/jibo/lps/CameraModelParamsL.json", "required", 2048),
    ("right", "var", "/jibo/lps/CameraModelParamsR.json", "required", 2048),
    ("inter_camera", "var", "/jibo/lps/InterCameraTransform.json", "required", 2048),
    ("imu", "var", "/jibo/imu/imu-cal.json", "expected", 512),
    ("audio", "var", "/jibo/audio/audio_calibration.json", "expected", 256),
    ("touchscreen", "rootfs", "/etc/pointercal", "conditional", 128),
    ("factory_record", "skills", "/mfg/calibration_manifest.json", "conditional", 512)))

class CalibrationError(Exception):
    pass

def validate_item(key, data):
    if key not in FILES or data != FILES[key]:
        raise CalibrationError("Unsupported synthetic item")
    return data

def extract(var_image, rootfs_image=None, skills_image=None):
    return SimpleNamespace(files=dict(FILES), status={s.key: "present" if s.key in FILES else "not_checked" for s in ITEMS})

def canonical_bundle(files):
    return json.dumps({"files": {k: base64.b64encode(v).decode("ascii") for k, v in files.items()}}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

def candidate_sha256(files):
    return hashlib.sha256(canonical_bundle(files)).hexdigest()

def robot_key_from_cid(cid_serial):
    if not isinstance(cid_serial, str) or not re.fullmatch("[0-9a-f]{32}", cid_serial) or cid_serial == "0" * 32:
        raise CalibrationError("Unsupported hardware ID")
    return hashlib.sha256(b"jibo-calibration:emmc-cid:v1\0" + cid_serial.encode("ascii")).hexdigest()

def stock_equivalent(files):
    return {key: False for key in files}
