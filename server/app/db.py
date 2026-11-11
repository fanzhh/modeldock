"""SQLite 元数据层（SQLAlchemy 2.0）。凭证明文永不出现在这里。"""
from __future__ import annotations

from datetime import datetime, timezone

from pathlib import Path

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ModelFamily(Base):
    """一个模型家族（如 MiniMax-H3、Qwen3-8B）。"""

    __tablename__ = "model_families"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    purpose: Mapped[str] = mapped_column(String(32), default="generic")  # chat/image/video/audio
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    files: Mapped[list["ModelFile"]] = relationship(back_populates="family", cascade="all, delete-orphan")


class ModelFile(Base):
    __tablename__ = "model_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("model_families.id"))
    name: Mapped[str] = mapped_column(String(255))
    remote_path: Mapped[str] = mapped_column(String(1024))  # remote:relative/path
    bytes: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    kind: Mapped[str] = mapped_column(String(32), default="weights")  # weights/vae/tokenizer/other
    verified: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    family: Mapped[ModelFamily] = relationship(back_populates="files")


class DownloadTask(Base):
    """下载任务：源 → 云盘。状态机 queued/running/paused/done/failed/cancelled。"""

    __tablename__ = "download_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("model_families.id"))
    source: Mapped[str] = mapped_column(String(16))  # hf / modelscope / url
    repo: Mapped[str] = mapped_column(String(255), default="")  # org/name
    filename: Mapped[str] = mapped_column(String(255), default="")
    target_remote_path: Mapped[str] = mapped_column(String(1024))  # remote-relative
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[float] = mapped_column(default=0.0)  # 0..1
    done_bytes: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Recipe(Base):
    """部署配方。manifest: {"models": {"role": {"family": ..., "path": ...}}, "ports": [...]}"""

    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind_class: Mapped[str] = mapped_column(String(32), default="generic")  # comfyui/chat/generic
    description: Mapped[str] = mapped_column(Text, default="")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    install_script: Mapped[str] = mapped_column(Text, default="")
    run_script: Mapped[str] = mapped_column(Text, default="")
    ports: Mapped[list] = mapped_column(JSON, default=list)


class Deployment(Base):
    """一次部署（M0 仅记录状态）。状态机见 DESIGN.md §5。"""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    gpu: Mapped[str] = mapped_column(String(16), default="T4")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="queued")
    state_history: Mapped[list] = mapped_column(JSON, default=list)
    logs: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecretRef(Base):
    """凭证引用：明文在 vault（加密文件），库里只存 scope 与名称。"""

    __tablename__ = "secret_refs"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(24))  # google/hf/modelscope/colab
    name: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_engine = None


def get_db_engine():
    """懒加载 engine：允许测试在设置后再指定 sqlite 路径。"""
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{settings.db_path}",
                                 connect_args={"check_same_thread": False})
    return _engine


def set_db_path(path) -> None:
    """切换 sqlite 文件（仅测试/初始引导使用；必须在首次 get_engine 前调用）。

    db_path 是 data_dir 的派生 property，因此只设置 data_dir。
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
    settings.data_dir = Path(path).parent
    settings.download_dir = settings.data_dir / "cache"
    settings.download_dir.mkdir(exist_ok=True)


def init_db() -> None:
    Base.metadata.create_all(get_db_engine())
