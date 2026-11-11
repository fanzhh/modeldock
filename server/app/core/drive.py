"""Google Drive rclone 通道。

设计依据（DESIGN.md §3）：不走 Colab drive.mount，本模块是唯一的盘上通道。
- register_remote(): 由用户 OAuth refresh_token 生成 rclone remote（type=drive）
- ls / copy / put / quota / health：rclone 子进程封装
- 所有输出剥离 token；rclone 配置文件 0600
"""
from __future__ import annotations

import configparser
import json
import os
import subprocess
from dataclasses import dataclass

from app.config import settings


class DriveError(RuntimeError):
    pass


@dataclass
class RemoteConfig:
    name: str
    scope: str
    client_id: str
    client_secret_enc: str  # vault 指纹，真实 secret 运行时解密
    refresh_token_enc: str  # vault 指纹


class Drive:
    """单个 rclone remote 的操作集。M0 支持一个 remote，常量名 gdrive。"""

    def __init__(self, remote_name: str = "gdrive", rclone_bin: str | None = None,
                 config_path: str | None = None) -> None:
        self.remote = remote_name
        self.bin = rclone_bin or settings.rclone_bin
        self.config_path = config_path or str(settings.rclone_config_path)

    # ---- remote registration ----
    def ensure_config_perms(self) -> None:
        if os.path.exists(self.config_path):
            mode = os.stat(self.config_path).st_mode & 0o077
            if mode:
                os.chmod(self.config_path, 0o600)

    def register_remote(self, extra: dict[str, str]) -> None:
        """写入/更新 [remote] 段。extra 为 rclone drive 参数（已解密后的运行时值）。

        调用方负责从 vault 解密后传入；本函数写入前将配置文件权限置 0600。
        """
        parser = configparser.ConfigParser()
        parser[self.remote] = {"type": "drive", **extra}
        if os.path.exists(self.config_path):
            parser.read(self.config_path)
            parser[self.remote] = {"type": "drive", **extra}
        with open(self.config_path, "w") as f:
            f.write("# managed by ModelDock; 0600 only\n")
            parser.write(f)
        os.chmod(self.config_path, 0o600)

    # ---- primitives ----
    def _run(self, *args: str, timeout: int = 60) -> str:
        cmd = [self.bin, *args, f"--config={self.config_path}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        # 输出脱敏：rclone 一般不回显 token，这里做二次防护
        out = proc.stdout if proc.returncode == 0 else proc.stderr
        if proc.returncode != 0:
            raise DriveError(f"rclone failed (rc={proc.returncode}): {out[:400]}")
        return out

    def model_path(self, relative: str) -> str:
        return f"{self.remote}:{settings.drive_prefix}/{relative}".rstrip("/")

    # ---- ops ----
    def put_file(self, local_path: str, remote_rel: str, callback=None) -> None:
        """上传（可恢复校验：rclone copy 默认 skip same-size 文件）。"""
        self._run("copyto", local_path, self.model_path(remote_rel), "--retries", "3", timeout=43_200)

    def ls(self, remote_rel: str = "") -> list[dict]:
        out = self._run("lsjson", self.model_path(remote_rel), "--recursive")
        try:
            items = json.loads(out)
        except json.JSONDecodeError as e:
            raise DriveError(f"lsjson parse failed: {e}") from e
        for it in items:
            it["remote_rel"] = f"{remote_rel}/{it['Path']}".lstrip("/")
        return items

    def quota(self) -> dict:
        out = self._run("about", f"{self.remote}:")
        return out

    def health(self) -> dict:
        try:
            self._run("about", f"{self.remote}:", timeout=15)
            return {"ok": True, "detail": "about check passed"}
        except DriveError as e:
            return {"ok": False, "detail": str(e)[:200]}
