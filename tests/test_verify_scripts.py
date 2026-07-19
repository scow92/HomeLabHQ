"""Run the retained end-to-end verification scripts through pytest."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "_verify").glob("*_test.py"))
VERIFY_SCRIPT = ROOT / "scripts" / "verify.sh"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.stem)
def test_legacy_verification_script(script, tmp_path):
    env = os.environ | {"HLHQ_DATA_DIR": str(tmp_path / script.stem)}
    result = subprocess.run(
        [sys.executable, str(script)], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _playwright_verification_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for directory in ("backend", "_verify", "tests"):
        (repo / directory).mkdir()
    shutil.copy2(VERIFY_SCRIPT, repo / "scripts" / "verify.sh")
    (repo / "package.json").write_text(
        '{"scripts":{"test:e2e":"playwright test"}}', encoding="utf-8"
    )

    python_bin = repo / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    python_bin.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_node.chmod(0o755)
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        """#!/usr/bin/env bash
printf '%s|%s\n' "${PYTHON-}" "${PLAYWRIGHT_BROWSERS_PATH-}" > "$PLAYWRIGHT_ENV_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo, fake_bin


@pytest.mark.parametrize(
    ("browser_path", "expected_browser_path"),
    [(None, "/srv/playwright-browsers"), ("/custom/browsers", "/custom/browsers")],
)
def test_verify_exports_playwright_environment(
    tmp_path: Path, browser_path: str | None, expected_browser_path: str
) -> None:
    repo, fake_bin = _playwright_verification_repo(tmp_path)
    environment_log = tmp_path / "playwright-environment.log"
    env = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PLAYWRIGHT_ENV_LOG": str(environment_log),
    }
    env.pop("PYTHON", None)
    if browser_path is None:
        env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    else:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browser_path

    result = subprocess.run(
        ["bash", "scripts/verify.sh"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_python = repo / ".venv" / "bin" / "python"
    assert environment_log.read_text(encoding="utf-8").strip() == (
        f"{expected_python}|{expected_browser_path}"
    )
