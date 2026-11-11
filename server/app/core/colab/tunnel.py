"""SSH 隧道管理器：把部署就绪的服务端口经 colab CLI 隧道映射到本机。

消除终端操作：密钥自动生成、ssh_config 自动渲染、`ssh -N -L` 子进程托管、
endpoint 自动登记（Chat 页直接可用）。全部网页按钮触发。

拓扑：
  浏览器 → modeldock server(代理 chat API) → 127.0.0.1:local_port
  → [ssh -N -L, ProxyCommand=colab ssh --proxy-mode -s SESS] → Colab VM 服务端口
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import Deployment, get_db_engine

BASE_LOCAL_PORT = 18080


class TunnelError(RuntimeError):
    pass


class TunnelManager:
    def __init__(self, colab_bin: str | None = None, ssh_bin: str = "ssh",
                 ssh_keygen_bin: str = "ssh-keygen") -> None:
        if colab_bin is None:
            from app.core.colab.cli import _find_colab_bin
            colab_bin = _find_colab_bin()
        self.colab_bin = colab_bin
        self.ssh_bin = ssh_bin
        self.ssh_keygen_bin = ssh_keygen_bin
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ---- 路径 ----
    @property
    def key_path(self) -> Path:
        return Path(settings.data_dir) / "colab_ssh_key"

    @property
    def ssh_config_path(self) -> Path:
        return Path(settings.data_dir) / "ssh_config"

    def ensure_key(self) -> Path:
        key = self.key_path
        if not key.exists():
            subprocess.run(
                [self.ssh_keygen_bin, "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                check=True, timeout=30)
            os.chmod(key, 0o600)
        return key

    def render_ssh_config(self, session_id: str, host_alias: str) -> Path:
        key = self.ensure_key()
        cfg = self.ssh_config_path
        cfg.write_text(
            f"Host {host_alias}\n"
            "  HostName colab-vm\n"
            "  User root\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
            "  ServerAliveInterval 30\n"
            f"  ProxyCommand {self.colab_bin} ssh --proxy-mode -s {session_id} -i {key}\n"
        )
        return cfg

    @staticmethod
    def _set_dep_params(dep_id: int, **kv) -> None:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d is None:
                raise TunnelError(f"deployment {dep_id} not found")
            params = dict(d.params or {})
            for k, v in kv.items():
                params[k] = v
            d.params = params  # JSON 列整体重赋值
            s.commit()

    @staticmethod
    def _get_dep(dep_id: int) -> Deployment:
        with Session(get_db_engine()) as s:
            return s.get(Deployment, dep_id)

    def _local_port(self, dep_id: int) -> int:
        return BASE_LOCAL_PORT + dep_id

    # ---- lifecycle ----
    def start(self, dep_id: int, service_port: int | None = None) -> dict:
        with self._lock:
            if dep_id in self._procs and self._procs[dep_id].poll() is None:
                raise TunnelError("tunnel already running")
            d = self._get_dep(dep_id)
            if d is None:
                raise TunnelError(f"deployment {dep_id} not found")
            if d.params and (d.params.get("session") or {}).get("id"):
                session_id = d.params["session"]["id"]
            else:
                raise TunnelError("deployment has no colab session (start it first)")
            if not service_port:
                ports = (d.params or {}).get("access", {}).get("ports") or []
                service_port = ports[0] if ports else 8080
            alias = f"modeldock-dep{dep_id}"
            cfg = self.render_ssh_config(session_id, alias)
            local_port = self._local_port(dep_id)
            proc = subprocess.Popen(
                [self.ssh_bin, "-F", str(cfg), "-N",
                 "-L", f"127.0.0.1:{local_port}:127.0.0.1:{service_port}", alias],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._procs[dep_id] = proc
        # 等握手：进程死亡率检查（ProxyCommand 阶段失败会很快退出）
        time.sleep(1.5)
        if proc.poll() is not None:
            self._procs.pop(dep_id, None)
            raise TunnelError(
                f"ssh 隧道启动失败（rc={proc.returncode}）；Colab 未登录或会话已结束？")
        self._set_dep_params(dep_id,
                             tunnel={"local_port": local_port,
                                     "service_port": service_port,
                                     "session": session_id})
        return {"local_port": local_port, "service_port": service_port,
                "endpoint": f"http://127.0.0.1:{local_port}"}

    def stop(self, dep_id: int) -> dict:
        proc = self._procs.pop(dep_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d is not None:
                params = dict(d.params or {})
                params.pop("tunnel", None)
                d.params = params  # 整体重赋值（去掉 tunnel）
                s.commit()
        return {"ok": True, "stopped": proc is not None}

    def status(self, dep_id: int) -> dict:
        proc = self._procs.get(dep_id)
        d = self._get_dep(dep_id)
        tunnel = (d.params or {}) if d else {}
        running = bool(proc and proc.poll() is None)
        return {"running": running,
                "pid": proc.pid if running else None,
                "tunnel": tunnel.get("tunnel")}


_tunnel_manager: TunnelManager | None = None


def get_tunnel_manager() -> TunnelManager:
    global _tunnel_manager
    if _tunnel_manager is None:
        _tunnel_manager = TunnelManager()
    return _tunnel_manager


def set_tunnel_manager(m: TunnelManager) -> None:
    global _tunnel_manager
    _tunnel_manager = m
