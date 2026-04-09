import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from dataclasses import dataclass
from playwright.async_api import Page

from .browser import chrome
from .concurrency import clipboard_section, USE_DOM_EXTRACTION
from .upload import upload_files

logger = logging.getLogger(__name__)
IMAGES_DIR = Path(__file__).parent.parent.parent / "images"
DEBUG_SCREENSHOT_DIR = Path(__file__).parent.parent.parent / "outputs" / "chat_attachments_debug"
GEMINI_URL = "https://gemini.google.com/app"
MAX_POLL_TIME = 120  # seconds
POLL_INTERVAL = 2  # seconds
SEND_BUTTON_WAIT_TIMEOUT = 600  # 10 minutes - wait for Send button to become clickable

# Gemini selectors (English UI)
INPUT_SELECTOR = '[aria-label="Enter a prompt for Gemini"]'
# Send button: Gemini uses aria-label="Send message" (not "Send")
SEND_BUTTON_SELECTORS = [
    '[aria-label="Send message"]',
    'button[aria-label="Send message"]',
    'button.send-button',
    'button.submit',
    '[aria-label="Send"]',
    'button[aria-label="Send"]',
]
COPY_BUTTON_SELECTOR = '[aria-label="Copy"]'
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'
# Tool selectors
TOOL_SELECTORS = {
    "deep_research": 'button:has-text("Deep Research")',
    "video": 'button:has-text("Create video")',
    "image": 'button:has-text("Create image")',
    "canvas": 'button:has-text("Canvas")',
    "tutor": 'button:has-text("Help me learn")',
}


@dataclass
class ImageResult:
    url: str
    local_path: str


@dataclass
class GeminiResponse:
    text: str
    images: list[ImageResult]


async def send_prompt(
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    page: Optional[Page] = None,
    job_id: str = "",
) -> GeminiResponse:
    """Send prompt to Gemini and wait for response.

    Args:
        prompt: The prompt to send
        tool: Optional tool. One of: deep_research, video, image, canvas, tutor
        attachments: Optional list of local file paths to upload
        page: Playwright Page bound to this task's dedicated tab (required for concurrency)
        job_id: Task ID used for clipboard mutex and logging
    """
    if page is None:
        raise ValueError("send_prompt requires a 'page' argument (task-bound tab)")

    run_dir: Path | None = None
    if attachments:
        run_dir = DEBUG_SCREENSHOT_DIR / time.strftime("%Y%m%d_%H%M%S", time.localtime())
        run_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[attachment] job=%s screenshot run_dir: %s", job_id, run_dir)

    step = 0
    # 1. Navigate to Gemini (with retry on this task's dedicated page)
    await _navigate_with_retry(page, GEMINI_URL, job_id)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "01_after_navigate")

    # Select tool if specified
    if tool and tool in TOOL_SELECTORS:
        await page.click(TOOLS_BUTTON_SELECTOR)
        await asyncio.sleep(1)
        await page.click(TOOL_SELECTORS[tool])
        await asyncio.sleep(1)
        if run_dir:
            step += 1
            await _take_screenshot(page, run_dir, step, "02_after_tool_select")

    # Upload attachments if provided
    if attachments:
        if run_dir:
            step += 1
            await _take_screenshot(page, run_dir, step, "03_before_upload")
        await upload_files(page, attachments)
        await asyncio.sleep(2)
        if run_dir:
            step += 1
            await _take_screenshot(page, run_dir, step, "04_after_upload")

    # Fill the chat input and send
    await page.locator(INPUT_SELECTOR).first.fill(prompt, timeout=120_000)
    await asyncio.sleep(0.5)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "05_after_fill")

    await _wait_for_send_button_clickable(page)

    sent = False
    for sel in SEND_BUTTON_SELECTORS:
        try:
            await page.locator(sel).first.click(timeout=30_000)
            sent = True
            break
        except Exception as e:
            logger.debug("[send] job=%s selector %s failed: %s", job_id, sel, str(e)[:80])
    if not sent:
        logger.info("[send] job=%s Send button not found, falling back to Enter", job_id)
        await page.locator(INPUT_SELECTOR).first.press("Enter", timeout=30_000)
    await asyncio.sleep(0.5)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "06_after_click_send")
    await asyncio.sleep(30)

    # Poll until copy button is available (response is ready)
    await _wait_for_copy_button(page)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "07_after_wait_copy")

    # Get text response
    text = await _get_text_response(page, job_id)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "08_after_get_text")

    return GeminiResponse(text=text, images=[])


async def _navigate_with_retry(page: Page, url: str, job_id: str, max_retries: int = 3, interval: int = 5) -> None:
    """Navigate *page* to *url* with retry on failure."""
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            await asyncio.sleep(5 if attempt == 0 else 2)
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "[navigation] job=%s attempt %d/%d failed: %s",
                job_id, attempt + 1, max_retries, str(e)[:100],
            )
            if attempt < max_retries - 1:
                try:
                    await page.goto("about:blank", timeout=10_000)
                except Exception as reset_err:
                    logger.warning("[navigation] job=%s reset failed: %s", job_id, reset_err)
                await asyncio.sleep(interval)
    if last_error:
        raise last_error


async def _take_screenshot(page: Page, run_dir: Path, step_num: int, step_name: str) -> str | None:
    """Take screenshot of *page*. Returns path or None on error."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{step_num:02d}_{step_name}.png"
    try:
        await page.screenshot(path=str(path))
        logger.info("[attachment] screenshot: %s", path)
        return str(path)
    except Exception as e:
        logger.warning("[attachment] screenshot failed: %s", e)
        return None


async def _wait_for_copy_button(page: Page) -> None:
    """Poll until copy button appears on *page* (response is ready)."""
    await asyncio.sleep(2)

    elapsed = 0
    while elapsed < MAX_POLL_TIME:
        try:
            count = await page.locator(COPY_BUTTON_SELECTOR).count()
            if count > 0:
                await asyncio.sleep(0.5)
                return
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL


async def _is_send_button_clickable(page: Page) -> bool:
    """Check if Send button on *page* is clickable (aria-disabled !== 'true')."""
    try:
        clickable = await page.evaluate("""() => {
            for (const sel of [
                '[aria-label="Send message"]',
                'button[aria-label="Send message"]',
                '[aria-label="Send"]',
                'button[aria-label="Send"]'
            ]) {
                const el = document.querySelector(sel);
                if (el) {
                    return el.getAttribute('aria-disabled') !== 'true';
                }
            }
            return false;
        }""")
        return bool(clickable)
    except Exception:
        return False


async def _wait_for_send_button_clickable(page: Page) -> None:
    """Poll until Send button on *page* becomes clickable, max SEND_BUTTON_WAIT_TIMEOUT seconds."""
    elapsed = 0
    while elapsed < SEND_BUTTON_WAIT_TIMEOUT:
        if await _is_send_button_clickable(page):
            logger.info("[send] Send button clickable after %.0fs", elapsed)
            return
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        if elapsed % 30 == 0 and elapsed > 0:
            logger.info("[send] Waiting for Send button... %.0fs elapsed", elapsed)
    raise TimeoutError(
        f"Send button not clickable within {SEND_BUTTON_WAIT_TIMEOUT}s (aria-disabled remained true)"
    )


async def _get_text_response(page: Page, job_id: str) -> str:
    """Get Gemini response text.

    Strategy (controlled by WG_USE_DOM_EXTRACTION env var):
    - Default: try clipboard (global mutex), fallback to DOM
    - WG_USE_DOM_EXTRACTION=1: always use DOM (no clipboard lock needed)
    """
    if USE_DOM_EXTRACTION:
        logger.info("[job] job=%s using DOM extraction (clipboard skipped)", job_id)
        text = await _get_text_response_via_dom(page)
        return text.strip() if text else ""

    # Clipboard path: click Copy + read clipboard inside a global mutex to prevent
    # concurrent tasks from corrupting each other's clipboard reads.
    copy_btn_count = await page.locator(COPY_BUTTON_SELECTOR).count()
    logger.info("[job] job=%s copy button count before copy: %d", job_id, copy_btn_count)

    clipboard_text: str = ""
    try:
        async with clipboard_section(job_id):
            # Critical section: click → read → store (minimised scope)
            await page.locator(COPY_BUTTON_SELECTOR).first.click(timeout=30_000)
            await asyncio.sleep(0.5)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: __import__("subprocess").run(
                    ["pbpaste"], capture_output=True, text=True
                ),
            )
            if result.returncode == 0:
                clipboard_text = result.stdout  # store immediately before releasing lock
    except Exception as e:
        logger.warning("[job] job=%s clipboard path failed: %s", job_id, e)

    if clipboard_text.strip():
        return clipboard_text.strip()

    # Fallback: DOM extraction (no mutex needed)
    logger.info("[job] job=%s clipboard empty, falling back to DOM extraction", job_id)
    text = await _get_text_response_via_dom(page)
    return text.strip() if text else ""


async def _get_text_response_via_dom(page: Page) -> str:
    """Extract full response text from *page* DOM via Playwright (no clipboard)."""
    try:
        text = await page.evaluate("""() => {
            const copyBtn = document.querySelector('[aria-label="Copy"]');
            if (!copyBtn) return '';

            let el = copyBtn;
            for (let i = 0; i < 15 && el; i++) {
                el = el.parentElement;
                if (!el) break;
                let raw = el.innerText || el.textContent || '';
                raw = raw.replace(/\\s+/g, ' ').trim();
                if (raw.length > 80 && !/^\\s*(Copy|Regenerate|Thumbs|More)\\s*$/i.test(raw)) {
                    return raw.replace(/\\s*(Copy|Regenerate|Thumbs up|Thumbs down|More)\\s*$/gi, '').trim();
                }
            }

            const blocks = document.querySelectorAll(
                '[data-message-author-role="model"], [class*="model-response"], [class*="message-content"]'
            );
            for (let i = blocks.length - 1; i >= 0; i--) {
                const t = (blocks[i].innerText || blocks[i].textContent || '').replace(/\\s+/g, ' ').trim();
                if (t.length > 50) return t;
            }
            return '';
        }""")
        return text or ""
    except Exception:
        return ""

