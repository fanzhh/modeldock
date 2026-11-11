"""下载引擎测试：模块级（不真正开 rclone 拉远端）。

- sources：用假 httpx transport 测 resolve/槽位
- engine：用本地 http server 流式下载断点续传
"""
from __future__ import annotations

import httpx
import pytest

from app.core.downloads.sources import HuggingFaceSource, ModelScopeSource, SourceError


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _patch_client(monkeypatch, responses):
    """让 resolve 里的 httpx.Client.head 走假 responses[repo]。"""
    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def head(self, url, headers=None):
            class R:
                status_code = responses["status"]
                headers = responses["headers"]
                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise httpx.HTTPStatusError("err", request=None, response=None)
            return R()
    monkeypatch.setattr("app.core.downloads.sources.httpx.Client", FakeClient)


def test_hf_resolve_ok(monkeypatch):
    _patch_client(monkeypatch, {"status": 200, "headers": {"content-length": "1234", "etag": "W/\"a\""}})
    r = HuggingFaceSource().resolve("Qwen/Qwen3-8B", "q3.gguf", token="tok")
    assert r.total_bytes == 1234
    assert r.headers["Authorization"] == "Bearer tok"
    assert "/Qwen/Qwen3-8B/resolve/main/q3.gguf" in r.url


def test_hf_gated_rejected(monkeypatch):
    _patch_client(monkeypatch, {"status": 401, "headers": {}})
    with pytest.raises(SourceError, match="gated"):
        HuggingFaceSource().resolve("meta-llama/Llama-3-8B", "model.safetensors", token="bad")


def test_hf_404(monkeypatch):
    _patch_client(monkeypatch, {"status": 404, "headers": {}})
    with pytest.raises(SourceError, match="不存在"):
        HuggingFaceSource().resolve("a/b", "nope.gguf")


def test_modelscope_url_shape(monkeypatch):
    _patch_client(monkeypatch, {"status": 200, "headers": {"content-length": "10"}})
    r = ModelScopeSource().resolve("Qwen/Qwen3-8B-GGUF", "q3-q4.gguf")
    assert "modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/q3-q4.gguf" in r.url


def test_unknown_source():
    from app.core.downloads.sources import resolve_any
    with pytest.raises(SourceError, match="unknown source"):
        resolve_any("baidu", "a/b", "f")


# ---- engine: 断点续传 ----
@pytest.mark.asyncio
async def test_stream_resume(tmp_path, monkeypatch):
    import asyncio

    from app.core.downloads.engine import DownloadEngine, Job
    from app.core.downloads.sources import ResolvedFile

    payload = b"x" * 300_000
    local = tmp_path / "part.bin"

    # 假造已完成 100 字节的本地文件 → 请求带 Range
    local.write_bytes(payload[:100])

    captured_headers = {}

    class FakeStream:
        def __init__(self, headers):
            self.status_code = 206
            self.headers = {"content-range": f"bytes 100-{len(payload)-1}/{len(payload)}"}
            captured_headers.update(headers)
        def raise_for_status(self):
            return None
        async def aiter_bytes(self, chunk):
            view = memoryview(payload)[100:]
            for i in range(0, len(view), chunk):
                yield bytes(view[i:i + chunk])
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, headers=None):
            return FakeStream(headers or {})

    monkeypatch.setattr("app.core.downloads.engine.httpx.AsyncClient", FakeAsyncClient)

    eng = DownloadEngine(vault_getter=lambda k: None, drive=None)
    job = Job(task_id=1, kind="url", repo="", filename="f", token_fingerprint="",
              target_remote_rel="x")
    resolved = ResolvedFile(url="http://x/f", total_bytes=len(payload), etag=None, headers={})
    await eng._stream_to(local, resolved, job)

    assert local.read_bytes() == payload  # 续传拼合后完整
    assert "Range" in captured_headers and captured_headers["Range"].startswith("bytes=100-")
    assert job.done_bytes == len(payload)
