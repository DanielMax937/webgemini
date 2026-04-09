"""Per-task browser page lifecycle: open new tab at task start, close on exit.

A single shared Playwright → CDP connection is reused across tasks; each task
gets its own *Page* (tab) so they never interfere with each other's navigation
or DOM state.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from playwright.async_api import async_playwright, Browser, Page, Playwright

from .chrome_automation.paths import CDP_URL

logger = logging.getLogger(__name__)

# Module-level shared Playwright + Browser connection.
# Access only via _get_shared_browser() to handle reconnects safely.
_pw: Playwright | None = None
_browser: Browser | None = None
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _get_shared_browser() -> Browser:
    """Return a shared Browser connection; reconnect automatically if disconnected."""
    global _pw, _browser

    async with _get_lock():
        if _browser is not None and _browser.is_connected():
            return _browser

        # Tear down any stale connection.
        if _pw is not None:
            try:
                await _pw.stop()
            except Exception:
                pass
            _pw = None
            _browser = None

        _pw = await async_playwright().start()
        _browser = await _pw.chromium.connect_over_cdp(CDP_URL)
        logger.info("[page_context] connected to Chrome CDP at %s", CDP_URL)
        return _browser


@asynccontextmanager
async def task_page(job_id: str) -> AsyncIterator[Page]:
    """Open a new browser tab for *job_id*, yield the Page, then close it.

    Each invocation creates a fresh tab so tasks never share page state.
    The tab is closed (and the slot released) even if the task raises.

    Usage::

        async with task_page(job_id) as page:
            await page.goto("https://gemini.google.com/app")
            ...
    """
    browser = await _get_shared_browser()
    if not browser.contexts:
        raise RuntimeError("No browser context available — is Chrome running with CDP?")

    context = browser.contexts[0]
    page = await context.new_page()
    logger.info(
        "[page_context] job=%s tab opened (total tabs=%d)",
        job_id, len(context.pages),
    )
    try:
        yield page
    finally:
        try:
            if not page.is_closed():
                await page.close()
                logger.info("[page_context] job=%s tab closed", job_id)
        except Exception as exc:
            logger.warning("[page_context] job=%s tab close failed: %s", job_id, exc)
