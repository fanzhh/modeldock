"""凭证 API：只写不读。GET 只返回指纹元数据。Drive 注册三通道：导入 / OAuth / 状态。"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.drive import Drive, DriveError
from app.core.gauth import GAuthError, OAuthSession, authorize_url, exchange_code, new_session
from app.core.vault import VaultError, vault
from app.db import SecretRef, get_db_engine
from app.schemas import OAuthStartIn, SecretIn, SecretOut

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# M1：OAuth state ↔ 会话的内存表（单进程足够；重启即失效，安全）
_pending_oauth: dict[str, OAuthSession] = {}


def _drive() -> Drive:
    return Drive(config_path=str(settings.rclone_config_path))


def _home_rclone_conf() -> str:
    return os.path.expanduser("~/.config/rclone/rclone.conf")


@router.get("/secrets", response_model=list[SecretOut])
def list_secrets():
    with Session(get_db_engine()) as s:
        return s.scalars(select(SecretRef).order_by(SecretRef.scope, SecretRef.id)).all()


@router.put("/secrets", response_model=SecretOut, status_code=201)
def put_secret(body: SecretIn):
    fp = vault.put(body.scope, body.name, body.secret)
    with Session(get_db_engine()) as s:
        existing = s.scalars(
            select(SecretRef).where(SecretRef.scope == body.scope, SecretRef.name == body.name)
        ).first()
        if existing:
            existing.fingerprint = fp
            existing.meta = body.meta
            obj = existing
        else:
            obj = SecretRef(scope=body.scope, name=body.name, meta=body.meta, fingerprint=fp)
            s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj


@router.delete("/secrets/{secret_id}", status_code=204)
def delete_secret(secret_id: int):
    with Session(get_db_engine()) as s:
        ref = s.get(SecretRef, secret_id)
        if not ref:
            raise HTTPException(404, "secret not found")
        try:
            vault.delete(ref.fingerprint)
        except VaultError as e:
            raise HTTPException(500, str(e)) from e
        s.delete(ref)
        s.commit()


def token_for(kind: str) -> str | None:
    """下载引擎注入用：按 source kind 返回对应 token（内存解密，不落库不落日志）。

    hf/modelscope → scope 同名；url → None（不支持带 token 的自定义源 M0）
    """
    scope_map = {"hf": "hf", "modelscope": "modelscope"}
    scope = scope_map.get(kind)
    if not scope:
        return None
    with Session(get_db_engine()) as s:
        ref = s.scalars(select(SecretRef).where(SecretRef.scope == scope).order_by(SecretRef.id)).first()
        if not ref:
            return None
        try:
            return vault.get(ref.fingerprint)
        except VaultError:
            return None


# ---------------------------------------------------------------- Drive 通道


@router.get("/drive/status")
def drive_status():
    """托管 remote 状态 + 用户机器上可导入的 remotes（不泄露 secret 值）。"""
    d = _drive()
    managed = d.has_remote()
    home_conf = _home_rclone_conf()
    importable: list[dict] = []
    importable_source = None
    for cand in (home_conf,):
        if os.path.exists(cand):
            items = Drive.list_remotes(cand)
            drive_items = [i for i in items if i["type"] == "drive"]
            if drive_items:
                importable = drive_items
                importable_source = cand
    health = d.health() if managed else {"ok": False, "detail": "managed remote not registered"}
    return {
        "managed_remote": d.remote,
        "registered": managed,
        "health": health,
        "importable": importable,
        "importable_source": importable_source,
    }


@router.post("/drive/import")
def drive_import(body: dict):
    """从现有 rclone 配置导入 drive remote（统一落到托管名 gdrive，0600）。"""
    source = body.get("source_config_path") or _home_rclone_conf()
    name = body.get("remote_name") or "gdrive"
    if not os.path.exists(source):
        raise HTTPException(422, f"source config not found: {source}")
    try:
        _drive().import_remote(source, name, as_name=_drive().remote)
    except DriveError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True, "managed_remote": _drive().remote,
            "health": _drive().health()}


@router.post("/drive/oauth/start")
def oauth_start(body: OAuthStartIn):
    """开始 OAuth：返回授权 URL。client_secret 即时存 vault，不返回。"""
    sess = new_session(body.client_id, body.client_secret,
                       f"http://127.0.0.1:{settings.port}/api/accounts/drive/oauth/callback")
    _pending_oauth[sess.state] = sess
    return {"authorize_url": authorize_url(sess), "state": sess.state,
            "redirect_uri": sess.redirect_uri,
            "hint": "在浏览器打开 URL 完成授权；回调由本服务自动处理"}


@router.get("/drive/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """浏览器回调：校验 state → 交换 token → 存 vault → 注册 rclone remote。"""
    if error:
        return {"ok": False, "stage": "google", "detail": error}
    sess = _pending_oauth.pop(state, None)
    if not sess:
        return {"ok": False, "stage": "state", "detail": "unknown or expired state; restart oauth"}
    try:
        tokens = exchange_code(sess, code)
    except GAuthError as e:
        return {"ok": False, "stage": "exchange", "detail": str(e)[:300]}
    # 客户端凭证与 refresh_token 入 vault；rclone.conf（0600）持有运行所需副本
    vault.put("google", "drive-refresh", tokens["refresh_token"])
    vault.put("google", "drive-client", sess.client_secret)
    _drive().register_refresh_token(sess.client_id, sess.client_secret, tokens["refresh_token"])
    return {"ok": True, "stage": "done",
            "health": _drive().health(),
            "note": "managed remote ready; client secret/refresh token sealed in vault"}


@router.get("/drive/quota")
def drive_quota():
    d = _drive()
    if not d.has_remote():
        raise HTTPException(409, "drive remote not registered")
    try:
        return {"ok": True, "about": d.quota()}
    except DriveError as e:
        raise HTTPException(502, str(e)[:300]) from e
