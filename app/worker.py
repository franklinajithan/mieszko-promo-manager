"""Persistent PostgreSQL job queue. One advisory-locked worker per database."""

import json
import logging
import signal
import time

from sqlalchemy import select, text

from app.config import settings
from app.images import atomic_write
from app.imports import commit_import, preview_import, progress
from app.labels import render_pdf
from app.models import Job, SessionLocal, audit, engine

log = logging.getLogger("promo.worker")
stopping = False


def process_one():
    with SessionLocal() as s:
        job = s.scalar(
            select(Job)
            .where(Job.status.in_(["queued_preview", "queued"]))
            .order_by(Job.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return False
        if job.cancel_requested:
            job.status = "cancelled"
            s.commit()
            return True
        job.status = "running"
        s.commit()
    try:
        if job.kind == "import":
            status, result = (
                commit_import(job) if job.payload.get("phase") == "commit" else preview_import(job)
            )
        else:
            rows = job.payload["rows"]

            # Render as one document, checking cancellation between every page.
            def pages():
                for i, row in enumerate(rows):
                    if progress(job.id, i, len(rows)):
                        raise InterruptedError("Cancelled")
                    yield row

            output = render_pdf(pages(), job.payload["template"])
            filename = f"{job.id}.pdf"
            atomic_write(settings.data_dir / "prints" / filename, output)
            status, result = "completed", {"file": filename, "pages": len(rows), "bytes": len(output)}
        with SessionLocal() as s:
            current = s.get(Job, job.id)
            assert current is not None
            current.status = "cancelled" if current.cancel_requested else status
            current.result = result
            if status == "completed":
                current.progress = current.total or result.get("processed", current.progress)
            audit(s, job.owner_id, f"{job.kind}.{current.status}", job.id)
            s.commit()
    except Exception as exc:
        log.exception(json.dumps({"event": "job_failed", "job_id": job.id}))
        with SessionLocal() as s:
            current = s.get(Job, job.id)
            assert current is not None
            current.status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
            current.error = (
                str(exc)[:500]
                if isinstance(exc, (ValueError, InterruptedError))
                else "Job failed. See server log using job ID."
            )
            audit(s, job.owner_id, f"{job.kind}.{current.status}", job.id)
            s.commit()
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    global stopping

    def stop(*_):
        global stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with engine.connect() as lock:
        if engine.dialect.name == "postgresql":
            if not lock.scalar(text("SELECT pg_try_advisory_lock(48215305)")):
                raise SystemExit("Another worker already owns this queue")
        with SessionLocal() as s:
            # Recovery only while holding the database-wide worker lock.
            for job in s.scalars(select(Job).where(Job.status == "running")):
                job.status = "failed"
                job.error = (
                    "Worker stopped during processing. Retry safely; completed import batches are preserved."
                )
            s.commit()
        log.info(json.dumps({"event": "worker_ready"}))
        while not stopping:
            (settings.data_dir / "worker-heartbeat").touch()
            if not process_one():
                time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
