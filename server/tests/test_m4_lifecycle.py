"""M4 测试：余额快照、空闲自动释放、params 透出。

约束：mock backend + tmp DB，时钟可控，不触真实 CLI/网络。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.colab.autoreleaser import AutoReleaser
from app.core.colab.mock import MockColabBackend
from app.core.colab.orchestrator import Orchestrator
from app.db import (ChatMessage, ChatThread, Deployment, ModelFamily, ModelFile,
                    Recipe, get_db_engine, init_db, set_db_path)
from app.main import app


def _setup(tmp_path, dep_params: dict | None = None) -> tuple[int, Orchestrator]:
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    (tmp_path / "rclone.conf").write_text("[gdrive]\ntype = drive\ntoken = x\n")
    with Session(get_db_engine()) as s:
        fam = ModelFamily(name="Qwen3-8B", purpose="chat")
        s.add(fam)
        s.flush()
        s.add(ModelFile(family_id=fam.id, name="q.gguf", remote_path="q/q.gguf", bytes=1))
        rec = Recipe(name="llamacpp-chat", kind_class="chat",
                     manifest={"models": {"chat": {"family": "Qwen3-8B"}}},
                     run_script="true", ports=[8080])
        s.add(rec)
        s.flush()
        dep = Deployment(recipe_id=rec.id, gpu="T4",
                         state="queued", state_history=[{"state": "queued"}], logs="")
        dep.params = dict(dep_params or {})
        dep.params.setdefault("session", {"id": "mock-x", "gpu": "T4"})
        s.add(dep)
        s.commit()
        return dep.id, Orchestrator(backend=MockColabBackend())


def _iso_minutes_ago(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat()


def _get_dep(dep_id: int) -> Deployment:
    with Session(get_db_engine()) as s:
        return s.get(Deployment, dep_id)


async def _wait_state(dep_id: int, want: set[str], timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = _get_dep(dep_id)
        if d.state in want:
            return d.state
        await asyncio.sleep(0.05)
    raise AssertionError(f"did not reach {want} (last={d.state})")


# ---------- 余额快照 ----------

@pytest.mark.asyncio
async def test_usage_snapshots_before_and_after(tmp_path):
    """启动自动抓 usage_before；release 抓 usage_after（会话费用估算依据）。"""
    dep_id, orch = _setup(tmp_path)
    orch.start(dep_id)
    await _wait_state(dep_id, {"ready", "failed", "done"})
    d = _get_dep(dep_id)
    assert d.params["usage_before"] == {"compute_units": "42.0 (mock)"}
    await orch.release(dep_id)
    d = _get_dep(dep_id)
    assert d.state == "done"
    assert d.params["usage_after"] == {"compute_units": "42.0 (mock)"}


# ---------- 自动释放 ----------

def _make_ready(dep_id: int, ready_at: str, last_msg_at=None) -> None:
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        d.state = "ready"
        hist = [h for h in (d.state_history or []) if h.get("state") != "ready"]
        d.state_history = hist + [{"state": "ready", "at": ready_at}]
        s.commit()
        if last_msg_at is not None:
            t = ChatThread(deployment_id=dep_id, title="t")
            s.add(t)
            s.flush()
            s.add(ChatMessage(thread_id=t.id, role="assistant", content="hi",
                              created_at=last_msg_at))
            s.commit()


@pytest.mark.asyncio
async def test_auto_release_on_idle_ready_state(tmp_path):
    """ready 后无任何消息 → 以 ready 时间为基准超时释放。"""
    dep_id, orch = _setup(tmp_path, dep_params={"idle_timeout_min": 10})
    _make_ready(dep_id, _iso_minutes_ago(30))  # ready 已 30 分钟
    released = []
    releaser = AutoReleaser(lambda i: released.append(i) or asyncio.sleep(0),
                           interval=0.05)
    await releaser.start()
    for _ in range(100):
        if released:
            break
        await asyncio.sleep(0.05)
    await releaser.stop()
    assert released == [dep_id]


@pytest.mark.asyncio
async def test_chat_activity_resets_idle(tmp_path):
    """有最近消息 → 活动基准是消息时间，未超时不释放。"""
    dep_id, orch = _setup(tmp_path, dep_params={"idle_timeout_min": 60})
    _make_ready(dep_id, _iso_minutes_ago(120),  # ready 很久了
                last_msg_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5))
    released = []
    releaser = AutoReleaser(lambda i: released.append(i) or asyncio.sleep(0),
                            interval=0.05)
    await releaser.start()
    await asyncio.sleep(0.3)
    await releaser.stop()
    assert released == []  # 消息 5 分钟前 < 60 分钟阈值 → 不释放


def test_candidates_timeout_off(tmp_path):
    """idle_timeout_min <= 0（默认 0）→ 不在任何候选里。"""
    dep_id, orch = _setup(tmp_path)  # 无 params.idle_timeout_min，env 默认 0
    _make_ready(dep_id, _iso_minutes_ago(9999))
    releaser = AutoReleaser(lambda i: None)
    assert releaser._candidates() == []


@pytest.mark.asyncio
async def test_auto_release_via_orchestrator_integration(tmp_path):
    """集成：AutoReleaser 直接调 Orchestrator.release，状态变 done。"""
    dep_id, orch = _setup(tmp_path, dep_params={"idle_timeout_min": 5})
    _make_ready(dep_id, _iso_minutes_ago(30))
    releaser = AutoReleaser(orch.release, interval=0.05)
    await releaser.start()
    await _wait_state(dep_id, {"done"}, timeout=5)
    await releaser.stop()


# ---------- API：params 透出 ----------

def test_deployment_out_carries_params(tmp_path):
    from app.core.colab.orchestrator import set_orchestrator
    dep_id, orch = _setup(tmp_path, dep_params={"idle_timeout_min": 12})
    with Session(get_db_engine()) as s:
        dep = s.get(Deployment, dep_id)
        params = dict(dep.params or {})
        params["chat_endpoint"] = "http://127.0.0.1:18080"
        dep.params = params  # JSON 列需整体重赋值才会标脏
        s.commit()
    set_orchestrator(Orchestrator(backend=MockColabBackend()))
    with TestClient(app) as c:
        r = c.get(f"/api/deployments/{dep_id}").json()
        assert r["params"]["idle_timeout_min"] == 12
        assert r["params"]["chat_endpoint"] == "http://127.0.0.1:18080"
