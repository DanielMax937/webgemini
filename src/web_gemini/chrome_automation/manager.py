"""Start/stop Chrome with remote debugging for Web Gemini."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from shutil import which

from .paths import CDP_PORT, chrome_pid_file, chrome_profile_dir


def _chrome_executable() -> str:
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform.startswith("linux"):
        for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            p = which(c)
            if p:
                return p
    raise RuntimeError("Google Chrome not found; install Chrome or set CHROME_PATH")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _cdp_ready() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def cmd_start() -> int:
    profile = chrome_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    pid_path = chrome_pid_file()

    if pid_path.is_file():
        try:
            old = int(pid_path.read_text(encoding="utf-8").strip())
            if _pid_alive(old) and _cdp_ready():
                print(f"Chrome already running (PID {old}, CDP {CDP_PORT}).")
                return 0
        except (ValueError, OSError):
            pass

    chrome = os.environ.get("CHROME_PATH") or _chrome_executable()
    args = [
        chrome,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    for _ in range(30):
        if _cdp_ready():
            print(f"Chrome started (PID {proc.pid}); CDP http://127.0.0.1:{CDP_PORT}")
            return 0
        time.sleep(0.5)
    print("Error: Chrome started but CDP did not become ready in time.", file=sys.stderr)
    return 1


def cmd_stop() -> int:
    """Terminate Chrome processes only; does not remove ``chrome_data/chrome-profile``."""
    pid_path = chrome_pid_file()
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            if _pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.3)
                    if not _pid_alive(pid):
                        break
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
        except (ValueError, ProcessLookupError):
            pass
        try:
            pid_path.unlink()
        except OSError:
            pass

    pat = str(chrome_profile_dir())
    subprocess.run(["pkill", "-f", pat], capture_output=True, text=True)
    print("Chrome automation stop issued.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Web Gemini Chrome CDP manager")
    ap.add_argument("command", choices=("start", "stop"))
    ns = ap.parse_args()
    if ns.command == "start":
        return cmd_start()
    return cmd_stop()


if __name__ == "__main__":
    raise SystemExit(main())
