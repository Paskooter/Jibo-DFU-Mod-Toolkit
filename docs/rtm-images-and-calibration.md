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
immediately before the flash,
sets the hostname and OOBE mode, and checks the edited filesystem before
writing. It leaves old Wi-Fi credentials and user data behind. The matching
backup is selected automatically. If the saved camera files match the package
defaults, they are still copied and the update record notes that match. If no
matching backup with the identity and camera files exists,
the flash stops before writing any partition. The equivalent CLI switch is
`flash-update PACKAGE --fresh-var-with-calibration --yes`.

This option restores the camera files documented in the archived flashing
guide. The guide also runs `imu_test_calib` as a separate physical calibration
step. Its output was not present in the inspected `var` backups, so copying
`var` files cannot substitute for performing that test if it is needed.
