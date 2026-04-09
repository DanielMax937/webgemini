"""Async Grok-on-X chat job processor (parallel to ``chat.py``)."""

from typing import Optional

from .grok import GROK_URL, send_prompt
from .jobs import JobStatus, persist_job, update_job
from .page_context import task_page


async def process_grok_chat(
    job_id: str,
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
) -> None:
    """Process a Grok chat job: open dedicated tab, run automation, close tab.

    The caller must hold a concurrency slot (via ``concurrency_slot``).
    """
    update_job(job_id, status=JobStatus.PROCESSING)
    persist_job(job_id, status=JobStatus.PROCESSING.value)

    try:
        async with task_page(job_id) as page:
            result = await send_prompt(prompt, tool, attachments, page=page, job_id=job_id)

        images_data = [
            {"url": img.url, "local_path": img.local_path}
            for img in result.images
        ]

        update_job(
            job_id,
            status=JobStatus.COMPLETED,
            text=result.text,
            images=images_data,
            gemini_url=GROK_URL,
        )
        persist_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            text=result.text,
            images=images_data,
            gemini_url=GROK_URL,
        )
    except Exception as e:
        update_job(job_id, status=JobStatus.FAILED, error=str(e))
        persist_job(job_id, status=JobStatus.FAILED.value, error=str(e))

