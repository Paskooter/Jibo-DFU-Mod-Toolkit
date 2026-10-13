# Windows EXE setup and troubleshooting

The single-file EXE uses WSL 2 + WSLg and usbipd-win. See
[Windows authorization and private Linux setup](release.md#windows-authorization-release-exe-only)
for the complete behavior.

- The first console line identifies the build revision. Include it with any error report.
- The launcher first reuses its own `Jibo-DFU-Toolkit` Linux, then a matching standard Ubuntu 22.04+.
- NVIDIA Workbench, Docker, Rancher, Podman and custom Linux distributions are never offered or used automatically.
- If none matches, approve a one-time verified Ubuntu download to create private Linux. No Linux account/password prompts are needed.
- Setup runs as your Windows user. Administrator approval remains for installing missing Windows prerequisites and sharing USB through usbipd.
- The GUI always runs as a normal Linux user; Jibo-only USB permissions use that distro's root account without sudo or a Linux password.
- After a requested Windows reboot, reopen the same EXE. Declined setup can be retried by reopening it.
- If private Linux is damaged, copy any Linux files/backups you need to Windows first, then run `wsl --unregister Jibo-DFU-Toolkit` and reopen the EXE. Unregister deletes the entire private filesystem, not other distributions.
- An existing private-storage directory is never overwritten automatically. If setup reports one without a registered distro, preserve or move it before retrying.

Source launchers are separate: `run.ps1` and `run-gui.cmd` retain their existing source-build and Linux sudo requirements.
