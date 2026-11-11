"""配方与部署 API。

M0：记录级编排。M2：接 Colab 编排器（start/cancel/release/logs/notebook/usage）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.colab.notebook import render_cell
from app.core.colab.orchestrator import get_orchestrator
from app.db import Deployment, Recipe, get_db_engine
from app.schemas import DeploymentIn, DeploymentOut, RecipeIn, RecipeOut

router = APIRouter(prefix="/api", tags=["recipes"])


# ---- recipes ----
@router.get("/recipes", response_model=list[RecipeOut])
def list_recipes():
    with Session(get_db_engine()) as s:
        return s.scalars(select(Recipe).order_by(Recipe.id)).all()


@router.post("/recipes", response_model=RecipeOut, status_code=201)
def create_recipe(body: RecipeIn):
    with Session(get_db_engine()) as s:
        r = Recipe(
            name=body.name, kind_class=body.kind_class, description=body.description,
            manifest=body.manifest, install_script=body.install_script,
            run_script=body.run_script, ports=body.ports,
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return r


# ---- deployments ----
@router.get("/deployments", response_model=list[DeploymentOut])
def list_deployments():
    with Session(get_db_engine()) as s:
        return s.scalars(select(Deployment).order_by(Deployment.id.desc())).all()


@router.post("/deployments", response_model=DeploymentOut, status_code=201)
def create_deployment(body: DeploymentIn):
    with Session(get_db_engine()) as s:
        r = s.get(Recipe, body.recipe_id)
        if not r:
            raise HTTPException(404, "recipe not found")
        # 预检：manifest.models 中引用的 family 是否已在库（告警但不阻断，M0 只标注）
        d = Deployment(recipe_id=body.recipe_id, gpu=body.gpu, params=body.params,
                       state="queued",
                       state_history=[{"state": "queued"}],
                       logs="M0: deployment accepted; Colab orchestration not wired yet\n")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d


@router.get("/deployments/{dep_id}", response_model=DeploymentOut)
def get_deployment(dep_id: int):
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        if not d:
            raise HTTPException(404, "deployment not found")
        return d


# ---- M2：Colab 编排 ----


def _get_dep(dep_id: int) -> Deployment:
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
    if not d:
        raise HTTPException(404, "deployment not found")
    return d


@router.post("/deployments/{dep_id}/start", response_model=DeploymentOut)
async def start_deployment(dep_id: int):
    """启动（或继续）编排流水线。已在跑 → 409。"""
    d = _get_dep(dep_id)
    if not get_orchestrator().start(dep_id):
        raise HTTPException(409, "deployment already running")
    return _get_dep(dep_id)


@router.post("/deployments/{dep_id}/cancel", response_model=DeploymentOut)
def cancel_deployment(dep_id: int):
    _get_dep(dep_id)
    get_orchestrator().cancel(dep_id)
    return _get_dep(dep_id)


@router.post("/deployments/{dep_id}/release", response_model=DeploymentOut)
async def release_deployment(dep_id: int):
    """释放会话（幂等）：删临时凭证 → 停 VM → done。"""
    _get_dep(dep_id)
    await get_orchestrator().release(dep_id)
    return _get_dep(dep_id)


@router.get("/deployments/{dep_id}/logs", response_class=PlainTextResponse)
def deployment_logs(dep_id: int):
    return _get_dep(dep_id).logs or ""


@router.get("/deployments/{dep_id}/notebook", response_class=PlainTextResponse)
def deployment_notebook(dep_id: int):
    """手动兜底：生成可粘贴到 Colab 网页版的单元格（API 变动时的退路）。"""
    try:
        return render_cell(dep_id)
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(404, str(e)) from e
        raise HTTPException(422, str(e)) from e


@router.get("/colab/usage")
def colab_usage():
    """compute units 余额（经当前后端；mock 返回占位）。"""
    return get_orchestrator().backend.usage()


# ---- SSH 隧道（网页一键开通，替代手动 ssh -L + 登记端点） ----

class TunnelStartIn(BaseModel):
    service_port: int = Field(default=0, ge=0, le=65535)


@router.post("/deployments/{dep_id}/tunnel/start")
def tunnel_start(dep_id: int, body: TunnelStartIn | None = None):
    """一键开通：自动生成密钥/ssh 配置、起端口转发，并把 chat endpoint 登记。
    之后 Chat 页（及任何 OpenAI 兼容客户端指向返回的 endpoint）即可直用。"""
    from app.core.colab.tunnel import TunnelError, get_tunnel_manager
    _get_dep(dep_id)  # 404 检查
    try:
        out = get_tunnel_manager().start(dep_id, body.service_port or None)
    except TunnelError as e:
        raise HTTPException(409, str(e)) from e
    # endpoint 自动登记（幂等覆盖）
    with Session(get_db_engine()) as s:
        d = s.get(Deployment, dep_id)
        params = dict(d.params or {})
        params["chat_endpoint"] = out["endpoint"]
        d.params = params
        s.commit()
    return {**out, "chat_endpoint": out["endpoint"]}


@router.post("/deployments/{dep_id}/tunnel/stop")
def tunnel_stop(dep_id: int):
    from app.core.colab.tunnel import get_tunnel_manager
    _get_dep(dep_id)
    return get_tunnel_manager().stop(dep_id)


@router.get("/deployments/{dep_id}/tunnel/status")
def tunnel_status(dep_id: int):
    from app.core.colab.tunnel import get_tunnel_manager
    _get_dep(dep_id)
    return get_tunnel_manager().status(dep_id)


# ---- Colab 网页登录（无头 OAuth 转接；凭证由 colab CLI 自管） ----

@router.post("/colab/auth/start")
def colab_auth_start():
    """起 CLI 授权进程并返回授权 URL；已登录则直接返回余额输出。"""
    from app.core.colab.auth import ColabAuthError, get_auth_manager
    try:
        return get_auth_manager().start()
    except ColabAuthError as e:
        raise HTTPException(422, str(e)) from e


class ColabAuthCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


@router.post("/colab/auth/submit")
def colab_auth_submit(body: ColabAuthCodeIn):
    """把浏览器回调码写回 CLI 进程，完成登录。"""
    from app.core.colab.auth import ColabAuthError, get_auth_manager
    try:
        return get_auth_manager().submit(body.code)
    except ColabAuthError as e:
        raise HTTPException(409, str(e)) from e
