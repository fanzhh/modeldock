"""API schema（pydantic v2）。约束：凭证字段只写不读。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---- accounts / secrets ----
class SecretIn(BaseModel):
    scope: str = Field(pattern="^(google|hf|modelscope|colab)$")
    name: str = Field(min_length=1, max_length=64)
    secret: str = Field(min_length=1, max_length=8192)
    meta: dict = Field(default_factory=dict)


class SecretOut(BaseModel):
    id: int
    scope: str
    name: str
    fingerprint: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HealthOut(BaseModel):
    ok: bool
    drive: dict
    version: str = "0.1.0"


# ---- models ----
class FamilyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="generic", pattern="^(chat|image|video|audio|generic)$")
    notes: str = ""


class FamilyOut(BaseModel):
    id: int
    name: str
    purpose: str
    notes: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaskIn(BaseModel):
    family_id: int
    source: str = Field(pattern="^(hf|modelscope|url)$")
    repo: str = Field(default="", max_length=255)
    filename: str = Field(default="", max_length=255)
    target_remote_path: str = Field(min_length=1, max_length=1024)


class TaskOut(BaseModel):
    id: int
    family_id: int
    source: str
    repo: str
    filename: str
    target_remote_path: str
    status: str
    progress: float
    done_bytes: int
    total_bytes: int
    error: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---- recipes / deployments ----
class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind_class: str = Field(default="generic", pattern="^(comfyui|chat|generic)$")
    description: str = ""
    manifest: dict = Field(default_factory=dict)
    install_script: str = ""
    run_script: str = ""
    ports: list[int] = Field(default_factory=list)


class RecipeOut(BaseModel):
    id: int
    name: str
    kind_class: str
    description: str
    manifest: dict
    ports: list

    model_config = ConfigDict(from_attributes=True)


class DeploymentIn(BaseModel):
    recipe_id: int
    gpu: str = Field(default="T4", pattern="^(T4|L4|A100)$")
    params: dict = Field(default_factory=dict)


class DeploymentOut(BaseModel):
    id: int
    recipe_id: int
    gpu: str
    state: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
