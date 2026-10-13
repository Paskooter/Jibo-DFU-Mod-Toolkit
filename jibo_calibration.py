"""Pure, exact-byte calibration helpers. No USB, network or robot writes.

Only the frozen v3 profile is eligible for sharing. Private backup images and
unknown formats remain untouched. Format validation is not factory provenance
or a claim that measurements are mechanically safe.
"""
from dataclasses import dataclass
import base64
import hashlib
import json
import re

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
