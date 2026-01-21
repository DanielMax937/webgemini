"""Debug script to test browser interaction with Gemini."""
import asyncio
from pathlib import Path
from patchright.async_api import async_playwright

BROWSER_PROFILE_DIR = Path(__file__).parent / "browser-profiles" / "gemini-profile"
OUTPUT_FILE = Path(__file__).parent / "debug_output.txt"


async def main():
    output = []
    def log(msg):
        print(msg)
        output.append(msg)

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        channel="chrome",
        headless=False,
        viewport=None,
    )

    try:
        await context.grant_permissions(['clipboard-read', 'clipboard-write'])
        log("Clipboard permissions granted")
    except Exception as e:
        log(f"Failed to grant clipboard permissions: {e}")

    page = await context.new_page()
    await page.goto("https://gemini.google.com/app")
    await page.wait_for_load_state("domcontentloaded")

    log("Page loaded. Waiting 5 seconds...")
    await asyncio.sleep(5)

    # Take screenshot
    await page.screenshot(path="debug_screenshot.png")
    log(f"Screenshot saved. URL: {page.url}")

    # Check if we need to login
    if "accounts.google" in page.url:
        log("ERROR: Need to login to Google first!")
        log("Please login manually in the browser window...")
        await asyncio.sleep(60)
        await context.close()
        await pw.stop()
        OUTPUT_FILE.write_text("\n".join(output))
        return

    # Find and fill input
    input_el = page.locator('.ql-editor').first
    if await input_el.count() > 0:
        log("Found input element")
        await input_el.click()
        await asyncio.sleep(0.3)
        await input_el.fill("What is 2+2?")
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        log("Sent prompt")
    else:
        log("ERROR: Input element not found")
        # Dump page HTML for debugging
        html = await page.content()
        log(f"Page HTML length: {len(html)}")

    # Wait for response
    log("Waiting for response (15 seconds)...")
    await asyncio.sleep(15)

    # Take screenshot after response
    await page.screenshot(path="debug_after_response.png")
    log("Screenshot saved to debug_after_response.png")

    # Dump all buttons on page
    log("\nAll buttons on page:")
    buttons = page.locator('button')
    count = await buttons.count()
    for i in range(min(count, 20)):
        btn = buttons.nth(i)
        aria = await btn.get_attribute('aria-label') or ''
        tooltip = await btn.get_attribute('data-tooltip') or ''
        text = await btn.inner_text() or ''
        log(f"  Button {i}: aria='{aria}' tooltip='{tooltip}' text='{text[:30]}'")

    # Save output
    OUTPUT_FILE.write_text("\n".join(output))
    log(f"\nOutput saved to {OUTPUT_FILE}")

    log("\nBrowser will stay open for 60 seconds for manual inspection...")
    await asyncio.sleep(60)

    await context.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
