"""Installer checks use stub commands; they never install system packages or edit PATH."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@pytest.fixture
def windows_installer(tmp_path):
    if sys.platform != "win32" or not POWERSHELL:
        pytest.skip("Windows PowerShell is required")
    commands = tmp_path / "stub commands"
    commands.mkdir()
    tool_bin = tmp_path / "tool bin"
    tool_bin.mkdir()
    log = tmp_path / "uv.log"
    (commands / "uv.cmd").write_bytes(
        (
            '@echo off\r\n'
            'echo %*>>"%VPSOPS_TEST_LOG%"\r\n'
            'if "%1 %2"=="tool install" exit /b %VPSOPS_TEST_INSTALL_EXIT%\r\n'
            'if "%1 %2"=="tool update-shell" exit /b %VPSOPS_TEST_SHELL_EXIT%\r\n'
            'if "%1 %2 %3"=="tool dir --bin" echo %VPSOPS_TEST_BIN%\r\n'
            'exit /b 0\r\n'
        ).encode("ascii")
    )
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ.get("PATH", ""),
        "VPSOPS_TEST_LOG": str(log),
        "VPSOPS_TEST_BIN": str(tool_bin),
        "VPSOPS_TEST_INSTALL_EXIT": "0",
        "VPSOPS_TEST_SHELL_EXIT": "0",
    }

    def run(*, proxy=True, install_exit=0, shell_exit=0, args=()):
        if proxy:
            # Default installation only checks this path; it must never execute it.
            (tool_bin / "vpsops-proxy.exe").touch()
        env["VPSOPS_TEST_INSTALL_EXIT"] = str(install_exit)
        env["VPSOPS_TEST_SHELL_EXIT"] = str(shell_exit)
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "deploy" / "install.ps1"), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )
        calls = log.read_text() if log.exists() else ""
        return result, calls

    return run


def test_windows_installer_uses_archive_and_custom_tool_directory(windows_installer):
    result, calls = windows_installer()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tool install --python 3.11 --upgrade" in calls
    assert "vpsops[proxy] @ https://github.com/Saito-Sakuya/vpsops/archive/refs/heads/master.tar.gz" in calls
    assert "tool update-shell" in calls and "tool dir --bin" in calls
    assert "vpsops-proxy --client codex login" in result.stdout


def test_windows_installer_stops_after_failed_install(windows_installer):
    result, calls = windows_installer(install_exit=7)
    assert result.returncode != 0
    assert "installation failed" in result.stderr
    assert "tool update-shell" not in calls


def test_windows_installer_reports_path_update_failure(windows_installer):
    result, calls = windows_installer(shell_exit=5)
    assert result.returncode != 0
    assert "Could not add" in result.stderr
    assert "tool dir --bin" not in calls


def test_windows_installer_checks_executable_exists(windows_installer):
    result, _ = windows_installer(proxy=False)
    assert result.returncode != 0
    assert "proxy executable was not found" in result.stderr


def test_windows_installer_help_does_not_install(windows_installer):
    result, calls = windows_installer(args=("-?",))
    assert result.returncode == 0
    assert not calls
