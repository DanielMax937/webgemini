import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from . import db as db_module


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
    # Music results
    audio_url: Optional[str] = None
    audio_path: Optional[str] = None
    error: Optional[str] = None
    gemini_url: Optional[str] = None
    created_at: float = field(default_factory=time.time)


JOB_TTL_SECONDS = 3600  # 1 hour

_jobs: dict[str, Job] = {}
_tasks: dict[str, asyncio.Task] = {}
_pg_enabled: Optional[bool] = None


def _pg_available() -> bool:
    """Check if PostgreSQL is available."""
    global _pg_enabled
    if _pg_enabled is not None:
        return _pg_enabled
    try:
        db_module.init_db()
        _pg_enabled = True
        return True
    except Exception:
        _pg_enabled = False
        return False


def create_job(
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    image_paths: Optional[list[str]] = None,
) -> Job:
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        job_id=job_id,
        status=JobStatus.PENDING,
        prompt=prompt,
        tool=tool,
        attachments=attachments or [],
        image_paths=image_paths or [],
    )
    _jobs[job_id] = job
    if _pg_available():
        try:
            db_module.insert_job(
                job_id=job_id,
                status=job.status.value,
                prompt=prompt,
                tool=tool,
                attachments=job.attachments,
                image_paths=job.image_paths,
            )
        except Exception:
            pass
    return job


def get_job(job_id: str) -> Optional[Job]:
    """Get job from memory first, then from PostgreSQL."""
    job = _jobs.get(job_id)
    if job:
        return job
    if _pg_available():
        try:
            row = db_module.get_job_db(job_id)
            if row:
                created = row.get("created_at")
                if hasattr(created, "timestamp"):
                    created = created.timestamp()
                elif created is None:
                    created = time.time()
                job = Job(
                    job_id=row["job_id"],
                    status=JobStatus(row["status"]),
                    prompt=row["prompt"],
                    tool=row.get("tool"),
                    attachments=row.get("attachments") or [],
                    image_paths=row.get("image_paths") or [],
                    text=row.get("text"),
                    images=row.get("images") or [],
                    video_url=row.get("video_url"),
                    local_path=row.get("local_path"),
                    audio_url=row.get("audio_url"),
                    audio_path=row.get("audio_path"),
                    error=row.get("error"),
                    gemini_url=row.get("gemini_url"),
                    created_at=created,
                )
                _jobs[job_id] = job
                return job
        except Exception:
            pass
    return None


def update_job(job_id: str, **kwargs) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    for key, value in kwargs.items():
        setattr(job, key, value)


def persist_job(job_id: str, **kwargs) -> None:
    """Persist job updates to PostgreSQL."""
    if not _pg_available():
        return
    try:
        db_module.update_job_db(job_id=job_id, **kwargs)
    except Exception:
        pass


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
