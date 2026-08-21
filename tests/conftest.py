"""pytest 隔离：每个场景都使用临时 portfwd 配置目录。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    import portfwd.config as config_module

    config_dir = tmp_path / "portfwd"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.setenv("PORTFWD_HOME", str(config_dir))
    monkeypatch.setenv("PORTFWD_DISABLE_KEYCHAIN", "1")
