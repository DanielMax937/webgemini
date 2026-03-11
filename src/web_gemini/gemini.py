import asyncio
import subprocess
import json
import re
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .browser import chrome

IMAGES_DIR = Path(__file__).parent.parent.parent / "images"
GEMINI_URL = "https://gemini.google.com/app"
MAX_POLL_TIME = 120  # seconds
POLL_INTERVAL = 2  # seconds

# Gemini selectors (English UI)
INPUT_SELECTOR = '[aria-label="Enter a prompt for Gemini"]'
SEND_BUTTON_SELECTOR = '[aria-label="Send"]'
COPY_BUTTON_SELECTOR = '[aria-label="Copy"]'
TOOLS_BUTTON_SELECTOR = 'button:has-text("Tools")'

# Tool selectors
TOOL_SELECTORS = {
    "deep_research": 'button:has-text("Deep Research")',
    "video": 'button:has-text("Create video")',
    "image": 'button:has-text("Create image")',
    "canvas": 'button:has-text("Canvas")',
    "tutor": 'button:has-text("Help me learn")',
}


@dataclass
class ImageResult:
    url: str
    local_path: str


@dataclass
class GeminiResponse:
    text: str
    images: list[ImageResult]


async def send_prompt(prompt: str, tool: Optional[str] = None) -> GeminiResponse:
    """Send prompt to Gemini and wait for response.

    Args:
        prompt: The prompt to send
        tool: Optional tool to use. One of: deep_research, video, image, canvas, tutor
    """

    # Navigate to Gemini (reuse current tab instead of opening new one)
    await chrome.run_cmd("act", "--url", GEMINI_URL)
    await asyncio.sleep(5)  # Wait for page to fully load

    # Select tool if specified
    if tool and tool in TOOL_SELECTORS:
        # Click tools button to open dropdown
        await chrome.run_cmd("act", "--selector", TOOLS_BUTTON_SELECTOR, "--action", "click")
        await asyncio.sleep(1)

        # Click the specific tool
        await chrome.run_cmd("act", "--selector", TOOL_SELECTORS[tool], "--action", "click")
        await asyncio.sleep(1)

    # Fill the chat input and press Enter to send
    await chrome.run_cmd("act", "--selector", INPUT_SELECTOR, "--action", "fill", "--value", prompt)
    await asyncio.sleep(0.5)
    await chrome.run_cmd("act", "--selector", INPUT_SELECTOR, "--action", "press", "--value", "Enter")

    # Poll until copy button is available
    await _wait_for_copy_button()

    # Get text response via copy button
    text = await _get_text_response()

    return GeminiResponse(text=text, images=[])


async def _wait_for_copy_button():
    """Poll until copy button appears (response is ready)."""
    # Initial wait for response to start
    await asyncio.sleep(2)

    elapsed = 0
    while elapsed < MAX_POLL_TIME:
        try:
            dom_json = await chrome.distill_dom(as_json=True)
            # Parse JSON from output
            start = dom_json.find('[')
            if start >= 0:
                items = json.loads(dom_json[start:])
                copy_buttons = [
                    item for item in items
                    if item.get('aria_label') == 'Copy' and item['tag'] == 'button'
                ]
                if copy_buttons:
                    # Copy button found, response is ready
                    await asyncio.sleep(0.5)
                    return
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    # Timeout - proceed anyway
    return


async def _get_text_response() -> str:
    """Click copy button and get text from clipboard."""
    try:
        await chrome.run_cmd("act", "--selector", COPY_BUTTON_SELECTOR, "--action", "click")
        await asyncio.sleep(0.5)

        # Get clipboard content
        result = subprocess.run(["pbpaste"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    return ""
