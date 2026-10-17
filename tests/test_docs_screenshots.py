"""Exercise the clean docs fixture, including the optional Phoenix screens."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1",
                                reason="docs captures require Tk and a display")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("phoenix", [False, True])
def test_docs_fixture_captures_phoenix_screens(tmp_path, phoenix):
    args = [sys.executable, str(ROOT / "scripts/docs_screenshots.py"), "--out", str(tmp_path)]
    if phoenix:
        args.append("--phoenix")
    run = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=40)
    assert run.returncode == 0, run.stdout + run.stderr
    expected = {"ready.png", "mode-wifi.png", "review.png", "progress.png"}
    if phoenix:
        expected.update({"phoenix-entry.png", "phoenix-guidance.png", "phoenix-complete.png"})
    assert {path.name for path in tmp_path.glob("*.png")} == expected
    assert f"{len(expected)} screens captured at 1080 x 720" in run.stdout


def test_docs_fixture_rejects_visible_fixture_paths_before_capture(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("docs_shots", ROOT / "scripts/docs_screenshots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.DocsApi, "BACKUP_ROOT", Path("/tmp/forbidden-example"))
    monkeypatch.setattr(sys, "argv", ["docs_screenshots.py", "--phoenix", "--out", str(tmp_path)])
    with pytest.raises(AssertionError):
        module.main()
    assert not list(tmp_path.glob("*.png"))


def test_docs_fixture_captures_rcm_help_without_usb(tmp_path):
    args = [sys.executable, str(ROOT / "scripts/docs_screenshots.py"), "--rcm-help", "--out", str(tmp_path)]
    run = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert {path.name for path in tmp_path.glob("*.png")} == {
        "rcm-help-entry.png", "rcm-help.png", "rcm-help-small.png",
        "ready.png", "mode-wifi.png", "review.png", "progress.png"}
    assert "7 screens captured at 1080 x 720 (one at 920 x 640)" in run.stdout
