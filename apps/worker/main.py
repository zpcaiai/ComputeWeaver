from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from apps.api.store import PostgresResourceStore
from apps.worker.optimizer import optimization_handler
from config.settings import Settings
from packages.jobs.queue import DurableJob, PostgresJobQueue
from packages.persistence.postgres import PostgresRuntime

LOGGER = logging.getLogger("computeweaver.worker")
Handler = Callable[[DurableJob], Awaitable[dict[str, Any]]]


class Worker:
    def __init__(self, queue: PostgresJobQueue, *, worker_id: str, handlers: dict[str, Handler]) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = handlers
        self.processed = 0

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.queue.claim, worker_id=self.worker_id)
        if job is None:
            return False
        try:
            handler = self.handlers[job.kind]
            result = await handler(job)
            await asyncio.to_thread(self.queue.succeed, job, worker_id=self.worker_id, result=result)
            self.processed += 1
        except Exception as error:
            LOGGER.exception("durable job failed", extra={"job_id": job.id, "kind": job.kind})
            await asyncio.to_thread(
                self.queue.fail,
                job,
                worker_id=self.worker_id,
                error=f"{type(error).__name__}: {error}",
            )
        return True


def default_handlers(store: PostgresResourceStore) -> dict[str, Handler]:
    async def resource_put(job: DurableJob) -> dict[str, Any]:
        payload = job.payload
        resource = await asyncio.to_thread(
            store.put,
            kind=str(payload["kind"]),
            resource_id=str(payload["resource_id"]),
            tenant_id=job.tenant_id,
            body=dict(payload["body"]),
            idempotency_key=f"job:{job.idempotency_key}",
            if_match=payload.get("if_match"),
        )
        return {"resource_id": resource.id, "version": resource.version, "etag": resource.etag}

    async def heartbeat(job: DurableJob) -> dict[str, Any]:
        return {"status": "ok", "job_id": job.id}

    return {
        "resource_put": resource_put,
        "heartbeat": heartbeat,
        "optimization_run": optimization_handler,
    }


async def main() -> None:
    settings = Settings.from_env()
    if settings.in_memory_mode:
        raise RuntimeError("worker requires PostgreSQL persistence")
    runtime = PostgresRuntime(
        settings.database_url,
        min_size=settings.database_pool_min,
        max_size=settings.database_pool_max,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    runtime.open()
    queue = PostgresJobQueue(runtime)
    store = PostgresResourceStore(runtime)
    worker_id = os.getenv("COMPUTEWEAVER_WORKER_ID", f"{socket.gethostname()}:{os.getpid()}")
    worker = Worker(queue, worker_id=worker_id, handlers=default_handlers(store))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    try:
        while not stop.is_set():
            if not await worker.run_once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
    finally:
        runtime.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("COMPUTEWEAVER_LOG_LEVEL", "INFO"))
    asyncio.run(main())
