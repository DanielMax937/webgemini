import asyncio
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .browser import chrome
from .chat import process_chat
from .image import generate_image as generate_image_func
from .jobs import (
    JobStatus,
    create_job,
    get_job,
    periodic_cleanup,
    register_task,
    update_job,
)
from .video import generate_video

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage browser lifecycle."""
    await chrome.start_browser()
    cleanup_task = asyncio.create_task(periodic_cleanup())
    yield
    cleanup_task.cancel()
    await chrome.stop_browser()


app = FastAPI(title="Gemini Web Service", lifespan=lifespan)


@app.post("/chat", response_model=ChatJobResponse)
async def chat(request: ChatRequest) -> ChatJobResponse:
    """Submit a chat job with prompt, optional tool, and optional attachments."""
    job = create_job(
        prompt=request.prompt,
        tool=request.tool,
        attachments=request.attachments,
    )

    async def _run_job():
        async with chrome.lock:
            await process_chat(
                job.job_id,
                job.prompt,
                job.tool,
                job.attachments,
            )

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return ChatJobResponse(job_id=job.job_id, status=job.status.value)


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
    )


@app.post("/video", response_model=VideoJobResponse)
async def create_video(
    prompt: str = Form(...),
    images: list[UploadFile] = File(...),
) -> VideoJobResponse:
    """Submit a Veo3 video generation job with prompt and reference images."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_IMAGES} images allowed")
    if len(images) == 0:
        raise HTTPException(status_code=400, detail="At least one image is required")

    # Validate and save uploaded images to temp directory
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []

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

    job = create_job(prompt=prompt, image_paths=saved_paths)

    async def _run_job():
        async with chrome.lock:
            await generate_video(job.job_id, job.prompt, job.image_paths)
        # Cleanup temp images after job completes
        for p in saved_paths:
            Path(p).unlink(missing_ok=True)

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return VideoJobResponse(job_id=job.job_id, status=job.status.value)


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
    images: list[UploadFile] = File(...),
) -> ImageJobResponse:
    """Submit an image generation job with prompt and reference images."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_IMAGES} images allowed")
    if len(images) == 0:
        raise HTTPException(status_code=400, detail="At least one image is required")

    # Validate and save uploaded images to temp directory
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []

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

    job = create_job(prompt=prompt, image_paths=saved_paths)

    async def _run_job():
        async with chrome.lock:
            await generate_image_func(job.job_id, job.prompt, job.image_paths)
        # Cleanup temp images after job completes
        for p in saved_paths:
            Path(p).unlink(missing_ok=True)

    task = asyncio.create_task(_run_job())
    register_task(job.job_id, task)

    return ImageJobResponse(job_id=job.job_id, status=job.status.value)


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


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
