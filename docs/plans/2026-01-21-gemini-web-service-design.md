# Gemini Web Service Design

## Overview

FastAPI service that accepts user prompts, interacts with Gemini Web via Patchwright browser automation, and returns text responses with generated images.

## Architecture

```
┌─────────────┐     ┌─────────────────┐     ┌─────────────┐
│   Client    │────▶│  FastAPI Server │────▶│   Gemini    │
│             │◀────│  + Patchwright  │◀────│   Web UI    │
└─────────────┘     └─────────────────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ Local Image │
                    │   Storage   │
                    └─────────────┘
```

## Key Decisions

- **Single browser instance** with one tab per request (tabs managed via async lock)
- **Persistent browser profile** for Google login session (user logs in once manually)
- **Patchwright** for detection-resistant browser automation
- **Returns**: text response + image URLs + local backup paths

## API Design

### POST /chat

**Request:**
```json
{
  "prompt": "Generate an image of a sunset over mountains"
}
```

**Response:**
```json
{
  "text": "Here's an image of a sunset over mountains...",
  "images": [
    {
      "url": "https://...",
      "local_path": "/path/to/images/2026-01-21-abc123.png"
    }
  ]
}
```

## Project Structure

```
web-gemini/
├── pyproject.toml          # uv project config
├── src/
│   └── web_gemini/
│       ├── __init__.py
│       ├── main.py         # FastAPI app + endpoint
│       ├── browser.py      # Patchwright browser manager
│       └── gemini.py       # Gemini interaction logic
├── images/                 # Local image backup storage
└── browser_profile/        # Persistent Chrome profile
```

## Implementation Flow

### Browser Manager (browser.py)

1. Launch Chromium with persistent profile on startup
2. Provide async lock for tab access (one request at a time)
3. Create new tab per request, close after completion

### Gemini Logic (gemini.py)

1. Navigate to `https://gemini.google.com/app`
2. Find input textarea, fill prompt, press Enter
3. Poll for stop button disappearance (max 120s timeout)
4. Click copy button, read clipboard for text
5. Extract image URLs from page, download to local storage
6. Return text + image data

### FastAPI App (main.py)

1. On startup: initialize browser manager
2. On shutdown: close browser
3. `/chat` endpoint: acquire lock → run gemini logic → release lock → return response
