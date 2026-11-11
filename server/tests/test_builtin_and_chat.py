"""M3 测试：内置配方 seed、$family 模板解析、Chat API（非流式/流式/历史/端点）。

约束：上游聊天服务用 httpx.MockTransport 注入，不联网。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.builtin_recipes import BUILTIN_RECIPES, seed_builtin_recipes
from app.core.colab.mock import MockColabBackend
from app.core.colab.orchestrator import Orchestrator, set_orchestrator
from app.core.drive import Drive
from app.db import (Deployment, ModelFamily, ModelFile, Recipe, get_db_engine,
                    init_db, set_db_path)
from app.main import app


def _wait_state(dep_id: int, want: set[str], timeout: float = 10.0) -> str:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d.state in want:
                return d.state
        time.sleep(0.05)
    raise AssertionError(f"deployment did not reach {want} (last={d.state})")


async def _wait_state_async(dep_id: int, want: set[str], timeout: float = 10.0) -> str:
    """异步等待：await 让出循环，后台 pipeline task 才能推进。"""
    import asyncio
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d.state in want:
                return d.state
        await asyncio.sleep(0.05)
    raise AssertionError(f"deployment did not reach {want} (last={d.state})")


def _setup_repo(tmp_path, family_audio: bool = False) -> int:
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    seed_builtin_recipes()  # 注意：不与下面的 Session 嵌套（sqlite 单写锁）
    (tmp_path / "rclone.conf").write_text(
        "[gdrive]\ntype = drive\nclient_id = cid\ntoken = {\"refresh_token\":\"rt\"}\n")
    with Session(get_db_engine()) as s:
        fam = ModelFamily(name="Qwen3-8B", purpose="chat")
        s.add(fam)
        s.flush()
        s.add(ModelFile(family_id=fam.id, name="q3-q4.gguf",
                        remote_path="qwen3/q3-q4.gguf", bytes=1_000))
        rec = s.query(Recipe).filter(Recipe.name == "builtin/llamacpp-chat").one()
        dep = Deployment(recipe_id=rec.id, gpu="T4",
                         params={},
                         state="queued", state_history=[{"state": "queued"}], logs="")
        s.add(dep)
        s.commit()
        return dep.id


# ---------- 内置配方 ----------

def test_seed_idempotent(tmp_path):
    _setup_repo(tmp_path)
    with Session(get_db_engine()) as s:
        assert s.query(Recipe).count() == len(BUILTIN_RECIPES)
    seed_builtin_recipes()
    seed_builtin_recipes()
    with Session(get_db_engine()) as s:
        assert s.query(Recipe).count() == len(BUILTIN_RECIPES)  # 不重复插入
        llm = s.query(Recipe).filter(Recipe.name == "builtin/llamacpp-chat").one()
        assert "llama-server" in llm.run_script
        assert llm.manifest["models"]["chat"]["family"] == "$chat"


def test_seed_does_not_overwrite_user_edits(tmp_path):
    _setup_repo(tmp_path)
    with Session(get_db_engine()) as s:
        rec = s.query(Recipe).filter(Recipe.name == "builtin/llamacpp-chat").one()
        rec.description = "my tuned recipe"
        s.commit()
    seed_builtin_recipes()
    with Session(get_db_engine()) as s:
        rec = s.query(Recipe).filter(Recipe.name == "builtin/llamacpp-chat").one()
        assert rec.description == "my tuned recipe"


@pytest.mark.asyncio
async def test_builtin_pipeline_with_family_template(tmp_path):
    """内置配方 $chat 模板：params.families.chat 提供名字 → 全流水线 ready。"""
    dep_id = _setup_repo(tmp_path)
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        d.params = {"families": {"chat": "Qwen3-8B"}}
        s.commit()
    mock = MockColabBackend()
    Orchestrator(backend=mock).start(dep_id)
    assert await _wait_state_async(dep_id, {"ready", "failed"}) == "ready"


@pytest.mark.asyncio
async def test_builtin_pipeline_missing_family_param(tmp_path):
    """不给 families.chat → failed，且错误信息明确指出缺哪个参数。"""
    dep_id = _setup_repo(tmp_path)
    Orchestrator(backend=MockColabBackend()).start(dep_id)
    assert await _wait_state_async(dep_id, {"failed", "ready"}) == "failed"
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        assert "params.families.chat" in d.logs


# ---------- Chat API ----------

@pytest.fixture()
def chat_env(tmp_path, monkeypatch):
    """TestClient + 上游 mock。返回 (client, dep_id, calls)。"""
    dep_id = _setup_repo(tmp_path)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        if request.url.path.endswith("/v1/chat/completions"):
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                         "content": "Hello, human."}}],
                "usage": {"total_tokens": 7},
            })
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.api.chat._build_client",
                        lambda: httpx.AsyncClient(transport=transport))
    with TestClient(app) as c:
        # 登记本地端点（模拟隧道就绪）
        r = c.patch(f"/api/chat/deployments/{dep_id}/endpoint",
                    json={"endpoint": "http://127.0.0.1:18080"})
        assert r.status_code == 200
        yield c, dep_id, calls


def test_thread_crud_and_missing_endpoint(tmp_path):
    dep_id = _setup_repo(tmp_path)
    with TestClient(app) as c:
        t = c.post(f"/api/chat/deployments/{dep_id}/threads",
                    json={"deployment_id": dep_id, "title": "First"}).json()
        assert t["id"] >= 1

        # 端点未登记 → 发消息 409
        r = c.post(f"/api/chat/threads/{t['id']}/messages", json={"message": "hi"})
        assert r.status_code == 409

        assert c.get(f"/api/chat/deployments/{dep_id}/threads").json()[0]["title"] == "First"

        # 不存在的部署 → 404
        assert c.post("/api/chat/deployments/999/threads",
                      json={"deployment_id": 999}).status_code == 404


def test_send_message_plain(chat_env):
    c, dep_id, calls = chat_env
    t = c.post(f"/api/chat/deployments/{dep_id}/threads",
               json={"deployment_id": dep_id,
                     "params": {"system_prompt": "You are terse.",
                                "temperature": 0.3}}).json()
    r = c.post(f"/api/chat/threads/{t['id']}/messages", json={"message": "hello"})
    assert r.status_code == 200
    msg = r.json()
    assert msg["role"] == "assistant" and msg["content"] == "Hello, human."

    # 上游请求体：system prompt 在前、user 在后、温度透传、非流式
    sent = calls[0]["body"]
    assert sent["stream"] is False and sent["temperature"] == 0.3
    assert sent["messages"][0] == {"role": "system", "content": "You are terse."}
    assert sent["messages"][-1] == {"role": "user", "content": "hello"}

    # 两条消息均已入历史，线程详情可回放
    detail = c.get(f"/api/chat/threads/{t['id']}").json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]


def test_history_compose_with_limit(chat_env):
    c, dep_id, calls = chat_env
    t = c.post(f"/api/chat/deployments/{dep_id}/threads",
               json={"deployment_id": dep_id,
                     "params": {"history_limit": 2}}).json()
    for i in range(5):
        r = c.post(f"/api/chat/threads/{t['id']}/messages",
                   json={"message": f"m{i}"})
        assert r.status_code == 200
    # 上游收到：截断后只保留最近 2 条历史 + 当前 user（history_limit=2）
    sent = calls[-1]["body"]
    assert len(sent["messages"]) == 3
    assert sent["messages"][0] == {"role": "user", "content": "m3"}
    assert sent["messages"][-1] == {"role": "user", "content": "m4"}


def test_upstream_error_maps_502(tmp_path, monkeypatch):
    dep_id = _setup_repo(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr("app.api.chat._build_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(app) as c:
        c.patch(f"/api/chat/deployments/{dep_id}/endpoint",
                json={"endpoint": "http://127.0.0.1:18080"})
        t = c.post(f"/api/chat/deployments/{dep_id}/threads",
                   json={"deployment_id": dep_id}).json()
        r = c.post(f"/api/chat/threads/{t['id']}/messages", json={"message": "hi"})
        assert r.status_code == 502
        # user 消息已保存（可重试语境），assistant 未保存
        detail = c.get(f"/api/chat/threads/{t['id']}").json()
        assert [m["role"] for m in detail["messages"]] == ["user"]


def test_stream_passthrough_and_save(tmp_path, monkeypatch):
    dep_id = _setup_repo(tmp_path)
    sse = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
           b'data: {"choices":[{"delta":{"content":"lo!"}}]}\n\n'
           b'data: [DONE]\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("app.api.chat._build_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(app) as c:
        c.patch(f"/api/chat/deployments/{dep_id}/endpoint",
                json={"endpoint": "http://127.0.0.1:18080"})
        t = c.post(f"/api/chat/deployments/{dep_id}/threads",
                   json={"deployment_id": dep_id}).json()
        with c.stream("POST", f"/api/chat/threads/{t['id']}/messages/stream",
                      json={"message": "hi"}) as r:
            assert r.status_code == 200
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
        assert b'"Hel"' in body and b"[DONE]" in body  # SSE 原样透传
        # 聚合后保存
        detail = c.get(f"/api/chat/threads/{t['id']}").json()
        contents = {m["role"]: m["content"] for m in detail["messages"]}
        assert contents["user"] == "hi"
        assert contents["assistant"] == "Hello!"
