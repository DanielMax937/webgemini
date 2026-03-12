import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    job_id: str
    status: JobStatus
    prompt: str
    tool: Optional[str] = None
    attachments: list[str] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)
    # Chat results
    text: Optional[str] = None
    images: list[dict] = field(default_factory=list)
    # Video results
    video_url: Optional[str] = None
    local_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


JOB_TTL_SECONDS = 3600  # 1 hour

_jobs: dict[str, Job] = {}
_tasks: dict[str, asyncio.Task] = {}


def create_job(
    prompt: str, 
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    image_paths: Optional[list[str]] = None
) -> Job:
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        job_id=job_id, 
        status=JobStatus.PENDING, 
        prompt=prompt,
        tool=tool,
        attachments=attachments or [],
        image_paths=image_paths or []
    )
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def update_job(job_id: str, **kwargs) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    for key, value in kwargs.items():
        setattr(job, key, value)


def register_task(job_id: str, task: asyncio.Task) -> None:
    _tasks[job_id] = task


def cleanup_expired_jobs() -> int:
    now = time.time()
    expired = [jid for jid, job in _jobs.items() if now - job.created_at > JOB_TTL_SECONDS]
    for jid in expired:
        _jobs.pop(jid, None)
        task = _tasks.pop(jid, None)
        if task and not task.done():
            task.cancel()
    return len(expired)


async def periodic_cleanup(interval: int = 300):
    """Run cleanup every `interval` seconds."""
    while True:
        await asyncio.sleep(interval)
        cleanup_expired_jobs()
