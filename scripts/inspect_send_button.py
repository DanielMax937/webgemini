#!/usr/bin/env python3
"""
Inspect Gemini page to find Send button HTML via Chrome DevTools (CDP).
Connect to existing Chrome at localhost:9222, get current page, find Send button.
"""
import asyncio
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        if not contexts:
            print("No browser context found")
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
            await page.goto("https://gemini.google.com/app", timeout=30000)
            await asyncio.sleep(3)

        # Find all potential Send button elements
        result = await page.evaluate("""() => {
            const out = [];
            // 1. aria-label="Send"
            const byAria = document.querySelectorAll('[aria-label="Send"]');
            byAria.forEach((el, i) => {
                out.push({ selector: '[aria-label="Send"]', index: i, outerHTML: el.outerHTML, tagName: el.tagName });
            });
            // 2. Buttons with Send in text/aria
            const buttons = document.querySelectorAll('button, [role="button"]');
            buttons.forEach((el, i) => {
                const aria = el.getAttribute('aria-label') || '';
                const text = (el.innerText || el.textContent || '').trim();
                if (aria.toLowerCase().includes('send') || text.toLowerCase().includes('send') ||
                    el.querySelector('[aria-label="Send"]') || el.querySelector('svg')) {
                    out.push({ selector: 'button/role=button', index: i, aria, text: text.slice(0,50), outerHTML: el.outerHTML.slice(0, 500), tagName: el.tagName });
                }
            });
            // 3. Paper airplane / send icon (common in chat)
            const withSvg = document.querySelectorAll('button[aria-label], [role="button"][aria-label]');
            const sendLike = [];
            withSvg.forEach(el => {
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                if (aria && (aria.includes('send') || aria.includes('submit') || aria.includes('发布') || aria.includes('发送'))) {
                    sendLike.push({ aria: el.getAttribute('aria-label'), outerHTML: el.outerHTML.slice(0, 600) });
                }
            });
            return { byAria: out.filter(x => x.selector === '[aria-label="Send"]'), buttons: out.filter(x => x.selector === 'button/role=button'), sendLike };
        }""")

        print("=== Elements with [aria-label='Send'] ===")
        for i, r in enumerate(result.get("byAria", [])):
            print(f"\n--- Match {i+1} ---")
            print(r.get("outerHTML", "")[:800])

        print("\n\n=== Buttons that might be Send (aria/text) ===")
        for i, r in enumerate(result.get("buttons", [])[:10]):
            print(f"\n--- Button {i+1} ---")
            print(f"aria={r.get('aria')} text={r.get('text')}")
            print(r.get("outerHTML", "")[:600])

        print("\n\n=== Send-like buttons (aria contains send/submit) ===")
        for i, r in enumerate(result.get("sendLike", [])):
            print(f"\n--- {i+1} ---")
            print(r.get("outerHTML", "")[:800])

        # Also dump all aria-labels in the input area
        allAria = await page.evaluate("""() => {
            const els = document.querySelectorAll('[aria-label]');
            return Array.from(els).map(el => ({
                tag: el.tagName,
                aria: el.getAttribute('aria-label'),
                html: el.outerHTML.slice(0, 300)
            })).filter(x => x.aria && (x.aria.toLowerCase().includes('send') || x.aria.toLowerCase().includes('submit') || x.aria.toLowerCase().includes('enter') || x.aria.toLowerCase().includes('input')));
        }""")
        print("\n\n=== All elements with send/submit/enter/input in aria-label ===")
        for x in allAria:
            print(f"{x['tag']} aria={x['aria']}")
            print(x["html"][:400])
            print("---")


if __name__ == "__main__":
    asyncio.run(main())
