import asyncio
from typing import Optional

from .gemini import send_prompt
from .jobs import JobStatus, update_job


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

    try:
        result = await send_prompt(prompt, tool, attachments)
        
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
        )
    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))
