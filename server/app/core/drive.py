"""Google Drive rclone 通道。

设计依据（DESIGN.md §3）：不走 Colab drive.mount，本模块是唯一的盘上通道。
- register_remote(): 直接写 rclone drive 段（token JSON 由 OAuth 向导产出）
- import_remote(): 从现有 rclone 配置（如 ~/.config/rclone/rclone.conf）导入 remote
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


def _read_config_parser(path: str | os.PathLike) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if os.path.exists(path):
        parser.read(path)
    return parser


class Drive:
    """单个 rclone remote 的操作集。M0/M1 支持一个托管 remote，常量名 gdrive。"""

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
        parser = _read_config_parser(self.config_path)
        parser[self.remote] = {"type": "drive", **extra}
        with open(self.config_path, "w") as f:
            f.write("# managed by ModelDock; 0600 only\n")
            parser.write(f)
        os.chmod(self.config_path, 0o600)

    def register_refresh_token(self, client_id: str, client_secret: str,
                               refresh_token: str, scope: str = "drive") -> None:
        """OAuth 向导产物 → rclone 配置段。refresh_token 明文只进 0600 配置文件。"""
        token_blob = {
            "access_token": "",
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expiry": "0001-01-01T00:00:00Z",
        }
        self.register_remote({
            "scope": scope,
            "client_id": client_id,
            "client_secret": client_secret,
            "token": json.dumps(token_blob),
        })

    # ---- 导入现有 remote ----
    @staticmethod
    def list_remotes(config_path: str | os.PathLike) -> list[dict]:
        """列出配置文件里的 remote（drive 型标注）。不返回任何 secret 字段值。"""
        parser = _read_config_parser(config_path)
        out = []
        for sec in parser.sections():
            kind = parser.get(sec, "type", fallback="")
            item = {"name": sec, "type": kind}
            if kind == "drive":
                item["has_token"] = parser.has_option(sec, "token")
                item["client_id_tail"] = parser.get(sec, "client_id", fallback="")[-12:]
            out.append(item)
        return out

    def import_remote(self, source_config_path: str, remote_name: str, as_name: str | None = None) -> None:
        """把 source 配置中的 remote_name 段复制进托管配置（可改名，默认托管名）。"""
        src = _read_config_parser(source_config_path)
        if not src.has_section(remote_name):
            raise DriveError(f"remote '{remote_name}' not found in {source_config_path}")
        section = dict(src[remote_name])
        if section.get("type") != "drive":
            raise DriveError(f"remote '{remote_name}' is not a drive remote (type={section.get('type')})")
        merged = {k: v for k, v in section.items() if k != "type"}
        original_remote = self.remote
        try:
            self.remote = as_name or remote_name
            self.register_remote(merged)
        finally:
            self.remote = original_remote
        self.ensure_config_perms()

    def has_remote(self, name: str | None = None) -> bool:
        name = name or self.remote
        parser = _read_config_parser(self.config_path)
        return parser.has_section(name) and parser.get(name, "type", fallback="") == "drive"


    # ---- primitives ----
    def _run(self, *args: str, timeout: int = 60) -> str:
        cmd = [self.bin, *args, f"--config={self.config_path}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise DriveError(f"rclone binary not found: {self.bin}") from e
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
