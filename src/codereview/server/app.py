"""FastAPI webhook server with a durable job queue.

The webhook returns 202 only after the job row is committed. Workers claim jobs
with an atomic ``pending -> running`` update, so a restart or a duplicate
delivery cannot run the same PR twice.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from codereview.config import Settings
from codereview.github.webhook import parse_pull_request_event, should_review, verify_signature
from codereview.logging_utils import get_logger, setup_logging
from codereview.review.factory import build_pipeline
from codereview.server.metrics import Metrics
from codereview.storage.db import Database

log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    pipeline=None,
    db: Database | None = None,
    pipeline_factory: Callable[[Settings], object] | None = None,
    start_workers: bool = True,
    enforce_config: bool = True,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    setup_logging(cfg.log_level)
    if enforce_config:
        cfg.require_webhook()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = db or Database(cfg.db_path)
        app.state.db = database
        app.state.metrics = Metrics()
        app.state.queue = asyncio.Queue()
        app.state.pipeline = pipeline
        app.state.workers = []

        async def worker(worker_id: int) -> None:
            while True:
                delivery_id = await app.state.queue.get()
                try:
                    if not database.claim_job(delivery_id):
                        continue
                    job = database.job(delivery_id)
                    if not job:
                        continue
                    started = asyncio.get_running_loop().time()
                    try:
                        outcome = await asyncio.to_thread(
                            app.state.pipeline.review_pr,
                            job["repo"],
                            job["pr_number"],
                            job["installation_id"],
                        )
                        status = (
                            "done"
                            if outcome.status.startswith("completed") or outcome.status == "skipped"
                            else "failed"
                        )
                        database.finish_job(
                            delivery_id, status, "; ".join(outcome.errors) or None
                        )
                        app.state.metrics.inc(
                            "codereview_reviews_total", {"status": outcome.status}
                        )
                        app.state.metrics.inc(
                            "codereview_findings_total", amount=len(outcome.findings)
                        )
                        if outcome.cache_hit:
                            app.state.metrics.inc("codereview_cache_hits_total")
                    except Exception as exc:  # noqa: BLE001
                        database.finish_job(delivery_id, "failed", str(exc))
                        app.state.metrics.inc(
                            "codereview_reviews_total", {"status": "exception"}
                        )
                        log.exception(
                            "worker_failed",
                            extra={"delivery_id": delivery_id, "worker": worker_id},
                        )
                    finally:
                        elapsed = asyncio.get_running_loop().time() - started
                        app.state.metrics.observe(
                            "codereview_review_duration_seconds", elapsed
                        )
                finally:
                    app.state.queue.task_done()

        if start_workers:
            app.state.pipeline = app.state.pipeline or (pipeline_factory or build_pipeline)(cfg)
            app.state.workers = [
                asyncio.create_task(worker(index))
                for index in range(cfg.concurrency)
            ]
            for job in database.pending_jobs():
                app.state.queue.put_nowait(job["delivery_id"])

        try:
            yield
        finally:
            for task in app.state.workers:
                task.cancel()
            if app.state.workers:
                await asyncio.gather(*app.state.workers, return_exceptions=True)
            if db is None:
                database.close()

    app = FastAPI(title="codereview-agent", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        if not cfg.llm_available():
            raise HTTPException(503, "LLM is not configured")
        if not getattr(app.state, "pipeline", None):
            raise HTTPException(503, "pipeline is not ready")
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics():
        if not cfg.metrics_enabled:
            raise HTTPException(404, "metrics disabled")
        return PlainTextResponse(app.state.metrics.render())

    @app.post("/webhook")
    async def webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        secret = cfg.github_webhook_secret or ""
        if not verify_signature(secret, body, signature):
            app.state.metrics.inc(
                "codereview_webhook_rejected_total", {"reason": "signature"}
            )
            raise HTTPException(401, "invalid signature")

        event_name = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery") or uuid.uuid4().hex
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(400, "invalid JSON")

        database = app.state.db
        if not database.record_delivery(delivery_id, event_name):
            app.state.metrics.inc(
                "codereview_webhook_rejected_total", {"reason": "duplicate"}
            )
            return JSONResponse({"status": "duplicate"}, status_code=202)

        event = parse_pull_request_event(event_name, payload, delivery_id)
        if event is None:
            return JSONResponse({"status": "ignored"}, status_code=202)

        decision, reason = should_review(event, cfg)
        if not decision:
            app.state.metrics.inc(
                "codereview_webhook_rejected_total", {"reason": reason}
            )
            return JSONResponse({"status": "skipped", "reason": reason}, status_code=202)

        if not cfg.dry_run and not cfg.has_github_write():
            raise HTTPException(503, "GitHub write credentials are not configured")
        if not getattr(app.state, "pipeline", None):
            raise HTTPException(503, "pipeline is not ready")

        database.create_job(
            delivery_id,
            event.repo,
            event.number,
            event.installation_id,
            event.action,
            event.head_sha,
        )
        await app.state.queue.put(delivery_id)
        app.state.metrics.inc("codereview_webhook_received_total")
        return JSONResponse(
            {"status": "queued", "delivery": delivery_id}, status_code=202
        )

    return app
