"""API 集成测试：TestClient 全链路（每测试独立 sqlite，不触真实 rclone/网络）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, set_db_path
from app.main import app


@pytest.fixture()
def client(tmp_path):
    set_db_path(tmp_path / "modeldock.db")
    init_db()
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "0.1.0"


def test_secret_write_and_metadata_only(client):
    r = client.put("/api/accounts/secrets", json={
        "scope": "hf", "name": "main", "secret": "hf_ro_token", "meta": {}})
    assert r.status_code == 201
    out = r.json()
    assert out["scope"] == "hf" and out["name"] == "main"
    assert "secret" not in out  # 只写不读、不回显

    listed = client.get("/api/accounts/secrets").json()
    assert len(listed) == 1

    # 覆盖更新同名字
    r2 = client.put("/api/accounts/secrets", json={
        "scope": "hf", "name": "main", "secret": "hf_ro_token_2"})
    assert r2.status_code == 201
    assert client.get("/api/accounts/secrets").json().__len__() == 1


def test_family_and_task_flow(client):
    fam = client.post("/api/models/families",
                      json={"name": "Qwen3-8B", "purpose": "chat"}).json()
    assert fam["purpose"] == "chat"

    dup = client.post("/api/models/families", json={"name": "Qwen3-8B"})
    assert dup.status_code == 409

    bad = client.post("/api/models/tasks", json={
        "family_id": fam["id"], "source": "hf", "repo": "", "filename": "",
        "target_remote_path": "qwen3/q3.gguf"})
    assert bad.status_code == 422  # hf 需要 repo/filename

    ok = client.post("/api/models/tasks", json={
        "family_id": fam["id"], "source": "hf", "repo": "Qwen/Qwen3-8B-GGUF",
        "filename": "q3-q4.gguf", "target_remote_path": "qwen3/q3-q4.gguf"})
    assert ok.status_code == 201
    t = ok.json()
    assert t["status"] in ("queued", "running")

    tasks = client.get("/api/models/tasks").json()
    assert any(x["id"] == t["id"] for x in tasks)


def test_recipe_and_deployment(client):
    rec = client.post("/api/recipes", json={
        "name": "llama-cpp-chat", "kind_class": "chat",
        "ports": [8080], "manifest": {"models": {"chat": {"family": "Qwen3-8B"}}}}).json()
    dep = client.post("/api/deployments", json={"recipe_id": rec["id"], "gpu": "T4"})
    assert dep.status_code == 201
    assert dep.json()["state"] == "queued"

    got = client.get(f"/api/deployments/{dep.json()['id']}")
    assert got.status_code == 200

    missing = client.post("/api/deployments", json={"recipe_id": 9999})
    assert missing.status_code == 404
