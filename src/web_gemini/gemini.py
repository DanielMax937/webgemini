import asyncio
import json
import logging
import os
import re
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
DEEP_RESEARCH_LAYOUT_LOG_DIR = Path(__file__).parent.parent.parent / "outputs" / "deep_research_layout_logs"
_last_dr_body_layout_log_at: dict[str, float] = {}
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
# Gemini: "Copy"; Grok / X: "Copy text"
COPY_BUTTON_SELECTOR = (
    'button[aria-label="Copy text"], button[aria-label="Copy"], [aria-label="Copy"]'
)
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'
# Tool selectors
TOOL_SELECTORS = {
    "deep_research": 'button:has-text("Deep Research")',
    "video": 'button:has-text("Create video")',
    "image": 'button:has-text("Create image")',
    "canvas": 'button:has-text("Canvas")',
    "tutor": 'button:has-text("Help me learn")',
}

# After first "开始研究", Gemini shows a plan card with a confirmation URL; user must confirm again to run.
_DEFAULT_DEEP_RESEARCH_LINK_MARKERS = (
    "deep_research_confirmation",
    "googleusercontent.com/deep_research",
)


def _deep_research_link_markers() -> tuple[str, ...]:
    raw = os.environ.get(
        "WG_DEEP_RESEARCH_LINK_MARKERS",
        "deep_research_confirmation,googleusercontent.com/deep_research",
    )
    parts = tuple(s.strip() for s in raw.split(",") if s.strip())
    return parts if parts else _DEFAULT_DEEP_RESEARCH_LINK_MARKERS


async def _page_contains_deep_research_confirmation_link(page: Page, markers: tuple[str, ...]) -> bool:
    """True if plan confirmation URL (or marker text) is present.

    Gemini often renders the plan inside shadow-heavy message trees; ``body.innerText``
    alone can miss it, so we also use Playwright text/href locators.
    """
    try:
        if await page.evaluate(
            """(markers) => {
                const hay = ((document.body && document.body.innerText) || '') +
                    ' ' + ((document.documentElement && document.documentElement.innerHTML) || '');
                const hrefs = Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href || '').join(' ');
                const all = hay + ' ' + hrefs;
                return markers.some(m => m && all.includes(m));
            }""",
            list(markers),
        ):
            return True
    except Exception:
        pass

    try:
        if await page.locator('a[href*="deep_research_confirmation"]').count() > 0:
            return True
        if await page.locator('a[href*="googleusercontent.com"][href*="deep_research"]').count() > 0:
            return True
    except Exception:
        pass

    for m in markers:
        if not m:
            continue
        try:
            if await page.get_by_text(m, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def _deep_research_body_log_enabled() -> bool:
    return os.environ.get("WG_DEEP_RESEARCH_BODY_LOG", "1").lower() not in ("0", "false", "no")


def _deep_research_body_log_interval_s() -> float:
    return float(os.environ.get("WG_DEEP_RESEARCH_BODY_LOG_INTERVAL_S", "15"))


# Gemini 底部发送区：是否在「提交/发送」加载态（转圈、aria-busy、progressbar 等）
GEMINI_SUBMIT_SEND_LOADING_PROBE_JS = r"""() => {
    const sendSels = [
        '[aria-label="Send message"]',
        'button[aria-label="Send message"]',
        '[aria-label="Send"]',
        'button[aria-label="Send"]',
    ];
    let btn = null;
    let usedSel = '';
    for (const s of sendSels) {
        const el = document.querySelector(s);
        if (el) {
            btn = el;
            usedSel = s;
            break;
        }
    }
    if (!btn) {
        return {
            found: false,
            submitLoading: null,
            detail: 'no_send_button_in_dom',
        };
    }
    const ariaBusy = btn.getAttribute('aria-busy') === 'true';
    const ariaDisabled = btn.getAttribute('aria-disabled') === 'true';
    const spinSelectors = [
        'mat-progress-spinner',
        'mat-spinner',
        '[role="progressbar"]',
        '.loading-spinner',
        '[class*="spinner"]',
        '[class*="Spinner"]',
        '[class*="circular-progress"]',
        '[class*="CircularProgress"]',
        'svg[class*="spinner"]',
        'div[class*="throbber"]',
    ];
    let spinnerInSend = false;
    for (const ss of spinSelectors) {
        if (btn.querySelector(ss)) {
            spinnerInSend = true;
            break;
        }
    }
    const composer =
        btn.closest('[class*="input"]') ||
        btn.closest('footer') ||
        btn.closest('[class*="composer"]') ||
        btn.parentElement?.parentElement ||
        document.body;
    let spinnerNearSubmitBar = false;
    const bbr = btn.getBoundingClientRect();
    for (const ss of spinSelectors) {
        composer.querySelectorAll(ss).forEach((node) => {
            if (spinnerNearSubmitBar || !node || btn.contains(node)) return;
            const br = node.getBoundingClientRect?.();
            if (!br || br.width < 2 || br.height < 2) return;
            const dx = Math.abs(br.x + br.width / 2 - (bbr.x + bbr.width / 2));
            const dy = Math.abs(br.y + br.height / 2 - (bbr.y + bbr.height / 2));
            if (dx < 420 && dy < 140) spinnerNearSubmitBar = true;
        });
    }
    const cls = String(btn.className || '').slice(0, 240);
    const dataBusy =
        btn.getAttribute('data-loading') === 'true' ||
        btn.getAttribute('data-busy') === 'true';
    const submitLoading =
        ariaBusy ||
        dataBusy ||
        spinnerInSend ||
        (ariaDisabled && spinnerNearSubmitBar);
    return {
        found: true,
        usedSel,
        ariaBusy,
        ariaDisabled,
        dataBusyAttrs: dataBusy,
        spinnerInSendButton: spinnerInSend,
        spinnerNearSubmitBar,
        submitLoading,
        buttonClassPreview: cls,
    };
}"""


async def _probe_gemini_submit_send_loading(page: Page) -> dict:
    """Detect whether the Gemini send/submit control still looks loading (spinner / busy)."""
    try:
        raw = await page.evaluate(GEMINI_SUBMIT_SEND_LOADING_PROBE_JS)
        return raw if isinstance(raw, dict) else {"found": False, "detail": "bad_probe_type"}
    except Exception as e:
        return {"found": False, "detail": ("evaluate_failed:" + str(e))[:200]}


async def _dump_deep_research_body_html(page: Page, job_id: str, stem: str) -> Optional[Path]:
    """Write ``document.body.outerHTML`` under ``outputs/deep_research_layout_logs/<job_id>/`` (no probe)."""
    out_dir = DEEP_RESEARCH_LAYOUT_LOG_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = out_dir / f"{stem}_{ts}.html"
    try:
        html = await page.evaluate("() => (document.body && document.body.outerHTML) || ''")
    except Exception as e:
        logger.warning("[deep_research] job=%s body HTML dump %s failed: %s", job_id, stem, e)
        return None
    max_bytes = int(os.environ.get("WG_DEEP_RESEARCH_BODY_LOG_MAX_BYTES", "12000000"))
    raw = html.encode("utf-8")
    if len(raw) > max_bytes:
        html = raw[:max_bytes].decode("utf-8", errors="ignore") + (
            "\n<!-- TRUNCATED: WG_DEEP_RESEARCH_BODY_LOG_MAX_BYTES -->"
        )
    try:
        path.write_text(html, encoding="utf-8")
    except OSError as e:
        logger.warning("[deep_research] job=%s write body HTML %s: %s", job_id, path, e)
        return None
    return path


async def _wait_until_deep_research_submit_not_loading_for_export(page: Page, job_id: str) -> bool:
    """Wait until chat send area is not in loading state; required before clicking Share/Export."""
    timeout_s = int(os.environ.get("WG_DEEP_RESEARCH_EXPORT_WAIT_NOT_SPINNING_S", "300"))
    poll = float(os.environ.get("WG_DEEP_RESEARCH_EXPORT_SPIN_POLL_S", "2"))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = await _probe_gemini_submit_send_loading(page)
        if not st.get("found"):
            logger.debug(
                "[deep_research] job=%s export_preflight: send control not in DOM; treating as not spinning",
                job_id,
            )
            return True
        if st.get("submitLoading") is False:
            logger.debug("[deep_research] job=%s export_preflight: send area idle (not spinning)", job_id)
            return True
        logger.debug(
            "[deep_research] job=%s export_preflight: send area still loading; wait (ariaBusy=%s "
            "spinInBtn=%s spinNearBar=%s)",
            job_id,
            st.get("ariaBusy"),
            st.get("spinnerInSendButton"),
            st.get("spinnerNearSubmitBar"),
        )
        await asyncio.sleep(poll)
    logger.warning(
        "[deep_research] job=%s export_preflight: still loading after %ss — skip Share/Export click",
        job_id,
        timeout_s,
    )
    return False


async def _deep_research_click_share_export_when_ready_and_log(page: Page, job_id: str) -> bool:
    """When send bar is idle, click Share & Export (or 分享/导出), then dump body HTML for menu inspection.

    Returns True if the Share & Export control was clicked successfully.
    """
    if os.environ.get("WG_DEEP_RESEARCH_EXPORT_CLICK", "1").lower() in ("0", "false", "no"):
        logger.debug("[deep_research] job=%s Share/Export step disabled (WG_DEEP_RESEARCH_EXPORT_CLICK)", job_id)
        return False
    if not await _wait_until_deep_research_submit_not_loading_for_export(page, job_id):
        await _dump_deep_research_body_html(page, job_id, "export_skipped_still_spinning")
        return False
    await _dump_deep_research_body_html(page, job_id, "before_export_click")
    clicked = await page.evaluate("""() => {
        const buttons = Array.from(document.querySelectorAll('button'));
        for (const b of buttons) {
            const t = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!t) continue;
            if (/share/i.test(t) && /export/i.test(t)) {
                b.click();
                return { ok: true, label: t.slice(0, 120) };
            }
            if (/分享/.test(t) && /导出/.test(t)) {
                b.click();
                return { ok: true, label: t.slice(0, 120) };
            }
        }
        return { ok: false, label: '' };
    }""")
    if not isinstance(clicked, dict) or not clicked.get("ok"):
        logger.warning(
            "[deep_research] job=%s Share/Export button not found or not clicked (dom scan)",
            job_id,
        )
        await _dump_deep_research_body_html(page, job_id, "after_export_click_failed")
        return False
    logger.debug("[deep_research] job=%s clicked Share/Export label=%r", job_id, clicked.get("label"))
    post_wait = float(os.environ.get("WG_DEEP_RESEARCH_EXPORT_POST_CLICK_WAIT_S", "2"))
    await asyncio.sleep(post_wait)
    await _dump_deep_research_body_html(page, job_id, "after_export_click")
    await asyncio.sleep(1.0)
    await _dump_deep_research_body_html(page, job_id, "after_export_click_plus1s")
    return True


async def _deep_research_click_copy_contents_in_export_menu(page: Page, job_id: str) -> bool:
    """After Share & Export, click **Copy contents** in the overlay menu (full report to clipboard)."""
    if os.environ.get("WG_DEEP_RESEARCH_COPY_CONTENTS_CLICK", "1").lower() in ("0", "false", "no"):
        logger.debug("[deep_research] job=%s Copy contents step disabled (WG_DEEP_RESEARCH_COPY_CONTENTS_CLICK)", job_id)
        return False
    timeout_s = int(os.environ.get("WG_DEEP_RESEARCH_COPY_CONTENTS_TIMEOUT_S", "45"))
    poll_s = float(os.environ.get("WG_DEEP_RESEARCH_COPY_CONTENTS_POLL_S", "0.5"))
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        try:
            mi = page.locator('[role="menuitem"]').filter(has_text=re.compile(r"copy\s*contents", re.I))
            if await mi.count() > 0:
                await mi.first.click(timeout=15_000)
                logger.debug("[deep_research] job=%s clicked Copy contents (menuitem copy contents)", job_id)
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[deep_research] job=%s menuitem en: %s", job_id, str(e)[:100])

        try:
            mi_zh = page.locator('[role="menuitem"]').filter(has_text="复制内容")
            if await mi_zh.count() > 0:
                await mi_zh.first.click(timeout=15_000)
                logger.debug("[deep_research] job=%s clicked Copy contents (menuitem 复制内容)", job_id)
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[deep_research] job=%s menuitem zh: %s", job_id, str(e)[:100])

        try:
            clicked = await page.evaluate("""() => {
                const spans = Array.from(document.querySelectorAll('span.mat-mdc-menu-item-text'));
                for (const s of spans) {
                    const t = (s.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (/^copy\\s*contents$/i.test(t) || t === '复制内容') {
                        const btn = s.closest('button');
                        if (btn) {
                            btn.click();
                            return { ok: true, label: t };
                        }
                    }
                }
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (/copy\\s*contents/i.test(t) || /^复制内容$/.test(t)) {
                        b.click();
                        return { ok: true, label: t.slice(0, 120) };
                    }
                }
                return { ok: false, label: '' };
            }""")
            if isinstance(clicked, dict) and clicked.get("ok"):
                logger.debug(
                    "[deep_research] job=%s clicked Copy contents (dom) label=%r",
                    job_id,
                    clicked.get("label"),
                )
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[deep_research] job=%s dom copy contents: %s", job_id, str(e)[:120])

        await asyncio.sleep(poll_s)

    logger.warning(
        "[deep_research] job=%s Copy contents not found within %ss",
        job_id,
        timeout_s,
    )
    await _dump_deep_research_body_html(page, job_id, "copy_contents_not_found")
    return False


async def _maybe_log_deep_research_body_layout_snapshot(
    page: Page,
    job_id: str,
    phase: str,
    poll_seq: int,
    *,
    force: bool = False,
) -> None:
    """While polling Deep Research UI, save ``document.body.outerHTML`` + a small layout probe (e.g. top-level columns).

    Files under ``outputs/deep_research_layout_logs/<job_id>/`` (gitignored). Throttled by
    ``WG_DEEP_RESEARCH_BODY_LOG_INTERVAL_S`` per ``job_id:phase`` unless ``force`` is True.
    """
    if not _deep_research_body_log_enabled():
        return
    key = f"{job_id}:{phase}"
    now = time.monotonic()
    interval = _deep_research_body_log_interval_s()
    if not force and now - _last_dr_body_layout_log_at.get(key, 0) < interval:
        return
    _last_dr_body_layout_log_at[key] = now

    out_dir = DEEP_RESEARCH_LAYOUT_LOG_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    stem = f"{phase}_poll{poll_seq:05d}_{ts}"

    try:
        pack = await page.evaluate(
            """() => {
                const b = document.body;
                if (!b) return { html: '', probe: { error: 'no_body' } };
                const html = b.outerHTML;
                const br = b.getBoundingClientRect();
                const topChildren = Array.from(b.children).map(c => {
                    const cr = c.getBoundingClientRect();
                    return {
                        tag: c.tagName,
                        id: c.id || '',
                        cls: String(c.className || '').slice(0, 220),
                        w: Math.round(cr.width),
                        h: Math.round(cr.height),
                        x: Math.round(cr.x),
                        y: Math.round(cr.y),
                    };
                });
                const minWide = Math.min(360, Math.max(200, br.width * 0.22));
                const wideTop = topChildren.filter(t => t.w >= minWide);
                return {
                    html,
                    probe: {
                        bodyRect: { w: Math.round(br.width), h: Math.round(br.height) },
                        childCount: b.children.length,
                        topChildren,
                        wideTopLayoutChildCount: wideTop.length,
                        wideTopChildren: wideTop.slice(0, 12),
                        linkHints: {
                            deep_research_confirmation: html.includes('deep_research_confirmation'),
                            googleusercontent_dr:
                                html.includes('googleusercontent') && html.includes('deep_research'),
                        },
                    },
                };
            }"""
        )
    except Exception as e:
        logger.warning("[deep_research] job=%s layout snapshot evaluate failed: %s", job_id, e)
        return

    html = pack.get("html") or ""
    probe = pack.get("probe") or {}
    submit_probe = await _probe_gemini_submit_send_loading(page)
    probe["submitSendButton"] = submit_probe
    max_bytes = int(os.environ.get("WG_DEEP_RESEARCH_BODY_LOG_MAX_BYTES", "12000000"))
    raw = html.encode("utf-8")
    partial = False
    if len(raw) > max_bytes:
        html = raw[:max_bytes].decode("utf-8", errors="ignore") + (
            "\n<!-- TRUNCATED: WG_DEEP_RESEARCH_BODY_LOG_MAX_BYTES -->"
        )
        partial = True

    html_path = out_dir / f"{stem}.html"
    probe_path = out_dir / f"{stem}.probe.json"
    try:
        html_path.write_text(html, encoding="utf-8")
        probe_path.write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("[deep_research] job=%s layout write failed: %s", job_id, e)
        return

    ss = probe.get("submitSendButton")
    logger.debug(
        "[deep_research] job=%s layout_snapshot phase=%s poll=%d html_bytes=%d partial=%s "
        "html=%s probe=%s submit_found=%s submit_loading=%s",
        job_id,
        phase,
        poll_seq,
        len(html.encode("utf-8")),
        partial,
        html_path.name,
        probe_path.name,
        isinstance(ss, dict) and ss.get("found"),
        isinstance(ss, dict) and ss.get("submitLoading"),
    )


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
        tool: Optional tool. One of: deep_research (uses longer copy-button poll; see WG_DEEP_RESEARCH_MAX_POLL_S), video, image, canvas, tutor
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
        logger.debug("[attachment] job=%s screenshot run_dir: %s", job_id, run_dir)

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
        logger.debug("[send] job=%s Send button not found, falling back to Enter", job_id)
        await page.locator(INPUT_SELECTOR).first.press("Enter", timeout=30_000)
    await asyncio.sleep(0.5)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "06_after_click_send")

    if tool == "deep_research":
        await asyncio.sleep(2)
        await _confirm_deep_research_start(page, job_id)
        if run_dir:
            step += 1
            await _take_screenshot(page, run_dir, step, "06b_after_confirm_research")
        await asyncio.sleep(2)
        await _wait_deep_research_link_then_confirm_execution(page, job_id)
        if run_dir:
            step += 1
            await _take_screenshot(page, run_dir, step, "06c_after_execution_confirm")
        await asyncio.sleep(2)
    else:
        await asyncio.sleep(30)

    # Poll until copy button is available (final response is ready; only after execution confirm for deep research)
    copy_poll_limit = MAX_POLL_TIME
    deep_research_clipboard_only = False
    if tool == "deep_research":
        copy_poll_limit = int(os.environ.get("WG_DEEP_RESEARCH_MAX_POLL_S", "3600"))
        await _wait_for_copy_button(
            page,
            max_poll_time=copy_poll_limit,
            layout_log_job_id=job_id,
        )
        export_ok = await _deep_research_click_share_export_when_ready_and_log(page, job_id)
        if export_ok:
            deep_research_clipboard_only = await _deep_research_click_copy_contents_in_export_menu(page, job_id)
            if not deep_research_clipboard_only:
                logger.warning(
                    "[deep_research] job=%s Copy contents failed; will extract via assistant Copy / DOM",
                    job_id,
                )
    else:
        await _wait_for_copy_button(page, max_poll_time=copy_poll_limit)
    if run_dir:
        step += 1
        await _take_screenshot(page, run_dir, step, "07_after_wait_copy")

    # Get text response
    text = await _get_text_response(page, job_id, clipboard_only=deep_research_clipboard_only)
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
        logger.debug("[attachment] screenshot: %s", path)
        return str(path)
    except Exception as e:
        logger.warning("[attachment] screenshot failed: %s", e)
        return None


async def _confirm_deep_research_start(page: Page, job_id: str) -> None:
    """After sending a Deep Research prompt, Gemini may show a second step (e.g. 'Start research' / '开始研究')."""
    timeout_s = int(os.environ.get("WG_DEEP_RESEARCH_CONFIRM_TIMEOUT_S", "120"))
    poll = float(os.environ.get("WG_DEEP_RESEARCH_CONFIRM_POLL_S", "2"))
    deadline = time.monotonic() + timeout_s
    role_names = (
        "Start research",
        "Start Research",
        "开始研究",
        "Begin research",
    )
    locator_selectors = (
        'button:has-text("开始研究")',
        'button:has-text("Start research")',
        'button:has-text("Start Research")',
        'button:has-text("Begin research")',
    )

    while time.monotonic() < deadline:
        for name in role_names:
            try:
                loc = page.get_by_role("button", name=name, exact=True)
                if await loc.count() == 0:
                    continue
                btn = loc.first
                if not await btn.is_visible():
                    continue
                await btn.click(timeout=15_000)
                logger.debug(
                    "[deep_research] job=%s clicked confirm (get_by_role exact=%r)",
                    job_id,
                    name,
                )
                await asyncio.sleep(1)
                return
            except Exception as e:
                logger.debug(
                    "[deep_research] job=%s role %r: %s",
                    job_id,
                    name,
                    str(e)[:120],
                )

        for sel in locator_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() == 0:
                    continue
                btn = loc.first
                if not await btn.is_visible():
                    continue
                await btn.click(timeout=15_000)
                logger.debug("[deep_research] job=%s clicked confirm (%s)", job_id, sel)
                await asyncio.sleep(1)
                return
            except Exception as e:
                logger.debug(
                    "[deep_research] job=%s selector %s: %s",
                    job_id,
                    sel,
                    str(e)[:120],
                )

        try:
            clicked = await page.evaluate("""() => {
                const needles = ['开始研究', 'Start research', 'Start Research', 'Begin research'];
                const els = document.querySelectorAll('button, [role="button"]');
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 2 || rect.height < 2) continue;
                    const t = (el.innerText || el.textContent || '')
                        .replace(/\\s+/g, ' ').trim();
                    for (const n of needles) {
                        if (t === n || (t.length < 120 && t.includes(n))) {
                            el.click();
                            return n;
                        }
                    }
                }
                return '';
            }""")
            if clicked:
                logger.debug(
                    "[deep_research] job=%s clicked confirm (dom evaluate matched=%r)",
                    job_id,
                    clicked,
                )
                await asyncio.sleep(1)
                return
        except Exception as e:
            logger.debug("[deep_research] job=%s evaluate: %s", job_id, str(e)[:120])

        await asyncio.sleep(poll)

    logger.warning(
        "[deep_research] job=%s no confirm button within %ss; continuing (locale/UI may differ)",
        job_id,
        timeout_s,
    )


async def _confirm_deep_research_execution(page: Page, job_id: str) -> None:
    """Click the second confirmation after the plan / `deep_research_confirmation` link appears (starts real run)."""
    timeout_s = int(os.environ.get("WG_DEEP_RESEARCH_EXEC_CONFIRM_TIMEOUT_S", "120"))
    poll = float(os.environ.get("WG_DEEP_RESEARCH_EXEC_CONFIRM_POLL_S", "2"))
    deadline = time.monotonic() + timeout_s
    role_names = (
        "Confirm and start",
        "确认并开始",
        "开始执行",
        "Confirm",
        "确认",
        "确定",
        "Continue",
        "继续",
    )
    locator_selectors = (
        'button:has-text("确认并开始")',
        'button:has-text("Confirm and start")',
        'button:has-text("开始执行")',
        'button:has-text("确认")',
        'button:has-text("Confirm")',
        'button:has-text("确定")',
        'button:has-text("Continue")',
        'button:has-text("继续")',
    )

    while time.monotonic() < deadline:
        for name in role_names:
            try:
                loc = page.get_by_role("button", name=name, exact=True)
                if await loc.count() == 0:
                    continue
                btn = loc.first
                if not await btn.is_visible():
                    continue
                await btn.click(timeout=15_000)
                logger.debug(
                    "[deep_research] job=%s clicked execution confirm (get_by_role exact=%r)",
                    job_id,
                    name,
                )
                await asyncio.sleep(1)
                return
            except Exception as e:
                logger.debug(
                    "[deep_research] job=%s exec role %r: %s",
                    job_id,
                    name,
                    str(e)[:120],
                )

        for sel in locator_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() == 0:
                    continue
                btn = loc.first
                if not await btn.is_visible():
                    continue
                await btn.click(timeout=15_000)
                logger.debug("[deep_research] job=%s clicked execution confirm (%s)", job_id, sel)
                await asyncio.sleep(1)
                return
            except Exception as e:
                logger.debug(
                    "[deep_research] job=%s exec selector %s: %s",
                    job_id,
                    sel,
                    str(e)[:120],
                )

        try:
            clicked = await page.evaluate("""() => {
                const needles = [
                    '确认并开始', 'Confirm and start', '开始执行', 'Confirm', '确认', '确定', 'Continue', '继续'
                ];
                const els = document.querySelectorAll('button, [role="button"]');
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 2 || rect.height < 2) continue;
                    const t = (el.innerText || el.textContent || '')
                        .replace(/\\s+/g, ' ').trim();
                    for (const n of needles) {
                        if (t === n || (t.length < 80 && t.includes(n))) {
                            el.click();
                            return n;
                        }
                    }
                }
                return '';
            }""")
            if clicked:
                logger.debug(
                    "[deep_research] job=%s clicked execution confirm (dom evaluate matched=%r)",
                    job_id,
                    clicked,
                )
                await asyncio.sleep(1)
                return
        except Exception as e:
            logger.debug("[deep_research] job=%s exec evaluate: %s", job_id, str(e)[:120])

        await asyncio.sleep(poll)

    logger.warning(
        "[deep_research] job=%s no execution confirm button within %ss; research may not start",
        job_id,
        timeout_s,
    )


async def _wait_deep_research_link_then_confirm_execution(page: Page, job_id: str) -> None:
    """Wait until plan confirmation URL is visible, then click start-execution confirm. Only then may we poll for Copy."""
    markers = _deep_research_link_markers()
    timeout_link = int(os.environ.get("WG_DEEP_RESEARCH_PLAN_LINK_TIMEOUT_S", "600"))
    poll = float(os.environ.get("WG_DEEP_RESEARCH_PLAN_LINK_POLL_S", "2"))
    deadline = time.monotonic() + timeout_link
    poll_seq = 0

    while time.monotonic() < deadline:
        sl = await _probe_gemini_submit_send_loading(page)
        if isinstance(sl, dict) and sl.get("found"):
            logger.debug(
                "[deep_research] job=%s link_poll_submit_loading poll=%d loading=%s "
                "ariaBusy=%s ariaDisabled=%s spinInBtn=%s spinNearBar=%s sel=%s",
                job_id,
                poll_seq,
                sl.get("submitLoading"),
                sl.get("ariaBusy"),
                sl.get("ariaDisabled"),
                sl.get("spinnerInSendButton"),
                sl.get("spinnerNearSubmitBar"),
                sl.get("usedSel"),
            )
        else:
            logger.debug(
                "[deep_research] job=%s link_poll_submit_probe poll=%d detail=%s",
                job_id,
                poll_seq,
                (sl or {}).get("detail"),
            )
        await _maybe_log_deep_research_body_layout_snapshot(
            page,
            job_id,
            "link_poll",
            poll_seq,
            force=(poll_seq == 0),
        )
        if await _page_contains_deep_research_confirmation_link(page, markers):
            logger.debug(
                "[deep_research] job=%s plan confirmation link detected (markers=%s)",
                job_id,
                markers,
            )
            await _maybe_log_deep_research_body_layout_snapshot(
                page,
                job_id,
                "link_detected",
                poll_seq,
                force=True,
            )
            await asyncio.sleep(1)
            await _confirm_deep_research_execution(page, job_id)
            return
        poll_seq += 1
        await asyncio.sleep(poll)

    logger.warning(
        "[deep_research] job=%s confirmation link not seen within %ss; skipping execution confirm",
        job_id,
        timeout_link,
    )


async def _wait_for_copy_button(
    page: Page,
    max_poll_time: Optional[int] = None,
    *,
    layout_log_job_id: Optional[str] = None,
) -> None:
    """Poll until copy button appears on *page* (response is ready)."""
    limit = MAX_POLL_TIME if max_poll_time is None else max_poll_time
    await asyncio.sleep(2)

    elapsed = 0
    copy_poll_seq = 0
    while elapsed < limit:
        if layout_log_job_id:
            csl = await _probe_gemini_submit_send_loading(page)
            if isinstance(csl, dict) and csl.get("found"):
                logger.debug(
                    "[deep_research] job=%s copy_poll_submit_loading poll=%d loading=%s "
                    "ariaBusy=%s ariaDisabled=%s spinInBtn=%s spinNearBar=%s",
                    layout_log_job_id,
                    copy_poll_seq,
                    csl.get("submitLoading"),
                    csl.get("ariaBusy"),
                    csl.get("ariaDisabled"),
                    csl.get("spinnerInSendButton"),
                    csl.get("spinnerNearSubmitBar"),
                )
            await _maybe_log_deep_research_body_layout_snapshot(
                page,
                layout_log_job_id,
                "copy_poll",
                copy_poll_seq,
                force=(copy_poll_seq == 0),
            )
        try:
            count = await page.locator(COPY_BUTTON_SELECTOR).count()
            if count > 0:
                await asyncio.sleep(0.5)
                return
        except Exception:
            pass

        copy_poll_seq += 1
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
            logger.debug("[send] Send button clickable after %.0fs", elapsed)
            return
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        if elapsed % 30 == 0 and elapsed > 0:
            logger.debug("[send] Waiting for Send button... %.0fs elapsed", elapsed)
    raise TimeoutError(
        f"Send button not clickable within {SEND_BUTTON_WAIT_TIMEOUT}s (aria-disabled remained true)"
    )


async def _get_text_response(
    page: Page,
    job_id: str,
    *,
    clipboard_only: bool = False,
) -> str:
    """Get Gemini response text.

    Strategy (controlled by WG_USE_DOM_EXTRACTION env var):
    - Default: try clipboard (global mutex), fallback to DOM
    - WG_USE_DOM_EXTRACTION=1: always use DOM (no clipboard lock needed)
    - clipboard_only=True: read clipboard only (Deep Research after **Copy contents** in export menu).
    """
    if clipboard_only:
        try:
            async with clipboard_section(job_id):
                await asyncio.sleep(0.6)
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(["pbpaste"], capture_output=True, text=True),
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.debug(
                        "[job] job=%s returning text from clipboard after Copy contents (%d chars)",
                        job_id,
                        len(result.stdout.strip()),
                    )
                    return result.stdout.strip()
        except Exception as e:
            logger.warning("[job] job=%s clipboard_only read failed: %s", job_id, e)
        logger.debug("[job] job=%s clipboard_only empty; falling back to assistant Copy / DOM", job_id)

    if USE_DOM_EXTRACTION:
        logger.debug("[job] job=%s using DOM extraction (clipboard skipped)", job_id)
        text = await _get_text_response_via_dom(page)
        return text.strip() if text else ""

    # Clipboard path: click Copy + read clipboard inside a global mutex to prevent
    # concurrent tasks from corrupting each other's clipboard reads.
    copy_btn_count = await page.locator(COPY_BUTTON_SELECTOR).count()
    logger.debug("[job] job=%s copy button count before copy: %d", job_id, copy_btn_count)

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
    logger.debug("[job] job=%s clipboard empty, falling back to DOM extraction", job_id)
    text = await _get_text_response_via_dom(page)
    return text.strip() if text else ""


async def _get_text_response_via_dom(page: Page) -> str:
    """Extract full response text from *page* DOM via Playwright (no clipboard)."""
    try:
        text = await page.evaluate("""() => {
            const copyBtn = document.querySelector('button[aria-label="Copy text"]')
                || document.querySelector('button[aria-label="Copy"]')
                || document.querySelector('[aria-label="Copy"]');
            if (!copyBtn) return '';

            let el = copyBtn;
            for (let i = 0; i < 15 && el; i++) {
                el = el.parentElement;
                if (!el) break;
                let raw = el.innerText || el.textContent || '';
                raw = raw.replace(/\\s+/g, ' ').trim();
                if (raw.length > 80 && !/^\\s*(Copy|Regenerate|Thumbs|More)\\s*$/i.test(raw)) {
                    return raw.replace(/\\s*(Copy text|Copy|Regenerate|Thumbs up|Thumbs down|More)\\s*$/gi, '').trim();
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

