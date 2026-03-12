import asyncio
import base64
import json
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

from .jobs import JobStatus, update_job

OUTPUTS_DIR = Path(__file__).parent.parent.parent / "outputs"
GEMINI_URL = "https://gemini.google.com/app"
MAX_IMAGE_POLL_TIME = 300  # 5 minutes
POLL_INTERVAL = 3

# Selectors
INPUT_SELECTOR = '[aria-label="Enter a prompt for Gemini"]'
UPLOAD_MENU_SELECTOR = '[aria-label="Open upload file menu"]'
UPLOAD_FILES_SELECTOR = '[aria-label="Upload files. Documents, data, code files"]'
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'
CREATE_IMAGE_SELECTOR = 'button.mat-mdc-list-item:has-text("Create image")'

CDP_URL = "http://localhost:9222"


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
    
    # Calculate total size for dynamic wait time
    total_size = sum(Path(p).stat().st_size for p in image_paths)
    
    await file_chooser.set_files(image_paths)
    
    # Dynamic wait time based on file count and size
    # Base 2s + 1s per file + 0.3s per MB
    file_count = len(image_paths)
    wait_time = 2 + (file_count * 1) + (total_size / (1024 * 1024) * 0.3)
    wait_time = min(wait_time, 30)  # Max 30 seconds
    
    await asyncio.sleep(wait_time)


async def _select_create_image(page: Page) -> None:
    """Select 'Create image' tool from the Tools menu."""
    await page.click(TOOLS_BUTTON_SELECTOR)
    await asyncio.sleep(1)
    await page.click(CREATE_IMAGE_SELECTOR)
    await asyncio.sleep(1)
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)


async def _wait_for_images(page: Page) -> list[str]:
    """Poll the page for generated images and return their URLs."""
    elapsed = 0
    while elapsed < MAX_IMAGE_POLL_TIME:
        try:
            # Look for generated images - check for download buttons which indicate generation is complete
            download_buttons = await page.locator('[aria-label="Download full size image"]').count()
            if download_buttons > 0:
                await asyncio.sleep(2)  # Wait to ensure images are fully loaded
                
                # Get image URLs from the response container
                # Look for large images in the conversation (not profile pics)
                image_urls = await page.evaluate("""() => {
                    const urls = [];
                    const images = document.querySelectorAll('img');
                    for (const img of images) {
                        if (img.src && img.src.startsWith('http')) {
                            // Filter out small images (profile pics, icons)
                            const width = img.naturalWidth || img.width;
                            const height = img.naturalHeight || img.height;
                            if (width > 200 && height > 200) {
                                urls.push(img.src);
                            }
                        }
                    }
                    return [...new Set(urls)];
                }""")
                if image_urls and len(image_urls) > 0:
                    return image_urls
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    return []


async def _download_image_via_browser(page: Page, url: str, local_path: Path) -> None:
    """Download image using the browser's authenticated session."""
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


async def generate_image(job_id: str, prompt: str, image_paths: list[str]) -> None:
    """Full image generation flow. Updates job state throughout.

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

        # 2. Select Create image tool
        await _select_create_image(page)

        # 3. Upload reference images if provided
        if image_paths:
            try:
                await _upload_images(page, image_paths)
            except Exception as e:
                # If upload fails, continue without reference images
                print(f"Warning: Failed to upload images: {e}")

        # 4. Fill prompt and send
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.5)
        await page.keyboard.type(prompt, delay=20)
        await asyncio.sleep(0.5)
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")

        # 5. Poll for generated images
        image_urls = await _wait_for_images(page)
        if not image_urls:
            update_job(job_id, status=JobStatus.FAILED, error="timeout: no images generated within 3 minutes")
            return

        # 6. Download images locally
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        downloaded_images = []
        
        for idx, url in enumerate(image_urls):
            local_path = OUTPUTS_DIR / f"{job_id}_{idx}.png"
            await _download_image_via_browser(page, url, local_path)
            downloaded_images.append({
                "url": url,
                "local_path": str(local_path)
            })

        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            images=downloaded_images,
        )

    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))

    finally:
        if pw:
            await pw.stop()
