#!/usr/bin/env python3
"""
Inspect Send button HTML when it's in disabled/non-clickable state.
Connect to Chrome at localhost:9222, find Send button, dump attributes.
Run this when: Gemini page is open, prompt filled, attachment uploaded, but Send is disabled.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"
SEND_SELECTORS = [
    '[aria-label="Send message"]',
    'button[aria-label="Send message"]',
    '[aria-label="Send"]',
    'button[aria-label="Send"]',
]


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
            print("Not on Gemini. Please navigate to Gemini, fill prompt, upload attachment,")
            print("then run this script again when Send button is disabled.")
            return

        # Find Send button and dump full HTML + attributes
        result = await page.evaluate("""() => {
            const selectors = [
                '[aria-label="Send message"]',
                'button[aria-label="Send message"]',
                '[aria-label="Send"]',
                'button[aria-label="Send"]'
            ];
            const found = [];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                els.forEach((el, i) => {
                    const attrs = {};
                    for (const a of el.attributes) {
                        attrs[a.name] = a.value;
                    }
                    found.push({
                        selector: sel,
                        index: i,
                        tagName: el.tagName,
                        outerHTML: el.outerHTML,
                        attributes: attrs,
                        disabled: el.disabled,
                        hasDisabledAttr: el.hasAttribute('disabled'),
                        ariaDisabled: el.getAttribute('aria-disabled'),
                        tabIndex: el.tabIndex,
                        className: el.className,
                        computedDisplay: getComputedStyle(el).display,
                        computedPointerEvents: getComputedStyle(el).pointerEvents,
                        computedOpacity: getComputedStyle(el).opacity,
                    });
                });
            }
            return found;
        }""")

        if not result:
            print("No Send button found. Trying broader search...")
            result = await page.evaluate("""() => {
                const buttons = document.querySelectorAll('button, [role="button"]');
                const found = [];
                buttons.forEach((el, i) => {
                    const aria = el.getAttribute('aria-label') || '';
                    if (aria.toLowerCase().includes('send') || aria.toLowerCase().includes('message')) {
                        const attrs = {};
                        for (const a of el.attributes) attrs[a.name] = a.value;
                        found.push({
                            selector: 'button/role=button',
                            index: i,
                            tagName: el.tagName,
                            outerHTML: el.outerHTML,
                            attributes: attrs,
                            disabled: el.disabled,
                            hasDisabledAttr: el.hasAttribute('disabled'),
                            ariaDisabled: el.getAttribute('aria-disabled'),
                            tabIndex: el.tabIndex,
                            className: el.className,
                        });
                    }
                });
                return found;
            }""")

        print("=== Send button(s) found ===\n")
        for i, r in enumerate(result):
            print(f"--- Match {i+1} (selector: {r.get('selector')}) ---")
            print(f"  disabled: {r.get('disabled')}")
            print(f"  hasAttribute('disabled'): {r.get('hasDisabledAttr')}")
            print(f"  aria-disabled: {r.get('ariaDisabled')}")
            print(f"  tabIndex: {r.get('tabIndex')}")
            print(f"  className: {r.get('className', '')[:100]}")
            if 'computedPointerEvents' in r:
                print(f"  pointer-events: {r.get('computedPointerEvents')}")
            if 'computedOpacity' in r:
                print(f"  opacity: {r.get('computedOpacity')}")
            print(f"\n  outerHTML:\n{r.get('outerHTML', '')}\n")


if __name__ == "__main__":
    asyncio.run(main())
