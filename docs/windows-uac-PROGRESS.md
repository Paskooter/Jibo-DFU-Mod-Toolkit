# Windows download licensing

The Windows EXE embeds the toolkit's `LICENSE` (GPL-3.0-only), copyright
`NOTICE.md`, and `THIRD-PARTY-NOTICES.md`. The embedded Linux payload also
contains these notices and source-accompanied dependency licenses.

Download the matching corresponding-source archive alongside each release and
keep it with the executable. The release's `SHA256SUMS` and build-info receipt
identify the exact build. To inspect the Linux payload, run:

```powershell
Jibo-DFU-Windows-x64.exe --extract-payload payload.tar.gz
```

See [release, sources and troubleshooting](release.md) for dependency licensing,
rebuilding, relinking and verification instructions. Setup and USB authorization
are described in [Windows setup](windows-uac-REPORT.md).
