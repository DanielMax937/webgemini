import asyncio
import logging
import subprocess
import sys
from pathlib import Path

CHROME_AUTOMATION_DIR = Path.home() / ".claude/skills/chrome-automation"
GEMINI_URL = "https://gemini.google.com/app"
NAVIGATE_MAX_RETRIES = 3
NAVIGATE_RETRY_INTERVAL = 5

logger = logging.getLogger(__name__)
CHROME_MANAGER = CHROME_AUTOMATION_DIR / "scripts/chrome_manager.py"
RUN_SCRIPT = CHROME_AUTOMATION_DIR / "ai_browser_agent/run.py"


class ChromeAutomation:
    """Wrapper for chrome-automation skill commands."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._started = False

    def _run_cmd(self, *args) -> str:
        """Run chrome-automation command and return output."""
        cmd = [sys.executable, str(RUN_SCRIPT)] + list(args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(CHROME_AUTOMATION_DIR / "ai_browser_agent"),
        )
        # Ignore stderr warnings, only fail on actual errors
        if result.returncode != 0 and "Error" in result.stderr:
            raise RuntimeError(f"Command failed: {result.stderr}")
        return result.stdout

    async def run_cmd(self, *args) -> str:
        """Run chrome-automation command asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_cmd, *args)

    async def start_browser(self):
        """Start Chrome browser with debugging enabled."""
        if self._started:
            return

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, str(CHROME_MANAGER), "start"],
                capture_output=True,
                text=True,
            )
        )
        self._started = True
        # Wait for browser to be ready
        await asyncio.sleep(3)

    async def stop_browser(self):
        """Stop Chrome browser."""
        if not self._started:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, str(CHROME_MANAGER), "stop"],
                capture_output=True,
                text=True,
            )
        )
        self._started = False

    async def new_tab(self, url: str) -> str:
        """Open a new tab with the given URL."""
        return await self.run_cmd("act", "--url", url, "--new-tab")

    async def close_tab(self):
        """Navigate to blank page instead of closing tab (keyboard shortcuts don't work)."""
        # Note: Meta+w keyboard shortcut doesn't work via Playwright
        # Workaround: navigate current tab to blank page
        return await self.run_cmd("act", "--url", "about:blank")

    async def navigate_to_gemini_with_retry(self, url: str = GEMINI_URL) -> None:
        """
        导航到 Gemini 页面，失败则关闭重试。
        最多重试 3 次，每次间隔 5 秒。全部失败则抛出最后一次异常。
        """
        last_error = None
        for attempt in range(NAVIGATE_MAX_RETRIES):
            try:
                await self.run_cmd("act", "--url", url)
                await asyncio.sleep(5 if attempt == 0 else 2)
                return
            except Exception as e:
                last_error = e
                logger.warning(
                    "[navigation] act --url attempt %d/%d failed: %s",
                    attempt + 1,
                    NAVIGATE_MAX_RETRIES,
                    str(e)[:100],
                )
                if attempt < NAVIGATE_MAX_RETRIES - 1:
                    try:
                        await self.close_tab()
                    except Exception as reset_err:
                        logger.warning("[navigation] close_tab failed: %s", reset_err)
                    await asyncio.sleep(NAVIGATE_RETRY_INTERVAL)
        raise last_error

    async def get_page_info(self) -> dict:
        """Get current page info."""
        output = await self.run_cmd("page")
        info = {}
        for line in output.split("\n"):
            if "URL:" in line:
                info["url"] = line.split("URL:")[1].strip()
            elif "标题:" in line:
                info["title"] = line.split("标题:")[1].strip()
        return info

    async def distill_dom(self, as_json: bool = False) -> str:
        """Get distilled DOM."""
        args = ["distill"]
        if as_json:
            args.append("--json")
        return await self.run_cmd(*args)

    @property
    def lock(self) -> asyncio.Lock:
        """Lock for serializing operations."""
        return self._lock


chrome = ChromeAutomation()
