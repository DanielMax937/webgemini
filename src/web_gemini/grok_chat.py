"""Async Grok-on-X chat job processor (parallel to ``chat.py``)."""

from typing import Optional

from .browser import chrome
from .grok import GROK_URL, send_prompt
from .jobs import JobStatus, persist_job, update_job


async def process_grok_chat(
    job_id: str,
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
) -> None:
    """Process a Grok chat job; caller must hold ``chrome.lock``."""
    update_job(job_id, status=JobStatus.PROCESSING)
    persist_job(job_id, status=JobStatus.PROCESSING.value)

    try:
        result = await send_prompt(prompt, tool, attachments)

        page_url = GROK_URL
        try:
            page_info = await chrome.get_page_info()
            if page_info.get("url"):
                page_url = page_info["url"]
        except Exception:
            pass

        images_data = [
            {"url": img.url, "local_path": img.local_path}
            for img in result.images
        ]

        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            text=result.text,
            images=images_data,
            gemini_url=page_url,
        )
        persist_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            text=result.text,
            images=images_data,
            gemini_url=page_url,
        )
    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))
        persist_job(job_id, status=JobStatus.FAILED.value, error=str(e))
