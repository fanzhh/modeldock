"""真实后端：`google-colab-cli`（uv tool，二进制通常为 ~/.local/bin/colab）。

命令形状（0.7.5 实测 --help）：
  colab new -s NAME --gpu T4|L4|G4|H100|A100 [--high-mem]
  colab exec -s NAME -f FILE [--timeout SEC]
  colab upload -s NAME LOCAL REMOTE
  colab stop -s NAME
  colab usage

安全：凭证由 colab CLI 自己的 oauth config（~/.colab-cli-oauth-config…）管理，
本适配器不读取、不复制任何凭证；执行脚本经临时文件传递，凭证类内容不落命令行
（避免出现在 ps/日志）。subclassing point：Colab 非公开 API 变动只改这里。
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from app.core.colab.base import ColabBackend, ColabError, ExecResult, SessionInfo

log = logging.getLogger("modeldock.colab")

_TIMEOUT_DEFAULT = 120.0


def _find_colab_bin() -> str:
    """优先 server venv 内置 CLI（randomdep google-colab-cli），其次独立安装。"""
    import sys
    for cand in (os.environ.get("MODELDOCK_COLAB_BIN", ""),
                 os.path.join(sys.prefix, "bin", "colab"),
                 os.path.expanduser("~/.local/bin/colab"),
                 "colab"):
        if cand and (cand == "colab" or os.path.exists(cand)):
            return cand
    raise ColabError("colab CLI not found")


class ColabCliBackend(ColabBackend):
    name = "cli"

    def __init__(self, colab_bin: str | None = None):
        self.bin = colab_bin or _find_colab_bin()

    def _run(self, *args: str, timeout: float = 300.0) -> tuple[int, str]:
        cmd = [self.bin, *args]
        # colab CLI 会打印更新提示，无害；失败时只截断 400 字符入异常/日志
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise ColabError(f"colab binary missing: {self.bin}") from e
        except subprocess.TimeoutExpired as e:
            raise ColabError(f"colab command timed out: {args[0]}") from e
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out

    def create_session(self, gpu: str, session_name: str | None = None) -> SessionInfo:
        sid = session_name or f"modeldock-{os.urandom(4).hex()}"
        rc, out = self._run("new", "-s", sid, "--gpu", gpu, timeout=600)
        if rc != 0:
            raise ColabError(f"colab new failed: {out[-400:]}")
        return SessionInfo(session_id=sid, gpu=gpu)

    def exec_script(self, session_id: str, script: str, timeout: float = _TIMEOUT_DEFAULT) -> ExecResult:
        """`colab exec` 执行的是 Python 文件；这里把 bash 包成 python 子进程执行，
        结尾打印 MDX_RC:<rc> 哨兵，按哨兵判定成败（不依赖 CLI 语义）。"""
        py = (
            "import subprocess, sys\n"
            f"BASH = {script!r}\n"
            "r = subprocess.run(['bash', '-c', BASH], capture_output=True, text=True)\n"
            "sys.stdout.write(r.stdout or '')\n"
            "sys.stdout.write(r.stderr or '')\n"
            "print(f'MDX_RC:{r.returncode}')\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(py)
            path = f.name
        try:
            rc, out = self._run("exec", "-s", session_id, "-f", path,
                                "--timeout", str(max(timeout, 30.0)), timeout=timeout + 60)
        finally:
            os.unlink(path)
        if rc != 0:
            return ExecResult(ok=False, stdout=out, detail=f"colab exec rc={rc}: {out[-300:]}")
        marker_pos = out.rfind("MDX_RC:")
        if marker_pos < 0:
            return ExecResult(ok=False, stdout=out, detail="no MDX_RC marker in output")
        try:
            exit_rc = int(out[marker_pos + len("MDX_RC:"):].strip().splitlines()[0])
        except (ValueError, IndexError):
            return ExecResult(ok=False, stdout=out, detail="bad MDX_RC marker")
        stdout = out[:marker_pos]
        return ExecResult(ok=exit_rc == 0, stdout=stdout, detail="" if exit_rc == 0 else stdout[-300:])

    def upload_text(self, session_id: str, remote_path: str, content: str) -> None:
        # 小文件走 upload；凭证类内容不进命令行参数
        with tempfile.NamedTemporaryFile("w", suffix=".tmp", delete=False) as f:
            f.write(content)
            f.flush()
            tmp = f.name
            try:
                rc, out = self._run("upload", "-s", session_id, tmp, remote_path)
                if rc != 0:
                    raise ColabError(f"colab upload failed: {out[-400:]}")
            finally:
                os.unlink(tmp)

    def stop_session(self, session_id: str) -> None:
        rc, out = self._run("stop", "-s", session_id, timeout=120)
        if rc != 0:
            log.warning("colab stop rc=%s (%s)", rc, out[-200:])

    def usage(self) -> dict:
        rc, out = self._run("usage", timeout=60)
        if rc != 0:
            return {"error": out[-200:]}
        return {"compute_units": re.sub(r"\s+", " ", out).strip()[-200:]}


def backend_from_env() -> ColabBackend:
    """MODELDOCK_COLAB_BACKEND=cli|mock（默认 mock：未配凭证时 API 可用但不真开会话）。"""
    kind = os.environ.get("MODELDOCK_COLAB_BACKEND", "mock").lower()
    if kind == "cli":
        return ColabCliBackend()
    from app.core.colab.mock import MockColabBackend
    return MockColabBackend()
