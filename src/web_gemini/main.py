import asyncio
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .browser import chrome
from .gemini import send_prompt, GeminiResponse
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


class ImageResponse(BaseModel):
    url: str
    local_path: str


class ChatResponse(BaseModel):
    text: str
    images: list[ImageResponse]


class VideoJobResponse(BaseModel):
    job_id: str
    status: str


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


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Send prompt to Gemini and return response."""
    async with chrome.lock:
        try:
            result: GeminiResponse = await send_prompt(request.prompt, request.tool)
            return ChatResponse(
                text=result.text,
                images=[
                    ImageResponse(url=img.url, local_path=img.local_path)
                    for img in result.images
                ],
            )
        except TimeoutError as e:
            raise HTTPException(status_code=504, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


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


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
