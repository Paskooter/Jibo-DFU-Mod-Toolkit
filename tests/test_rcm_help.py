"""The RCM how-to: one wording for README, GUI help and text menu, and the pinned photo."""
import curses
import hashlib
import importlib.util
import json
import re
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import jibo_tui

ROOT = Path(__file__).resolve().parents[1]
PHOTO = ROOT / jibo_tui.RCM_HELP_IMAGE


def png_chunks(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, chunks = 8, []
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        chunks.append(kind)
        offset += length + 12
        if kind == b"IEND":
            break
    return chunks, offset


def readme_section():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    parts = readme.split("\n## Put Jibo in RCM mode\n", 1)
    assert len(parts) == 2, "README needs a 'Put Jibo in RCM mode' section"
    return parts[1].split("\n## ", 1)[0]


def test_photo_is_the_pinned_metadata_free_png():
    data = PHOTO.read_bytes()
    assert hashlib.sha256(data).hexdigest() == jibo_tui.RCM_HELP_IMAGE_SHA256
    chunks, end = png_chunks(data)
    # Only image data: no text, EXIF, ICC, time or trailing bytes.
    assert set(chunks) == {b"IHDR", b"IDAT", b"IEND"} and end == len(data)
    width, height = struct.unpack_from(">II", data, 16)
    assert (width, height) == (360, 360)


def test_package_and_gui_share_one_photo_pin():
    spec = importlib.util.spec_from_file_location("package_pins", ROOT / "scripts/package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.RCM_HELP_IMAGE == jibo_tui.RCM_HELP_IMAGE
    assert module.RCM_HELP_IMAGE_SHA256 == jibo_tui.RCM_HELP_IMAGE_SHA256


def test_readme_section_uses_the_shared_wording_and_photo():
    section = readme_section()
    image = re.search(r'<img src="([^"]+)" width="360" height="360" alt="([^"]+)">', section)
    assert image, "the section shows the photo at its real size"
    assert image.group(1) == jibo_tui.RCM_HELP_IMAGE
    assert image.group(2) == jibo_tui.RCM_HELP_ALT
    assert "RESET" in image.group(2) and "RCM" in image.group(2)
    assert "*" + jibo_tui.RCM_HELP_CAPTION + "*" in section
    lines = [line for line in section.splitlines() if re.match(r"\d\. ", line)]
    assert tuple(lines) == jibo_tui.RCM_HELP_STEPS
    assert jibo_tui.RCM_HELP_LINK in section and "press `h`" in section


def test_every_rcm_instruction_in_the_docs_links_to_the_how_to():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count("](#put-jibo-in-rcm-mode)") >= 3
    for name in ("docs/gui.md", "docs/macos.md"):
        assert "README.md#put-jibo-in-rcm-mode" in (ROOT / name).read_text(encoding="utf-8"), name
    # The vague old pointer is gone from the readiness text.
    assert "during reset" not in jibo_tui.status_lines(jibo_tui.Readiness("none", ()))[1]


def test_steps_keep_the_owner_wording_and_nothing_invented():
    text = " ".join(jibo_tui.RCM_HELP_STEPS)
    for phrase in ("Fully shut Jibo down", "hold the RCM button", "turn Jibo back on",
                   "red light on his forehead", "don't hear his startup chime"):
        assert phrase in text
    # The RESET button is only named, never part of the procedure.
    assert "RESET" not in text
    assert "Enter DFU with ShofEL (RAM loader)" in jibo_tui.RCM_HELP_STEPS[-1]


def fake_api(devices=()):
    return SimpleNamespace(MARKER="jibo-dfu-v1", devices=lambda: list(devices),
                           tool=lambda _name: "dfu-util",
                           dfu_alternatives=lambda _tool, _port: ([], ""),
                           shofel_dfu_available=lambda: True,
                           _update_candidates=lambda _folder: [])


class Screen:
    def __init__(self, keys):
        self.keys, self.lines = list(keys), []

    def erase(self):
        self.lines.clear()

    def getmaxyx(self):
        return (30, 120)

    def addnstr(self, y, x, text, length, attr=0):
        self.lines.append(text[:length])

    def refresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def timeout(self, _ms):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")


@pytest.mark.parametrize("key", ["h", "H", "?"])
def test_text_menu_h_opens_rcm_help_without_touching_usb_actions(key):
    app = jibo_tui.TerminalMenu(fake_api())
    screen = Screen([ord(key)])
    with patch.object(curses, "curs_set", return_value=None):
        app.session(screen)
    assert app.command == "rcm-help"
    assert any("h " + jibo_tui.RCM_HELP_LINK in line for line in screen.lines)


def test_text_menu_run_shows_the_same_steps_then_returns_to_the_menu():
    shown, sessions, calls = [], [], []

    def session(screen):
        calls.append(1)
        sessions[-1].command = "rcm-help" if len(calls) == 1 else "quit"

    class Menu(jibo_tui.TerminalMenu):
        def __init__(self, api):
            super().__init__(api)
            sessions.append(self)

    api = fake_api()
    with patch.object(jibo_tui, "_terminal_available", return_value=True), \
            patch.object(jibo_tui, "TerminalMenu", Menu), \
            patch.object(jibo_tui.curses, "wrapper", side_effect=lambda fn: session(None)), \
            patch.object(jibo_tui, "show_screen", side_effect=lambda t, l: shown.append((t, l))), \
            patch.object(jibo_tui, "execute_action") as execute:
        assert jibo_tui.run(api) == 0
    execute.assert_not_called()
    assert shown == [(jibo_tui.RCM_HELP_TITLE, jibo_tui.rcm_help_lines())]
    lines = jibo_tui.rcm_help_lines()
    assert lines[2:2 + len(jibo_tui.RCM_HELP_STEPS)] == jibo_tui.RCM_HELP_STEPS
    assert "photo" in lines[-1] and jibo_tui.RCM_HELP_CAPTION in lines[-1]


def test_runtime_self_check_pins_the_bundled_photo(tmp_path):
    path = ROOT / "scripts/release/native_launcher.py"
    spec = importlib.util.spec_from_file_location("native_launcher_rcm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / jibo_tui.RCM_HELP_IMAGE
    target.parent.mkdir(parents=True)
    target.write_bytes(PHOTO.read_bytes())
    receipt = module.rcm_help_check(tmp_path)
    assert receipt == {"path": jibo_tui.RCM_HELP_IMAGE, "sha256": jibo_tui.RCM_HELP_IMAGE_SHA256,
                       "bytes": PHOTO.stat().st_size}
    target.write_bytes(PHOTO.read_bytes() + b"x")
    with pytest.raises(RuntimeError, match="RCM help photo"):
        module.rcm_help_check(tmp_path)
    target.unlink()
    with pytest.raises(OSError):
        module.rcm_help_check(tmp_path)


def test_release_payload_copies_and_hashes_the_photo(tmp_path):
    """build.py copies assets/ into the runtime before hashing it; mirror that seam."""
    build = (ROOT / "scripts/release/build.py").read_text()
    assert "shutil.copytree(ROOT / 'assets', runtime / 'assets')" in build
    copy_at = build.index("shutil.copytree(ROOT / 'assets', runtime / 'assets')")
    assert copy_at < build.index("write_manifest(runtime, revision, exclude=excluded)")
    sys.path.insert(0, str(ROOT / "scripts/release"))
    try:
        import manifest
    finally:
        sys.path.pop(0)
    import shutil
    runtime = tmp_path / "runtime"
    shutil.copytree(ROOT / "assets/help", runtime / "assets/help")
    manifest.write_manifest(runtime, "revision")
    receipt = manifest.verify_manifest(runtime)
    assert receipt["files"][jibo_tui.RCM_HELP_IMAGE] == jibo_tui.RCM_HELP_IMAGE_SHA256
    (runtime / jibo_tui.RCM_HELP_IMAGE).write_bytes(b"not the photo")
    with pytest.raises(ValueError, match="rcm-buttons.png"):
        manifest.verify_manifest(runtime)
