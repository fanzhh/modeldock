"""网页连接服务测试：OAuth redirect_uri 动态化 + Colab CLI 登录转接。

全部用 fake colab 脚本 / TestClient，不触真实 CLI、不联网。
"""
from __future__ import annotations

import stat

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.colab.auth import ColabAuthError, ColabAuthManager, set_auth_manager
from app.core.colab.mock import MockColabBackend
from app.core.colab.orchestrator import Orchestrator, set_orchestrator
from app.db import Deployment, Recipe, get_db_engine, init_db, set_db_path
from app.main import app


@pytest.fixture()
def client(tmp_path):
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    set_orchestrator(Orchestrator(backend=MockColabBackend()))
    with TestClient(app) as c:
        yield c


# ---------- OAuth 动态 redirect ----------

def test_oauth_start_redirect_follows_browser_host(client):
    r = client.post("/api/accounts/drive/oauth/start",
                    json={"client_id": "cid", "client_secret": "cs"},
                    headers={"host": "hk.tail96897d.ts.net:8820"})
    assert r.status_code == 200
    body = r.json()
    assert body["redirect_uri"] == "http://hk.tail96897d.ts.net:8820/api/accounts/drive/oauth/callback"
    assert "hk.tail96897d.ts.net" in body["authorize_url"]  # google 会收到同样的 callback

    r2 = client.post("/api/accounts/drive/oauth/start",
                     json={"client_id": "cid", "client_secret": "cs"},
                     headers={"host": "127.0.0.1:8820"})
    assert r2.json()["redirect_uri"] == "http://127.0.0.1:8820/api/accounts/drive/oauth/callback"


def test_oauth_start_respects_forwarded_proto(client):
    """tailscale serve（https 终结）→ 内部 http 服务要保留 https。"""
    r = client.post("/api/accounts/drive/oauth/start",
                    json={"client_id": "cid", "client_secret": "cs"},
                    headers={"host": "hk.tail96897d.ts.net",
                             "x-forwarded-proto": "https"})
    assert r.json()["redirect_uri"] == "https://hk.tail96897d.ts.net/api/accounts/drive/oauth/callback"


# ---------- Colab 网页登录 ----------

def _fake_colab(tmp_path, mode: str = "oauth"):
    """模拟 colab CLI：打印授权 URL、stdin 收码。
    mode=already：直接输出余额退出（已登录态）。"""
    script = tmp_path / f"colab-{mode}"
    if mode == "already":
        script.write_text(
            "#!/bin/sh\n"
            "echo 'compute units: 33.44'\n"
            "exit 0\n")
    else:
        script.write_text(
            "#!/bin/sh\n"
            "echo 'To authorize colab-cli, visit this URL in any browser:'\n"
            "echo '  https://accounts.google.com/o/oauth2/auth?state=xyz&code_challenge=abc'\n"
            "echo 'After approving, Google will display an authorization code.'\n"
            "echo 'Enter the authorization code:'\n"
            "read -r code\n"
            "if [ \"$code\" = \"GOODCODE\" ]; then\n"
            "  echo 'compute units: 33.44 saved'\n"
            "  exit 0\n"
            "else\n"
            "  echo 'Error: invalid_grant'\n"
            "  exit 1\n"
            "fi\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_colab_auth_start_and_submit(tmp_path, client):
    mg = ColabAuthManager(colab_bin=_fake_colab(tmp_path, "oauth"))
    set_auth_manager(mg)
    r = client.post("/api/colab/auth/start")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is True
    assert "accounts.google.com/o/oauth2/auth" in body["url"]

    ok = client.post("/api/colab/auth/submit", json={"code": "GOODCODE"})
    assert ok.status_code == 200
    out = ok.json()
    assert out["ok"] is True and "compute units" in out["output"]

    # 会话结束后不能重复提交
    late = client.post("/api/colab/auth/submit", json={"code": "GOODCODE"})
    assert late.status_code == 409


def test_colab_auth_bad_code(tmp_path, client):
    set_auth_manager(ColabAuthManager(colab_bin=_fake_colab(tmp_path, "oauth")))
    client.post("/api/colab/auth/start")
    r = client.post("/api/colab/auth/submit", json={"code": "WRONG"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_colab_auth_already_logged_in(tmp_path, client):
    set_auth_manager(ColabAuthManager(colab_bin=_fake_colab(tmp_path, "already")))
    r = client.post("/api/colab/auth/start")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is False
    assert "compute units" in body["usage"]


def test_colab_auth_start_twice_conflict(tmp_path, client):
    """第一个会话未结束前再 start → 422（保持单会话）。"""
    set_auth_manager(ColabAuthManager(colab_bin=_fake_colab(tmp_path, "oauth")))
    assert client.post("/api/colab/auth/start").status_code == 200
    assert client.post("/api/colab/auth/start").status_code == 422
    # 收尾，避免悬挂进程
    mg2 = ColabAuthManager(colab_bin=_fake_colab(tmp_path, "already"))
    set_auth_manager(mg2)


def test_colab_auth_cancel_frees_session(tmp_path, client):
    """cancel 后可立即重新 start（用户点开授权不完成时的自愈路径）。"""
    set_auth_manager(ColabAuthManager(colab_bin=_fake_colab(tmp_path, "oauth")))
    client.post("/api/colab/auth/start")
    cancel = client.post("/api/colab/auth/cancel")
    assert cancel.status_code == 200 and cancel.json()["ok"] is True
    again = client.post("/api/colab/auth/start")
    assert again.status_code == 200
    assert again.json()["auth_required"] is True
    assert client.post("/api/colab/auth/cancel").status_code == 200


def test_stale_session_auto_reaped(tmp_path):
    """超时悬挂会话 → start 自动回收并允许新会话（不再死锁 422）。"""
    import time as _time
    mg = ColabAuthManager(colab_bin=_fake_colab(tmp_path, "oauth"))
    mg.start()
    mg.session.created_at = _time.time() - 7200  # 篡改为两小时前
    out = mg.start()  # _reap_locked 应回收旧会话
    assert out["auth_required"] is True
    mg.cancel()


def test_finished_session_auto_reaped(tmp_path):
    """已退出的会话（未登录场景直接跑完）→ 下次 start 不应报占用。"""
    mg = ColabAuthManager(colab_bin=_fake_colab(tmp_path, "already"))
    assert mg.start()["auth_required"] is False  # 进程立即退出并输出余额
    out2 = mg.start()                            # 旧会话已退出 → 自动回收
    assert out2["auth_required"] is False


def test_probe_usage_logged_in(tmp_path):
    mg = ColabAuthManager(colab_bin=_fake_colab(tmp_path, "already"))
    out = mg.probe_usage()
    assert out["logged_in"] is True and "compute units" in out["usage"]


def test_probe_usage_not_logged_in(tmp_path):
    """未登录 CLI 会打印授权 URL 等 stdin → 超时即未登录（不入死等）。"""
    mg = ColabAuthManager(colab_bin=_fake_colab(tmp_path, "oauth"))
    out = mg.probe_usage(timeout=3)
    assert out["logged_in"] is False
