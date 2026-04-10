import asyncio
import base64
import json
from pathlib import Path
from typing import Optional

import httpx
import websockets as ws_lib
from playwright.async_api import async_playwright, Page

from .chrome_automation.paths import CDP_URL, CDP_PORT
from .jobs import JobStatus, persist_job, update_job
from .navigation import navigate_page_to_gemini_with_retry
from .upload import upload_files

OUTPUTS_DIR = Path(__file__).parent.parent.parent / "outputs"


def _record_gemini_page_url(job_id: str, page: Optional[Page]) -> None:
    """Store current Gemini page URL on the job (memory + optional PostgreSQL); not returned by HTTP API."""
    if not page:
        return
    try:
        url = page.url
        if not url:
            return
        update_job(job_id, gemini_url=url)
        persist_job(job_id, gemini_url=url)
    except Exception:
        pass
GEMINI_URL = "https://gemini.google.com/app"
MAX_IMAGE_POLL_TIME = 900  # 15 minutes — Gemini image generation can take 10+ minutes
POLL_INTERVAL = 3

# Selectors
INPUT_SELECTOR = '[aria-label="Enter a prompt for Gemini"]'
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'
# Exact match avoids matching the "🖼️ Create image" chip (which has an emoji prefix)
CREATE_IMAGE_SELECTOR = 'text="Create image"'

async def _connect_to_chrome() -> tuple:
    """Connect to existing Chrome instance via CDP."""
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    return pw, browser, context


async def _cdp_click_send_button(page: Page, job_id: str) -> bool:
    """Click the Send button via direct CDP websocket (dispatches a trusted mouse event).

    Playwright's synthetic events are filtered by Gemini's Angular app; raw CDP
    Input.dispatchMouseEvent bypasses that check.
    Returns True if button was found and click was sent.
    """
    btn_pos = await page.evaluate("""() => {
        const btn = document.querySelector('[aria-label="Send message"]');
        if (!btn) return null;
        const r = btn.getBoundingClientRect();
        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
    }""")
    if not btn_pos:
        return False

    # Mark the page with job_id so we can identify it among all open tabs
    await page.evaluate(f"window.__wg_job_id = {json.dumps(job_id)}")

    # Find this specific page's CDP websocket URL by matching the marker
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"http://127.0.0.1:{CDP_PORT}/json")
        targets = resp.json()

    page_target = None
    for t in targets:
        if 'gemini.google.com' not in t.get('url', ''):
            continue
        ws_url = t.get('webSocketDebuggerUrl')
        if not ws_url:
            continue
        try:
            async with ws_lib.connect(ws_url) as ws:
                await ws.send(json.dumps({'id': 99, 'method': 'Runtime.evaluate',
                    'params': {'expression': 'window.__wg_job_id || ""'}}))
                # Read until we get the response with id=99 (Chrome may send unsolicited events first)
                val = ''
                deadline = asyncio.get_event_loop().time() + 3
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    resp_msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                    msg = json.loads(resp_msg)
                    if msg.get('id') == 99:
                        val = msg.get('result', {}).get('result', {}).get('value', '')
                        break
                if val == job_id:
                    page_target = t
                    break
        except Exception:
            continue

    if not page_target:
        return False

    ws_url = page_target['webSocketDebuggerUrl']
    async with ws_lib.connect(ws_url) as ws:
        for evt_id, evt_type in enumerate(('mousePressed', 'mouseReleased'), start=1):
            await ws.send(json.dumps({
                'id': evt_id,
                'method': 'Input.dispatchMouseEvent',
                'params': {
                    'type': evt_type,
                    'x': btn_pos['x'],
                    'y': btn_pos['y'],
                    'button': 'left',
                    'clickCount': 1,
                    'modifiers': 0,
                },
            }))
            # Drain messages until we receive our response
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                if json.loads(msg_raw).get('id') == evt_id:
                    break
    return True


async def _upload_images(page: Page, image_paths: list[str]) -> None:
    """Upload images. Uses CDP to bypass 50MB limit."""
    await upload_files(page, image_paths)


async def _select_create_image(page: Page) -> None:
    """Ensure 'Create image' tool is active on the given page.

    On this Chrome profile, 'Create image' is often pre-selected.
    We check the button's aria-label first: if it reads 'Deselect Create image'
    the mode is already active and we skip the click (which would deactivate it).
    """
    # Click input to activate the toolbar so we can inspect button state
    await page.click(INPUT_SELECTOR)
    await asyncio.sleep(0.5)

    # Check current Create image button state
    aria = await page.evaluate("""() => {
        const btn = document.querySelector('[aria-label*="Create image"]');
        return btn ? btn.getAttribute('aria-label') : null;
    }""")
    print(f"[_select_create_image] Create image aria-label: {aria!r}")

    if aria == 'Deselect Create image':
        # Already active — nothing to do
        print("[_select_create_image] Create image already active, skipping click")
        return

    # Not active — need to activate via Tools menu
    await page.keyboard.type(" ")
    await asyncio.sleep(0.5)
    await page.click(TOOLS_BUTTON_SELECTOR)
    await asyncio.sleep(1)
    await page.click(CREATE_IMAGE_SELECTOR)
    await asyncio.sleep(1)

    # Verify it was activated
    aria2 = await page.evaluate("""() => {
        const btn = document.querySelector('[aria-label*="Create image"]');
        return btn ? btn.getAttribute('aria-label') : null;
    }""")
    print(f"[_select_create_image] After clicking: {aria2!r}")

    # Remove the activation space we typed
    await page.click(INPUT_SELECTOR)
    await asyncio.sleep(0.2)
    await page.keyboard.press("End")
    await asyncio.sleep(0.1)
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.3)


async def _wait_for_download_buttons(page: Page) -> int:
    """Poll until generated images appear, return count (0 = timeout).

    Guards against false-positives: the input placeholder attachment also shows a
    "Download full size image" button, so we require at least one large blob: image
    (naturalWidth > 100) before declaring success.  Also continuously dismisses the
    "Pick a style" overlay that Gemini can show at any point after submission.
    """
    elapsed = 0
    brought_to_front = False

    while elapsed < MAX_IMAGE_POLL_TIME:
        try:
            # Dismiss style picker whenever it appears (partial-text match is more robust
            # than exact match in case Gemini slightly changes the copy)
            style_count = await page.locator('text=/Pick a style/i').count()
            if style_count > 0:
                print(f"[wait_for_download] style picker detected, clicking Monochrome (elapsed={elapsed}s)")
                # Try Playwright force-click on Monochrome first
                clicked = False
                try:
                    await page.get_by_text("Monochrome", exact=True).first.click(timeout=2000, force=True)
                    clicked = True
                except Exception:
                    pass
                if not clicked:
                    # JS fallback: find any element with "Monochrome" text
                    clicked = await page.evaluate("""() => {
                        const all = Array.from(document.querySelectorAll('*'));
                        const mono = all.find(el =>
                            el.childElementCount === 0 && el.textContent.trim() === 'Monochrome');
                        if (mono) { mono.click(); return true; }
                        // Click first [role=option] as last resort
                        const first = document.querySelector('[role="option"]');
                        if (first) { first.click(); return 'first-option'; }
                        return false;
                    }""")
                print(f"[wait_for_download] Monochrome click result: {clicked}")
                await asyncio.sleep(3)
                continue

            count = await page.locator('[aria-label="Download full size image"]').count()
            if count > 0:
                # Only count as done when a GENERATED (large) blob image exists.
                # The input-placeholder attachment also has a download button but its
                # blob naturalWidth == 1, so filtering by > 100 avoids false positives.
                # bring_to_front() is required so Chrome decodes blob: images (background
                # tabs have naturalWidth==0 for blob images).
                if not brought_to_front:
                    await page.bring_to_front()
                    await asyncio.sleep(1)
                    brought_to_front = True
                has_generated = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('img[src^="blob:"]'))
                        .some(img => img.naturalWidth > 100)
                """)
                if has_generated:
                    await asyncio.sleep(1)
                    return count
                # Image not yet decoded — refresh decode after bringing to front
                brought_to_front = False
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    return 0


async def _download_images_via_canvas(page: Page, job_id: str) -> list[dict]:
    """Extract generated images using canvas drawImage on the Gemini page.

    Gemini renders generated images as <img src="blob:https://gemini.google.com/UUID">.
    These have naturalWidth=0 in background tabs, but after bring_to_front() Chrome
    decodes them. We filter by naturalWidth > 100 to skip the small 1×1 placeholder
    input images that also have blob: URLs.
    """
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    # Bring page to front so Chrome decodes and renders all blob images
    try:
        await page.bring_to_front()
        await asyncio.sleep(3)
        # Scroll to download button area to trigger lazy rendering
        await page.evaluate("""() => {
            const btn = document.querySelector('[aria-label="Download full size image"]');
            if (btn) btn.scrollIntoView({behavior: 'instant', block: 'center'});
        }""")
        await asyncio.sleep(2)
    except Exception:
        pass

    # Wait for at least one large blob: image to be decoded (naturalWidth > 100)
    # This filters out 1×1 placeholder input images (naturalWidth == 1)
    try:
        await page.wait_for_function(
            """() => Array.from(document.querySelectorAll('img[src^="blob:"]')).some(i => i.naturalWidth > 100)""",
            timeout=30000,
        )
        print(f"[_download_images] {job_id}: blob img naturalWidth ready")
    except Exception as e:
        print(f"[_download_images] {job_id}: wait_for_function timed out: {e}")
        # Log diagnostics
        try:
            info = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('img[src^="blob:"]'))
                    .map(i => ({nw: i.naturalWidth, nh: i.naturalHeight, src: i.src.substring(0,50)}));
            }""")
            print(f"[_download_images] {job_id}: blob img diagnostic: {info}")
        except Exception:
            pass

    downloaded = []

    # Extract all large blob: images using canvas drawImage
    idx = 0
    while True:
        local_path = OUTPUTS_DIR / f"{job_id}_{idx}.png"
        b64: str | None = await page.evaluate(f"""() => {{
            const imgs = Array.from(document.querySelectorAll('img[src^="blob:"]'))
                .filter(i => i.naturalWidth > 100);
            const img = imgs[{idx}];
            if (!img) return null;
            try {{
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth;
                canvas.height = img.naturalHeight;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0);
                const data = ctx.getImageData(0, 0, 1, 1).data;
                if (data[3] === 0) return null;
                return canvas.toDataURL('image/png').split(',')[1];
            }} catch(e) {{ return null; }}
        }}""")
        if not b64:
            break
        local_path.write_bytes(base64.b64decode(b64))
        print(f"[_download_images] {job_id}: canvas saved image {idx} ({local_path.stat().st_size} bytes)")
        downloaded.append({"url": f"file://{local_path}", "local_path": str(local_path)})
        idx += 1

    print(f"[_download_images] {job_id}: total {len(downloaded)} image(s) extracted")
    return downloaded


async def generate_image(job_id: str, prompt: str, image_paths: list[str]) -> None:
    """Full image generation flow. Updates job state throughout.

    This function is meant to be run as a background task inside a concurrency slot.
    """
    update_job(job_id, status=JobStatus.PROCESSING)
    persist_job(job_id, status=JobStatus.PROCESSING.value)

    pw = None
    page = None
    try:
        print(f"[generate_image] {job_id}: connecting to Chrome")
        pw, browser, context = await _connect_to_chrome()
        page = await context.new_page()
        print(f"[generate_image] {job_id}: new page created, navigating to Gemini")

        # 1. Navigate to fresh Gemini conversation (with retry: close and reopen, max 3 times, 5s interval)
        await navigate_page_to_gemini_with_retry(page, GEMINI_URL, timeout=60000)
        print(f"[generate_image] {job_id}: navigation done, URL={page.url}")
        _record_gemini_page_url(job_id, page)

        # 2. Select Create image tool
        await _select_create_image(page)
        print(f"[generate_image] {job_id}: create image mode selected, URL={page.url}")
        _record_gemini_page_url(job_id, page)

        # 3. Upload reference images if provided
        if image_paths:
            try:
                await _upload_images(page, image_paths)
            except Exception as e:
                print(f"Warning: Failed to upload images: {e}")

        # 4. Fill prompt and send
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.5)
        await page.keyboard.type(prompt, delay=20)
        print(f"[generate_image] {job_id}: prompt typed ({len(prompt)} chars)")
        await asyncio.sleep(0.5)

        # Debug: check chip and input state before submit
        dom_state = await page.evaluate("""() => {
            const inp = document.querySelector('[aria-label="Enter a prompt for Gemini"]');
            const chips = Array.from(document.querySelectorAll('[role="option"][aria-selected="true"], [class*="tool-chip"], [class*="selected-chip"]'));
            const allChips = Array.from(document.querySelectorAll('[class*="chip"]'));
            return JSON.stringify({
                inputText: inp ? inp.textContent.slice(0, 50) : 'NOT FOUND',
                inputType: inp ? inp.tagName : 'none',
                selectedChips: chips.map(c=>c.textContent.slice(0,20)),
                allChipClasses: allChips.slice(0,3).map(c=>c.className.slice(0,40))
            });
        }""")
        print(f"[generate_image] {job_id}: DOM state before submit: {dom_state}")

        await page.bring_to_front()
        await asyncio.sleep(0.5)
        # Ensure input is focused then press Enter (natural Gemini submission)
        await page.click(INPUT_SELECTOR)
        await asyncio.sleep(0.3)

        # Primary: press Enter in the input (what real users do)
        await page.locator(INPUT_SELECTOR).press("Enter")
        print(f"[generate_image] {job_id}: Enter pressed in input")

        # Wait for URL to change from /app to /app/<conversation_id>
        import re as _re
        try:
            await page.wait_for_url(_re.compile(r'/app/[a-z0-9]+'), timeout=30000)
            print(f"[generate_image] {job_id}: navigation OK, URL now={page.url}")
        except Exception:
            # The style picker may have appeared — use JS click to bypass pointer-events overlay
            print(f"[generate_image] {job_id}: Enter didn't navigate, trying JS send button click")
            try:
                # JS click bypasses Playwright's actionability checks (pointer-events: none, overlays)
                await page.evaluate("""() => {
                    const btn = document.querySelector('[aria-label="Send message"]');
                    if (btn) btn.click();
                }""")
                await page.wait_for_url(_re.compile(r'/app/[a-z0-9]+'), timeout=30000)
                print(f"[generate_image] {job_id}: JS send click navigated, URL now={page.url}")
            except Exception as e2:
                # Final fallback: Playwright force click (bypasses visibility/actionability)
                print(f"[generate_image] {job_id}: JS click didn't navigate, trying force click: {e2}")
                send_locator = page.locator('[aria-label="Send message"]')
                try:
                    await send_locator.click(timeout=15000, force=True)
                    await page.wait_for_url(_re.compile(r'/app/[a-z0-9]+'), timeout=30000)
                    print(f"[generate_image] {job_id}: force click navigated, URL now={page.url}")
                except Exception as e3:
                    print(f"[generate_image] {job_id}: all submit attempts failed, URL={page.url}: {e3}")

        _record_gemini_page_url(job_id, page)

        # 5. Wait for download buttons (image generation complete)
        # Note: style picker is handled continuously inside _wait_for_download_buttons
        print(f"[generate_image] {job_id}: waiting for download buttons...")
        button_count = await _wait_for_download_buttons(page)
        print(f"[generate_image] {job_id}: download buttons count={button_count}, URL={page.url}")
        _record_gemini_page_url(job_id, page)
        if not button_count:
            update_job(job_id, status=JobStatus.FAILED, error="timeout: no images generated within poll window")
            persist_job(job_id, status=JobStatus.FAILED.value, error="timeout: no images generated within poll window")
            return

        # 6. Download each image using canvas drawImage (avoids browser-download event issues)
        downloaded_images = await _download_images_via_canvas(page, job_id)
        print(f"[generate_image] {job_id}: canvas extracted {len(downloaded_images)} image(s)")

        if not downloaded_images:
            update_job(job_id, status=JobStatus.FAILED, error="canvas extraction returned no images")
            persist_job(job_id, status=JobStatus.FAILED.value, error="canvas extraction returned no images")
            return

        final_url = page.url
        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            images=downloaded_images,
            gemini_url=final_url,
        )
        persist_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            images=downloaded_images,
            gemini_url=final_url,
        )
        print(f"[generate_image] {job_id}: COMPLETED with {len(downloaded_images)} image(s)")

    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))
        persist_job(job_id, status=JobStatus.FAILED.value, error=str(e))

    finally:
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        if pw:
            await pw.stop()
