import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from dataclasses import dataclass
from playwright.async_api import async_playwright

from .browser import chrome
from .chrome_automation.paths import CDP_URL
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
    attachments: Optional[list[str]] = None
) -> GeminiResponse:
    """Send prompt to Gemini and wait for response.

    Args:
        prompt: The prompt to send
        tool: Optional tool to use. One of: deep_research, video, image, canvas, tutor
        attachments: Optional list of local file paths to upload
    """
    run_dir: Path | None = None
    if attachments:
        run_dir = DEBUG_SCREENSHOT_DIR / time.strftime("%Y%m%d_%H%M%S", time.localtime())
        run_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[attachment] screenshot run_dir: %s", run_dir)

    step = 0
    # 1. Navigate to Gemini (with retry: close and reopen on failure, max 3 times, 5s interval)
    await chrome.navigate_to_gemini_with_retry(GEMINI_URL)
    if run_dir:
        step += 1
        await _take_screenshot(run_dir, step, "01_after_navigate")

    # Select tool if specified
    if tool and tool in TOOL_SELECTORS:
        await chrome.run_cmd("act", "--selector", TOOLS_BUTTON_SELECTOR, "--action", "click")
        await asyncio.sleep(1)
        await chrome.run_cmd("act", "--selector", TOOL_SELECTORS[tool], "--action", "click")
        await asyncio.sleep(1)
        if run_dir:
            step += 1
            await _take_screenshot(run_dir, step, "02_after_tool_select")

    # Upload attachments if provided
    if attachments:
        if run_dir:
            step += 1
            await _take_screenshot(run_dir, step, "03_before_upload")
        await _upload_attachments(attachments)
        await asyncio.sleep(2)
        if run_dir:
            step += 1
            await _take_screenshot(run_dir, step, "04_after_upload")

    # Fill the chat input and send
    await chrome.run_cmd("act", "--selector", INPUT_SELECTOR, "--action", "fill", "--value", prompt)
    await asyncio.sleep(0.5)
    if run_dir:
        step += 1
        await _take_screenshot(run_dir, step, "05_after_fill")
    # Wait for Send button to become clickable (aria-disabled !== 'true'), max 10 min
    await _wait_for_send_button_clickable()
    # Click Send button; fallback to Enter if not found
    sent = False
    for sel in SEND_BUTTON_SELECTORS:
        try:
            await chrome.run_cmd("act", "--selector", sel, "--action", "click")
            sent = True
            break
        except Exception as e:
            logger.debug("[send] selector %s failed: %s", sel, str(e)[:80])
    if not sent:
        logger.info("[send] Send button not found, falling back to Enter")
        await chrome.run_cmd("act", "--selector", INPUT_SELECTOR, "--action", "press", "--value", "Enter")
    await asyncio.sleep(0.5)
    if run_dir:
        step += 1
        await _take_screenshot(run_dir, step, "06_after_click_send")
    await asyncio.sleep(30)

    # Poll until copy button is available
    await _wait_for_copy_button()
    if run_dir:
        step += 1
        await _take_screenshot(run_dir, step, "07_after_wait_copy")

    # Get text response via copy button
    text = await _get_text_response()
    if run_dir:
        step += 1
        await _take_screenshot(run_dir, step, "08_after_get_text")

    return GeminiResponse(text=text, images=[])


async def _take_screenshot(run_dir: Path, step_num: int, step_name: str) -> str | None:
    """Take screenshot via Playwright. Returns path or None on error."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{step_num:02d}_{step_name}.png"
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
        await page.screenshot(path=str(path))
        logger.info("[attachment] screenshot: %s", path)
        return str(path)
    except Exception as e:
        logger.warning("[attachment] screenshot failed: %s", e)
        return None
    finally:
        if pw:
            await pw.stop()


async def _upload_attachments(file_paths: list[str]) -> None:
    """Upload attachment files. Uses CDP to bypass 50MB limit."""
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
        await upload_files(page, file_paths)
    finally:
        if pw:
            await pw.stop()


async def _wait_for_copy_button():
    """Poll until copy button appears (response is ready)."""
    # Initial wait for response to start
    await asyncio.sleep(2)

    elapsed = 0
    while elapsed < MAX_POLL_TIME:
        try:
            dom_json = await chrome.distill_dom(as_json=True)
            # Parse JSON from output
            start = dom_json.find('[')
            if start >= 0:
                items = json.loads(dom_json[start:])
                copy_buttons = [
                    item for item in items
                    if item.get('aria_label') == 'Copy' and item['tag'] == 'button'
                ]
                if copy_buttons:
                    # Copy button found, response is ready
                    await asyncio.sleep(0.5)
                    return
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    # Timeout - proceed anyway
    return


async def _is_send_button_clickable() -> bool:
    """Check if Send button is clickable (aria-disabled is not 'true')."""
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
        clickable = await page.evaluate("""() => {
            for (const sel of [
                '[aria-label="Send message"]',
                'button[aria-label="Send message"]',
                '[aria-label="Send"]',
                'button[aria-label="Send"]'
            ]) {
                const el = document.querySelector(sel);
                if (el) {
                    const ariaDisabled = el.getAttribute('aria-disabled');
                    return ariaDisabled !== 'true';
                }
            }
            return false;
        }""")
        return bool(clickable)
    except Exception:
        return False
    finally:
        if pw:
            await pw.stop()


async def _wait_for_send_button_clickable() -> None:
    """Poll until Send button is clickable (aria-disabled !== 'true'), max 10 minutes.
    Raises TimeoutError if button never becomes clickable."""
    elapsed = 0
    while elapsed < SEND_BUTTON_WAIT_TIMEOUT:
        if await _is_send_button_clickable():
            logger.info("[send] Send button is clickable after %.0fs", elapsed)
            return
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        if elapsed % 30 == 0 and elapsed > 0:
            logger.info("[send] Waiting for Send button to become clickable... %.0fs elapsed", elapsed)
    raise TimeoutError(
        f"Send button did not become clickable within {SEND_BUTTON_WAIT_TIMEOUT}s "
        "(aria-disabled remained true)"
    )


async def _check_copy_button_exists() -> bool:
    """Check if Copy button is present in DOM. Used for job logging."""
    try:
        dom_json = await chrome.distill_dom(as_json=True)
        start = dom_json.find("[")
        if start >= 0:
            items = json.loads(dom_json[start:])
            copy_buttons = [
                item for item in items
                if item.get("aria_label") == "Copy" and item.get("tag") == "button"
            ]
            return len(copy_buttons) > 0
    except Exception:
        pass
    return False


async def _get_text_response() -> str:
    """Get full Gemini response: prefer copy button (clipboard), fallback to DOM extraction."""
    # Log whether copy button exists before copy operation
    has_copy_btn = await _check_copy_button_exists()
    logger.info("[job] copy button exists before copy: %s", has_copy_btn)

    # 1. Try copy button first (default)
    try:
        await chrome.run_cmd("act", "--selector", COPY_BUTTON_SELECTOR, "--action", "click")
        await asyncio.sleep(0.5)
        result = subprocess.run(["pbpaste"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    # 2. Fallback to DOM extraction
    text = await _get_text_response_via_dom()
    if text and text.strip():
        return text.strip()

    return ""


async def _get_text_response_via_dom() -> str:
    """Extract full response text from DOM via CDP. More reliable than clipboard."""
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()

        text = await page.evaluate("""() => {
            // Find Copy button, then get response from its container
            const copyBtn = document.querySelector('[aria-label="Copy"]');
            if (!copyBtn) return '';

            // Traverse up to find message/response container
            let el = copyBtn;
            for (let i = 0; i < 15 && el; i++) {
                el = el.parentElement;
                if (!el) break;
                let raw = el.innerText || el.textContent || '';
                raw = raw.replace(/\\s+/g, ' ').trim();
                // Response usually 50+ chars; exclude toolbar-only (Copy, Regenerate, etc.)
                if (raw.length > 80 && !/^\\s*(Copy|Regenerate|Thumbs|More)\\s*$/i.test(raw)) {
                    // Strip trailing toolbar labels
                    return raw.replace(/\\s*(Copy|Regenerate|Thumbs up|Thumbs down|More)\\s*$/gi, '').trim();
                }
            }

            // Fallback: last model message block
            const blocks = document.querySelectorAll('[data-message-author-role="model"], [class*="model-response"], [class*="message-content"]');
            for (let i = blocks.length - 1; i >= 0; i--) {
                const t = (blocks[i].innerText || blocks[i].textContent || '').replace(/\\s+/g, ' ').trim();
                if (t.length > 50) return t;
            }
            return '';
        }""")
        return text or ""
    except Exception:
        return ""
    finally:
        if pw:
            await pw.stop()
