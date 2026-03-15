"""
PostgreSQL persistence for webgemini jobs.
"""
import json
import os
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

POSTGRES_CONFIG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": int(os.getenv("PGPORT", "5432")),
    "database": os.getenv("PGDATABASE", "webgemini"),
    "user": os.getenv("PGUSER", os.getenv("USER", "postgres")),
    "password": os.getenv("PGPASSWORD", "postgres"),
}


def get_connection():
    """Get a PostgreSQL connection."""
    return psycopg2.connect(**POSTGRES_CONFIG)


def init_db():
    """Create jobs table if not exists."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS webgemini_jobs (
                    job_id VARCHAR(32) PRIMARY KEY,
                    status VARCHAR(32) NOT NULL,
                    prompt TEXT NOT NULL,
                    tool VARCHAR(64),
                    attachments JSONB DEFAULT '[]',
                    image_paths JSONB DEFAULT '[]',
                    text TEXT,
                    images JSONB DEFAULT '[]',
                    video_url TEXT,
                    local_path TEXT,
                    error TEXT,
                    gemini_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_webgemini_jobs_status ON webgemini_jobs(status);
                CREATE INDEX IF NOT EXISTS idx_webgemini_jobs_created_at ON webgemini_jobs(created_at);
            """)
            conn.commit()
    finally:
        conn.close()


def insert_job(
    job_id: str,
    status: str,
    prompt: str,
    tool: Optional[str] = None,
    attachments: Optional[list] = None,
    image_paths: Optional[list] = None,
) -> None:
    """Insert a new job."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webgemini_jobs (job_id, status, prompt, tool, attachments, image_paths)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    job_id,
                    status,
                    prompt,
                    tool,
                    json.dumps(attachments or []),
                    json.dumps(image_paths or []),
                ),
            )
            conn.commit()
    finally:
        conn.close()


def update_job_db(
    job_id: str,
    status: Optional[str] = None,
    text: Optional[str] = None,
    images: Optional[list] = None,
    video_url: Optional[str] = None,
    local_path: Optional[str] = None,
    error: Optional[str] = None,
    gemini_url: Optional[str] = None,
) -> None:
    """Update job fields in database."""
    conn = get_connection()
    try:
        updates = []
        values = []
        if status is not None:
            updates.append("status = %s")
            values.append(status)
        if text is not None:
            updates.append("text = %s")
            values.append(text)
        if images is not None:
            updates.append("images = %s")
            values.append(json.dumps(images))
        if video_url is not None:
            updates.append("video_url = %s")
            values.append(video_url)
        if local_path is not None:
            updates.append("local_path = %s")
            values.append(local_path)
        if error is not None:
            updates.append("error = %s")
            values.append(error)
        if gemini_url is not None:
            updates.append("gemini_url = %s")
            values.append(gemini_url)

        if not updates:
            return

        updates.append("updated_at = CURRENT_TIMESTAMP")
        values.append(job_id)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE webgemini_jobs SET {', '.join(updates)} WHERE job_id = %s",
                values,
            )
            conn.commit()
    finally:
        conn.close()


def get_job_db(job_id: str) -> Optional[dict]:
    """Load job from database."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM webgemini_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            # JSONB columns may come as str from some drivers
            for key in ("attachments", "image_paths", "images"):
                if d.get(key) and isinstance(d[key], str):
                    d[key] = json.loads(d[key])
            return d
    finally:
        conn.close()
