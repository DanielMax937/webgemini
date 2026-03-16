#!/usr/bin/env python3
"""
点击 Download track -> Audio only MP3，拦截网络请求获取下载 URL。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"
DOWNLOAD_TRACK_SELECTOR = '[aria-label="Download track"]'
AUDIO_ONLY_MENU_SELECTOR = 'button.mat-mdc-menu-item:has-text("Audio only")'


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://gemini.google.com/app/54e6a8115530a2b1"
    captured_urls = []

    async def handle_route(route):
        req = route.request
        u = req.url
        if ".mp3" in u or ".wav" in u or "audio" in u.lower() or "download" in u.lower():
            captured_urls.append(u)
        await route.continue_()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
        if "gemini.google.com" not in page.url:
            await page.goto(url, timeout=30000)
            await asyncio.sleep(4)

        await page.route("**/*", handle_route)
        await page.click(DOWNLOAD_TRACK_SELECTOR)
        await asyncio.sleep(1.5)
        await page.click(AUDIO_ONLY_MENU_SELECTOR)
        await asyncio.sleep(5)
        await page.unroute("**/*")

        print("=== 拦截到的可能下载 URL ===")
        for u in captured_urls:
            print(u)

        if not captured_urls:
            # 检查是否有新出现的 audio/video src
            urls = await page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('audio[src], video[src], audio source[src], a[href*=".mp3"], a[href*=".wav"]').forEach(el => {
                    const u = el.src || el.getAttribute('src') || el.href;
                    if (u) out.push(u);
                });
                return out;
            }""")
            print("\n=== 页面中的音频 URL ===")
            for u in urls:
                print(u)

        await page.keyboard.press("Escape")


if __name__ == "__main__":
    asyncio.run(main())
