"""ModelDock 全局配置。环境变量前缀 MODELDOCK_。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"MODELDOCK_{name.upper()}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("data_dir", "~/.modeldock")).expanduser())
    host: str = field(default_factory=lambda: _env("host", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("port", "8820")))
    # 仅允许本机访问；显式警告外部暴露
    allow_remote: bool = field(default_factory=lambda: _env("allow_remote", "0") == "1")
    rclone_bin: str = field(default_factory=lambda: _env("rclone_bin", "rclone"))
    hf_endpoint: str = field(default_factory=lambda: _env("hf_endpoint", "https://huggingface.co"))
    modelscope_endpoint: str = field(default_factory=lambda: _env("modelscope_endpoint", "https://modelscope.cn"))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not hasattr(self, "download_dir") or not self.download_dir:
            self.download_dir = self.data_dir / "cache"
        self.download_dir.mkdir(exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "modeldock.db"

    @property
    def vault_key_path(self) -> Path:
        return self.data_dir / "vault.key"

    @property
    def rclone_config_path(self) -> Path:
        return self.data_dir / "rclone.conf"

    @property
    def drive_prefix(self) -> str:
        """云盘内模型根目录（rclone remote:path 形式中的 path 部分）。"""
        return _env("drive_prefix", "modeldock/models")

    @property
    def max_parallel_downloads(self) -> int:
        return int(_env("max_parallel_downloads", "2"))

    @property
    def idle_timeout_min(self) -> float:
        """自动释放：ready 部署空闲 N 分钟后释放（部署 params.idle_timeout_min 优先；
        0 = 关闭）。"""
        return float(_env("idle_timeout_min", "0"))


settings = Settings()
