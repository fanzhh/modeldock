"""空闲自动释放器（DESIGN.md §4.5）。

- 每 interval 秒扫描一次 `ready` 部署，超过空闲阈值 → release
- 活动基准：部署下 Chat 线程的最后一条消息时间；无消息则用进入 ready 的时间
- 阈值：params.idle_timeout_min 优先，其次 MODELDOCK_IDLE_TIMEOUT_MIN；<=0 关闭
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import ChatMessage, ChatThread, Deployment, get_db_engine

log = logging.getLogger("modeldock.autorelease")


def _as_utc(value: dt.datetime | None) -> float:
    """DB 里可能是 naive（sqlite）或 aware；统一转 epoch 秒。"""
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.timestamp()


class AutoReleaser:
    def __init__(self, releaser, interval: float = 60.0) -> None:
        """releaser: async callable(dep_id)（Orchestrator.release）。"""
        self._releaser = releaser
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def _candidates(self) -> list[tuple[int, float]]:
        """[((dep_id, idle_minutes))] 需要自动释放的 ready 部署。"""
        now = _as_utc(dt.datetime.now(dt.timezone.utc))
        out: list[tuple[int, float]] = []
        with Session(get_db_engine()) as s:
            deps = s.scalars(select(Deployment).where(Deployment.state == "ready")).all()
            for d in deps:
                timeout_min = float((d.params or {}).get("idle_timeout_min",
                                                         settings.idle_timeout_min) or 0)
                if timeout_min <= 0:
                    continue
                last_msg = s.scalar(
                    select(func.max(ChatMessage.created_at))
                    .join(ChatThread, ChatMessage.thread_id == ChatThread.id)
                    .where(ChatThread.deployment_id == d.id))
                if last_msg is not None:
                    last_active = _as_utc(last_msg)
                else:
                    # 无消息：取 state_history 中进入 ready 的时间
                    ts = next((h.get("at") for h in reversed(d.state_history or [])
                               if h.get("state") == "ready"), None)
                    last_active = _as_utc(dt.datetime.fromisoformat(ts)) if ts else 0.0
                idle_min = (now - last_active) / 60.0
                if idle_min >= timeout_min:
                    out.append((d.id, idle_min))
        return out

    async def _loop(self) -> None:
        while True:
            try:
                for dep_id, idle_min in self._candidates():
                    log.info("auto-release deployment %s (idle %.1f min)", dep_id, idle_min)
                    await self._releaser(dep_id)
            except Exception:  # noqa: BLE001
                log.exception("auto-release scan failed")
            await asyncio.sleep(self._interval)
