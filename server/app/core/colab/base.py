"""Colab 会话后端抽象（DESIGN.md §4.3 / §6）。

接口层抽象 + 版本化：真实通道是 `google-colab-cli`（本机 ~/.local/bin/colab），
测试与 dry-run 用 MockColabBackend。Colab 非公开 API 变动时只改 cli.py 一个文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# GPU 请求优先级链：请求型号不可用时按序降级（DESIGN.md §4.3 第 1 步）
GPU_FALLBACK: dict[str, list[str]] = {
    "A100": ["A100", "L4", "T4"],
    "H100": ["H100", "A100", "L4", "T4"],
    "G4": ["G4", "L4", "T4"],
    "L4": ["L4", "T4"],
    "T4": ["T4"],
}
VALID_GPUS = tuple(GPU_FALLBACK)


class ColabError(RuntimeError):
    """后端失败（创建失败/执行失败/凭证失效等）。"""


@dataclass
class SessionInfo:
    session_id: str
    gpu: str
    created: bool = True
    meta: dict = field(default_factory=dict)


@dataclass
class ExecResult:
    ok: bool
    stdout: str = ""
    detail: str = ""


class ColabBackend:
    """最小接口：编排器只依赖这五个方法。"""

    name = "abstract"

    def create_session(self, gpu: str, session_name: str | None = None) -> SessionInfo:
        raise NotImplementedError

    def exec_script(self, session_id: str, script: str, timeout: float = 120.0) -> ExecResult:
        """在会话里执行一段 bash 脚本（写临时文件上传执行）。成功标准 rc==0。"""
        raise NotImplementedError

    def upload_text(self, session_id: str, remote_path: str, content: str) -> None:
        """小文件上传（临时 rclone 凭证注入用）。"""
        raise NotImplementedError

    def stop_session(self, session_id: str) -> None:
        raise NotImplementedError

    def usage(self) -> dict:
        """compute units 余额（后端不支持时返回空 dict）。"""
        return {}
