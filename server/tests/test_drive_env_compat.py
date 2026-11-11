"""环境兼容性测试：rclone 未安装的机器（如全新 VPS）上错误要收敛为 DriveError。"""
from __future__ import annotations

import pytest

from app.core.drive import Drive, DriveError


def test_missing_rclone_binary_raises_drive_error(tmp_path):
    d = Drive(config_path=str(tmp_path / "rclone.conf"),
              rclone_bin=str(tmp_path / "no-such-rclone"))
    with pytest.raises(DriveError, match="rclone binary not found"):
        d._run("about", "gdrive:")


def test_health_says_not_found_without_rclone(tmp_path):
    d = Drive(config_path=str(tmp_path / "rclone.conf"),
              rclone_bin=str(tmp_path / "no-such-rclone"))
    health = d.health()
    assert health["ok"] is False
    assert "rclone binary not found" in health["detail"]
