# Veo3 Video Generation Endpoint Design

## Overview

New FastAPI routes that accept text prompts + images, interact with Gemini's Veo3 video generation via browser automation, and return generated videos through an async job-polling pattern.

## API Design

### POST /video

Submit a video generation job. Accepts multipart form data.

- `prompt` (text field, required) — The video generation prompt
- `images` (file uploads, 1-5 files) — Reference images (png, jpg, gif, webp, max 10MB each)

**Response:**
```json
{
  "job_id": "abc123",
  "status": "pending"
}
```

### GET /video/{job_id}

Poll job status and retrieve result.

**Responses by status:**
- `pending` — Job queued, waiting for browser lock
- `processing` — Gemini is generating the video
- `completed` — Video ready, includes `video_url` and `local_path`
- `failed` — Error occurred, includes `error` message

```json
{
  "job_id": "abc123",
  "status": "completed",
  "video_url": "https://...",
  "local_path": "outputs/abc123.mp4"
}
```

## Browser Automation Flow

1. Navigate to `https://gemini.google.com/app` (fresh conversation)
2. Upload images via file chooser
3. Fill text input with user prompt
4. Select Veo3 from model picker dropdown
5. Press Enter to submit
6. Poll for video element/download link (max 5 minutes)
7. Extract video URL, download to `outputs/{job_id}.mp4`
8. Navigate away to free the tab

## Architecture

### File Structure

```
src/web_gemini/
├── main.py          # Add /video and /video/{job_id} routes
├── browser.py       # No changes
├── gemini.py        # Existing chat logic (unchanged)
├── video.py         # Veo3 interaction logic
└── jobs.py          # Job store and background task management
outputs/             # Local video file storage
```

### Job Management

- In-memory dictionary: `job_id → {status, video_url, local_path, error, created_at}`
- Background `asyncio.Task` per job
- Jobs older than 1 hour auto-cleaned
- Shares `chrome.lock` with `/chat` endpoint for serialization

### Error Handling

- Upload failure → `failed` with descriptive error
- Veo3 not available → `failed` with `"veo3_not_available"`
- Generation timeout (5 min) → `failed` with `"timeout"`
- File validation: max 5 images, common formats only, max 10MB each
- Server restart loses in-memory jobs (acceptable for single-user tool)
