"""Music generation via Gemini Web browser automation."""
import asyncio
import base64
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

from .jobs import JobStatus, persist_job, update_job
from .navigation import navigate_page_to_gemini_with_retry
from .upload import upload_files

logger = logging.getLogger(__name__)
OUTPUTS_DIR = Path(__file__).parent.parent.parent / "outputs"
MUSIC_DEBUG_DIR = OUTPUTS_DIR / "music_debug"
SEND_FILE_SCRIPT = Path.home() / ".cursor/skills/telegram-send-file/scripts/send-file.sh"
GEMINI_URL = "https://gemini.google.com/app"
MAX_AUDIO_POLL_TIME = 300  # 5 minutes
POLL_INTERVAL = 3

# Selectors - adjust when Gemini adds music generation tool
INPUT_SELECTOR = '[aria-label="Enter a prompt for Gemini"]'
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'
CREATE_MUSIC_SELECTOR = 'button.mat-mdc-list-item:has-text("Create music")'
DOWNLOAD_TRACK_SELECTOR = '[aria-label="Download track"]'
AUDIO_ONLY_MENU_SELECTOR = 'button.mat-mdc-menu-item:has-text("Audio only")'

CDP_URL = "http://localhost:9222"


async def _take_screenshot(page: Page, run_dir: Path, step_num: int, step_name: str) -> Path | None:
    """Take screenshot. Returns path or None on error."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{step_num:02d}_{step_name}.png"
    try:
        await page.screenshot(path=str(path))
        logger.info("[music] screenshot: %s", path)
        return path
    except Exception as e:
        logger.warning("[music] screenshot failed: %s", e)
        return None


def _send_screenshots_to_telegram(run_dir: Path, job_id: str) -> None:
    """Send all screenshots in run_dir via telegram send-file."""
    if not SEND_FILE_SCRIPT.exists():
        logger.warning("[music] send-file.sh not found, skip sending screenshots")
        return
    screenshots = sorted(run_dir.glob("*.png"))
    if not screenshots:
        return
    for i, p in enumerate(screenshots):
        try:
            caption = f"music {job_id} step {i+1}: {p.stem}"
            subprocess.run(
                [str(SEND_FILE_SCRIPT), str(p), caption],
                capture_output=True,
                timeout=30,
            )
        except Exception as e:
            logger.warning("[music] send screenshot %s failed: %s", p.name, e)


async def _connect_to_chrome() -> tuple:
    """Connect to existing Chrome instance via CDP."""
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    return pw, browser, context


async def _upload_images(page: Page, image_paths: list[str]) -> None:
    """Upload reference images. Uses CDP to bypass 50MB limit."""
    await upload_files(page, image_paths)


async def _select_create_music(page: Page) -> bool:
    """Select 'Create music' from the Tools menu. Returns False if not found."""
    await page.click(TOOLS_BUTTON_SELECTOR)
    await asyncio.sleep(1)
    try:
        await page.click(CREATE_MUSIC_SELECTOR, timeout=3000)
        await asyncio.sleep(1)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        return True
    except Exception:
        await page.keyboard.press("Escape")
        return False


async def _wait_for_music_ready(page: Page) -> tuple[bool, Optional[str]]:
    """Poll for music ready. Returns (download_button_found, audio_url).
    Prefer download button (Gemini's native Download track) over raw audio URL.
    """
    elapsed = 0
    while elapsed < MAX_AUDIO_POLL_TIME:
        try:
            # 1. Check for Download track button (preferred - triggers native download)
            if await page.locator(DOWNLOAD_TRACK_SELECTOR).count() > 0:
                return (True, None)
            # 2. Check for audio element / direct URL (fallback)
            audio_url = await page.evaluate("""() => {
                const audios = document.querySelectorAll('audio source, audio[src]');
                for (const a of audios) {
                    const src = a.src || a.getAttribute('src');
                    if (src && src.startsWith('http')) return src;
                }
                const links = document.querySelectorAll('a[href*=".mp3"], a[href*=".wav"], a[download]');
                for (const a of links) {
                    if (a.href && (a.href.includes('.mp3') || a.href.includes('.wav')))
                        return a.href;
                }
                return null;
            }""")
            if audio_url:
                return (False, audio_url)
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    return (False, None)


async def _download_audio_via_browser(page: Page, url: str, local_path: Path) -> None:
    """Download audio using the browser's authenticated session (fetch)."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    response_bytes = await page.evaluate("""async (url) => {
        const response = await fetch(url, { credentials: 'include' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        const reader = new FileReader();
        return new Promise((resolve, reject) => {
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
    }""", url)
    local_path.write_bytes(base64.b64decode(response_bytes))


async def _download_via_download_button(page: Page, job_id: str) -> tuple[Path, Optional[str]]:
    """Click Download track -> Audio only, intercept URL, fetch to save.
    Returns (save_path, download_url).
    """
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    captured_url: list[str] = []

    async def capture_download(route):
        req = route.request
        u = req.url
        if "usercontent.google.com/download" in u or (
            "download" in u.lower() and (".mp3" in u or ".wav" in u)
        ):
            captured_url.append(u)
            logger.info("[music] captured download URL: %s", u[:80])
        await route.continue_()

    await page.route("**/*", capture_download)
    try:
        await page.click(DOWNLOAD_TRACK_SELECTOR)
        await asyncio.sleep(1.5)
        menu_item = page.locator(AUDIO_ONLY_MENU_SELECTOR).first
        await menu_item.wait_for(state="visible", timeout=5000)
        await menu_item.click()
        for _ in range(10):
            await asyncio.sleep(1)
            if captured_url:
                break
    finally:
        await page.unroute("**/*")

    if not captured_url:
        raise RuntimeError("No download URL captured from Download track menu")

    download_url = captured_url[0]
    ext = ".mp3" if ".mp3" in download_url else ".wav"
    save_path = OUTPUTS_DIR / f"{job_id}{ext}"

    # Use fetch (browser context) to download
    response_bytes = await page.evaluate("""async (url) => {
        const response = await fetch(url, { credentials: 'include' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        const reader = new FileReader();
        return new Promise((resolve, reject) => {
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
    }""", download_url)
    save_path.write_bytes(base64.b64decode(response_bytes))
    return save_path, download_url


async def generate_music(
    job_id: str,
    prompt: str,
    image_paths: list[str],
) -> None:
    """Full music generation flow. Updates job state throughout.

    This function is meant to be run as a background task.
    It acquires no lock — the caller should hold chrome.lock.
    Takes screenshot at each step and sends via Telegram send-file.
    """
    update_job(job_id, status=JobStatus.PROCESSING)
    persist_job(job_id, status=JobStatus.PROCESSING.value)

    run_dir = MUSIC_DEBUG_DIR / f"{job_id}_{time.strftime('%Y%m%d_%H%M%S', time.localtime())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("[music] screenshot run_dir: %s", run_dir)

    pw = None
    step = 0
    try:
        pw, browser, context = await _connect_to_chrome()
        page = context.pages[-1] if context.pages else await context.new_page()

        await navigate_page_to_gemini_with_retry(page, GEMINI_URL, timeout=60000)
        step += 1
        await _take_screenshot(page, run_dir, step, "01_after_navigate")

        has_music_tool = await _select_create_music(page)
        step += 1
        await _take_screenshot(page, run_dir, step, "02_after_select_create_music")
        if not has_music_tool:
            update_job(
                job_id,
                status=JobStatus.FAILED,
                error="Create music tool not found. Gemini may not support music generation yet.",
            )
            persist_job(
                job_id,
                status=JobStatus.FAILED.value,
                error="Create music tool not found. Gemini may not support music generation yet.",
            )
            return

        if image_paths:
            step += 1
            await _take_screenshot(page, run_dir, step, "03_before_upload")
            await _upload_images(page, image_paths)
            await asyncio.sleep(1)
            step += 1
            await _take_screenshot(page, run_dir, step, "04_after_upload")

        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.5)
        await page.keyboard.type(prompt, delay=20)
        await asyncio.sleep(0.5)
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        step += 1
        await _take_screenshot(page, run_dir, step, "05_after_send_prompt")

        download_btn_found, audio_url = await _wait_for_music_ready(page)
        step += 1
        await _take_screenshot(page, run_dir, step, "06_after_wait_audio")
        if not download_btn_found and not audio_url:
            (run_dir / "page_url.txt").write_text(page.url, encoding="utf-8")
            update_job(
                job_id,
                status=JobStatus.FAILED,
                error="timeout: no music or download button found within 5 minutes",
            )
            persist_job(
                job_id,
                status=JobStatus.FAILED.value,
                error="timeout: no music or download button found within 5 minutes",
            )
            return

        (run_dir / "page_url.txt").write_text(page.url, encoding="utf-8")
        local_path: Path
        final_audio_url: Optional[str] = None

        if download_btn_found:
            local_path, final_audio_url = await _download_via_download_button(page, job_id)
        else:
            if not audio_url:
                raise RuntimeError("audio_url required when download button not found")
            ext = ".mp3" if ".mp3" in audio_url else ".wav"
            local_path = OUTPUTS_DIR / f"{job_id}{ext}"
            await _download_audio_via_browser(page, audio_url, local_path)
            final_audio_url = audio_url

        step += 1
        await _take_screenshot(page, run_dir, step, "07_after_download")

        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            audio_url=final_audio_url,
            audio_path=str(local_path),
        )
        persist_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            audio_url=final_audio_url,
            audio_path=str(local_path),
        )

    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))
        persist_job(job_id, status=JobStatus.FAILED.value, error=str(e))

    finally:
        if pw:
            await pw.stop()
        _send_screenshots_to_telegram(run_dir, job_id)
