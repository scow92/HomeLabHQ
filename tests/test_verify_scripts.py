"""Run the retained end-to-end verification scripts through pytest."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "_verify").glob("*_test.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.stem)
def test_legacy_verification_script(script, tmp_path):
    env = os.environ | {"HLHQ_DATA_DIR": str(tmp_path / script.stem)}
    result = subprocess.run(
        [sys.executable, str(script)], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
