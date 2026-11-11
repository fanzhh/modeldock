"""模型库 + 下载任务 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.downloads.engine import get_download_engine
from app.db import DownloadTask, ModelFamily, get_db_engine
from app.schemas import FamilyIn, FamilyOut, TaskIn, TaskOut

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/families", response_model=list[FamilyOut])
def list_families():
    with Session(get_db_engine()) as s:
        return s.scalars(select(ModelFamily).order_by(ModelFamily.id)).all()


@router.post("/families", response_model=FamilyOut, status_code=201)
def create_family(body: FamilyIn):
    with Session(get_db_engine()) as s:
        if s.scalars(select(ModelFamily).where(ModelFamily.name == body.name)).first():
            raise HTTPException(409, "family already exists")
        fam = ModelFamily(name=body.name, purpose=body.purpose, notes=body.notes)
        s.add(fam)
        s.commit()
        s.refresh(fam)
        return fam


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(family_id: int | None = None, status: str | None = None):
    q = select(DownloadTask).order_by(DownloadTask.id.desc())
    if family_id:
        q = q.where(DownloadTask.family_id == family_id)
    if status:
        q = q.where(DownloadTask.status == status)
    with Session(get_db_engine()) as s:
        return s.scalars(q).all()


@router.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(body: TaskIn):
    with Session(get_db_engine()) as s:
        fam = s.get(ModelFamily, body.family_id)
        if not fam:
            raise HTTPException(404, "family not found")
        # url 源：repo 留空、filename 必填且形如 https://...
        if body.source == "url" and not body.filename.startswith(("http://", "https://")):
            raise HTTPException(422, "url source requires filename to be a full http(s) URL")
        if body.source != "url" and (not body.repo or not body.filename):
            raise HTTPException(422, "hf/modelscope source requires repo and filename")
        t = DownloadTask(
            family_id=body.family_id, source=body.source, repo=body.repo, filename=body.filename,
            target_remote_path=body.target_remote_path,
        )
        s.add(t)
        s.commit()
        s.refresh(t)
    get_download_engine().enqueue(t.id)
    return t


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: int):
    get_download_engine().cancel(task_id)
    with Session(get_db_engine()) as s:
        t = s.get(DownloadTask, task_id)
        if not t:
            raise HTTPException(404, "task not found")
        s.refresh(t)
        return t
