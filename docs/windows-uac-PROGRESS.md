# WIN-UAC progress (continuation)

Predecessor state carried in: branch feature/windows-uac at ee45d5b; release CI run
38338339947 completed success (rerun of run 38337898951's failed linux-payload job);
the d865c26 diagnostic proved the ota-safe-overlay guard exits 1 directly while the
hook observed a different status (timing-dependent, inside `bounded()`'s watchdog).

## Item 2 — merge main (LICENSE/NOTICE) — VERIFIED

- `git fetch origin main`; main tip is `156982e2cfdfb8d89603bcdb89e482341f21fca4`
  ("license: release the toolkit under GPL-3.0-only"), one commit ahead of the
  merge base `711dae0`.
- Merge conflict only in LICENSE (add/add: release-branch's 11-line licensing stub
  vs main's canonical GPL-3.0 text). Resolved `--theirs` (main verbatim):
  `sha256sum LICENSE` = `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986`,
  identical to `git show origin/main:LICENSE | sha256sum` and to the canonical
  gnu.org GPL-3.0 hash asserted by tests/test_license.py.
- Merge commit `7a0161c96de290c4bcd35ec188ed5ab5385b1575` includes NOTICE.md,
  tests/test_license.py, scripts/package.py + LICENSE/NOTICE in packages, and
  check_package rejecting missing/changed LICENSE/NOTICE.
- THIRD-PARTY-NOTICES.md kept from feature/release-builds (sha256
  `596d53c996280e85471e413f25d1c53bb5403affc4c5ea914b4c33df1ca989d2`); it lists only
  bundled third-party components and carries no blanket-license text. Verified the
  only "blanket" string in the tree is docs/release-builds-report.md (historical).
- The release branch never had a licenses/ folder at the repo root; the build
  creates `licenses/` per artifact inside `_internal/runtime/licenses` (bundle data),
  with THIRD-PARTY-NOTICES.md inside it — verified inside the CI-built EXE artifact.

## Single-file artifact license contents — VERIFIED by extraction (old artifact)

- Extracted `Jibo-DFU-Windows-x64.exe` from Actions artifact 11786196396
  (run 38338339947, built from the pre-merge GPL stub state).
- PyInstaller CArchive TOC contains `LICENSE`, `THIRD-PARTY-NOTICES.md`,
  `Windows-Python-LICENSE.txt`; payload.tar.gz contains
  `_internal/runtime/licenses/LICENSE` and `_internal/runtime/licenses/THIRD-PARTY-NOTICES.md`
  (byte-identical to the repo file at build time).
- GAP: the outer EXE `LICENSE` is the old 665-byte stub; `NOTICE.md` is absent.
  Fix: build.py adds NOTICE.md next to LICENSE, and windows_launcher.py --self-check
  requires both, with packaging tests asserting it.