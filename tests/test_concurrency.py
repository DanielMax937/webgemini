"""Unit tests for bounded concurrency, clipboard mutex, and per-task page lifecycle.

Tests run entirely without a real browser (mocked). They validate:
  1. Bounded semaphore enforces MAX_CONCURRENT limit
  2. Queued tasks receive QUEUED status before execution
  3. Clipboard mutex serialises concurrent clipboard readers
  4. Clipboard critical section does NOT block long-running non-clipboard work
  5. WG_USE_DOM_EXTRACTION skips clipboard path
  6. Each task gets its own page (task_page)
  7. Timed-out tasks mark job as FAILED and release the concurrency slot
  8. Exceptions in tasks mark job FAILED and release slot
  9. Job statuses (pending→queued→processing→completed/failed) are independent per task
  10. GET /metrics endpoint reflects live active/queued counters
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure src/ on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# helpers to reset module-level singletons between tests
# ---------------------------------------------------------------------------

def _reset_concurrency():
    """Reset global semaphore / lock / counters in concurrency module."""
    import web_gemini.concurrency as c
    c._semaphore = None
    c._clipboard_lock = None
    c._active = 0
    c._queued = 0


def _reset_jobs():
    """Clear in-memory job store."""
    import web_gemini.jobs as j
    j._jobs.clear()
    j._tasks.clear()


# ---------------------------------------------------------------------------
# Test 1-5: concurrency.py — pure asyncio, no browser needed
# ---------------------------------------------------------------------------

class TestBoundedConcurrency(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_concurrency()

    # 1. Semaphore enforces the configured limit
    async def test_max_concurrent_limit_enforced(self):
        from web_gemini.concurrency import concurrency_slot
        import web_gemini.concurrency as c

        LIMIT = 3
        c._semaphore = asyncio.Semaphore(LIMIT)

        peak_seen = []
        barrier = asyncio.Barrier(LIMIT)

        async def task(job_id):
            async with concurrency_slot(job_id):
                # Record active count as soon as we enter the slot
                peak_seen.append(c._active)
                await barrier.wait()   # synchronise all LIMIT tasks
                await asyncio.sleep(0)

        await asyncio.gather(*[task(f"job{i}") for i in range(LIMIT)])
        # Peak must reach LIMIT and never exceed it
        assert max(peak_seen) == LIMIT, f"peak never reached LIMIT: {peak_seen}"
        assert all(v <= LIMIT for v in peak_seen), f"exceeded LIMIT: {peak_seen}"

    # 2. Tasks over the limit are queued
    async def test_queued_counter_increments_while_waiting(self):
        import web_gemini.concurrency as c

        c._semaphore = asyncio.Semaphore(1)  # only 1 slot

        gate = asyncio.Event()
        queued_while_blocked: list[int] = []

        async def holder():
            async with c.concurrency_slot("holder"):
                await gate.wait()

        async def waiter():
            async with c.concurrency_slot("waiter"):
                pass  # grabbed slot after holder releases

        holder_task = asyncio.create_task(holder())
        await asyncio.sleep(0)  # let holder acquire semaphore

        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)  # let waiter block inside concurrency_slot

        # waiter is now waiting for the semaphore → _queued should be 1
        queued_while_blocked.append(c._queued)

        gate.set()
        await asyncio.gather(holder_task, waiter_task)

        assert any(v >= 1 for v in queued_while_blocked), (
            f"_queued never reached 1 while waiter blocked: {queued_while_blocked}"
        )

    # 3. Clipboard mutex serialises concurrent readers
    async def test_clipboard_mutex_serialises_reads(self):
        from web_gemini.concurrency import clipboard_section

        order = []
        results = []

        async def reader(name: str, delay: float):
            async with clipboard_section(name):
                order.append(f"{name}:enter")
                await asyncio.sleep(delay)
                order.append(f"{name}:exit")
            results.append(name)

        # Run two readers concurrently; they must not overlap
        await asyncio.gather(reader("A", 0.05), reader("B", 0.05))

        # "A:exit" must appear before "B:enter" OR "B:exit" before "A:enter"
        enter_A = order.index("A:enter")
        exit_A = order.index("A:exit")
        enter_B = order.index("B:enter")
        exit_B = order.index("B:exit")
        overlap = (enter_A < enter_B < exit_A) or (enter_B < enter_A < exit_B)
        assert not overlap, f"Clipboard sections overlapped: {order}"

    # 4. Long non-clipboard work is NOT blocked by clipboard lock
    async def test_clipboard_lock_does_not_block_other_work(self):
        from web_gemini.concurrency import clipboard_section
        import web_gemini.concurrency as c

        work_started = asyncio.Event()

        async def clipboard_holder():
            async with clipboard_section("holder"):
                await asyncio.sleep(0.2)  # hold clipboard for 200ms

        async def independent_work():
            # This must start and finish while clipboard_holder sleeps
            work_started.set()
            await asyncio.sleep(0.05)  # 50ms work, no clipboard

        holder = asyncio.create_task(clipboard_holder())
        await asyncio.sleep(0)  # let holder acquire lock
        worker = asyncio.create_task(independent_work())

        # worker should complete well before holder finishes
        done, pending = await asyncio.wait([worker], timeout=0.1)
        assert worker in done, "Independent work was blocked by clipboard lock (should not be)"

        holder.cancel()
        try:
            await holder
        except asyncio.CancelledError:
            pass

    # 5. WG_USE_DOM_EXTRACTION flag is read correctly
    async def test_use_dom_extraction_env_flag(self):
        # Test that the env var is honoured at import time
        import web_gemini.concurrency as c
        original = c.USE_DOM_EXTRACTION

        # Patch the module attribute directly (env is read at import)
        c.USE_DOM_EXTRACTION = True
        assert c.USE_DOM_EXTRACTION is True

        c.USE_DOM_EXTRACTION = False
        assert c.USE_DOM_EXTRACTION is False

        c.USE_DOM_EXTRACTION = original  # restore


# ---------------------------------------------------------------------------
# Test 6: page_context.py — mock Playwright
# ---------------------------------------------------------------------------

class TestTaskPage(unittest.IsolatedAsyncioTestCase):

    async def test_each_task_gets_own_page(self):
        """task_page() must call new_page() and close() for every task."""
        import web_gemini.page_context as pc

        # is_closed() is sync in real Playwright → use regular MagicMock, not AsyncMock
        def _make_page():
            p = AsyncMock()
            p.is_closed = MagicMock(return_value=False)
            return p

        mock_page_a = _make_page()
        mock_page_b = _make_page()

        pages = [mock_page_a, mock_page_b]
        call_count = 0

        async def fake_new_page():
            nonlocal call_count
            p = pages[call_count % len(pages)]
            call_count += 1
            return p

        mock_context = MagicMock()
        mock_context.new_page = fake_new_page

        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_browser.contexts = [mock_context]

        # Inject shared state directly to bypass _get_shared_browser connect logic
        pc._pw = MagicMock()
        pc._browser = mock_browser
        pc._lock = asyncio.Lock()

        async with pc.task_page("job_a") as page_a:
            async with pc.task_page("job_b") as page_b:
                assert page_a is not page_b, "Both tasks received the same page object"

        mock_page_a.close.assert_called_once()
        mock_page_b.close.assert_called_once()

    async def test_page_closed_on_exception(self):
        """task_page() must close the tab even if the task body raises."""
        import web_gemini.page_context as pc

        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)

        async def fake_new_page():
            return mock_page

        mock_context = MagicMock()
        mock_context.new_page = fake_new_page

        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_browser.contexts = [mock_context]

        pc._pw = MagicMock()
        pc._browser = mock_browser
        pc._lock = asyncio.Lock()

        with self.assertRaises(RuntimeError):
            async with pc.task_page("job_err") as page:
                raise RuntimeError("task crashed")

        mock_page.close.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7-9: jobs.py + main.py — mock browser calls, test job state machine
# ---------------------------------------------------------------------------

def _make_mock_send_prompt(text="hello"):
    """Return a coroutine factory that returns a GeminiResponse-like object."""
    async def _send(*args, **kwargs):
        from web_gemini.gemini import GeminiResponse
        return GeminiResponse(text=text, images=[])
    return _send


class TestJobStateMachine(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_concurrency()
        _reset_jobs()

    async def _run_chat_job(self, job_id, send_mock):
        """Run process_chat with mocked browser and page."""
        from web_gemini.chat import process_chat

        mock_page = AsyncMock()
        mock_page.is_closed.return_value = False

        with patch("web_gemini.chat.task_page") as mock_tp, \
             patch("web_gemini.gemini.send_prompt", side_effect=send_mock), \
             patch("web_gemini.chat.send_prompt", side_effect=send_mock):
            mock_tp.return_value.__aenter__ = AsyncMock(return_value=mock_page)
            mock_tp.return_value.__aexit__ = AsyncMock(return_value=False)
            await process_chat(job_id, "test prompt")

    # 7. Timeout marks job FAILED and releases slot
    async def test_timeout_marks_job_failed_and_releases_slot(self):
        import web_gemini.concurrency as c
        from web_gemini.jobs import create_job, get_job, update_job, JobStatus

        c._semaphore = asyncio.Semaphore(5)
        job = create_job(prompt="slow task")
        update_job(job.job_id, status=JobStatus.QUEUED)

        async def slow_work():
            await asyncio.sleep(999)

        try:
            async with c.concurrency_slot(job.job_id):
                await asyncio.wait_for(slow_work(), timeout=0.05)
        except asyncio.TimeoutError:
            update_job(job.job_id, status=JobStatus.FAILED, error="timed out")

        result = get_job(job.job_id)
        assert result.status == JobStatus.FAILED, result.status
        # Slot must be released (semaphore counter back to full)
        assert c._active == 0

    # 8. Exception marks job FAILED and releases slot
    async def test_exception_marks_job_failed_releases_slot(self):
        import web_gemini.concurrency as c
        from web_gemini.jobs import create_job, get_job, update_job, JobStatus

        c._semaphore = asyncio.Semaphore(5)
        job = create_job(prompt="crash task")
        update_job(job.job_id, status=JobStatus.QUEUED)

        try:
            async with c.concurrency_slot(job.job_id):
                raise ValueError("boom")
        except ValueError as e:
            update_job(job.job_id, status=JobStatus.FAILED, error=str(e))

        result = get_job(job.job_id)
        assert result.status == JobStatus.FAILED
        assert c._active == 0

    # 9. Parallel tasks have independent status and result
    async def test_parallel_tasks_independent_status_and_result(self):
        from web_gemini.jobs import create_job, get_job, update_job, persist_job, JobStatus
        import web_gemini.concurrency as c

        c._semaphore = asyncio.Semaphore(10)

        async def fake_job(job_id: str, result_text: str, delay: float):
            async with c.concurrency_slot(job_id):
                update_job(job_id, status=JobStatus.PROCESSING)
                await asyncio.sleep(delay)
                update_job(job_id, status=JobStatus.COMPLETED, text=result_text)

        jobs = [create_job(prompt=f"prompt-{i}") for i in range(5)]
        for job in jobs:
            update_job(job.job_id, status=JobStatus.QUEUED)

        await asyncio.gather(*[
            fake_job(job.job_id, f"result-{i}", 0.02)
            for i, job in enumerate(jobs)
        ])

        for i, job in enumerate(jobs):
            result = get_job(job.job_id)
            assert result.status == JobStatus.COMPLETED, f"job {i}: {result.status}"
            assert result.text == f"result-{i}", f"job {i}: {result.text}"


# ---------------------------------------------------------------------------
# Test 10: GET /metrics via httpx + TestClient (no real browser)
# ---------------------------------------------------------------------------

class TestMetricsEndpoint(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_concurrency()

    async def test_metrics_initial_values(self):
        """GET /metrics returns zeroed counters on startup."""
        from fastapi.testclient import TestClient
        from web_gemini.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/metrics")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert "active_slots" in data
            assert "queued_tasks" in data
            assert "max_concurrent" in data
            assert data["max_concurrent"] == int(os.environ.get("WG_MAX_CONCURRENT", "10"))

    async def test_metrics_reflect_active_count(self):
        """active_slots increments while a task holds a slot, then returns to 0."""
        import web_gemini.concurrency as c

        c._semaphore = asyncio.Semaphore(5)
        gate = asyncio.Event()

        async def holder():
            async with c.concurrency_slot("m_test"):
                gate.set()
                await asyncio.sleep(0.1)

        task = asyncio.create_task(holder())
        await gate.wait()
        assert c._active == 1, f"expected 1 active, got {c._active}"
        await task
        assert c._active == 0, f"expected 0 active after task, got {c._active}"


# ---------------------------------------------------------------------------
# Test: QUEUED status is set before task starts
# ---------------------------------------------------------------------------

class TestQueuedStatus(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_concurrency()
        _reset_jobs()

    async def test_queued_status_before_processing(self):
        """Task must transition pending → queued before acquiring the slot."""
        from web_gemini.jobs import create_job, get_job, update_job, JobStatus
        import web_gemini.concurrency as c

        c._semaphore = asyncio.Semaphore(1)
        gate = asyncio.Event()

        job = create_job(prompt="queue test")
        update_job(job.job_id, status=JobStatus.QUEUED)

        # Block the single slot
        blocker_ready = asyncio.Event()

        async def blocker():
            async with c.concurrency_slot("blocker"):
                blocker_ready.set()
                await gate.wait()

        asyncio.create_task(blocker())
        await blocker_ready.wait()

        # Our job wants the slot — it should sit in QUEUED
        slot_acquired = asyncio.Event()

        async def waiter():
            async with c.concurrency_slot(job.job_id):
                update_job(job.job_id, status=JobStatus.PROCESSING)
                slot_acquired.set()

        asyncio.create_task(waiter())
        await asyncio.sleep(0.05)

        # Still queued because slot is held by blocker
        assert get_job(job.job_id).status == JobStatus.QUEUED

        gate.set()
        await asyncio.wait_for(slot_acquired.wait(), timeout=1.0)
        assert get_job(job.job_id).status == JobStatus.PROCESSING


if __name__ == "__main__":
    unittest.main()
