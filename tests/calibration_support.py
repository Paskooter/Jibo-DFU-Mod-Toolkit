"""Small synthetic ext4 fixtures shared by the calibration regression tests."""
import base64
import json
from pathlib import Path
import shutil
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
# Invented package defaults, distinct from FILES without changing v3 geometry,
# IDs, paths or versions. Only measurement values differ.
GENERIC_CAMERA = {}
for _key in ("left", "right", "inter_camera"):
    _value = json.loads(FILES[_key])
    if _key == "inter_camera":
        _value["Transform"][3] = 0.25
    else:
        _value["center_x"] += 10
        _value["focal_length_x"] += 10
    GENERIC_CAMERA[_key] = (json.dumps(_value, sort_keys=True) + "\n").encode("ascii")

POINTERCAL = b"1 0 -3 0 +1 4 65536 1280 720\n"
FACTORY_RECORD = b'{"imu":{"timedate":"20261013010203","passfail":"PASS"},"camera":{"timedate":"","passfail":"False"}}\n'
IDENTITY = b'{"name":"Test-Only-Robot-Name","serial_number":"SYNTHETIC","cpuid":"SYNTHETIC","wifi_mac":"00:00:00:00:00:01"}'
TEMP_DIR = None


def debug(image, command):
    return subprocess.run(["debugfs", "-w", "-R", command, str(image)],
                          check=True, capture_output=True, text=True)


def make_image(root, name, entries, metadata=None, size_bytes=8 * 1024 * 1024):
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
            stream.truncate(size_bytes)
        subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-d", str(tree), str(image)],
                       check=True, capture_output=True)
    for path, values in (metadata or {}).items():
        for field, value in values.items():
            debug(image, "set_inode_field {} {} {}".format(path, field, value))
    return image


def journal_pending(root, before, after, name="journal-pending.img"):
    """Leave checkpointed `before` blocks plus a committed JBD `after` transaction.

    Both inputs must be copies of the same small synthetic ext4 filesystem.
    debugfs does not replay this journal; e2fsck must do so on a disposable copy.
    """
    root, before, after = Path(root), Path(before), Path(after)
    pending = root / name
    if pending.exists() or pending.is_symlink() or pending in (before, after):
        raise ValueError("Choose a new journal fixture filename")
    header = subprocess.run(["dumpe2fs", "-h", str(before)], capture_output=True,
                            text=True, check=True).stdout
    block = int(header.split("Block size:")[1].split()[0])
    journal = set(map(int, subprocess.run(
        ["debugfs", "-R", "blocks <8>", str(before)], capture_output=True,
        text=True, check=True).stdout.split()))
    old, new = before.read_bytes(), after.read_bytes()
    assert len(old) == len(new) and len(old) % block == 0
    changed = [i for i in range(1, len(old) // block) if i not in journal and
               old[i * block:(i + 1) * block] != new[i * block:(i + 1) * block]]
    assert changed, "The journal fixture must contain an actual filesystem change"
    shutil.copyfile(before, pending)
    with tempfile.TemporaryDirectory(prefix="journal-payload-", dir=root) as directory:
        payload = Path(directory) / "blocks"
        payload.write_bytes(b"".join(new[i * block:(i + 1) * block] for i in changed))
        subprocess.run(["debugfs", "-w", "-f", "-", str(pending)],
                       check=True, capture_output=True, text=True,
                       input="jo\njw -b {} {}\njc\n".format(
                           ",".join(map(str, changed)), payload))
    features = subprocess.run(["debugfs", "-R", "features", str(pending)],
                              capture_output=True, text=True, check=True).stdout
    assert "needs_recovery" in features.split()
    return pending
