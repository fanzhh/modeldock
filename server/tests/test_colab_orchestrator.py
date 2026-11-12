"""M2 Colab 编排测试：状态机、GPU 降级、模型装配、释放、notebook 兜底、API。

约束：全部用 MockColabBackend / tmp DB，不触真实 colab CLI、不联网。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.colab.base import GPU_FALLBACK
from app.core.colab.mock import MockColabBackend
from app.core.colab.orchestrator import Orchestrator, set_orchestrator
from app.core.colab.notebook import render_cell
from app.core.drive import Drive
from app.db import (Deployment, ModelFamily, ModelFile, Recipe, get_db_engine,
                    init_db, set_db_path)
from app.main import app


def _setup_repo(tmp_path) -> int:
    """家庭 + 模型文件 + llama.cpp 配方 + 一个 queued 部署 + 托管 drive 段。"""
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    (tmp_path / "rclone.conf").write_text(
        "[gdrive]\ntype = drive\nclient_id = cid\ntoken = {\"refresh_token\":\"rt\"}\n")
    with Session(get_db_engine()) as s:
        fam = ModelFamily(name="Qwen3-8B", purpose="chat")
        s.add(fam)
        s.flush()
        s.add(ModelFile(family_id=fam.id, name="q3-q4.gguf",
                        remote_path="qwen3/q3-q4.gguf", bytes=4_800_000_000))
        rec = Recipe(name="llamacpp-chat", kind_class="chat",
                     manifest={"models": {"chat": {"family": "Qwen3-8B"}}, "ports": [8080]},
                     install_script="git clone https://github.com/ggerganov/llama.cpp",
                     run_script="llama-server --host 0.0.0.0 --port 8080",
                     ports=[8080])
        s.add(rec)
        s.flush()
        dep = Deployment(recipe_id=rec.id, gpu="A100", params={},
                         state="queued", state_history=[{"state": "queued"}], logs="")
        s.add(dep)
        s.commit()
        return dep.id


async def _wait_state_async(dep_id: int, want: set[str], timeout: float = 10.0) -> str:
    """异步等待（pytest-asyncio 场景：await 让出循环，后台 task 才能推进）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d.state in want:
                return d.state
        await asyncio.sleep(0.05)
    raise AssertionError(f"deployment did not reach {want} (last={d.state})")


def _wait_state(dep_id: int, want: set[str], timeout: float = 10.0) -> str:
    """同步轮询 DB（API 场景：backend task 由 TestClient 的 portal 循环驱动）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d.state in want:
                return d.state
        time.sleep(0.05)
    raise AssertionError(f"deployment did not reach {want} (last={d.state})")


def _get_dep(dep_id: int) -> Deployment:
    with Session(get_db_engine()) as s:
        return s.get(Deployment, dep_id)


# ---------- orchestrator ----------

@pytest.mark.asyncio
async def test_full_lifecycle_ready(tmp_path):
    dep_id = _setup_repo(tmp_path)
    mock = MockColabBackend()
    orch = Orchestrator(backend=mock)
    assert orch.start(dep_id)
    assert await _wait_state_async(dep_id, {"ready", "failed"}) == "ready"

    d = _get_dep(dep_id)
    states = [h["state"] for h in d.state_history]
    assert states == ["queued", "provision", "install", "syncing_models", "booting", "ready"]

    scripts = "\n".join(x[1] for x in mock.scripts)
    assert "llama.cpp" in scripts                      # install 含配方脚本
    assert "command -v rclone" in scripts              # rclone 兜底安装
    assert "rclone copy" in scripts and "modeldock/models" in scripts  # 托管前缀
    assert "curl" in scripts                           # boot 端口健康检查
    assert mock.colab_copies(), "装配步骤必须执行 rclone copy"

    # 临时 rclone 配置注入（路径 + 单段内容）
    assert len(mock.uploads) == 1
    assert mock.uploads[0][1] == "/content/.secrets/rclone.conf"
    assert mock.uploads[0][2].strip().startswith("[gdrive]")

    assert d.params["session"]["id"].startswith("mock-")


@pytest.mark.asyncio
async def test_gpu_fallback_chain(tmp_path):
    dep_id = _setup_repo(tmp_path)
    mock = MockColabBackend(created_gpu="L4")  # A100 缺货 → 实际给 L4
    Orchestrator(backend=mock).start(dep_id)
    assert await _wait_state_async(dep_id, {"ready", "failed"}) == "ready"
    d = _get_dep(dep_id)
    assert d.params["session"]["gpu"] == "L4"
    assert "downgraded to L4" in d.logs


@pytest.mark.asyncio
async def test_provision_exhaustion_fails(tmp_path):
    dep_id = _setup_repo(tmp_path)
    mock = MockColabBackend(fail_on={"create"})
    Orchestrator(backend=mock).start(dep_id)
    assert await _wait_state_async(dep_id, {"failed"}) == "failed"
    d = _get_dep(dep_id)
    assert "no GPU available" in d.logs
    for gpu in ("A100", "L4", "T4"):  # 全链尝试过
        assert gpu in d.logs


@pytest.mark.asyncio
async def test_sync_failure_marks_failed(tmp_path):
    dep_id = _setup_repo(tmp_path)
    mock = MockColabBackend(fail_on={"sync_step"})
    Orchestrator(backend=mock).start(dep_id)
    assert await _wait_state_async(dep_id, {"failed"}) == "failed"


@pytest.mark.asyncio
async def test_release_stops_session(tmp_path):
    dep_id = _setup_repo(tmp_path)
    mock = MockColabBackend()
    orch = Orchestrator(backend=mock)
    orch.start(dep_id)
    await _wait_state_async(dep_id, {"ready"})
    assert await orch.release(dep_id) == "done"
    assert mock.stopped
    assert "rm" in mock.scripts[-1][1]  # 删临时凭证的清理命令已下发
    d = _get_dep(dep_id)
    assert d.state == "done" and d.state_history[-1]["state"] == "done"


def test_gpu_fallback_table():
    assert GPU_FALLBACK["A100"] == ["A100", "L4", "T4"]
    assert GPU_FALLBACK["T4"] == ["T4"]


@pytest.mark.asyncio
async def test_no_drive_remote_fails_fast(tmp_path):
    """模型装配预检：托管 drive 未注册 → failed，且不开会话。"""
    dep_id = _setup_repo(tmp_path)
    (tmp_path / "rclone.conf").unlink()  # 撤销托管段
    mock = MockColabBackend()
    Orchestrator(backend=mock).start(dep_id)
    assert await _wait_state_async(dep_id, {"failed"}) == "failed"
    d = _get_dep(dep_id)
    assert "drive remote not registered" in d.logs
    assert not mock.sessions  # 未创建任何 VM


# ---------- notebook 兜底 ----------

def test_notebook_cell_renders(tmp_path):
    dep_id = _setup_repo(tmp_path)
    cell = render_cell(dep_id)
    assert "ModelDock 手动部署单元格" in cell
    assert "Qwen3-8B" in cell and "qwen3/q3-q4.gguf" in cell
    assert "gdrive:modeldock/models" in cell
    assert "rm -f /content/.secrets/rclone.conf" in cell  # 用完释放提示
    assert "GPU=A100" in cell


def test_notebook_missing_family(tmp_path):
    dep_id = _setup_repo(tmp_path)
    with Session(get_db_engine()) as s:
        s.query(Recipe).first().manifest = {"models": {"chat": {"family": "NoSuch"}}}
        s.commit()
    with pytest.raises(ValueError, match="NoSuch"):
        render_cell(dep_id)


# ---------- API 级别 ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient（自带事件循环驱动后台任务）+ 注入 mock 编排器与 fake rclone。"""
    init_db()
    fake_bin = tmp_path / "rclone-fake"
    fake_bin.write_text("#!/bin/sh\necho 'Total: 15 TiB'\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("app.api.accounts._drive",
                        lambda: Drive(config_path=str(tmp_path / "rclone.conf"),
                                      rclone_bin=str(fake_bin)))
    # colab usage 探测走 fake CLI（已登录模式，避免真实 CLI 10s 超时）
    fake_colab = tmp_path / "colab-fake"
    fake_colab.write_text("#!/bin/sh\necho 'compute units: 42.0'\nexit 0\n")
    import stat as _stat
    fake_colab.chmod(fake_colab.stat().st_mode | _stat.S_IEXEC)
    from app.core.colab.auth import ColabAuthManager, set_auth_manager
    set_auth_manager(ColabAuthManager(colab_bin=str(fake_colab)))
    set_orchestrator(Orchestrator(backend=MockColabBackend()))
    with TestClient(app) as c:
        yield c


def test_deployment_start_and_logs_api(client, tmp_path):
    dep_id = _setup_repo(tmp_path)
    r = client.post(f"/api/deployments/{dep_id}/start")
    assert r.status_code == 200
    assert _wait_state(dep_id, {"ready", "failed"}) == "ready"

    logs = client.get(f"/api/deployments/{dep_id}/logs").text
    assert "[provision]" in logs and "[syncing_models]" in logs
    assert client.get(f"/api/deployments/{dep_id}").json()["state"] == "ready"

    usage = client.get("/api/colab/usage").json()
    assert usage["logged_in"] is True and "compute units" in usage["usage"]


def test_notebook_endpoint(client, tmp_path):
    dep_id = _setup_repo(tmp_path)
    r = client.get(f"/api/deployments/{dep_id}/notebook")
    assert r.status_code == 200 and "ModelDock" in r.text
    assert client.get("/api/deployments/999/notebook").status_code == 404


def test_release_api(client, tmp_path):
    dep_id = _setup_repo(tmp_path)
    client.post(f"/api/deployments/{dep_id}/start")
    _wait_state(dep_id, {"ready", "failed"})
    r = client.post(f"/api/deployments/{dep_id}/release")
    assert r.status_code == 200 and r.json()["state"] == "done"


def test_missing_family_fails_pipeline(client, tmp_path):
    dep_id = _setup_repo(tmp_path)
    with Session(get_db_engine()) as s:
        s.query(Recipe).first().manifest = {"models": {"chat": {"family": "NoSuch"}}}
        s.commit()
    assert client.post(f"/api/deployments/{dep_id}/start").status_code == 200
    _wait_state(dep_id, {"ready", "failed", "cancelled"})
    d = _get_dep(dep_id)
    assert d.state == "failed" and "NoSuch" in d.logs
