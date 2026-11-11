"""ComfyUI API 客户端（生成图片/视频的使用层）。

与 Chat 同构：本地 server 代理转发到部署端点（隧道），浏览器不直连 Colab。
- queue()/history()/view(): ComfyUI HTTP API 封装
- txt2img_workflow(): 内置文生图模板（标准 API 格式），开箱即用
- render_workflow(): 进阶模式——粘贴 API workflow JSON，变量占位替换
（视频生成用 WAN 等官方 workflow 的 API 格式导出后粘进来即可）
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

import httpx


class ComfyError(RuntimeError):
    pass


@dataclass
class GenRequest:
    prompt: str = ""
    negative: str = ""
    checkpoint: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 25
    seed: int = 0  # 0 → 随机
    template: str = "txt2img"  # txt2img | raw
    raw_workflow: str = ""  # template=raw 时的 API workflow JSON


def txt2img_workflow(ckpt: str, prompt: str, negative: str, width: int, height: int,
                     steps: int, seed: int) -> dict:
    """标准 SD/SDXL 文生图工作流（API 格式）。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": 7.0, "sampler_name": "euler",
            "scheduler": "normal", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {
            "text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {
            "samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "modeldock", "images": ["8", 0]}},
    }


_PLACEHOLDERS = ("prompt", "negative", "ckpt", "seed", "width", "height", "steps")


def render_workflow(req: GenRequest) -> dict:
    """按请求生成 workflow（内置模板或 raw+变量）。"""
    if req.template != "raw":
        if not req.checkpoint:
            raise ComfyError("checkpoint 未选择（先部署 comfyui 配方并装配 checkpoint 家族）")
        seed = req.seed or random.randint(0, 2**31)
        return txt2img_workflow(req.checkpoint, req.prompt, req.negative,
                                req.width, req.height, req.steps, seed)
    if not req.raw_workflow.strip():
        raise ComfyError("raw 模式需要粘贴 ComfyUI API 格式 workflow JSON")
    text = req.raw_workflow
    values = {
        "prompt": req.prompt, "negative": req.negative, "ckpt": req.checkpoint,
        "seed": str(req.seed or random.randint(0, 2**31)),
        "width": str(req.width), "height": str(req.height),
        "steps": str(req.steps),
    }

    def _json_safe(v) -> str:
        # 占位符约定位于 JSON 字符串内：替换值需按 JSON 字符串转义
        s = json.dumps(v, ensure_ascii=False)
        return s[1:-1] if s.startswith('"') else s

    for k in _PLACEHOLDERS:
        # lambda 替换避免 replacement 里 \\ 被 re 解释为分组引用
        text = re.sub(r"\{\{\s*" + k + r"\s*\}\}",
                      lambda m, v=_json_safe(values[k]): v, text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ComfyError(f"workflow JSON 解析失败: {e}") from e


class ComfyClient:
    def __init__(self, endpoint: str, client=None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._client = client

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))

    def checkpoints(self) -> list[str]:
        with self._http() as c:
            r = c.get(f"{self.endpoint}/object_info/CheckpointLoaderSimple")
        if r.status_code != 200:
            raise ComfyError(f"object_info {r.status_code}: {r.text[:200]}")
        info = r.json().get("CheckpointLoaderSimple", {})
        try:
            return info["input"]["required"]["ckpt_name"][0]
        except (KeyError, TypeError, IndexError) as e:
            raise ComfyError("ComfyUI 未返回 checkpoint 列表") from e

    def queue(self, workflow: dict) -> str:
        import uuid
        with self._http() as c:
            r = c.post(f"{self.endpoint}/prompt",
                       json={"prompt": workflow, "client_id": uuid.uuid4().hex})
        if r.status_code != 200:
            raise ComfyError(f"queue {r.status_code}: {r.text[:200]}")
        data = r.json()
        pid = data.get("prompt_id")
        if not pid:
            raise ComfyError("ComfyUI 未返回 prompt_id")
        return pid

    def history(self, prompt_id: str) -> dict:
        """→ {state: pending|done|error, outputs: [{filename, subfolder, type, kind}]}"""
        with self._http() as c:
            r = c.get(f"{self.endpoint}/history/{prompt_id}")
        if r.status_code != 200:
            raise ComfyError(f"history {r.status_code}")
        hist = r.json()
        entry = hist.get(prompt_id)
        if entry is None:
            return {"state": "pending", "outputs": []}  # 还在队列/执行中
        status = (entry.get("status") or {})
        if status.get("status_str") == "error":
            return {"state": "error", "outputs": [], "messages": status.get("messages", [])}
        outputs = []
        for node_out in (entry.get("outputs") or {}).values():
            for img in (node_out.get("images") or []) + (node_out.get("gifs") or []):
                if img.get("filename"):
                    outputs.append({
                        "filename": img["filename"],
                        "subfolder": img.get("subfolder") or "",
                        "type": img.get("type") or "output",
                        "kind": "video" if img in (node_out.get("gifs") or []) else "image",
                    })
        return {"state": "done", "outputs": outputs}

    def view(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        with self._http() as c:
            r = c.get(f"{self.endpoint}/view", params={
                "filename": filename, "subfolder": subfolder, "type": type_})
        if r.status_code != 200:
            raise ComfyError(f"view {r.status_code}")
        return r.content
