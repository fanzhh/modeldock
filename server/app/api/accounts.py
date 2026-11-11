"""凭证 API：只写不读。GET 只返回指纹元数据。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.vault import VaultError, vault
from app.db import SecretRef, get_db_engine
from app.schemas import SecretIn, SecretOut

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


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
