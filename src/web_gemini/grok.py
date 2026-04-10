"""Browser automation for Grok on X (https://x.com/i/grok). Mirrors ``gemini.py`` flow: navigate → type → send → wait → copy/DOM."""

import asyncio
import logging
import re
import time

from playwright.async_api import Locator, Page

from .concurrency import clipboard_section, USE_DOM_EXTRACTION
from .gemini import COPY_BUTTON_SELECTOR, GeminiResponse, _wait_for_copy_button

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

# X / Grok composer send: prefer DOM click (Ctrl+Enter alone is unreliable on macOS / some builds)
# Primary: Grok composer uses aria-label like "Grok something" (see button[type=button] next to input)
GROK_SEND_BUTTON_SELECTORS = [
    'button[aria-label="Grok something"]',
    'button[aria-label^="Grok"]',
    'button[type="button"][aria-label^="Grok"]',
    # Fallback: generic X compose
    'button[data-testid="tweetButton"]',
    'div[data-testid="tweetButton"]',
    '[data-testid="tweetButtonInline"]',
    'button[data-testid="tweetButtonInline"]',
    'button[aria-label="Post"]',
    'div[role="button"][aria-label="Post"]',
    'button[aria-label*="Send"]',
    'div[role="button"][aria-label*="Send"]',
    'button[aria-label*="发送"]',
    'div[role="button"][aria-label*="发送"]',
]


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


async def _click_grok_send_button(page: Page, job_id: str) -> None:
    """Click the Grok/X send (Post) control in the DOM; fallback to keyboard shortcut."""
    # 1) CSS selectors first (Grok send: button[aria-label^="Grok"] — composer is usually last match)
    for sel in GROK_SEND_BUTTON_SELECTORS:
        loc = page.locator(sel).last
        try:
            await loc.wait_for(state="visible", timeout=10_000)
            await _wait_until_send_clickable(loc, job_id)
            await loc.click(timeout=15_000)
            logger.info("[grok] job=%s clicked send via CSS selector %s", job_id, sel)
            return
        except Exception as e:
            logger.debug("[grok] job=%s send selector %s: %s", job_id, sel, str(e)[:120])

    # 2) Playwright role-based (Post / Send / 发送 — i18n)
    for pattern in (r"Post", r"Send", r"发送", r"Grok"):
        try:
            btn = page.get_by_role("button", name=re.compile(pattern, re.I))
            n = await btn.count()
            if n == 0:
                continue
            target = btn.last
            await target.wait_for(state="visible", timeout=10_000)
            await _wait_until_send_clickable(target, job_id)
            await target.click(timeout=15_000)
            logger.info("[grok] job=%s clicked send via get_by_role pattern %s", job_id, pattern)
            return
        except Exception as e:
            logger.debug("[grok] job=%s get_by_role %s: %s", job_id, pattern, str(e)[:120])

    # 3) Broad DOM fallback: any visible, enabled tweet/send-like control
    try:
        clicked = await page.evaluate("""() => {
          const sels = [
            'button[aria-label^="Grok"]',
            'button[aria-label="Grok something"]',
            '[data-testid="tweetButton"]',
            '[data-testid="tweetButtonInline"]',
            'button[aria-label="Post"]',
            'button[aria-label*="Send"]',
            'div[role="button"][aria-label*="Send"]',
            'div[role="button"][aria-label="Post"]',
          ];
          for (const s of sels) {
            const nodes = document.querySelectorAll(s);
            for (let i = nodes.length - 1; i >= 0; i--) {
              const el = nodes[i];
              if (!el || el.offsetParent === null) continue;
              if (el.getAttribute('aria-disabled') === 'true') continue;
              if (el.hasAttribute('disabled')) continue;
              el.click();
              return true;
            }
          }
          return false;
        }""")
        if clicked:
            logger.info("[grok] job=%s clicked send via JS DOM fallback", job_id)
            return
    except Exception as e:
        logger.debug("[grok] job=%s JS send fallback failed: %s", job_id, str(e)[:120])

    # 4) Keyboard: single Enter (Grok/X composer submits with Enter per user feedback)
    await page.keyboard.press("Enter")
    logger.info("[grok] job=%s sent via Enter (keyboard fallback)", job_id)


async def _wait_until_send_clickable(loc: Locator, job_id: str, timeout_s: float = 12.0) -> None:
    """Wait until X enables the Post/Send button after text is in the composer."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if await loc.is_enabled():
                return
        except Exception:
            pass
        await asyncio.sleep(0.15)
    logger.warning("[grok] job=%s send button still disabled after wait; clicking anyway", job_id)


async def _type_and_send_grok(page: Page, prompt: str, job_id: str) -> None:
    """Focus Grok composer on *page*, insert text, click send (DOM), then keyboard fallback."""
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
            await _click_grok_send_button(page, job_id)
            logger.info("[grok] job=%s message submitted after composer selector %s", job_id, sel)
            return
        except Exception as e:
            logger.debug("[grok] job=%s selector %s failed: %s", job_id, sel, str(e)[:120])
            continue

    raise RuntimeError(
        "Could not find Grok input (textarea/contenteditable). "
        "Open https://x.com/i/grok in the automated Chrome and check the composer DOM."
    )


async def _grok_open_copy_menu_and_click_copy_text(page: Page, job_id: str) -> None:
    """Grok/X: toolbar Copy opens a dropdown; must click menuitem 'Copy text' to copy."""
    await page.locator(COPY_BUTTON_SELECTOR).last.click(timeout=30_000)
    await asyncio.sleep(0.35)

    try:
        mi = page.get_by_role("menuitem", name="Copy text")
        await mi.last.wait_for(state="visible", timeout=12_000)
        await mi.last.click(timeout=15_000)
        logger.info("[grok] job=%s clicked dropdown menuitem Copy text (get_by_role)", job_id)
        return
    except Exception as e:
        logger.debug("[grok] job=%s menuitem get_by_role: %s", job_id, str(e)[:120])

    try:
        loc = page.locator('[role="menuitem"]').filter(has_text="Copy text").last
        await loc.wait_for(state="visible", timeout=12_000)
        await loc.click(timeout=15_000)
        logger.info("[grok] job=%s clicked dropdown menuitem Copy text (locator filter)", job_id)
        return
    except Exception as e:
        logger.debug("[grok] job=%s menuitem filter: %s", job_id, str(e)[:120])

    try:
        loc = page.locator("#react-root [role=\"menuitem\"]").filter(has_text="Copy text").last
        await loc.wait_for(state="visible", timeout=8_000)
        await loc.click(timeout=15_000)
        logger.info("[grok] job=%s clicked #react-root menuitem Copy text", job_id)
        return
    except Exception as e:
        logger.debug("[grok] job=%s react-root menuitem: %s", job_id, str(e)[:120])

    raise RuntimeError(
        "Grok copy: opened toolbar menu but could not click menuitem 'Copy text'. "
        "Check #react-root [role=menuitem] in DevTools."
    )


async def _get_text_response(page: Page, job_id: str) -> str:
    """Get Grok response: clipboard (mutex) first, fallback to DOM."""
    if USE_DOM_EXTRACTION:
        logger.info("[grok] job=%s using DOM extraction (clipboard skipped)", job_id)
        return await _get_grok_text_via_dom(page)

    clipboard_text: str = ""
    try:
        async with clipboard_section(job_id):
            await _grok_open_copy_menu_and_click_copy_text(page, job_id)
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
          const copyBtn = document.querySelector('button[aria-label="Copy text"]')
            || document.querySelector('button[aria-label="Copy"]')
            || document.querySelector('[aria-label="Copy"]');
          if (copyBtn) {
            let el = copyBtn;
            for (let i = 0; i < 20 && el; i++) {
              el = el.parentElement;
              if (!el) break;
              let raw = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
              if (raw.length > 80) {
                return raw.replace(/\\s*(Copy text|Copy|More|Regenerate)\\s*$/gi, '').trim();
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

