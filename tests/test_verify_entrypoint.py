"""Regression coverage for the authoritative verification entry point."""

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts" / "verify.sh"


def _verification_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for directory in ("backend", "_verify", "tests"):
        (repo / directory).mkdir()
    shutil.copy2(VERIFY_SCRIPT, repo / "scripts" / "verify.sh")
    (repo / "package.json").write_text(
        '{"scripts":{"test:e2e":"playwright test"}}', encoding="utf-8"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "python-commands.log"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$VERIFY_COMMAND_LOG"
if [[ "$1" == "-c" ]]; then
    module="${@: -1}"
    if [[ ",${VERIFY_MISSING_MODULES:-}," == *",$module,"* ]]; then
        exit 1
    fi
    exit 0
fi
if [[ "$1" == "-m" && "$2" == "${VERIFY_FAIL_MODULE:-}" && "${3:-}" != "--version" ]]; then
    exit 7
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo, fake_python, command_log


def _run_verify(
    tmp_path: Path, *, fail_module: str = "", missing_modules: str = ""
) -> tuple[subprocess.CompletedProcess[str], str]:
    repo, fake_python, command_log = _verification_repo(tmp_path)
    env = os.environ | {
        "PYTHON": str(fake_python),
        "VERIFY_COMMAND_LOG": str(command_log),
        "VERIFY_FAIL_MODULE": fail_module,
        "VERIFY_MISSING_MODULES": missing_modules,
    }
    result = subprocess.run(
        ["bash", "scripts/verify.sh"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, command_log.read_text(encoding="utf-8")


def test_verify_uses_documented_ruff_and_configured_mypy_scopes(tmp_path: Path) -> None:
    result, commands = _run_verify(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "-m ruff check backend _verify tests\n" in commands
    assert "-m mypy\n" in commands
    assert "-m mypy .\n" not in commands
    assert "Verification completed: 5 PASS, 0 FAIL, 1 SKIP." in result.stdout


def test_verify_reports_a_failed_check_and_returns_nonzero(tmp_path: Path) -> None:
    result, _ = _run_verify(tmp_path, fail_module="mypy")

    assert result.returncode == 1
    assert "FAIL: MyPy" in result.stdout
    assert "Verification completed: 4 PASS, 1 FAIL, 1 SKIP." in result.stdout


def test_verify_reports_an_unavailable_dependency_as_skipped(tmp_path: Path) -> None:
    result, _ = _run_verify(tmp_path, missing_modules="ruff")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP: Ruff" in result.stdout
    assert "-m pip install -r requirements.txt" in result.stdout
    assert "Verification completed: 4 PASS, 0 FAIL, 2 SKIP." in result.stdout
