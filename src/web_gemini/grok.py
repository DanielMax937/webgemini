"""Browser automation for Grok on X (https://x.com/i/grok). Mirrors ``gemini.py`` flow: navigate → type → send → wait → copy/DOM."""

import asyncio
import logging

from playwright.async_api import Page

from .concurrency import clipboard_section, USE_DOM_EXTRACTION
from .gemini import GeminiResponse, _wait_for_copy_button

logger = logging.getLogger(__name__)

GROK_URL = "https://x.com/i/grok"

# X / Grok UI changes often — try visible composers in order
GROK_INPUT_SELECTORS = [
    'textarea[placeholder*="Ask"]',
    'textarea[placeholder*="Grok"]',
    'textarea[placeholder*="What"]',
    'div[data-testid="tweetTextarea_0"]',
    'div[role="textbox"][data-testid="tweetTextarea_0"]',
    'div[role="textbox"][contenteditable="true"]',
    "textarea",
]

COPY_BUTTON_SELECTOR = '[aria-label="Copy"]'


async def send_prompt(
    prompt: str,
    tool: str | None = None,
    attachments: list[str] | None = None,
    page: Page | None = None,
    job_id: str = "",
) -> GeminiResponse:
    """Send prompt to Grok on X and wait for response.

    Args:
        prompt: The prompt to send
        tool: Ignored (Grok web has no Gemini-style Tools menu)
        attachments: Not uploaded in this integration (logged only)
        page: Playwright Page bound to this task's dedicated tab (required)
        job_id: Task ID used for clipboard mutex and logging
    """
    if page is None:
        raise ValueError("send_prompt requires a 'page' argument (task-bound tab)")

    if tool:
        logger.info("[grok] job=%s ignoring tool=%s (not supported)", job_id, tool)
    if attachments:
        logger.warning("[grok] job=%s attachments not supported; paths=%s", job_id, attachments)

    await page.goto(GROK_URL, wait_until="domcontentloaded", timeout=120_000)
    await asyncio.sleep(3)

    await _type_and_send_grok(page, prompt, job_id)
    await asyncio.sleep(2)
    await _wait_for_copy_button(page)
    await asyncio.sleep(0.5)

    text = await _get_text_response(page, job_id)
    if not text.strip():
        text = await _get_grok_text_via_dom(page)

    return GeminiResponse(text=text.strip(), images=[])


async def _type_and_send_grok(page: Page, prompt: str, job_id: str) -> None:
    """Focus Grok composer on *page*, insert text, send via Ctrl+Enter."""
    for sel in GROK_INPUT_SELECTORS:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=15_000)
            await loc.click(timeout=10_000)
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
            is_ce = await loc.evaluate("el => el.getAttribute('contenteditable') === 'true'")
            if tag == "textarea" or not is_ce:
                try:
                    await loc.fill(prompt, timeout=30_000)
                except Exception:
                    await loc.press_sequentially(prompt, delay=15)
            else:
                await loc.evaluate(
                    """(el, text) => {
                      el.focus();
                      el.innerHTML = '';
                      document.execCommand('insertText', false, text);
                    }""",
                    prompt,
                )
            await asyncio.sleep(0.4)
            await page.keyboard.press("Control+Enter")
            logger.info("[grok] job=%s sent via selector %s", job_id, sel)
            return
        except Exception as e:
            logger.debug("[grok] job=%s selector %s failed: %s", job_id, sel, str(e)[:120])
            continue

    raise RuntimeError(
        "Could not find Grok input (textarea/contenteditable). "
        "Open https://x.com/i/grok in the automated Chrome and check the composer DOM."
    )


async def _get_text_response(page: Page, job_id: str) -> str:
    """Get Grok response: clipboard (mutex) first, fallback to DOM."""
    if USE_DOM_EXTRACTION:
        logger.info("[grok] job=%s using DOM extraction (clipboard skipped)", job_id)
        return await _get_grok_text_via_dom(page)

    clipboard_text: str = ""
    try:
        async with clipboard_section(job_id):
            await page.locator(COPY_BUTTON_SELECTOR).first.click(timeout=30_000)
            await asyncio.sleep(0.5)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: __import__("subprocess").run(
                    ["pbpaste"], capture_output=True, text=True
                ),
            )
            if result.returncode == 0:
                clipboard_text = result.stdout
    except Exception as e:
        logger.warning("[grok] job=%s clipboard path failed: %s", job_id, e)

    if clipboard_text.strip():
        return clipboard_text.strip()

    return await _get_grok_text_via_dom(page)


async def _get_grok_text_via_dom(page: Page) -> str:
    """Extract last Grok reply from X DOM of *page*."""
    try:
        text = await page.evaluate("""() => {
          const copyBtn = document.querySelector('[aria-label="Copy"]');
          if (copyBtn) {
            let el = copyBtn;
            for (let i = 0; i < 20 && el; i++) {
              el = el.parentElement;
              if (!el) break;
              let raw = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
              if (raw.length > 80) {
                return raw.replace(/\\s*(Copy|More|Regenerate)\\s*$/gi, '').trim();
              }
            }
          }
          const articles = document.querySelectorAll('article, [data-testid="tweet"]');
          for (let i = articles.length - 1; i >= 0; i--) {
            const t = (articles[i].innerText || '').replace(/\\s+/g, ' ').trim();
            if (t.length > 40) return t;
          }
          return '';
        }""")
        return text or ""
    except Exception:
        return ""

