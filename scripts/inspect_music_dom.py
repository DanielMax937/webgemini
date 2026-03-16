#!/usr/bin/env python3
"""
使用 Chrome DevTools (CDP) 检查 Gemini 页面，获取音乐相关 DOM。
Connect to existing Chrome at localhost:9222, open Tools menu, extract music-related elements.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"
GEMINI_URL = "https://gemini.google.com/app"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        if not contexts:
            print("No browser context found. 请先启动 Chrome (--remote-debugging-port=9222)")
            return
        context = contexts[0]
        pages = context.pages
        if not pages:
            print("No pages found")
            return
        page = pages[-1]
        url = page.url
        print(f"Current page: {url}\n")

        if "gemini.google.com" not in url:
            print("Not on Gemini. Navigating to Gemini...")
            await page.goto(GEMINI_URL, timeout=30000)
            await asyncio.sleep(4)

        # 1. 点击 Tools 按钮打开菜单
        print("=== Opening Tools menu ===")
        tools_btn = page.locator('button:has-text("Tools")')
        if await tools_btn.count() > 0:
            await tools_btn.first.click()
            await asyncio.sleep(2)
        else:
            print("Tools button not found, trying alternative selectors...")
            alt = page.locator('[aria-label*="Tools"], [aria-label*="工具"]')
            if await alt.count() > 0:
                await alt.first.click()
                await asyncio.sleep(2)

        # 2. 提取 Tools 菜单内所有选项
        result = await page.evaluate("""() => {
            const out = { toolsMenuItems: [], musicRelated: [], allButtonsWithText: [] };

            // Tools 菜单内的 list items (与 video/image 相同的结构)
            const listItems = document.querySelectorAll('button.mat-mdc-list-item, [role="menuitem"], .mat-mdc-list-item');
            listItems.forEach((el, i) => {
                const text = (el.innerText || el.textContent || '').trim();
                const aria = el.getAttribute('aria-label') || '';
                const item = {
                    index: i,
                    tag: el.tagName,
                    text,
                    aria,
                    outerHTML: el.outerHTML.slice(0, 800),
                    className: el.className,
                };
                out.toolsMenuItems.push(item);
                if (text.toLowerCase().includes('music') || text.toLowerCase().includes('audio') ||
                    text.toLowerCase().includes('音乐') || text.toLowerCase().includes('音频') ||
                    aria.toLowerCase().includes('music') || aria.toLowerCase().includes('audio')) {
                    out.musicRelated.push(item);
                }
            });

            // 全页面搜索 music/audio 相关
            const allEls = document.querySelectorAll('button, [role="button"], [role="menuitem"], a, span');
            allEls.forEach(el => {
                const text = (el.innerText || el.textContent || '').trim();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                if (text && (text.toLowerCase().includes('music') || text.toLowerCase().includes('audio') ||
                    text.toLowerCase().includes('音乐') || text.toLowerCase().includes('音频') ||
                    text.toLowerCase().includes('create music') || text.toLowerCase().includes('generate music'))) {
                    out.allButtonsWithText.push({
                        tag: el.tagName,
                        text: text.slice(0, 80),
                        aria: el.getAttribute('aria-label'),
                        outerHTML: el.outerHTML.slice(0, 600),
                    });
                }
            });

            return out;
        }""")

        print("\n=== Tools 菜单内所有选项 (toolsMenuItems) ===")
        for i, item in enumerate(result.get("toolsMenuItems", [])):
            print(f"\n--- 选项 {i+1} ---")
            print(f"  text: {item.get('text')}")
            print(f"  aria: {item.get('aria')}")
            print(f"  tag: {item.get('tag')} class: {item.get('className', '')[:80]}")
            print(f"  HTML: {item.get('outerHTML', '')[:500]}")

        print("\n\n=== 音乐相关选项 (musicRelated) ===")
        music_items = result.get("musicRelated", [])
        if not music_items:
            print("(未找到包含 music/audio/音乐/音频 的选项)")
        for i, item in enumerate(music_items):
            print(f"\n--- 音乐选项 {i+1} ---")
            print(json.dumps(item, indent=2, ensure_ascii=False))

        print("\n\n=== 全页面 music/audio 相关元素 ===")
        all_music = result.get("allButtonsWithText", [])
        if not all_music:
            print("(未找到)")
        for i, x in enumerate(all_music[:15]):
            print(f"\n--- {i+1} ---")
            print(f"  tag={x.get('tag')} text={x.get('text')} aria={x.get('aria')}")
            print(f"  HTML: {x.get('outerHTML', '')[:400]}")

        # 3. 输出建议的 CSS 选择器（供 music.py 使用）
        print("\n\n=== 建议的 music 选择器 (基于 Tools 菜单结构) ===")
        for item in result.get("toolsMenuItems", []):
            text = (item.get("text") or "").lower()
            if "music" in text or "audio" in text or "音乐" in text or "音频" in text:
                t = item.get("text", "").strip()
                print(f"  CREATE_MUSIC_SELECTOR = 'button.mat-mdc-list-item:has-text(\"{t}\")'")
                break
        else:
            print("  未找到 music 选项，可能 Gemini 尚未提供音乐生成功能。")
            print("  当前 Tools 菜单选项列表:")
            for item in result.get("toolsMenuItems", []):
                print(f"    - {item.get('text', '(empty)')}")

        # 关闭 Tools 菜单
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
