"""生成链路测试：workflow 模板渲染 + ComfyUI 代理 API + 产物入库。

ComfyClient 用 httpx.MockTransport 注入，不触网络。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.gen import _media_type
from app.core.comfy import (ComfyClient, ComfyError, GenRequest,
                            render_workflow, txt2img_workflow)
from app.core.colab.mock import MockColabBackend
from app.core.colab.orchestrator import Orchestrator, set_orchestrator
from app.db import Deployment, GeneratedAsset, Recipe, get_db_engine, init_db, set_db_path
from app.main import app


# ---------- workflow 渲染 ----------

def test_txt2img_workflow_shape():
    wf = txt2img_workflow("flux.safetensors", "a cat", "ugly", 512, 512, 20, 42)
    assert wf["4"]["inputs"]["ckpt_name"] == "flux.safetensors"
    assert wf["6"]["inputs"]["text"] == "a cat"
    assert wf["3"]["inputs"]["seed"] == 42
    assert wf["9"]["class_type"] == "SaveImage"


def test_builtin_template_requires_checkpoint():
    with pytest.raises(ComfyError, match="checkpoint"):
        render_workflow(GenRequest(prompt="x", template="txt2img", checkpoint=""))


def test_raw_workflow_variables():
    raw = json.dumps({
        "1": {"class_type": "KSampler",
              # 裸值占位符（不带引号）→ JSON 数字
              "inputs": {"seed": "{{seed}}", "steps": "{{steps}}"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
    }).replace('"seed": "{{seed}}"', '"seed": {{seed}}').replace(
        '"steps": "{{steps}}"', '"steps": {{steps}}')
    out = render_workflow(GenRequest(prompt="a \\g<1> cat", template="raw", raw_workflow=raw))
    assert isinstance(out["1"]["inputs"]["seed"], int)  # 裸占位符 → JSON 数字
    assert out["2"]["inputs"]["text"] == "a \\g<1> cat"  # 反斜杠按字面替换
    assert out["1"]["inputs"]["steps"] == 25


def test_raw_workflow_bad_json():
    with pytest.raises(ComfyError, match="解析失败"):
        render_workflow(GenRequest(template="raw", raw_workflow="{oops"))


def test_media_type_helper():
    assert _media_type("a.png") == "image/png"
    assert _media_type("clip.mp4") == "video/mp4"
    assert _media_type("out.webm") == "video/webm"


# ---------- API（代理 ComfyUI） ----------

def _setup(tmp_path) -> int:
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    with Session(get_db_engine()) as s:
        rec = Recipe(name="comfy", kind_class="comfyui", manifest={"models": {}}, ports=[8188])
        s.add(rec)
        s.flush()
        dep = Deployment(recipe_id=rec.id, gpu="T4", state="ready",
                         params={"session": {"id": "s1", "gpu": "T4"},
                                 "endpoint": "http://127.0.0.1:18181"},
                         state_history=[], logs="")
        s.add(dep)
        s.commit()
        return dep.id


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dep_id = _setup(tmp_path)
    set_orchestrator(Orchestrator(backend=MockColabBackend()))

    state = {"finished": False}
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            assert "prompt" in json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": "pid-1"})
        if path == "/history/pid-1":
            if state["finished"]:
                return httpx.Response(200, json={
                    "pid-1": {"status": {"status_str": "success"},
                              "outputs": {"9": {"images": [{
                                  "filename": "modeldock_00001_.png",
                                  "subfolder": "", "type": "output"}]}}}})
            return httpx.Response(200, json={})
        if path == "/view":
            assert request.url.params["filename"] == "modeldock_00001_.png"
            return httpx.Response(200, content=b"\x89PNG-fake-bytes",
                                  headers={"content-type": "image/png"})
        if path == "/object_info/CheckpointLoaderSimple":
            return httpx.Response(200, json={
                "CheckpointLoaderSimple": {"input": {"required": {
                    "ckpt_name": [["sd_xl.safetensors", "flux1.safetensors"]]}}}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    class _C(ComfyClient):
        def __init__(self, endpoint, client=None):
            super().__init__(endpoint, httpx.Client(transport=transport))

    monkeypatch.setattr("app.api.gen.ComfyClient", _C)
    with TestClient(app) as c:
        yield {"c": c, "dep_id": dep_id, "state": state}


def test_gen_full_flow(client):
    dep_id = client["dep_id"]
    c = client["c"]
    # checkpoints
    ck = c.get(f"/api/gen/deployments/{dep_id}/checkpoints").json()
    assert ck["checkpoints"] == ["sd_xl.safetensors", "flux1.safetensors"]

    # 提交
    sub = c.post(f"/api/gen/deployments/{dep_id}/submit", json={
        "prompt": "a cat on mars", "negative": "blurry",
        "checkpoint": "sd_xl.safetensors", "width": 512, "height": 512}).json()
    assert sub["prompt_id"] == "pid-1"

    # 轮询：pending → done（产物入库）
    h1 = c.get(f"/api/gen/deployments/{dep_id}/history/pid-1").json()
    assert h1["state"] == "pending"
    client["state"]["finished"] = True
    h2 = c.get(f"/api/gen/deployments/{dep_id}/history/pid-1?prompt=a cat on mars").json()
    assert h2["state"] == "done" and len(h2["assets"]) == 1
    asset_id = h2["assets"][0]["id"]

    # 列表
    assets = c.get(f"/api/gen/deployments/{dep_id}/assets").json()
    assert assets[0]["kind"] == "image"
    assert assets[0]["prompt"] == "a cat on mars"

    # 文件取回（<img> src 直用）
    f = c.get(f"/api/gen/assets/{asset_id}/file")
    assert f.status_code == 200
    assert f.headers["content-type"] == "image/png"
    assert f.content == b"\x89PNG-fake-bytes"

    # 删除
    assert c.delete(f"/api/gen/assets/{asset_id}").status_code == 204
    assert c.get(f"/api/gen/deployments/{dep_id}/assets").json() == []


def test_gen_submit_validation(client):
    dep_id = client["dep_id"]
    c = client["c"]
    r = c.post(f"/api/gen/deployments/{dep_id}/submit",
               json={"prompt": "x", "checkpoint": "", "template": "txt2img"})
    assert r.status_code == 422  # 缺 checkpoint

    miss = c.post("/api/gen/deployments/999/submit", json={"prompt": "x"})
    assert miss.status_code == 404
