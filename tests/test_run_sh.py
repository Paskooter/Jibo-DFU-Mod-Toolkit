"""Integration tests for the one-command Linux launcher.

These tests run a copied launcher in a temporary repository and put harmless
command stubs first on PATH. They never invoke real package managers, build
tools, network access, or robot hardware.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SH = REPO_ROOT / "run.sh"
PYZ_RELATIVE = Path("dist/jibo-dfu-linux-x86_64.pyz")
BASH = shutil.which("bash") or "/bin/bash"


class RunLauncherTests(unittest.TestCase):
    def setUp(self):
        if not RUN_SH.is_file():
            self.skipTest("run.sh has not been added yet")

        self.tmp = tempfile.TemporaryDirectory(prefix="jibo-run-sh-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        shutil.copy2(RUN_SH, self.repo / "run.sh")
        (self.repo / "run.sh").chmod(0o755)
        (self.repo / "run.ps1").write_text("# mocked Windows USB helper\n")

        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "package.py").write_text("# intercepted by python3 stub\n")
        (self.repo / "scripts" / "check_package.py").write_text("# intercepted by python3 stub\n")
        (self.repo / "patches").mkdir()
        (self.repo / "patches" / "shofel2-dfu-entry.patch").write_text("fixture patch\n")
        (self.repo / "assets").mkdir()
        self.loader = self.repo / "assets" / "loader.bin"
        self.loader.write_bytes(b"fixture loader")

        self.shofel = self.repo / ".build" / "ShofEL2-for-T124"
        self.shofel.mkdir(parents=True)
        self.pyz = self.repo / PYZ_RELATIVE
        self.pyz.parent.mkdir(parents=True)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "commands.log"
        self._write_fake_commands()

        self.env = os.environ.copy()
        self.env.update({
            # Keep the child PATH hermetic so removing one fake command below
            # reliably simulates a missing system dependency.
            "PATH": str(self.bin),
            "JIBO_PYZ": str(self.pyz),
            "JIBO_LOADER": str(self.loader),
            "JIBO_SHOFEL_SRC": str(self.shofel),
            "JIBO_DFU_UTIL": str(self.bin / "dfu-util"),
            "JIBO_TEST_TRACE": str(self.trace),
            "JIBO_TEST_BIN": str(self.bin),
        })

    def _write_fake_commands(self):
        # Every stub records its invocation. The python stub accepts dependency
        # probes, fabricates a package when package.py is called, and records
        # the final .pyz invocation without running application code.
        common = r'''#!/bin/sh
name=${0##*/}
if [ "$name" = python3 ] && [ "${WSL_TEST_WAIT_FOR_MONITOR:-0}" = 1 ]; then
  case "${1-}" in
    *.pyz)
      seen=0
      attempts=0
      while [ "$attempts" -lt 200 ]; do
        if /bin/grep -q -- '-MonitorUsb' "$JIBO_TEST_TRACE" 2>/dev/null; then
          seen=1
          break
        fi
        /bin/sleep 0.01
        attempts=$((attempts + 1))
      done
      [ "$seen" = 1 ] || exit 71
      ;;
  esac
fi
printf '%s %s\n' "$name" "$*" >> "$JIBO_TEST_TRACE"
case "$name" in
  python3)
    case "${1-}" in
      -c|--version|-V) exit 0 ;;
      *check_package.py)
        if [ "${FAIL_PACKAGE_CHECK_ONCE:-0}" = 1 ]; then
          marker="$JIBO_TEST_BIN/package-check-failed"
          if [ ! -e "$marker" ]; then
            /usr/bin/touch "$marker"
            exit 1
          fi
        fi
        exit 0
        ;;
      *package.py)
        [ "${FAIL_PACKAGE:-0}" = 1 ] && exit 31
        output=
        previous=
        for argument do
          if [ "$previous" = out ]; then output=$argument; previous=; continue; fi
          case "$argument" in
            --out) previous=out ;;
            --out=*) output=${argument#--out=} ;;
          esac
        done
        if [ -z "$output" ]; then exit 32; fi
        mkdir -p "$(dirname "$output")" || exit 33
        printf 'fixture package\n' > "$output" || exit 34
        exit 0
        ;;
      *.pyz)
        [ "${FAIL_LAUNCH:-0}" = 1 ] && exit 41
        exit 0
        ;;
    esac
    ;;
  make)
    [ "${FAIL_MAKE:-0}" = 1 ] && exit 21
    source=${JIBO_SHOFEL_SRC:-.}
    printf 'fixture executable\n' > "$source/shofel2_t124"
    printf 'fixture payload\n' > "$source/intermezzo.bin"
    printf 'fixture stage\n' > "$source/dfu_stage2.bin"
    exit 0
    ;;
  git)
    case " $* " in *" --reverse "*) exit 1 ;; esac
    exit 0
    ;;
  apt-get)
    [ "${FAIL_APT_GET:-0}" = 1 ] && exit 51
    if [ "${1-}" = install ] && [ -n "${FAKE_INSTALL_COMMANDS:-}" ]; then
        for installed_command in $FAKE_INSTALL_COMMANDS; do
        /bin/cp "$JIBO_TEST_BIN/python3" "$JIBO_TEST_BIN/$installed_command" || exit 52
        /bin/chmod 755 "$JIBO_TEST_BIN/$installed_command" || exit 53
      done
    fi
    exit 0
    ;;
  dirname) exec /usr/bin/dirname "$@" ;;
  mkdir) exec /usr/bin/mkdir "$@" ;;
  mktemp) exec /usr/bin/mktemp "$@" ;;
  mv) exec /usr/bin/mv "$@" ;;
  rm) exec /usr/bin/rm "$@" ;;
  uname)
    case "${1-}" in
      -s) printf 'Linux\n' ;;
      -m) printf 'x86_64\n' ;;
      *) exit 64 ;;
    esac
    exit 0
    ;;
  wslpath)
    printf '%s\n' 'C:\Temp\run.ps1'
    exit 0
    ;;
  powershell.exe)
    exit 0
    ;;
  sudo)
    if [ "${1-}" = -v ]; then
      [ "${FAIL_SUDO_VALIDATE:-0}" = 1 ] && exit 61
      exit 0
    fi
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -n|-E|--preserve-env) shift ;;
        *) break ;;
      esac
    done
    [ "$#" -gt 0 ] || exit 62
    exec "$@"
    ;;
esac
exit 0
'''

        names = {
            "python3", "git", "make", "gcc", "arm-none-eabi-gcc",
            "arm-none-eabi-as", "arm-none-eabi-nm", "arm-none-eabi-objcopy", "arm-none-eabi-objdump",
            "dfu-util", "apt-get", "sudo", "dpkg-query", "pkg-config", "patch",
            "dirname", "mkdir", "mktemp", "mv", "rm", "uname",
            "wslpath", "powershell.exe",
        }
        for name in names:
            path = self.bin / name
            path.write_text(common)
            path.chmod(0o755)

    def run_launcher(self, *, extra_env=None, create_pyz=False):
        if create_pyz:
            self.pyz.write_text("fixture executable\n")
            self.pyz.chmod(0o755)
        env = self.env.copy()
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, str(self.repo / "run.sh"), "--launcher-test-arg"],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def events(self):
        if not self.trace.exists():
            return []
        return self.trace.read_text().splitlines()

    def package_call_index(self, events):
        return next((i for i, event in enumerate(events)
                     if event.startswith("python3 ") and "/package.py " in event), None)

    def launch_index(self, events):
        return next((i for i, event in enumerate(events)
                     if event.startswith("python3 ") and "/package.py " not in event
                     and "/check_package.py " not in event
                     and ".pyz" in event), None)

    def test_existing_pyz_launches_without_build_or_package_install(self):
        result = self.run_launcher(create_pyz=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        self.assertIsNotNone(self.launch_index(events), events)
        self.assertIn("--launcher-test-arg", events[self.launch_index(events)])
        self.assertIsNone(self.package_call_index(events), events)
        self.assertFalse(any(event.startswith(("make ", "git ", "apt-get ")) for event in events), events)

    def test_missing_pyz_builds_package_then_launches_it(self):
        result = self.run_launcher()

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        package_index = self.package_call_index(events)
        launch_index = self.launch_index(events)
        self.assertTrue(any(event.startswith("make ") for event in events), events)
        self.assertIsNotNone(package_index, events)
        self.assertIsNotNone(launch_index, events)
        self.assertLess(package_index, launch_index, events)
        self.assertIn("--loader " + str(self.loader), events[package_index])
        self.assertIn("--shofel2 " + str(self.shofel / "shofel2_t124"), events[package_index])
        self.assertIn("--intermezzo " + str(self.shofel / "intermezzo.bin"), events[package_index])
        self.assertIn("--dfu-stage " + str(self.shofel / "dfu_stage2.bin"), events[package_index])
        self.assertIn("--dfu-util " + str(self.bin / "dfu-util"), events[package_index])
        self.assertIn("--out ", events[package_index])
        self.assertIn("/.jibo-package.", events[package_index])
        self.assertTrue(events[package_index].endswith("/jibo-dfu.pyz"), events[package_index])
        self.assertIn("--launcher-test-arg", events[launch_index])
        self.assertTrue(self.pyz.is_file())

    def test_stale_package_is_rebuilt_before_launch(self):
        result = self.run_launcher(create_pyz=True,
                                   extra_env={"FAIL_PACKAGE_CHECK_ONCE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        self.assertTrue(any(event.startswith("make ") for event in events), events)
        self.assertIsNotNone(self.package_call_index(events), events)
        self.assertLess(self.package_call_index(events), self.launch_index(events))

    def test_missing_dependency_is_installed_before_build(self):
        (self.bin / "gcc").unlink()
        result = self.run_launcher(extra_env={"FAKE_INSTALL_COMMANDS": "gcc"})

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        self.assertTrue(any(event.startswith("apt-get update") for event in events), events)
        self.assertTrue(any(event.startswith("apt-get install -y gcc") for event in events), events)
        self.assertTrue(any(event.startswith("make ") for event in events), events)
        self.assertIsNotNone(self.launch_index(events), events)

    @unittest.skipIf(os.geteuid() == 0, "requires a non-root test process")
    def test_nonroot_validates_sudo_in_session_before_launch(self):
        result = self.run_launcher(create_pyz=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        validation_index = next((i for i, event in enumerate(events)
                                 if event == "sudo -v"), None)
        sudo_launch_index = next((i for i, event in enumerate(events)
                                  if event.startswith("sudo python3 ") and ".pyz" in event), None)
        launch_index = self.launch_index(events)
        self.assertIsNotNone(validation_index, events)
        self.assertIsNotNone(sudo_launch_index, events)
        self.assertIsNotNone(launch_index, events)
        self.assertLess(validation_index, sudo_launch_index, events)
        self.assertLess(sudo_launch_index, launch_index, events)

    def test_build_failure_stops_before_packaging_or_launch(self):
        result = self.run_launcher(extra_env={"FAIL_MAKE": "1"})

        self.assertNotEqual(result.returncode, 0)
        events = self.events()
        self.assertIsNone(self.package_call_index(events), events)
        self.assertIsNone(self.launch_index(events), events)
        self.assertFalse(self.pyz.exists())

    def test_package_failure_stops_before_launch(self):
        result = self.run_launcher(extra_env={"FAIL_PACKAGE": "1"})

        self.assertNotEqual(result.returncode, 0)
        events = self.events()
        self.assertIsNotNone(self.package_call_index(events), events)
        self.assertIsNone(self.launch_index(events), events)
        self.assertFalse(self.pyz.exists())

    @unittest.skipIf(os.geteuid() == 0, "requires a non-root test process")
    def test_sudo_validation_failure_stops_before_launch(self):
        result = self.run_launcher(
            create_pyz=True,
            extra_env={"FAIL_SUDO_VALIDATE": "1"},
        )

        self.assertNotEqual(result.returncode, 0)
        events = self.events()
        self.assertIn("sudo -v", events)
        self.assertIsNone(self.launch_index(events), events)

    def test_wsl_runs_one_shot_usb_attach_then_monitor_before_tool(self):
        result = self.run_launcher(create_pyz=True, extra_env={
            "WSL_DISTRO_NAME": "Ubuntu",
            "JIBO_WINDOWS_POWERSHELL": str(self.bin / "powershell.exe"),
            "WSL_TEST_WAIT_FOR_MONITOR": "1",
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        path_index = next((i for i, event in enumerate(events)
                           if event.startswith("wslpath -w ")), None)
        one_shot_index = next((i for i, event in enumerate(events)
                               if event.startswith("powershell.exe ")
                               and "-UsbOnly" in event and "-MonitorUsb" not in event), None)
        monitor_index = next((i for i, event in enumerate(events)
                             if event.startswith("powershell.exe ") and "-MonitorUsb" in event), None)
        launch_index = self.launch_index(events)
        self.assertIsNotNone(path_index, events)
        self.assertIsNotNone(one_shot_index, events)
        self.assertIsNotNone(monitor_index, events)
        self.assertIsNotNone(launch_index, events)
        self.assertLess(path_index, one_shot_index, events)
        self.assertLess(one_shot_index, monitor_index, events)
        self.assertLess(monitor_index, launch_index, events)
        self.assertIn("-File C:\\Temp\\run.ps1", events[one_shot_index])
        self.assertIn("-Distro Ubuntu", events[one_shot_index])
        self.assertIn("-Distro Ubuntu", events[monitor_index])

    def test_manual_usb_setting_skips_wsl_powershell_handoff(self):
        result = self.run_launcher(create_pyz=True, extra_env={
            "WSL_DISTRO_NAME": "Ubuntu",
            "JIBO_MANUAL_USB": "1",
            "JIBO_WINDOWS_POWERSHELL": str(self.bin / "powershell.exe"),
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        self.assertTrue(any(event.startswith("python3 ") and ".pyz" in event for event in events), events)
        self.assertFalse(any(event.startswith(("wslpath ", "powershell.exe ")) for event in events), events)


if __name__ == "__main__":
    unittest.main()
