# RTM images and factory calibration

Four archived full-flash packages with `RTM` in their filenames are cached in
`updates/`. The toolkit menu scans that folder. Three come from the archive's
`release-production` directory; the earlier 3.0.8 RTM2 package comes from
`stable-builds`. Development RTM packages are excluded.

| Package | Source | SHA-256 |
| --- | --- | --- |
| `jibo-pvt-flash-3.0.8-RTM-2.tar.bz2` | [stable-builds](https://pvindex.org/repository/platformos/builds/stable-builds/) | `cb38d78d021eaaeac338e891169b135c49a12a2e31bbe975d53786d31d27270a` |
| `jibo-pvt-flash-build-RTM2-3.0.8-20170220.tar.bz2` | [release-production](https://pvindex.org/repository/platformos/builds/release-production/) | `abaffcc98bb644eb716280895ea61e1fd1d28609a0916939535f5ccb768fb542` |
| `jibo-pvt-flash-build-RTM2-3.0.9-20170303.tar.bz2` | [release-production](https://pvindex.org/repository/platformos/builds/release-production/) | `0cfe3f366d0c0c1aee84536623773755ea0c2ef7eef0e7ecdb0d4617092523a9` |
| `jibo-pvt-flash-build-RTM3-3.3.4-20170623.tar.bz2` | [release-production](https://pvindex.org/repository/platformos/builds/release-production/) | `225bc6721808d907a3acbc5c17b3d9ad6a8e5b5ed444a85ccee5cb7a50d228d8` |

The archive provides checksum lists for [stable-builds](https://pvindex.org/repository/platformos/builds/stable-builds/sha256.txt)
and [release-production](https://pvindex.org/repository/platformos/builds/release-production/sha256.txt).

## Factory calibration after flashing

The [archived flashing guide](https://pvindex.org/confluence/display/ENG/How+to+Flash+a+Robot+-+Simple)
describes these post-flash steps: set the robot's own name and serial with
`jibo-setidentity`, choose its mode with `jibo-setmode`, run `imu_test_calib`
while the robot is still and face down, update body board firmware with
`jibo-bbfw-update`, and copy its camera calibration JSON files into
`/var/jibo/lps`. The camera files came from a `.tgz` named for the robot's
four-word name in [robot_data](https://pvindex.org/repository/platformos/robot_data/).
An example archive contains `CameraModelParamsL.json`,
`CameraModelParamsR.json`, and `InterCameraTransform.json`, along with test
captures. The guide copies only the JSON files. The [IMU test ticket](https://pvindex.org/jira.jibo.com/browse/HRD-268.html)
says `imu_test_calib` generates calibration values, a JSON file, and a log.

The [partition design](https://pvindex.org/confluence/display/ENG/Embedded+Platform+Partition+and+File+System+Scheme)
places per-robot identity and calibration in writable `var`. The
[factory reflash ticket](https://pvindex.org/jira.jibo.com/browse/MAN-190.html)
specifically requires restoring camera and IMU calibration after reflashing a
built robot. Calibration was therefore added after the base image was flashed;
the RTM package alone does not recreate the final factory state.

## Using the existing per-robot `var` backups

The toolkit's **Preserve current var** option retains the robot's identity and
calibration, but also retains its other current settings and data. **Use package
var for a fresh setup** replaces the whole `var` filesystem and loses those
per-robot files. **Fresh setup with this robot's saved calibration** starts from
the package's stock `var`, first captures a current rollback backup, then copies
the three camera calibration JSON files and identity from a SHA-256-verified
backup associated with the connected robot, preferring the `var` image read
immediately before the flash. It also copies supported IMU and microphone files
when they are present in that backup, preserving their exact bytes, ownership
and permissions. It sets the hostname and OOBE mode, and checks the edited
filesystem before writing. It leaves old Wi-Fi credentials and user data behind.
The matching
backup is selected automatically. If the saved camera files match the package
defaults, they are still copied and the update record notes that match. If no
matching backup with the identity and camera files exists,
the flash stops before writing any partition. The equivalent CLI switch is
`flash-update PACKAGE --fresh-var-with-calibration --yes`.

### What is preserved

| Item | Live path | Fresh setup with saved calibration |
| --- | --- | --- |
| Left/right camera lenses | `/var/jibo/lps/CameraModelParamsL.json`, `CameraModelParamsR.json` | Required, copied from the matching local `var` backup |
| Camera-to-camera alignment | `/var/jibo/lps/InterCameraTransform.json` | Required, copied from the same backup |
| Motion sensor corrections | `/var/jibo/imu/imu-cal.json` | Copied when present and supported; absent or unsupported is reported |
| Six microphone measurements | `/var/jibo/audio/audio_calibration.json` | Copied when present and supported, including historical unquoted `mic1`–`mic6` output; absent or unsupported is reported |
| Touchscreen affine calibration | `/etc/pointercal` | Copied into both incoming rootfs images only when a verified same-robot rootfs backup contains a supported file |
| Factory calibration record | `/opt/mfg/calibration_manifest.json` | Copied into the incoming skills image only when a verified same-robot skills backup contains a supported file |

A flash replaces rootfs and skills under **all three var policies**. The toolkit
checks its existing local partition backups for the conditional touchscreen and
factory record items before any partition write. It does not capture new rootfs
or skills backups automatically. Without a matching partition backup it reports
**not preserved (no backup of that partition)**. A `var` backup cannot recover
these two files. The toolkit never invents a missing `pointercal`, follows a
calibration symlink, or supports a `TSLIB_CALIBFILE` override in this profile.
Use **Back up selected partitions** before flashing to retain the existing
rootfs/skills files if you need them.

**Preserve current var** keeps all current var files in place, including IMU,
audio and private factory captures. **Use package var for a fresh setup** copies
no per-robot var measurements; its rollback backup retains them locally.
**Fresh setup with saved calibration** copies the supported measurements listed
above plus local identity, not photos, factory captures, distortion caches,
accounts or old network settings. Missing IMU/audio does not block this option;
the per-item status in the output and local update record states what was restored.
Unsupported or oversized originals remain unchanged in the private backup.

Every `distortionPrecomputeFile_*` entry in the fresh candidate's LPS directory
is removed, including old naming variants and package caches. A stale cache can
override restored camera JSON. The original backups and package images are not
changed. File readback, ownership/mode checks, unchanged image length and a
read-only ext4 check run before accepting an edited image.

The pure `jibo_calibration.py` v3 extractor accepts only capped, regular files
with exact schemas. It never exports identity, credentials, Wi-Fi, keys, camera
captures, face data, radio NVS or caches. The local camera/identity restore keeps
its existing package-schema compatibility check; this is separate from strict
public-export eligibility. A format check is not proof of factory provenance or
physical performance. The toolkit does not run manufacturing tests or move the
robot during preservation. Saving microphone measurements does not establish
which runtime reader uses them; offline image checks are not a hardware restore test.

The factory-record grammar is pinned to
[`manufacturing/automation_scripts@6ea44333f8707d140050d864c80ff1ceb96614bb`](https://pvindex.org/gitea/manufacturing/automation_scripts/src/commit/6ea44333f8707d140050d864c80ff1ceb96614bb/src/calibration_file_check.py):
exact `imu`/`camera` objects with `timedate` and `passfail` strings. Accepted dates
are empty, valid 14-digit `YYYYMMDDhhmmss` production timestamps, or the writer's
12-digit `YYMMDDhhmmss` example. Accepted results are empty, `False` (the
mainboard initializer), `PASS` or `FAIL`; arbitrary strings stay local. The
production timestamp and result constants come from `jibo_test_shared.py`, with
updates in `mainboard_test.py` and `camera_test.py` at that same revision.

## Wi-Fi after an RTM2 flash

The stock RTM2 `var` image starts `wpa_supplicant` directly from
`/etc/network/interfaces`. The later RTM3 image runs `wireless-startup` first;
that script disables TI radio power saving and the wlcore sleep mode before
starting the same supplicant file. Both images store Wi-Fi credentials in
`/var/etc/wpa_supplicant.conf`. The toolkit keeps a fresh flash stock until
you choose **Add or update a Wi-Fi network** or **Set mode and configure
Wi-Fi**. At that point, it recognizes the RTM2 startup hook and includes the
two RTM3 radio commands in the same reviewed Wi-Fi change. The original
`var` backup is retained, and a full transfer checks the edited image by
readback. Stock RTM2 network files span multiple 1 KiB blocks, so the quick
file editor will offer the full `var` transfer on the first edit. The full
edit also trims comment-only lines from stock network files; later edits can
use the quick path if the filesystem journal is clean.

This adjustment has been checked against the stock RTM2 and RTM3 images and
an ext4 image check, but association has not yet been verified on a live
robot. A WPA2/WPA3 mixed router is another possible source of failure; a
2.4 GHz WPA2-Personal test network can help distinguish router security from
the RTM2 startup path. The toolkit currently writes WPA-PSK credentials, so
it uses the WPA2 side of a mixed network; WPA3-only operation is unverified.
