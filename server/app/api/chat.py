"""Chat 面板 API（DESIGN.md §4.4）。

- 线程与消息历史存本地 DB；消息经部署的 OpenAI 兼容端点
  （llama.cpp llama-server /v1/chat/completions）转发，流量直达部署本机隧道
- 部署 ready 后 PATCH /api/chat/deployments/{id}/endpoint 登记本地转发地址
- 非流式：POST /threads/{id}/messages；流式：POST /threads/{id}/messages/stream
  （SSE 透传，聚合完整后保存）
"""
from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import ChatMessage, ChatThread, Deployment, get_db_engine
from app.schemas import (ChatEndpointIn, ChatMessageIn, ChatMessageOut,
                         ChatThreadDetail, ChatThreadIn, ChatThreadOut)

router = APIRouter(prefix="/api/chat", tags=["chat"])

log = logging.getLogger("modeldock.chat")

DEFAULT_HISTORY_LIMIT = 20
GEN_KEYS = ("temperature", "top_p", "max_tokens")


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))


# ---- threads ----

@router.get("/deployments/{dep_id}/threads", response_model=list[ChatThreadOut])
def list_threads(dep_id: int):
    with Session(get_db_engine()) as s:
        return s.scalars(select(ChatThread).where(ChatThread.deployment_id == dep_id)
                         .order_by(ChatThread.id.desc())).all()


@router.post("/deployments/{dep_id}/threads", response_model=ChatThreadOut, status_code=201)
def create_thread(dep_id: int, body: ChatThreadIn):
    with Session(get_db_engine()) as s:
        if not s.get(Deployment, dep_id):
            raise HTTPException(404, "deployment not found")
        t = ChatThread(deployment_id=dep_id, title=body.title, params=body.params)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


@router.get("/threads/{thread_id}", response_model=ChatThreadDetail)
def get_thread(thread_id: int):
    with Session(get_db_engine()) as s:
        t = s.get(ChatThread, thread_id)
        if not t:
            raise HTTPException(404, "thread not found")
        out = ChatThreadDetail.model_validate(t)
        out.messages = [ChatMessageOut.model_validate(m) for m in t.messages]
        return out


@router.delete("/threads/{thread_id}", status_code=204)
def delete_thread(thread_id: int):
    with Session(get_db_engine()) as s:
        t = s.get(ChatThread, thread_id)
        if not t:
            raise HTTPException(404, "thread not found")
        s.delete(t)
        s.commit()


# ---- endpoint 登记（隧道就绪后指向本地转发端口） ----

@router.patch("/deployments/{dep_id}/endpoint")
def set_endpoint(dep_id: int, body: ChatEndpointIn):
    """登记本地转发端点（聊天/生成统一键 endpoint；开隧道时自动登记）。"""
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        if not d:
            raise HTTPException(404, "deployment not found")
        params = dict(d.params or {})
        params["endpoint"] = body.endpoint.rstrip("/")
        params.pop("chat_endpoint", None)  # 旧键迁移
        d.params = params
        s.commit()
    return {"ok": True, "deployment_id": dep_id, "endpoint": params["endpoint"]}


# ---- 发消息 ----

def _endpoint_for(dep_id: int) -> str:
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        if not d:
            raise HTTPException(404, "deployment not found")
        params = d.params or {}
        endpoint = params.get("endpoint") or params.get("chat_endpoint")
    if not endpoint:
        raise HTTPException(409, f"endpoint not set for deployment {dep_id}; "
                                 "establish tunnel then POST "
                                 f"/api/deployments/{dep_id}/tunnel/start")
    return endpoint


def _prepare(thread_id: int, body: ChatMessageIn, stream: bool) -> tuple[str, dict]:
    """组装 OpenAI 兼容请求体（thread 参数 + 一次性覆盖 + 截断历史）。"""
    with Session(get_db_engine()) as s:
        t = s.get(ChatThread, thread_id)
        if not t:
            raise HTTPException(404, "thread not found")
        endpoint = _endpoint_for(t.deployment_id)
        history = [(m.role, m.content) for m in
                    s.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread_id)
                              .order_by(ChatMessage.id)).all()]
        merged = {**(t.params or {}), **(body.params or {})}
    limit = int(merged.get("history_limit", DEFAULT_HISTORY_LIMIT))
    messages: list[dict] = []
    if merged.get("system_prompt"):
        messages.append({"role": "system", "content": str(merged["system_prompt"])})
    for role, content in history[-limit:]:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": body.message})
    req = {"messages": messages, "stream": stream}
    for k in GEN_KEYS:
        if k in merged:
            req[k] = merged[k]
    return endpoint, req


def _save_msg(thread_id: int, role: str, content: str, meta: dict | None = None) -> ChatMessage:
    with Session(get_db_engine()) as s:
        m = ChatMessage(thread_id=thread_id, role=role, content=content, meta=meta or {})
        s.add(m)
        s.commit()
        s.refresh(m)
        return m


@router.post("/threads/{thread_id}/messages", response_model=ChatMessageOut)
async def send_message(thread_id: int, body: ChatMessageIn):
    """非流式：返回完整回复；user/assistant 均已入历史。"""
    endpoint, req = _prepare(thread_id, body, stream=False)
    _save_msg(thread_id, "user", body.message)
    try:
        async with _build_client() as client:
            r = await client.post(f"{endpoint}/v1/chat/completions", json=req)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"chat upstream unreachable: {e}") from e
    if r.status_code != 200:
        raise HTTPException(502, f"chat upstream {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(502, f"unexpected upstream response: {str(data)[:300]}") from e
    return _save_msg(thread_id, "assistant", content, meta={"usage": data.get("usage", {})})


@router.post("/threads/{thread_id}/messages/stream")
async def send_message_stream(thread_id: int, body: ChatMessageIn):
    """流式：SSE 透传上游，聚合 delta，结束后保存完整 assistant 回复。"""
    endpoint, req = _prepare(thread_id, body, stream=True)
    _save_msg(thread_id, "user", body.message)

    async def gen():
        chunks: list[str] = []
        try:
            async with _build_client() as client:
                async with client.stream("POST", f"{endpoint}/v1/chat/completions",
                                         json=req) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode(errors="replace")[:300]
                        yield f'data: {json.dumps({"error": f"upstream {r.status_code}: {detail}"})}\n\n'
                    else:
                        async for line in r.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            yield f"data: {data}\n\n"
                            if data.strip() == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            for ch in obj.get("choices", []):
                                delta = ch.get("delta", {}).get("content") or ""
                                if delta:
                                    chunks.append(delta)
        except httpx.HTTPError as e:
            yield f'data: {json.dumps({"error": f"upstream unreachable: {e}"})}\n\n'
        finally:
            if chunks:
                _save_msg(thread_id, "assistant", "".join(chunks), meta={"stream": True})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
