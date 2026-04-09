import asyncio
from typing import Optional

from .gemini import GEMINI_URL, send_prompt
from .jobs import JobStatus, persist_job, update_job
from .page_context import task_page


async def process_chat(
    job_id: str,
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
) -> None:
    """Process a chat job: open dedicated tab, run automation, close tab.

    The caller must hold a concurrency slot (via ``concurrency_slot``).
    """
    update_job(job_id, status=JobStatus.PROCESSING)
    persist_job(job_id, status=JobStatus.PROCESSING.value)

    try:
        async with task_page(job_id) as page:
            result = await send_prompt(prompt, tool, attachments, page=page, job_id=job_id)

        gemini_url = GEMINI_URL
        try:
            # page is closed at this point; URL was the last navigated URL
            pass
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

