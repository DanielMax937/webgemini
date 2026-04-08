"""Playwright-over-CDP operations (act / page / distill) for Web Gemini."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .paths import CDP_URL


def _with_page(fn):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Error: playwright is required (install project deps: uv sync)") from e

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("Error: no browser context (is Chrome running with CDP?)")
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else context.new_page()
        return fn(page, context)


def _act(argv: list[str]) -> str:
    ap = argparse.ArgumentParser(prog="act")
    ap.add_argument("--url", default=None)
    ap.add_argument("--new-tab", action="store_true")
    ap.add_argument("--selector", default=None)
    ap.add_argument("--action", default=None)
    ap.add_argument("--value", default="")
    ns = ap.parse_args(argv)

    def run(page, context):
        if ns.url is not None:
            if ns.new_tab:
                page = context.new_page()
            page.goto(ns.url, wait_until="domcontentloaded", timeout=120_000)
            return ""
        if not ns.selector or not ns.action:
            raise RuntimeError("Error: act requires --url or (--selector and --action)")
        loc = page.locator(ns.selector).first
        if ns.action == "click":
            loc.click(timeout=120_000)
        elif ns.action == "fill":
            loc.fill(ns.value, timeout=120_000)
        elif ns.action == "press":
            loc.press(ns.value, timeout=120_000)
        else:
            raise RuntimeError(f"Error: unknown action {ns.action!r}")
        return ""

    return _with_page(run)


def _page(_argv: list[str]) -> str:
    def run(page, context):
        try:
            title = page.title()
        except Exception:
            title = ""
        return f"URL: {page.url}\n标题: {title}\n"

    return _with_page(run)


def _distill(argv: list[str]) -> str:
    ap = argparse.ArgumentParser(prog="distill")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)

    def run(page, context):
        if ns.json:
            data: Any = page.evaluate(
                """() => {
  const out = [];
  for (const el of document.querySelectorAll(
    'button, a[href], [role="button"], input, textarea, [contenteditable="true"]'
  )) {
    const tag = el.tagName.toLowerCase();
    const aria = el.getAttribute('aria-label') || '';
    out.push({ tag, aria_label: aria });
  }
  return out;
}"""
            )
            return json.dumps(data, ensure_ascii=False) + "\n"
        text = page.evaluate(
            """() => {
  const main = document.querySelector('main') || document.body;
  return main ? main.innerText.slice(0, 8000) : '';
}"""
        )
        return text + "\n"

    return _with_page(run)


def run_cli(argv: list[str]) -> str:
    """CLI-compatible entry: argv e.g. ``[\"act\", \"--url\", \"https://...\"]``."""
    if not argv:
        raise RuntimeError("Error: empty command")
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "act":
        return _act(rest)
    if cmd == "page":
        return _page(rest)
    if cmd == "distill":
        return _distill(rest)
    raise RuntimeError(f"Error: unknown command {cmd!r}")
