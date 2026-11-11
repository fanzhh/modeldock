"""M1 账号通道测试：gauth、drive 导入/脱敏、OAuth 与导入 API。

约束：全部用 tmp conf + fake rclone 脚本，不触发真实 rclone / 网络执行。
"""
from __future__ import annotations

import configparser
import os

import pytest
from fastapi.testclient import TestClient

from app.core.drive import Drive, DriveError
from app.core.gauth import GAuthError, authorize_url, exchange_code, new_session
from app.core.vault import Vault
from app.db import init_db, set_db_path
from app.main import app

CALLBACK = "http://127.0.0.1:8820/api/accounts/drive/oauth/callback"


# ---------- gauth ----------

def test_authorize_url_shape():
    sess = new_session("cid.apps.googleusercontent.com", "csecret", CALLBACK)
    url = authorize_url(sess)
    assert "accounts.google.com/o/oauth2/v2/auth" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert f"state={sess.state}" in url
    assert "drive" in url  # scope 含 drive 权限


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, data=None):
        self.posts.append((url, dict(data or {})))
        return _FakeResp(self.status_code, self.payload)


def test_exchange_code_success():
    sess = new_session("cid", "csecret", CALLBACK)
    fake = _FakeClient(payload={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    out = exchange_code(sess, "the-code", http=fake)
    assert out["refresh_token"] == "rt"
    url, body = fake.posts[0]
    assert "oauth2.googleapis.com/token" in url
    assert body["grant_type"] == "authorization_code"
    assert body["client_secret"] == "csecret" and body["code"] == "the-code"


def test_exchange_code_missing_refresh_token_rejected():
    sess = new_session("cid", "csecret", CALLBACK)
    fake = _FakeClient(payload={"access_token": "at"})
    with pytest.raises(GAuthError):
        exchange_code(sess, "c", http=fake)


def test_exchange_code_http_error():
    sess = new_session("cid", "csecret", CALLBACK)
    fake = _FakeClient(status_code=400, payload={"error": "invalid_grant"})
    with pytest.raises(GAuthError, match="HTTP 400"):
        exchange_code(sess, "c", http=fake)


# ---------- drive: conf helpers ----------

def _write_conf(path: str, section: str, **kv) -> None:
    parser = configparser.ConfigParser()
    parser[section] = dict(kv)
    with open(path, "w") as f:
        parser.write(f)


def _managed_conf(tmp_path):
    return str(tmp_path / "managed.conf")


def test_list_remotes_masks_secrets(tmp_path):
    src = str(tmp_path / "src.conf")
    _write_conf(src, "mydrive", type="drive", token='{"refresh_token":"SECRET"}',
                client_id="123456789012.apps.googleusercontent.com", client_secret="cs")
    # 追加一个非 drive 段
    with open(src, "a") as f:
        f.write("[box]\ntype = sftp\n")
    items = Drive.list_remotes(src)
    by_name = {i["name"]: i for i in items}
    assert by_name["mydrive"]["type"] == "drive"
    assert by_name["mydrive"]["has_token"] is True
    assert by_name["mydrive"]["client_id_tail"] == "rcontent.com"  # 仅末 12 字符
    dumped = repr(items)
    assert "SECRET" not in dumped and "cs" not in dumped
    assert "123456789012.apps.googleusercontent.com" not in dumped


def test_import_remote_copies_with_rename(tmp_path):
    src = str(tmp_path / "src.conf")
    _write_conf(src, "mydrive", type="drive", scope="drive",
                client_id="cid", client_secret="cs", token='{"refresh_token":"rt"}')
    managed = _managed_conf(tmp_path)
    d = Drive(config_path=managed)
    d.import_remote(src, "mydrive", as_name="gdrive")
    parser = configparser.ConfigParser()
    parser.read(managed)
    assert parser.has_section("gdrive")
    assert parser.get("gdrive", "type") == "drive"
    assert "rt" in parser.get("gdrive", "token")
    assert parser.get("gdrive", "client_id") == "cid"
    mode = os.stat(managed).st_mode & 0o777
    assert mode == 0o600
    assert d.has_remote()


def test_import_remote_rejects_non_drive(tmp_path):
    src = str(tmp_path / "src.conf")
    _write_conf(src, "box", type="sftp", host="h")
    d = Drive(config_path=_managed_conf(tmp_path))
    with pytest.raises(DriveError, match="not a drive"):
        d.import_remote(src, "box")

    with pytest.raises(DriveError, match="not found"):
        d.import_remote(src, "missing")


# ---------- API 级别 ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    # fake rclone：任何子命令直接成功退出，绝不执行真实 rclone
    fake_bin = tmp_path / "rclone-fake"
    fake_bin.write_text("#!/bin/sh\necho 'Total: 15 TiB'\necho 'Used: 1 TiB'\nexit 0\n")
    fake_bin.chmod(0o755)
    managed = _managed_conf(tmp_path)
    monkeypatch.setattr("app.api.accounts._drive",
                        lambda: Drive(config_path=str(managed), rclone_bin=str(fake_bin)))
    with TestClient(app) as c:
        yield c
    return None


def test_drive_status_and_import_api(client, tmp_path, monkeypatch):
    src = str(tmp_path / "home-rclone.conf")
    _write_conf(src, "gdrive", type="drive", client_id="cid",
                client_secret="cs", token='{"refresh_token":"rt"}')
    monkeypatch.setattr("app.api.accounts._home_rclone_conf", lambda: src)

    st = client.get("/api/accounts/drive/status").json()
    assert st["managed_remote"] == "gdrive"
    assert st["registered"] is False
    assert st["importable"][0]["name"] == "gdrive"
    assert "cs" not in repr(st)  # 不回显 secret

    r = client.post("/api/accounts/drive/import",
                    json={"source_config_path": src, "remote_name": "gdrive"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["managed_remote"] == "gdrive"
    assert body["health"]["ok"] is True

    st2 = client.get("/api/accounts/drive/status").json()
    assert st2["registered"] is True

    parser = configparser.ConfigParser()
    parser.read(_managed_conf(tmp_path))
    assert parser.has_section("gdrive") and parser.get("gdrive", "type") == "drive"


def test_drive_import_missing_source(client, tmp_path):
    r = client.post("/api/accounts/drive/import",
                    json={"source_config_path": str(tmp_path / "nope.conf")})
    assert r.status_code == 422


def test_oauth_start_and_callback(client, tmp_path, monkeypatch):
    start = client.post("/api/accounts/drive/oauth/start",
                         json={"client_id": "cid.apps.googleusercontent.com", "client_secret": "cs"},
                         headers={"host": "127.0.0.1:8820"})  # 网页发起：回调随浏览器地址
    assert start.status_code == 200
    body = start.json()
    assert "accounts.google.com" in body["authorize_url"]
    assert body["redirect_uri"] == "http://127.0.0.1:8820/api/accounts/drive/oauth/callback"
    state = body["state"]

    # 回调：mock token 交换 + 临时 vault（不触真实 ~/.modeldock）
    monkeypatch.setattr("app.api.accounts.exchange_code",
                        lambda sess, code: {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    v = Vault(key_path=tmp_path / "vault.key")
    monkeypatch.setattr("app.api.accounts.vault", v)
    monkeypatch.setattr("app.core.vault.settings.data_dir", tmp_path)

    cb = client.get(f"/api/accounts/drive/oauth/callback?code=x&state={state}")
    assert cb.status_code == 200
    out = cb.json()
    assert out["ok"] is True and out["stage"] == "done"

    # rclone 托管段已写入 refresh token JSON，且文件 0600
    parser = configparser.ConfigParser()
    parser.read(_managed_conf(tmp_path))
    assert parser.has_section("gdrive")
    assert "rt" in parser.get("gdrive", "token")
    mode = os.stat(_managed_conf(tmp_path)).st_mode & 0o777
    assert mode == 0o600

    # state 一次性：重放失败
    replay = client.get(f"/api/accounts/drive/oauth/callback?code=x&state={state}")
    assert replay.json()["ok"] is False

    # 未知 state / error 参数
    unknown = client.get("/api/accounts/drive/oauth/callback?code=x&state=bogus")
    assert unknown.json()["ok"] is False
    err = client.get("/api/accounts/drive/oauth/callback?error=access_denied")
    assert err.json()["ok"] is False and err.json()["stage"] == "google"


def test_oauth_start_validation(client):
    r = client.post("/api/accounts/drive/oauth/start", json={"client_id": "cid"})
    assert r.status_code == 422
