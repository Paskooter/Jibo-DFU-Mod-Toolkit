"""Small synthetic ext4 fixtures shared by the calibration regression tests."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile

VECTOR = json.loads((Path(__file__).parent / "fixtures/cal-v3-vectors.json").read_text())
FILES = {key: base64.b64decode(value, validate=True) for key, value in VECTOR["files_b64"].items()}
PATHS = {
    "left": "/jibo/lps/CameraModelParamsL.json",
    "right": "/jibo/lps/CameraModelParamsR.json",
    "inter_camera": "/jibo/lps/InterCameraTransform.json",
    "imu": "/jibo/imu/imu-cal.json",
    "audio": "/jibo/audio/audio_calibration.json",
    "touchscreen": "/etc/pointercal",
    "factory_record": "/mfg/calibration_manifest.json",
}
POINTERCAL = b"1 0 -3 0 +1 4 65536 1280 720\n"
FACTORY_RECORD = b'{"imu":{"timedate":"20261013010203","passfail":"PASS"},"camera":{"timedate":"","passfail":"False"}}\n'
IDENTITY = b'{"name":"Test-Only-Robot-Name","serial_number":"SYNTHETIC","cpuid":"SYNTHETIC","wifi_mac":"00:00:00:00:00:01"}'
TEMP_DIR = None


def debug(image, command):
    return subprocess.run(["debugfs", "-w", "-R", command, str(image)],
                          check=True, capture_output=True, text=True)


def make_image(root, name, entries, metadata=None):
    image = Path(root) / name
    with tempfile.TemporaryDirectory(dir=root) as temp:
        tree = Path(temp)
        for directory in ("jibo/lps", "etc", "mfg"):
            (tree / directory).mkdir(parents=True, exist_ok=True)
        for path, data in entries.items():
            file = tree / path.lstrip("/")
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        with image.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024)
        subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-d", str(tree), str(image)],
                       check=True, capture_output=True)
    for path, values in (metadata or {}).items():
        for field, value in values.items():
            debug(image, "set_inode_field {} {} {}".format(path, field, value))
    return image
