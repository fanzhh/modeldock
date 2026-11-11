"""内置配方（DESIGN.md §4.3）：llama.cpp 聊天 + ComfyUI。

- seed 幂等：按 name upsert；用户改过的内置配方不会被覆盖（只在缺失时插入）
- manifest.models.<role>.family 支持模板 "$<key>"：部署时由
  Deployment.params.families[key] 提供真实家族名（内置配方默认走模板）
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Recipe, get_db_engine

log = logging.getLogger("modeldock.builtin")


BUILTIN_RECIPES: list[dict] = [
    {
        "name": "builtin/llamacpp-chat",
        "kind_class": "chat",
        "description": "llama.cpp llama-server：任意 GGUF 聊天模型，OpenAI 兼容 /v1 "
                       "API（Chat 面板直接对话；部署时 families.chat 指定家族）",
        "manifest": {
            "builtin": True,
            "models": {"chat": {"family": "$chat"}},
            "ports": [8080],
        },
        "install_script": (
            "set -e\n"
            "apt-get -qq update && apt-get -qq install -y build-essential cmake curl libcurl4-openssl-dev\n"
            "git clone --depth 1 https://github.com/ggml-org/llama.cpp /content/llama.cpp\n"
            "cmake -S /content/llama.cpp -B /content/llama.cpp/build -DGGML_CUDA=ON "
            "-DCMAKE_BUILD_TYPE=Release\n"
            "cmake --build /content/llama.cpp/build --target llama-server -j 2"
        ),
        "run_script": (
            "GGUF=$(find /content/models -name '*.gguf' | head -1)\n"
            "echo \"[llamacpp] serving $GGUF\"\n"
            "'/content/llama.cpp/build/bin/llama-server' -m \"$GGUF\" "
            "--host 0.0.0.0 --port 8080 --n-gpu-layers 999 --ctx-size 8192"
        ),
        "ports": [8080],
    },
    {
        "name": "builtin/comfyui",
        "kind_class": "comfyui",
        "description": "ComfyUI（含 GGUF/视频节点支持）：本地浏览器经隧道访问 8188；"
                       "部署时 families.checkpoint 指定图像/视频模型家族",
        "manifest": {
            "builtin": True,
            "models": {"checkpoint": {"family": "$checkpoint"}},
            "ports": [8188],
        },
        "install_script": (
            "set -e\n"
            "git clone --depth 1 https://github.com/comfyanonymous/ComfyUI /content/ComfyUI\n"
            "pip -q install torch torchvision --index-url "
            "https://download.pytorch.org/whl/cu124\n"
            "pip -q install -r /content/ComfyUI/requirements.txt\n"
            "# GGUF / 视频常用节点\n"
            "git clone --depth 1 https://github.com/city96/ComfyUI-GGUF "
            "/content/ComfyUI/custom_nodes/ComfyUI-GGUF\n"
            "git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite "
            "/content/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite"
        ),
        "run_script": (
            "cd /content/ComfyUI\n"
            "mkdir -p models/checkpoints\n"
            "find /content/models -name '*.safetensors' -o -name '*.gguf' | "
            "while read f; do ln -sf \"$f\" models/checkpoints/; done\n"
            "python main.py --listen 0.0.0.0 --port 8188 "
            "--output-directory /content/outputs"
        ),
        "ports": [8188],
    },
]


def seed_builtin_recipes() -> int:
    """启动时调用：只插入缺失的内置配方（不覆盖用户改名/改脚本的副本）。"""
    added = 0
    with Session(get_db_engine()) as s:
        for spec in BUILTIN_RECIPES:
            exists = s.scalars(select(Recipe).where(Recipe.name == spec["name"])).first()
            if exists:
                continue
            s.add(Recipe(**spec))
            added += 1
        s.commit()
    if added:
        log.info("seeded %d builtin recipes", added)
    return added
