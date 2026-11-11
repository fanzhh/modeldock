"""网页隧道管理测试：配置渲染、进程托管、endpoint 自动登记。

用 fake ssh / fake keygen 脚本，不触真实网络与密钥。
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.colab.mock import MockColabBackend
from app.core.colab.orchestrator import Orchestrator, set_orchestrator
from app.core.colab.tunnel import TunnelError, TunnelManager, set_tunnel_manager
from app.config import settings
from app.db import Deployment, Recipe, get_db_engine, init_db, set_db_path
from app.main import app


class FakeTunnelManager(TunnelManager):
    """ssh/keygen 全替换为 fake；start 走真实 Popen 流程但目标是无害脚本。"""

    def __init__(self, tmp_path, ssh_exit: str = "sleep 30"):
        self._tmp = tmp_path
        fake_ssh = tmp_path / "ssh-fake"
        fake_keygen = tmp_path / "keygen-fake"
        fake_colab = tmp_path / "colab-fake"
        fake_ssh.write_text(f"#!/bin/sh\n{ssh_exit}\n")
        fake_keygen.write_text("#!/bin/sh\n# args: -t ed25519 -N '' -q -f PATH\ntouch \"$7\"\n")
        fake_colab.write_text("#!/bin/sh\nexit 0\n")
        for f in (fake_ssh, fake_keygen, fake_colab):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        super().__init__(colab_bin=str(fake_colab), ssh_bin=str(fake_ssh),
                         ssh_keygen_bin=str(fake_keygen))


def _setup(tmp_path, ready: bool = True) -> int:
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    with Session(get_db_engine()) as s:
        rec = Recipe(name="r", kind_class="chat", manifest={"models": {}}, ports=[8080])
        s.add(rec)
        s.flush()
        d = Deployment(recipe_id=rec.id, gpu="T4",
                       state="ready" if ready else "failed",
                       params={"session": {"id": "sess-a", "gpu": "T4"},
                               "access": {"ports": [8080]}},
                       state_history=[], logs="")
        s.add(d)
        s.commit()
        return d.id


# ---------- manager 单元 ----------

def test_ensure_key_and_config_render(tmp_path):
    dep_id = _setup(tmp_path)
    tm = FakeTunnelManager(tmp_path)
    key = tm.ensure_key()
    assert key.exists() and key == Path(settings.data_dir) / "colab_ssh_key"
    assert (key.stat().st_mode & 0o777) == 0o600

    cfg = tm.render_ssh_config("sess-a", "modeldock-dep1")
    text = cfg.read_text()
    assert "ProxyCommand" in text and "ssh --proxy-mode -s sess-a" in text
    assert "StrictHostKeyChecking no" in text
    assert str(key) in text


def test_start_registers_tunnel_and_endpoint(tmp_path):
    dep_id = _setup(tmp_path)
    tm = FakeTunnelManager(tmp_path)
    out = tm.start(dep_id)
    assert out["local_port"] == 18080 + dep_id
    assert out["service_port"] == 8080  # access.ports[0]
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        assert d.params["tunnel"]["local_port"] == out["local_port"]
    tm.stop(dep_id)
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        assert "tunnel" not in (d.params or {})
    assert tm.status(dep_id)["running"] is False


def test_start_requires_session(tmp_path):
    dep_id = _setup(tmp_path)  # 先切 DB 再开 Session（否则绑定旧 engine）
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        d.params = {"access": {"ports": [8080]}}  # 无 session
        s.commit()
    tm = FakeTunnelManager(tmp_path)
    with pytest.raises(TunnelError, match="no colab session"):
        tm.start(dep_id)


def test_ssh_process_death_detects(tmp_path):
    """ssh 立即退出（colab 未登录场景）→ start 报错。"""
    dep_id = _setup(tmp_path)
    tm = FakeTunnelManager(tmp_path, ssh_exit="exit 7")
    with pytest.raises(TunnelError, match="rc=7"):
        tm.start(dep_id)


# ---------- API ----------

def test_tunnel_api_flow(tmp_path, monkeypatch):
    dep_id = _setup(tmp_path)
    set_orchestrator(Orchestrator(backend=MockColabBackend()))
    tm = FakeTunnelManager(tmp_path)
    set_tunnel_manager(tm)
    with TestClient(app) as c:
        r = c.post(f"/api/deployments/{dep_id}/tunnel/start",
                   json={"service_port": 0}).json()
        assert r["local_port"] == 18080 + dep_id
        assert r["endpoint"] == f"http://127.0.0.1:{18080 + dep_id}"
        # endpoint 已自动登记（chat/gen 页零手工）
        dep = c.get(f"/api/deployments/{dep_id}").json()
        assert dep["params"]["endpoint"] == r["endpoint"]

        st = c.get(f"/api/deployments/{dep_id}/tunnel/status").json()
        assert st["running"] is True and st["tunnel"]["service_port"] == 8080

        stop = c.post(f"/api/deployments/{dep_id}/tunnel/stop").json()
        assert stop["ok"] is True

        missing = c.post("/api/deployments/999/tunnel/start", json={})
        assert missing.status_code == 404
