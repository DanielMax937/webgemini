"""Browser automation for Grok on X (https://x.com/i/grok). Mirrors ``gemini.py`` flow: navigate → type → send → wait → copy/DOM."""

import asyncio
import logging
import subprocess

from playwright.async_api import async_playwright

from .browser import chrome
from .chrome_automation.paths import CDP_URL
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
) -> GeminiResponse:
    """Send prompt to Grok on X and wait for response (same contract as ``gemini.send_prompt``).

    ``tool`` is ignored (Grok web has no Gemini-style Tools menu in this integration).
    ``attachments`` are not uploaded in this version (logged); use Gemini ``/chat`` for file flows.
    """
    if tool:
        logger.info("[grok] ignoring tool=%s (not supported on Grok web)", tool)
    if attachments:
        logger.warning(
            "[grok] attachments are not supported on Grok web in this build; paths=%s",
            attachments,
        )

    await chrome.navigate_to_gemini_with_retry(GROK_URL)
    await asyncio.sleep(3)

    await _type_and_send_grok(prompt)
    await asyncio.sleep(2)
    await _wait_for_copy_button()
    await asyncio.sleep(0.5)

    text = await _get_text_response()
    if not text.strip():
        text = await _get_grok_text_via_dom()

    return GeminiResponse(text=text.strip(), images=[])


async def _type_and_send_grok(prompt: str) -> None:
    """Focus Grok composer, insert text, send (Ctrl+Enter).

    Grok's composer is multiline: plain Enter only inserts a newline; Ctrl+Enter submits
    (matches X web UI). Uses Playwright over CDP.
    """
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()

        for sel in GROK_INPUT_SELECTORS:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=15_000)
                await loc.click(timeout=10_000)
                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                is_ce = await loc.evaluate(
                    "el => el.getAttribute('contenteditable') === 'true'"
                )
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
                logger.info("[grok] sent via selector %s", sel)
                return
            except Exception as e:
                logger.debug("[grok] selector %s failed: %s", sel, str(e)[:120])
                continue

        raise RuntimeError(
            "Could not find Grok input (textarea/contenteditable). "
            "Open https://x.com/i/grok in the automated Chrome and check the composer DOM."
        )
    finally:
        if pw:
            await pw.stop()


async def _get_text_response() -> str:
    """Prefer Copy button + clipboard; fallback to Grok/X DOM extraction."""
    try:
        await chrome.run_cmd("act", "--selector", COPY_BUTTON_SELECTOR, "--action", "click")
        await asyncio.sleep(0.5)
        result = subprocess.run(["pbpaste"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return await _get_grok_text_via_dom()


async def _get_grok_text_via_dom() -> str:
    """Extract last Grok reply from X DOM."""
    pw = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
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
    finally:
        if pw:
            await pw.stop()
