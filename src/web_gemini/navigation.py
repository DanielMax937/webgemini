"""
页面导航重试逻辑：执行前检查页面是否正常打开，失败则关闭重试，最多 3 次，间隔 5 秒。
"""
import asyncio
import logging
from typing import Callable, Awaitable, TypeVar

from playwright.async_api import Page

logger = logging.getLogger(__name__)

T = TypeVar("T")
MAX_RETRIES = 3
RETRY_INTERVAL = 5


async def navigate_with_retry(
    navigate_fn: Callable[[], Awaitable[None]],
    reset_fn: Callable[[], Awaitable[None]],
    check_fn: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """
    执行导航，失败则 reset 后重试。
    - navigate_fn: 执行导航（如打开 Gemini 页面）
    - reset_fn: 重置/关闭当前页面（如 about:blank）
    - check_fn: 可选，检查页面是否正常打开，返回 True 表示成功
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            await navigate_fn()
            await asyncio.sleep(5 if attempt == 0 else 2)
            if check_fn:
                if await check_fn():
                    return
                raise RuntimeError("Page check failed")
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "[navigation] Attempt %d/%d failed: %s",
                attempt + 1,
                MAX_RETRIES,
                str(e)[:100],
            )
            if attempt < MAX_RETRIES - 1:
                try:
                    await reset_fn()
                except Exception as reset_err:
                    logger.warning("[navigation] Reset failed: %s", reset_err)
                await asyncio.sleep(RETRY_INTERVAL)
    raise last_error


async def navigate_page_to_gemini_with_retry(
    page: Page,
    gemini_url: str = "https://gemini.google.com/app",
    timeout: int = 60000,
) -> None:
    """
    Playwright page.goto 导航到 Gemini，失败则 about:blank 后重试。
    用于 video、image 接口。
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            await page.goto(gemini_url, timeout=timeout)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5 if attempt == 0 else 2)
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "[navigation] page.goto attempt %d/%d failed: %s",
                attempt + 1,
                MAX_RETRIES,
                str(e)[:100],
            )
            if attempt < MAX_RETRIES - 1:
                try:
                    await page.goto("about:blank", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(RETRY_INTERVAL)
    raise last_error
