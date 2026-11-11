"""生成 API（图片/视频使用层，DESIGN.md §4.4 同级）。

模式与 Chat 相同：本地 server 代理 ComfyUI HTTP API（经隧道），
浏览器零直连。产物引用入 GeneratedAsset 表，可回看。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.chat import _endpoint_for
from app.core.comfy import ComfyClient, ComfyError, GenRequest, render_workflow
from app.db import Deployment, GeneratedAsset, get_db_engine
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/gen", tags=["gen"])


class GenSubmitIn(BaseModel):
    prompt: str = Field(default="", max_length=8192)
    negative: str = Field(default="", max_length=4096)
    checkpoint: str = Field(default="", max_length=512)
    width: int = Field(default=1024, ge=64, le=4096)
    height: int = Field(default=1024, ge=64, le=4096)
    steps: int = Field(default=25, ge=1, le=200)
    seed: int = Field(default=0, ge=0)
    template: str = Field(default="txt2img", pattern="^(txt2img|raw)$")
    raw_workflow: str = Field(default="", max_length=262144)


MediaType = {"image": "image/png", "video": "video/mp4"}
IMG_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
VID_EXTS = {".mp4": "video/mp4", ".webm": "video/webm", ".gif": "image/gif"}


def _kind_for(filename: str) -> str:
    lower = filename.lower()
    for ext in VID_EXTS:
        if lower.endswith(ext):
            return "video"
    return "image"


def _media_type(filename: str) -> str:
    lower = filename.lower()
    for exts in (VID_EXTS, IMG_EXTS):
        for ext, mime in exts.items():
            if lower.endswith(ext):
                return mime
    return "application/octet-stream"


def _client_for(dep_id: int) -> ComfyClient:
    return ComfyClient(_endpoint_for(dep_id))


# ---- 提交与轮询 ----

@router.post("/deployments/{dep_id}/submit")
def submit(dep_id: int, body: GenSubmitIn):
    """渲染 workflow → 提交到 ComfyUI，返回 prompt_id（前端轮询 history）。"""
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        if not d:
            raise HTTPException(404, "deployment not found")
    try:
        workflow = render_workflow(GenRequest(**body.model_dump()))
        client = _client_for(dep_id)
        prompt_id = client.queue(workflow)
    except ComfyError as e:
        raise HTTPException(422, str(e)) from e
    return {"prompt_id": prompt_id, "template": body.template}


@router.get("/deployments/{dep_id}/history/{prompt_id}")
def history(dep_id: int, prompt_id: str, prompt: str = ""):
    """轮询；done 时产物入库并返回资产列表。"""
    try:
        out = _client_for(dep_id).history(prompt_id)
    except ComfyError as e:
        raise HTTPException(502, str(e)) from e
    assets = []
    if out["state"] == "done":
        for f in out["outputs"]:
            with Session(get_db_engine()) as s:
                a = GeneratedAsset(
                    deployment_id=dep_id,
                    kind=_kind_for(f["filename"]),
                    filename=f["filename"], subfolder=f.get("subfolder", ""),
                    prompt=prompt[:4096],
                    meta={"prompt_id": prompt_id, "type": f.get("type", "output")},
                )
                s.add(a)
                s.commit()
                s.refresh(a)
            assets.append({"id": a.id, "kind": a.kind, "filename": a.filename})
        return {"state": "done", "assets": assets}
    return {"state": out["state"], "assets": [],
            "messages": out.get("messages", [])}


# ---- 资产 ----

@router.get("/deployments/{dep_id}/assets")
def list_assets(dep_id: int, limit: int = 60):
    with Session(get_db_engine()) as s:
        rows = s.scalars(select(GeneratedAsset)
                         .where(GeneratedAsset.deployment_id == dep_id)
                         .order_by(GeneratedAsset.id.desc()).limit(limit)).all()
        return [{"id": r.id, "kind": r.kind, "filename": r.filename,
                 "prompt": r.prompt, "created_at": r.created_at} for r in rows]


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: int):
    with Session(get_db_engine()) as s:
        a = s.get(GeneratedAsset, asset_id)
        if not a:
            raise HTTPException(404, "asset not found")
        s.delete(a)
        s.commit()


@router.get("/assets/{asset_id}/file")
def asset_file(asset_id: int):
    """取回产物字节（<img>/<video> src 直用；经 server 代理，浏览器不直连）。"""
    with Session(get_db_engine()) as s:
        a = s.get(GeneratedAsset, asset_id)
        if not a:
            raise HTTPException(404, "asset not found")
        meta = a.meta or {}
    try:
        data = _client_for(a.deployment_id).view(
            a.filename, a.subfolder, meta.get("type", "output"))
    except ComfyError as e:
        raise HTTPException(502, str(e)) from e
    media = _media_type(a.filename)
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "private, max-age=3600"})


# ---- 辅助 ----

@router.get("/deployments/{dep_id}/checkpoints")
def checkpoints(dep_id: int):
    """checkpoint 下拉数据源（ComfyUI object_info）。"""
    try:
        return {"checkpoints": _client_for(dep_id).checkpoints()}
    except ComfyError as e:
        raise HTTPException(502, str(e)) from e
