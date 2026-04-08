"""Paths for bundled Chrome (CDP) runtime."""

from __future__ import annotations

from pathlib import Path

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"


def repo_root() -> Path:
    """Project root (directory containing pyproject.toml)."""
    # src/web_gemini/chrome_automation/paths.py -> parents[3]
    return Path(__file__).resolve().parents[3]


def chrome_data_dir() -> Path:
    return repo_root() / "chrome_data"


def chrome_profile_dir() -> Path:
    """Persistent Chrome user-data-dir (login session). Never deleted by manager stop."""
    return chrome_data_dir() / "chrome-profile"


def chrome_pid_file() -> Path:
    return chrome_data_dir() / ".chrome_automation.pid"
