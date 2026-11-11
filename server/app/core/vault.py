"""凭证保险箱：Fernet 对称加密，密钥文件仅本机 0600。

- 明文 token / refresh_token 只存在于 vault 文件（加密）与进程内存
- DB 的 SecretRef 表只保存 scope、名称与指纹（sha256 前 16 字节）
- 日志与 API 输出一律不包含明文
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401  (re-export)

from app.config import settings


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, key_path: Path | None = None) -> None:
        self.key_path = key_path or settings.vault_key_path
        self._fernet: Fernet | None = None

    # ---- key management ----
    def _ensure_key(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        path = self.key_path
        if not path.exists():
            key = Fernet.generate_key()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入 + 0600，避免短暂窗口内密钥可读
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(key)
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        if path.stat().st_mode & 0o077:
            raise VaultError(f"vault key permissions too open: {path} ({oct(path.stat().st_mode)})")
        self._fernet = Fernet(path.read_bytes())
        return self._fernet

    @property
    def _blob_dir(self) -> Path:
        return settings.data_dir / "vault"

    # ---- api ----
    def put(self, scope: str, name: str, secret: str) -> str:
        """存入一条凭证，返回指纹。同 scope/name 覆盖旧条目。"""
        f = self._ensure_key()
        self._blob_dir.mkdir(exist_ok=True)
        fp = self.fingerprint(scope, name, secret)
        target = self._blob_dir / f"{fp}.vault"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(f.encrypt(secret.encode()))
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        return fp

    def get(self, fp: str) -> str:
        f = self._ensure_key()
        target = self._blob_dir / f"{fp}.vault"
        if not target.exists():
            raise VaultError(f"secret not found: fp={fp}")
        return f.decrypt(target.read_bytes()).decode()

    def delete(self, fp: str) -> bool:
        target = self._blob_dir / f"{fp}.vault"
        if target.exists():
            target.unlink()
            return True
        return False

    @staticmethod
    def fingerprint(scope: str, name: str, secret: str) -> str:
        h = hashlib.sha256()
        h.update(scope.encode())
        h.update(b"\x00")
        h.update(name.encode())
        h.update(b"\x00")
        h.update(secret.encode())
        return h.hexdigest()[:16]


vault = Vault()
