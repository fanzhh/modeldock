"""下载引擎：asyncio 队列 + 流式断点续传 + 进度回写 + 完成后由 Drive 通道入盘。

状态机：
  queued → running → (paused | failed | done)
进度按 chunk 更新到内存，定期 flush 到 DownloadTask（M0 直接每次写库，量小可接受）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.downloads.sources import SourceError, resolve_any
from app.core.drive import Drive
from app.db import DownloadTask, get_db_engine

log = logging.getLogger("modeldock.downloads")

CHUNK = 1024 * 1024  # 1 MiB


@dataclass
class Job:
    task_id: int
    kind: str
    repo: str
    filename: str
    token_fingerprint: str  # 用于从 vault 取 token 的数据库指纹
    target_remote_rel: str
    total_bytes: int = 0
    done_bytes: int = 0
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


class DownloadEngine:
    def __init__(self, vault_getter, drive: Drive | None = None, max_parallel: int | None = None) -> None:
        """vault_getter: callable(kind:str) -> token str | None（由上层注入，按源的 scope 定位凭证）"""
        self._vault_get = vault_getter
        self._drive = drive
        self._sem = asyncio.Semaphore(max_parallel or settings.max_parallel_downloads)
        self._jobs: dict[int, Job] = {}
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    # ---- lifecycle ----
    async def start(self, n: int = 2) -> None:
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(n)]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()

    # ---- public ----
    def enqueue(self, task_id: int) -> None:
        self._queue.put_nowait(task_id)

    def cancel(self, task_id: int) -> None:
        job = self._jobs.get(task_id)
        if job:
            job.cancel.set()

    # ---- worker ----
    async def _worker(self, idx: int) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                async with self._sem:
                    await self._run_task(task_id)
            except Exception as e:  # noqa: BLE001
                log.exception("download task %s crashed", task_id)
                self._set_status(task_id, "failed", error=str(e)[:500])
            finally:
                self._queue.task_done()
                self._jobs.pop(task_id, None)

    def _load_task(self, task_id: int) -> DownloadTask:
        with Session(get_db_engine()) as s:
            t = s.get(DownloadTask, task_id)
            if t is None:
                raise RuntimeError(f"task {task_id} not found")
            return t

    def _set_status(self, task_id: int, status: str, *, progress: float | None = None,
                    done_bytes: int | None = None, error: str | None = None) -> None:
        with Session(get_db_engine()) as s:
            t = s.get(DownloadTask, task_id)
            if not t:
                return
            t.status = status
            if progress is not None:
                t.progress = round(progress, 4)
            if done_bytes is not None:
                t.done_bytes = done_bytes
            if error is not None:
                t.error = error
            s.commit()

    async def _run_task(self, task_id: int) -> None:
        t = self._load_task(task_id)
        job = Job(
            task_id=task_id, kind=t.source, repo=t.repo, filename=t.filename,
            token_fingerprint="", target_remote_rel=t.target_remote_path,
            done_bytes=t.done_bytes,
        )
        self._jobs[task_id] = job
        self._set_status(task_id, "running")

        token = self._vault_get(t.source) if t.source in ("hf", "modelscope", "url") else None
        resolved = await asyncio.to_thread(resolve_any, t.source, t.repo, t.filename, token)
        job.total_bytes = resolved.total_bytes or t.total_bytes

        local = Path(settings.download_dir) / f"task-{task_id}-{t.filename}"
        await self._stream_to(local, resolved, job)

        if self._drive is not None:
            self._set_status(task_id, "running", progress=1.0, done_bytes=job.done_bytes)
            await asyncio.to_thread(self._drive.put_file, str(local), job.target_remote_rel)
        local.unlink(missing_ok=True)
        self._set_status(task_id, "done", progress=1.0, done_bytes=job.done_bytes)

    async def _stream_to(self, local: Path, resolved, job: Job) -> None:
        headers = dict(resolved.headers)
        if local.exists() and local.stat().st_size > 0:
            headers["Range"] = f"bytes={local.stat().st_size}-"
        mode = "ab" if "Range" in headers else "wb"
        job.done_bytes = local.stat().st_size if "Range" in headers else 0
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            async with client.stream("GET", resolved.url, headers=headers) as r:
                if r.status_code == 416:  # 已完整
                    return
                r.raise_for_status()
                if "content-range" in r.headers:
                    total = int(r.headers["content-range"].rsplit("/", 1)[1])
                    job.total_bytes = total
                with open(local, mode) as f:
                    async for chunk in r.aiter_bytes(CHUNK):
                        if job.cancel.is_set():
                            self._set_status(job.task_id, "paused", done_bytes=job.done_bytes)
                            raise asyncio.CancelledError
                        f.write(chunk)
                        job.done_bytes += len(chunk)
                        if job.total_bytes and job.done_bytes % (CHUNK * 8) < CHUNK:
                            prog = job.done_bytes / job.total_bytes
                            self._set_status(job.task_id, "running", progress=prog, done_bytes=job.done_bytes)
                if job.total_bytes:
                    self._set_status(job.task_id, "running", progress=1.0, done_bytes=job.done_bytes)


# ---- 单例管理（main.py lifespan 中 start/stop）----
engine_singleton: DownloadEngine | None = None


def get_download_engine() -> DownloadEngine:
    global engine_singleton
    if engine_singleton is None:
        raise RuntimeError("download engine not started")
    return engine_singleton
