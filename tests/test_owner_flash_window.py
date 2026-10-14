"""Real Tk documentation flow and geometry for the unified flash list."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('JIBO_GUI_TEST_DISPLAY') != '1',
                                reason='requires a real Tk display')
ROOT = Path(__file__).resolve().parents[1]


def test_hosted_docs_flow_has_featured_list_and_no_download_only_action(tmp_path):
    result = subprocess.run([sys.executable, 'scripts/flash_docs_screenshots.py',
                             '--hosted', '--out', str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name for path in tmp_path.glob('hosted-*.png')} == {
        'hosted-home.png', 'hosted-chooser.png', 'hosted-progress.png', 'hosted-resuming.png'}
    assert 'Verified hosted flow: featured first, calibration before download, Cancel-default review' in result.stdout
    assert 'Geometry checked at 920x640 and 1080x720' in result.stdout
