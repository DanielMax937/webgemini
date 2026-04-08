import asyncio
import logging

from .chrome_automation import driver

GEMINI_URL = "https://gemini.google.com/app"
NAVIGATE_MAX_RETRIES = 3
NAVIGATE_RETRY_INTERVAL = 5

logger = logging.getLogger(__name__)


class ChromeAutomation:
    """Chrome + CDP + Playwright driver (bundled in ``web_gemini.chrome_automation``)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._started = False

    def _run_cmd(self, *args: str) -> str:
        """Run act | page | distill (same semantics as the old skill CLI)."""
        try:
            return driver.run_cli(list(args))
        except Exception as e:
            if "Error" in str(e):
                raise RuntimeError(str(e)) from e
            raise

    async def run_cmd(self, *args: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._run_cmd(*args))

    async def start_browser(self) -> None:
        """Start Chrome via ``python -m web_gemini.chrome_automation.manager start`` (or use start-bg.sh)."""
        if self._started:
            return
        from .chrome_automation import manager

        loop = asyncio.get_event_loop()
        code = await loop.run_in_executor(None, manager.cmd_start)
        if code != 0:
            raise RuntimeError("Chrome manager start failed (see stderr above)")
        self._started = True
        await asyncio.sleep(3)

    async def stop_browser(self) -> None:
        if not self._started:
            return
        from .chrome_automation import manager

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, manager.cmd_stop)
        self._started = False

    async def new_tab(self, url: str) -> str:
        return await self.run_cmd("act", "--url", url, "--new-tab")

    async def close_tab(self) -> str:
        return await self.run_cmd("act", "--url", "about:blank")

    async def navigate_to_gemini_with_retry(self, url: str = GEMINI_URL) -> None:
        last_error: Exception | None = None
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
        if last_error:
            raise last_error

    async def get_page_info(self) -> dict:
        output = await self.run_cmd("page")
        info: dict = {}
        for line in output.split("\n"):
            if "URL:" in line:
                info["url"] = line.split("URL:")[1].strip()
            elif "标题:" in line:
                info["title"] = line.split("标题:")[1].strip()
        return info

    async def distill_dom(self, as_json: bool = False) -> str:
        args: list[str] = ["distill"]
        if as_json:
            args.append("--json")
        return await self.run_cmd(*args)

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


chrome = ChromeAutomation()
