from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .browser import chrome
from .gemini import send_prompt, GeminiResponse


class ChatRequest(BaseModel):
    prompt: str
    tool: Optional[str] = None  # One of: deep_research, video, image, canvas, tutor


class ImageResponse(BaseModel):
    url: str
    local_path: str


class ChatResponse(BaseModel):
    text: str
    images: list[ImageResponse]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage browser lifecycle."""
    # Start browser on startup
    await chrome.start_browser()
    yield
    # Stop browser on shutdown
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


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
