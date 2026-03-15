"""File upload utilities. Uses CDP DOM.setFileInputFiles to bypass Playwright's 50MB limit when connecting over CDP."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


UPLOAD_MENU_SELECTOR = '[aria-label="Open upload file menu"]'
UPLOAD_FILES_SELECTOR = '[aria-label="Upload files. Documents, data, code files"]'


async def upload_files(page: "Page", file_paths: list[str]) -> None:
    """Upload files to Gemini. Uses CDP DOM.setFileInputFiles to bypass 50MB limit.

    Playwright's file_chooser.set_files() has a 50MB limit when connecting over CDP.
    CDP's DOM.setFileInputFiles passes file paths directly to the browser, so the
    browser reads from local disk without transfer over the wire.
    """
    for path in file_paths:
        if not Path(path).exists():
            raise FileNotFoundError(f"Attachment file not found: {path}")

    await page.click(UPLOAD_MENU_SELECTOR)
    await asyncio.sleep(1)

    # Trigger file chooser (click opens it), then use CDP to set files instead of
    # file_chooser.set_files() which has 50MB limit over CDP.
    async with page.expect_file_chooser() as fc_info:
        await page.click(UPLOAD_FILES_SELECTOR)

    file_chooser = await fc_info.value

    # Use CDP DOM.setFileInputFiles to bypass 50MB limit (browser reads from local path)
    try:
        await _set_files_via_cdp(page, file_paths)
    except Exception:
        # Fallback to Playwright (fails for >50MB over CDP)
        await file_chooser.set_files(file_paths)

    total_size = sum(Path(p).stat().st_size for p in file_paths)
    file_count = len(file_paths)
    wait_time = 2 + (file_count * 1) + (total_size / (1024 * 1024) * 0.3)
    wait_time = min(wait_time, 60)  # Max 60s for large files
    await asyncio.sleep(wait_time)


async def _set_files_via_cdp(page: "Page", file_paths: list[str]) -> None:
    """Use CDP DOM.setFileInputFiles to set files directly. No 50MB limit."""
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("DOM.enable")

    doc = await cdp.send("DOM.getDocument")
    root_id = doc["root"]["nodeId"]

    # Try upload-area input first, then fallback to any file input
    node_id = None
    for selector in (
        "[aria-label*='Upload files'] input[type='file']",
        "input[type='file']",
    ):
        result = await cdp.send("DOM.querySelector", {
            "nodeId": root_id,
            "selector": selector,
        })
        node_id = result.get("nodeId")
        if node_id:
            break
    if not node_id:
        raise RuntimeError("Could not find file input element")

    abs_paths = [str(Path(p).resolve()) for p in file_paths]
    await cdp.send("DOM.setFileInputFiles", {
        "nodeId": node_id,
        "files": abs_paths,
    })
