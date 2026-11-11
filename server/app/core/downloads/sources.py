"""模型源适配器：把「来源描述」解析为可下载直链。

- hf:        https://huggingface.co/{repo}/resolve/{rev}/{filename}（gated 仓库需 token）
- modelscope:https://modelscope.cn/models/{repo}/resolve/master/{filename}
- url:       任意直链

适配器只做「解析 + 探测」；下载由 engine 的 httpx 流式执行。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings


class SourceError(RuntimeError):
    pass


@dataclass
class ResolvedFile:
    url: str
    total_bytes: int
    etag: str | None
    headers: dict[str, str]  # 需要透传给下载器的头（如 Authorization）


class BaseSource:
    name: str = "base"

    def resolve(self, repo: str, filename: str, token: str | None = None, revision: str = "main") -> ResolvedFile:
        raise NotImplementedError


class HuggingFaceSource(BaseSource):
    name = "hf"

    def resolve(self, repo: str, filename: str, token: str | None = None, revision: str = "main") -> ResolvedFile:
        base = settings.hf_endpoint.rstrip("/")
        url = f"{base}/{repo}/resolve/{revision}/{filename}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # HEAD 探测：确认存在 + 拿 size/etag（HF 302 到 CDN，follow redirects）
        with httpx.Client(follow_redirects=True, timeout=20) as c:
            r = c.head(url, headers=headers)
            if r.status_code == 401 or r.status_code == 403:
                raise SourceError("hf: 访问被拒绝（gated 仓库需先在 HF 站内同意协议，并使用有效 token）")
            if r.status_code == 404:
                raise SourceError("hf: 文件不存在")
            r.raise_for_status()
        return ResolvedFile(
            url=url,
            total_bytes=int(r.headers.get("content-length", 0) or 0) or _fallback_size(r, url, headers),
            etag=r.headers.get("etag"),
            headers=headers,
        )


class ModelScopeSource(BaseSource):
    name = "modelscope"

    def resolve(self, repo: str, filename: str, token: str | None = None, revision: str = "master") -> ResolvedFile:
        base = settings.modelscope_endpoint.rstrip("/")
        url = f"{base}/models/{repo}/resolve/{revision}/{filename}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(follow_redirects=True, timeout=20) as c:
            r = c.head(url, headers=headers)
            if r.status_code == 404:
                raise SourceError("modelscope: 文件不存在")
            r.raise_for_status()
        return ResolvedFile(
            url=url,
            total_bytes=int(r.headers.get("content-length", 0) or 0),
            etag=r.headers.get("etag"),
            headers=headers,
        )


SOURCES: dict[str, BaseSource] = {
    HuggingFaceSource.name: HuggingFaceSource(),
    ModelScopeSource.name: ModelScopeSource(),
}


def resolve_any(kind: str, repo: str, filename: str, token: str | None = None) -> ResolvedFile:
    src = SOURCES.get(kind)
    if not src:
        raise SourceError(f"unknown source kind: {kind}")
    return src.resolve(repo, filename, token)


def _fallback_size(r: httpx.Response, url: str, headers: dict[str, str]) -> int:
    """HEAD 无 content-length 时退化为 GET 0 字节探测。"""
    with httpx.Client(follow_redirects=True, timeout=20) as c:
        with c.stream("GET", url, headers={**headers, "Range": "bytes=0-0"}) as s:
            cr = s.headers.get("content-range", "")
            if "/" in cr:
                return int(cr.rsplit("/", 1)[1])
    return 0
