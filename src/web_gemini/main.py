import asyncio
import logging
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

# Configure logging so web_gemini module logs (e.g. [job] copy button) appear in stdout/log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
from pydantic import BaseModel

from .concurrency import TASK_TIMEOUT_S, concurrency_slot, metrics as concurrency_metrics
from .chat import process_chat
from .grok_chat import process_grok_chat
from .image import generate_image as generate_image_func
from .jobs import (
    JobStatus,
    create_job,
    get_job,
    periodic_cleanup,
    register_task,
    update_job,
    persist_job,
)
from .video import generate_video
from .music import generate_music

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_IMAGES = 5
UPLOAD_DIR = Path(tempfile.gettempdir()) / "web-gemini-uploads"


class ChatRequest(BaseModel):
    prompt: str
    tool: Optional[str] = None
    attachments: Optional[list[str]] = None  # Local file paths to upload


class ChatJobResponse(BaseModel):
    job_id: str
    status: str


class ChatStatusResponse(BaseModel):
    job_id: str
    status: str
    text: Optional[str] = None
    images: list[dict] = []
    error: Optional[str] = None
    gemini_url: Optional[str] = None


class ImageResponse(BaseModel):
    url: str
    local_path: str


class VideoJobResponse(BaseModel):
    job_id: str
    status: str


class ImageJobResponse(BaseModel):
    job_id: str
    status: str


class ImageStatusResponse(BaseModel):
    job_id: str
    status: str
    images: list[dict] = []
    error: Optional[str] = None


class VideoStatusResponse(BaseModel):
    job_id: str
    status: str
    video_url: Optional[str] = None
    local_path: Optional[str] = None
    error: Optional[str] = None


class MusicJobResponse(BaseModel):
    job_id: str
    status: str


class MusicStatusResponse(BaseModel):
    job_id: str
    status: str
    audio_url: Optional[str] = None
    local_path: Optional[str] = None
    error: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown. Chrome is started/stopped by ./start-bg.sh / ./stop-bg.sh (``web_gemini.chrome_automation.manager``), not here."""
    from . import db


    try:
        db.init_db()
    except Exception:
        pass  # PostgreSQL optional, fallback to in-memory
    cleanup_task = asyncio.create_task(periodic_cleanup())
    yield
    cleanup_task.cancel()


app = FastAPI(
    title="Gemini & Grok Web Service",
    description="Browser automation for Gemini (gemini.google.com) and Grok on X (x.com/i/grok).",
    lifespan=lifespan,
)


@app.post("/chat", response_model=ChatJobResponse)
async def chat(request: ChatRequest) -> ChatJobResponse:
    """Submit a chat job with prompt, optional tool, and optional attachments."""
    job = create_job(
        prompt=request.prompt,
        tool=request.tool,
        attachments=request.attachments,
    )
    update_job(job.job_id, status=JobStatus.QUEUED)

    async def _run_job():
        try:
            async with concurrency_slot(job.job_id):
                await asyncio.wait_for(
                    process_chat(job.job_id, job.prompt, job.tool, job.attachments),
                    timeout=TASK_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            update_job(job.job_id, status=JobStatus.FAILED, error=f"Task timed out after {TASK_TIMEOUT_S}s")
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=f"Task timed out after {TASK_TIMEOUT_S}s")
        except Exception as e:
            update_job(job.job_id, status=JobStatus.FAILED, error=str(e))
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=str(e))

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return ChatJobResponse(job_id=job.job_id, status=JobStatus.QUEUED.value)


@app.get("/chat/{job_id}", response_model=ChatStatusResponse)
async def get_chat_status(job_id: str) -> ChatStatusResponse:
    """Check the status of a chat job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return ChatStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        text=job.text,
        images=job.images,
        error=job.error,
        gemini_url=job.gemini_url,
    )


@app.post("/grok/chat", response_model=ChatJobResponse)
async def grok_chat(request: ChatRequest) -> ChatJobResponse:
    """Submit a chat job against Grok on X (https://x.com/i/grok). Same body as POST /chat."""
    job = create_job(
        prompt=request.prompt,
        tool=request.tool,
        attachments=request.attachments,
    )
    update_job(job.job_id, status=JobStatus.QUEUED)

    async def _run_job():
        try:
            async with concurrency_slot(job.job_id):
                await asyncio.wait_for(
                    process_grok_chat(job.job_id, job.prompt, job.tool, job.attachments),
                    timeout=TASK_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            update_job(job.job_id, status=JobStatus.FAILED, error=f"Task timed out after {TASK_TIMEOUT_S}s")
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=f"Task timed out after {TASK_TIMEOUT_S}s")
        except Exception as e:
            update_job(job.job_id, status=JobStatus.FAILED, error=str(e))
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=str(e))

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return ChatJobResponse(job_id=job.job_id, status=JobStatus.QUEUED.value)


@app.get("/grok/chat/{job_id}", response_model=ChatStatusResponse)
async def get_grok_chat_status(job_id: str) -> ChatStatusResponse:
    """Poll Grok chat job status (same shape as GET /chat/{job_id}; ``gemini_url`` holds the X page URL)."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return ChatStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        text=job.text,
        images=job.images,
        error=job.error,
        gemini_url=job.gemini_url,
    )


@app.post("/video", response_model=VideoJobResponse)
async def create_video(
    prompt: str = Form(...),
    images: list[UploadFile] = File(default=[]),
) -> VideoJobResponse:
    """Submit a Veo3 video generation job with prompt and optional reference images."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_IMAGES} images allowed")

    saved_paths: list[str] = []
    if images:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for img in images:
            if img.content_type not in ALLOWED_IMAGE_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid image type: {img.content_type}. Allowed: png, jpg, gif, webp",
                )
            content = await img.read()
            if len(content) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail=f"Image {img.filename} exceeds 10MB limit")

            dest = UPLOAD_DIR / f"{img.filename}"
            dest.write_bytes(content)
            saved_paths.append(str(dest))

    job = create_job(prompt=prompt, image_paths=saved_paths if saved_paths else None)
    update_job(job.job_id, status=JobStatus.QUEUED)

    async def _run_job():
        try:
            async with concurrency_slot(job.job_id):
                await asyncio.wait_for(
                    generate_video(job.job_id, job.prompt, job.image_paths),
                    timeout=TASK_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            update_job(job.job_id, status=JobStatus.FAILED, error=f"Task timed out after {TASK_TIMEOUT_S}s")
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=f"Task timed out after {TASK_TIMEOUT_S}s")
        except Exception as e:
            update_job(job.job_id, status=JobStatus.FAILED, error=str(e))
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=str(e))
        finally:
            for p in saved_paths:
                Path(p).unlink(missing_ok=True)

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return VideoJobResponse(job_id=job.job_id, status=JobStatus.QUEUED.value)


@app.get("/video/{job_id}", response_model=VideoStatusResponse)
async def get_video_status(job_id: str) -> VideoStatusResponse:
    """Check the status of a video generation job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return VideoStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        video_url=job.video_url,
        local_path=job.local_path,
        error=job.error,
    )


@app.post("/image", response_model=ImageJobResponse)
async def create_image(
    prompt: str = Form(...),
    images: list[UploadFile] = File(default=[]),
) -> ImageJobResponse:
    """Submit an image generation job with prompt and optional reference images."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_IMAGES} images allowed")

    saved_paths: list[str] = []
    if images:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for img in images:
            if img.content_type not in ALLOWED_IMAGE_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid image type: {img.content_type}. Allowed: png, jpg, gif, webp",
                )
            content = await img.read()
            if len(content) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail=f"Image {img.filename} exceeds 10MB limit")

            dest = UPLOAD_DIR / f"{img.filename}"
            dest.write_bytes(content)
            saved_paths.append(str(dest))

    job = create_job(prompt=prompt, image_paths=saved_paths if saved_paths else None)
    update_job(job.job_id, status=JobStatus.QUEUED)

    async def _run_job():
        try:
            async with concurrency_slot(job.job_id):
                await asyncio.wait_for(
                    generate_image_func(job.job_id, job.prompt, job.image_paths),
                    timeout=TASK_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            update_job(job.job_id, status=JobStatus.FAILED, error=f"Task timed out after {TASK_TIMEOUT_S}s")
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=f"Task timed out after {TASK_TIMEOUT_S}s")
        except Exception as e:
            update_job(job.job_id, status=JobStatus.FAILED, error=str(e))
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=str(e))
        finally:
            for p in saved_paths:
                Path(p).unlink(missing_ok=True)

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return ImageJobResponse(job_id=job.job_id, status=JobStatus.QUEUED.value)


@app.get("/image/{job_id}", response_model=ImageStatusResponse)
async def get_image_status(job_id: str) -> ImageStatusResponse:
    """Check the status of an image generation job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return ImageStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        images=job.images,
        error=job.error,
    )


@app.post("/music", response_model=MusicJobResponse)
async def create_music(
    prompt: str = Form(...),
    images: list[UploadFile] = File(default=[]),
) -> MusicJobResponse:
    """Submit a music generation job with prompt and optional reference images."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_IMAGES} images allowed")

    saved_paths: list[str] = []
    if images:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for img in images:
            if img.content_type not in ALLOWED_IMAGE_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid image type: {img.content_type}. Allowed: png, jpg, gif, webp",
                )
            content = await img.read()
            if len(content) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail=f"Image {img.filename} exceeds 10MB limit")

            dest = UPLOAD_DIR / f"{img.filename}"
            dest.write_bytes(content)
            saved_paths.append(str(dest))

    job = create_job(prompt=prompt, image_paths=saved_paths if saved_paths else None)
    update_job(job.job_id, status=JobStatus.QUEUED)

    async def _run_job():
        try:
            async with concurrency_slot(job.job_id):
                await asyncio.wait_for(
                    generate_music(job.job_id, job.prompt, job.image_paths or []),
                    timeout=TASK_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            update_job(job.job_id, status=JobStatus.FAILED, error=f"Task timed out after {TASK_TIMEOUT_S}s")
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=f"Task timed out after {TASK_TIMEOUT_S}s")
        except Exception as e:
            update_job(job.job_id, status=JobStatus.FAILED, error=str(e))
            persist_job(job.job_id, status=JobStatus.FAILED.value, error=str(e))
        finally:
            for p in saved_paths:
                Path(p).unlink(missing_ok=True)

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return MusicJobResponse(job_id=job.job_id, status=JobStatus.QUEUED.value)


@app.get("/music/{job_id}", response_model=MusicStatusResponse)
async def get_music_status(job_id: str) -> MusicStatusResponse:
    """Check the status of a music generation job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return MusicStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        audio_url=job.audio_url,
        local_path=job.audio_path,
        error=job.error,
    )


@app.get("/metrics")
async def metrics():
    """Concurrency and queue observability metrics."""
    m = concurrency_metrics()
    return {
        "active_slots": m["active"],
        "queued_tasks": m["queued"],
        "max_concurrent": m["max_concurrent"],
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
