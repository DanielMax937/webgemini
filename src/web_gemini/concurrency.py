"""Bounded concurrency, per-task page lifecycle, and clipboard mutex for webgemini.

Environment variables:
  WG_MAX_CONCURRENT   – max simultaneous tasks (default: 10)
  WG_TASK_TIMEOUT_S   – per-task timeout in seconds for chat/video/music (default: 600)
  WG_IMAGE_TASK_TIMEOUT_S – image jobs only: seconds from slot start until images must be ready (default: 300 = 5 min)
  WG_DEEP_RESEARCH_TASK_TIMEOUT_S – POST /deepresearch job wall-clock timeout in seconds (default: 3600)
  WG_DEEP_RESEARCH_MAX_POLL_S – gemini.send_prompt (tool=deep_research): max seconds to wait for assistant Copy button before export step (default: 3600)
  WG_DEEP_RESEARCH_CONFIRM_TIMEOUT_S / WG_DEEP_RESEARCH_CONFIRM_POLL_S – gemini.send_prompt: wait for and click “Start research” / 「开始研究」 after submit (defaults: 120s, 2s)
  WG_DEEP_RESEARCH_LINK_MARKERS – comma-separated substrings for the plan confirmation URL in page (default: deep_research_confirmation,googleusercontent.com/deep_research)
  WG_DEEP_RESEARCH_PLAN_LINK_TIMEOUT_S / WG_DEEP_RESEARCH_PLAN_LINK_POLL_S – wait until that link appears (defaults: 600s, 2s)
  WG_DEEP_RESEARCH_EXEC_CONFIRM_TIMEOUT_S / WG_DEEP_RESEARCH_EXEC_CONFIRM_POLL_S – after link appears, wait for and click second confirm to start execution (defaults: 120s, 2s)
  WG_DEEP_RESEARCH_BODY_LOG – set to "0"/"false" to disable saving body HTML + layout probe during Deep Research polls (default: on)
  WG_DEEP_RESEARCH_BODY_LOG_INTERVAL_S – min seconds between body snapshots per job+phase (default: 15)
  WG_DEEP_RESEARCH_BODY_LOG_MAX_BYTES – max UTF-8 bytes written per .html file; larger bodies are truncated (default: 12000000)
  WG_DEEP_RESEARCH_EXPORT_CLICK – set to "0"/"false" to skip Share/Export click + after-click HTML dumps (default: on)
  WG_DEEP_RESEARCH_EXPORT_WAIT_NOT_SPINNING_S / WG_DEEP_RESEARCH_EXPORT_SPIN_POLL_S – wait until send area not loading before Export (defaults: 300s, 2s)
  WG_DEEP_RESEARCH_EXPORT_POST_CLICK_WAIT_S – seconds to wait after click before first after-click HTML (default: 2)
  WG_DEEP_RESEARCH_COPY_CONTENTS_CLICK – set to "0"/"false" to skip clicking **Copy contents** in the Share & Export menu (default: on)
  WG_DEEP_RESEARCH_COPY_CONTENTS_TIMEOUT_S / WG_DEEP_RESEARCH_COPY_CONTENTS_POLL_S – wait for Copy contents menu item after Share & Export (defaults: 45s, 0.5s)
  WG_USE_DOM_EXTRACTION – set to "1"/"true" to skip clipboard and always use DOM (default: false)
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)

MAX_CONCURRENT: int = int(os.environ.get("WG_MAX_CONCURRENT", "10"))
TASK_TIMEOUT_S: int = int(os.environ.get("WG_TASK_TIMEOUT_S", "600"))
# Image generation: wall-clock from job execution start (concurrency slot) until success or failure.
IMAGE_TASK_TIMEOUT_S: int = int(os.environ.get("WG_IMAGE_TASK_TIMEOUT_S", "300"))
DEEP_RESEARCH_TASK_TIMEOUT_S: int = int(os.environ.get("WG_DEEP_RESEARCH_TASK_TIMEOUT_S", "3600"))
USE_DOM_EXTRACTION: bool = os.environ.get("WG_USE_DOM_EXTRACTION", "").lower() in ("1", "true", "yes")

# Lazy singletons — created on first access within the running event loop
_semaphore: asyncio.Semaphore | None = None
_clipboard_lock: asyncio.Lock | None = None

# Observable counters (for /metrics and log lines)
_active: int = 0
_queued: int = 0


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


def get_clipboard_lock() -> asyncio.Lock:
    global _clipboard_lock
    if _clipboard_lock is None:
        _clipboard_lock = asyncio.Lock()
    return _clipboard_lock


def metrics() -> dict:
    """Return current concurrency/queue metrics (for /metrics endpoint and logging)."""
    return {"active": _active, "queued": _queued, "max_concurrent": MAX_CONCURRENT}


@asynccontextmanager
async def concurrency_slot(job_id: str) -> AsyncIterator[None]:
    """Acquire a concurrency slot; tasks beyond MAX_CONCURRENT wait in queue.

    Usage::

        async with concurrency_slot(job_id):
            ...  # task body; slot is held for the duration
    """
    global _active, _queued

    sem = get_semaphore()
    _queued += 1
    logger.info(
        "[concurrency] job=%s QUEUED  queued=%d active=%d/%d",
        job_id, _queued, _active, MAX_CONCURRENT,
    )
    try:
        await sem.acquire()
    finally:
        _queued -= 1

    _active += 1
    logger.info(
        "[concurrency] job=%s STARTED queued=%d active=%d/%d",
        job_id, _queued, _active, MAX_CONCURRENT,
    )
    try:
        yield
    finally:
        sem.release()
        _active -= 1
        logger.info(
            "[concurrency] job=%s RELEASED queued=%d active=%d/%d",
            job_id, _queued, _active, MAX_CONCURRENT,
        )


@asynccontextmanager
async def clipboard_section(job_id: str) -> AsyncIterator[None]:
    """Global mutex for the clipboard critical section.

    Scope: click Copy button → read pbpaste → store text in task buffer.
    This prevents concurrent tasks from corrupting each other's clipboard reads.
    """
    lock = get_clipboard_lock()
    logger.debug("[clipboard] job=%s waiting for lock", job_id)
    async with lock:
        logger.debug("[clipboard] job=%s acquired lock", job_id)
        yield
    logger.debug("[clipboard] job=%s released lock", job_id)
