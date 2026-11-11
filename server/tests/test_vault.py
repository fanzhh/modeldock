"""Vault 测试：加解密轮转、指纹稳定性、权限收紧。"""
from __future__ import annotations

import os

import pytest

from app.core.vault import Vault, VaultError


@pytest.fixture()
def vault(tmp_path):
    return Vault(key_path=tmp_path / "vault.key")


def test_put_get_roundtrip(vault, tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.vault.settings.data_dir", tmp_path)
    fp = vault.put("hf", "main", "hf_token_xxx")
    assert len(fp) == 16
    assert vault.get(fp) == "hf_token_xxx"


def test_fingerprint_stable():
    a = Vault.fingerprint("hf", "main", "tok")
    b = Vault.fingerprint("hf", "main", "tok")
    c = Vault.fingerprint("hf", "other", "tok")
    assert a == b and a != c


def test_key_file_permissions(vault, tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.vault.settings.data_dir", tmp_path)
    vault.put("google", "drive", "refresh_x")
    mode = os.stat(vault.key_path).st_mode & 0o777
    assert mode == 0o600


def test_bad_scope_keyfile_rejected(tmp_path, monkeypatch):
    v = Vault(key_path=tmp_path / "vault.key")
    v.key_path.write_bytes(b"x" * 44)
    os.chmod(v.key_path, 0o644)  # 世界可读 → 必须拒绝
    with pytest.raises(VaultError):
        v.put("hf", "main", "tok")


def test_get_missing(vault):
    with pytest.raises(VaultError):
        vault.get("dead00beefdead00")
