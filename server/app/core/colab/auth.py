"""Colab CLI 网页登录（无头 OAuth 转接）。

流程：浏览器在网页上点「登录」→ 后端起 `colab usage` 子进程捕获其打印的
授权 URL → 用户浏览器完成 Google 授权 → 把回调码粘回网页 → 后端写入子进程
stdin → CLI 存储 token（会话文件/钥匙串）→ 返回余额输出验证。

不解析、不截存任何凭证；授权码只经内存转交 CLI 进程。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

AUTH_URL_RE = re.compile(r"(https://accounts\.google\.com/o/oauth2/auth\S+)")


class ColabAuthError(RuntimeError):
    pass


class ColabAuthSession:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.output = ""
        self.url: str | None = None
        self.finished_output: str | None = None  # 无 URL 直接结束（已登录）
        self._stop = threading.Event()

    def pump(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            self.output += line
            m = AUTH_URL_RE.search(line)
            if m and not self.url:
                self.url = m.group(1).strip()
        rc = self.proc.wait()
        if not self.url:
            self.finished_output = f"rc={rc}\n{self.output}"


class ColabAuthManager:
    """模块内单例；同一时间只允许一次登录会话。"""

    def __init__(self, colab_bin: str | None = None) -> None:
        self.bin = colab_bin or _find_colab()
        self._session: ColabAuthSession | None = None
        self._lock = threading.Lock()

    @property
    def session(self) -> ColabAuthSession | None:
        return self._session

    def start(self) -> dict:
        """返回 {auth_required, url?, usage?}。已登录时 colab usage 直接输出余额。"""
        with self._lock:
            if self._session and self._session.proc.poll() is None:
                raise ColabAuthError("登录已在进行中；先提交授权码或等待超时")
            proc = subprocess.Popen(
                [self.bin, "usage"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )
            s = ColabAuthSession(proc)
            self._session = s
        threading.Thread(target=s.pump, daemon=True).start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if s.url:
                return {"auth_required": True, "url": s.url}
            if s.proc.poll() is not None:
                return {"auth_required": False, "usage": (s.finished_output or s.output)[-600:]}
            time.sleep(0.2)
        raise ColabAuthError("20s 内未捕获授权 URL/余额输出；检查 colab CLI 与网络")

    def submit(self, code: str) -> dict:
        s = self._session
        if not s or s.proc.poll() is not None:
            raise ColabAuthError("没有进行中的登录会话；先点「开始登录」")
        try:
            s.proc.stdin.write(code.strip() + "\n")  # type: ignore[union-attr]
            s.proc.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, ValueError) as e:
            raise ColabAuthError(f"CLI 进程已退出：{e}") from e
        deadline = time.time() + 30
        while time.time() < deadline and s.proc.poll() is None:
            time.sleep(0.3)
        rc = s.proc.poll()
        if rc is None:
            s.proc.kill()
            raise ColabAuthError("提交授权码后 30s 未完成；可能码无效或网络失败")
        ok = rc == 0 and "Error" not in s.output and "error" not in s.output
        return {"ok": ok, "rc": rc, "output": s.output[-600:]}


def _find_colab() -> str:
    for c in (os.environ.get("MODELDOCK_COLAB_BIN", ""),
              os.path.expanduser("~/.local/bin/colab"),
              shutil.which("colab") or ""):
        if c and os.path.exists(c):
            return c
    raise ColabAuthError("colab CLI 未安装：uv tool install google-colab-cli")


_manager: ColabAuthManager | None = None


def get_auth_manager() -> ColabAuthManager:
    global _manager
    if _manager is None:
        _manager = ColabAuthManager()
    return _manager


def set_auth_manager(m: ColabAuthManager) -> None:
    global _manager
    _manager = m
