import asyncio
from typing import Optional

from .browser import chrome
from .gemini import GEMINI_URL, send_prompt
from .jobs import JobStatus, persist_job, update_job


async def process_chat(
    job_id: str,
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
) -> None:
    """Process a chat job asynchronously. Updates job state throughout.

    This function is meant to be run as a background task.
    It acquires no lock — the caller should hold chrome.lock.
    """
    update_job(job_id, status=JobStatus.PROCESSING)
    persist_job(job_id, status=JobStatus.PROCESSING.value)

    try:
        result = await send_prompt(prompt, tool, attachments)

        # Get current Gemini page URL for persistence
        gemini_url = GEMINI_URL
        try:
            page_info = await chrome.get_page_info()
            if page_info.get("url"):
                gemini_url = page_info["url"]
        except Exception:
            pass

        # Convert images to dict format for JSON serialization
        images_data = [
            {"url": img.url, "local_path": img.local_path}
            for img in result.images
        ]

        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            text=result.text,
            images=images_data,
            gemini_url=gemini_url,
        )
        persist_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            text=result.text,
            images=images_data,
            gemini_url=gemini_url,
        )
    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))
        persist_job(job_id, status=JobStatus.FAILED.value, error=str(e))
