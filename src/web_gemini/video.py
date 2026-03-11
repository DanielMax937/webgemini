import asyncio
import base64
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext

from .jobs import JobStatus, update_job

OUTPUTS_DIR = Path(__file__).parent.parent.parent / "outputs"
GEMINI_URL = "https://gemini.google.com/app"
MAX_VIDEO_POLL_TIME = 300  # 5 minutes
POLL_INTERVAL = 3

# Selectors discovered from live Gemini page
INPUT_SELECTOR = '[aria-label="Enter a prompt for Gemini"]'
UPLOAD_MENU_SELECTOR = '[aria-label="Open upload file menu"]'
UPLOAD_FILES_SELECTOR = '[aria-label="Upload files. Documents, data, code files"]'
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'
CREATE_VIDEO_SELECTOR = 'button.mat-mdc-list-item:has-text("Create video")'
MODE_PICKER_SELECTOR = '[aria-label="Open mode picker"]'

CDP_URL = "http://localhost:9222"


@dataclass
class VideoResult:
    video_url: str
    local_path: str


async def _connect_to_chrome() -> tuple:
    """Connect to existing Chrome instance via CDP."""
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    return pw, browser, context


async def _upload_images(page: Page, image_paths: list[str]) -> None:
    """Upload images via the file chooser dialog."""
    await page.click(UPLOAD_MENU_SELECTOR)
    await asyncio.sleep(1)

    async with page.expect_file_chooser() as fc_info:
        await page.click(UPLOAD_FILES_SELECTOR)

    file_chooser = await fc_info.value
    await file_chooser.set_files(image_paths)
    await asyncio.sleep(2)


async def _select_create_video(page: Page) -> None:
    """Select 'Create video' (Veo3) from the Tools menu."""
    await page.click(TOOLS_BUTTON_SELECTOR)
    await asyncio.sleep(1)
    await page.click(CREATE_VIDEO_SELECTOR)
    await asyncio.sleep(1)
    # Press Escape to close the tools dropdown
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)


async def _wait_for_video(page: Page) -> Optional[str]:
    """Poll the page for a video element and return its source URL."""
    elapsed = 0
    while elapsed < MAX_VIDEO_POLL_TIME:
        try:
            # Look for video elements in the response
            video_url = await page.evaluate("""() => {
                // Check for <video> elements with src
                const videos = document.querySelectorAll('video source, video[src]');
                for (const v of videos) {
                    const src = v.src || v.getAttribute('src');
                    if (src && src.startsWith('http')) return src;
                }
                // Check for download links with video extensions
                const links = document.querySelectorAll('a[href*=".mp4"], a[download]');
                for (const a of links) {
                    if (a.href && a.href.startsWith('http')) return a.href;
                }
                return null;
            }""")
            if video_url:
                return video_url
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    return None


async def _download_video_via_browser(page: Page, url: str, local_path: Path) -> None:
    """Download video using the browser's authenticated session."""
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


async def generate_video(job_id: str, prompt: str, image_paths: list[str]) -> None:
    """Full Veo3 video generation flow. Updates job state throughout.

    This function is meant to be run as a background task.
    It acquires no lock — the caller should hold chrome.lock.
    """
    update_job(job_id, status=JobStatus.PROCESSING)

    pw = None
    try:
        pw, browser, context = await _connect_to_chrome()
        page = context.pages[-1] if context.pages else await context.new_page()

        # 1. Navigate to fresh Gemini conversation
        await page.goto(GEMINI_URL, timeout=60000)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)

        # 2. Select Create video (Veo3) tool
        await _select_create_video(page)

        # 3. Upload images
        if image_paths:
            await _upload_images(page, image_paths)
            # Wait for images to finish uploading
            await asyncio.sleep(3)

        # 4. Fill prompt and send
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.5)
        await page.keyboard.type(prompt, delay=20)
        await asyncio.sleep(0.5)
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")

        # 5. Poll for video generation
        video_url = await _wait_for_video(page)
        if not video_url:
            update_job(job_id, status=JobStatus.FAILED, error="timeout: no video found within 5 minutes")
            return

        # 6. Download video locally via browser's authenticated session
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        local_path = OUTPUTS_DIR / f"{job_id}.mp4"
        await _download_video_via_browser(page, video_url, local_path)

        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            video_url=video_url,
            local_path=str(local_path),
        )

    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))

    finally:
        if pw:
            await pw.stop()
