"""配方与部署 API（M0：记录级编排，不真正开 Colab 会话）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

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
