"""Mock Colab 后端：测试与 dry-run。不触真实网络 / CLI。

可注入故障：fail_on={"create"|"exec"|"sync_step"}，模拟 Colab API 变动等场景。
模拟磁盘：exec 里若脚本含 `rclone copy`，记录到 .copies（供测试断言装配步骤）。
"""
from __future__ import annotations

from app.core.colab.base import ColabBackend, ColabError, ExecResult, SessionInfo


class MockColabBackend(ColabBackend):
    name = "mock"

    def __init__(self, fail_on: set[str] | None = None, created_gpu: str | None = None):
        self.fail_on = set(fail_on or ())
        self.created_gpu = created_gpu  # 实际可分配的 GPU（测降级链）
        self.sessions: dict[str, SessionInfo] = {}
        self.scripts: list[tuple[str, str]] = []   # (session_id, script)
        self.uploads: list[tuple[str, str, str]] = []  # (session_id, path, content)
        self.stopped: list[str] = []
        self.steps = 0

    # ---- ColabBackend ----
    def create_session(self, gpu: str, session_name: str | None = None) -> SessionInfo:
        if "create" in self.fail_on:
            raise ColabError("mock: session create failed")
        self.steps += 1
        sid = session_name or f"mock-{self.steps}"
        info = SessionInfo(session_id=sid, gpu=self.created_gpu or gpu)
        self.sessions[sid] = info
        return info

    def exec_script(self, session_id: str, script: str, timeout: float = 120.0) -> ExecResult:
        if session_id not in self.sessions:
            return ExecResult(ok=False, detail=f"mock: no session {session_id}")
        if "sync_step" in self.fail_on and "rclone copy" in script:
            return ExecResult(ok=False, stdout="", detail="mock: rclone copy failed")
        if "exec" in self.fail_on:
            return ExecResult(ok=False, stdout="", detail="mock: exec failed")
        self.scripts.append((session_id, script))
        return ExecResult(ok=True, stdout="mock ok\n")

    def upload_text(self, session_id: str, remote_path: str, content: str) -> None:
        if session_id not in self.sessions:
            raise ColabError(f"mock: no session {session_id}")
        self.uploads.append((session_id, remote_path, content))

    def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)
        self.sessions.pop(session_id, None)

    def usage(self) -> dict:
        return {"compute_units": "42.0 (mock)"}

    # ---- 测试辅助 ----
    def colab_copies(self) -> list[str]:
        """所有执行过的 rclone copy 目标行。"""
        out = []
        for _, script in self.scripts:
            for line in script.splitlines():
                if line.strip().startswith("rclone copy"):
                    out.append(line.strip())
        return out
