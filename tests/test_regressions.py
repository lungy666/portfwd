"""回归测试：安全策略、配置权限和运行期状态。

该文件使用标准库 unittest，便于在没有 pytest 的干净环境中执行：
    .venv/bin/python -m unittest discover -s tests -p 'test_regressions.py'
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PORTFWD_DISABLE_KEYCHAIN", "1")

import paramiko


class _HostKeyClient:
    def __init__(self, host_keys: paramiko.HostKeys) -> None:
        self._host_keys = host_keys

    def get_host_keys(self) -> paramiko.HostKeys:
        return self._host_keys


class RegressionTests(unittest.TestCase):
    def test_accept_new_rejects_changed_known_key(self) -> None:
        from portfwd.forwarding import AcceptNewPolicy

        known = paramiko.RSAKey.generate(1024)
        changed = paramiko.RSAKey.generate(1024)
        host_keys = paramiko.HostKeys()
        host_keys.add("example.test", known.get_name(), known)
        client = _HostKeyClient(host_keys)
        policy = AcceptNewPolicy("example.test", 22)

        with self.assertRaises(paramiko.SSHException):
            policy.missing_host_key(client, "example.test", changed)

    def test_accept_new_allows_same_known_key(self) -> None:
        from portfwd.forwarding import AcceptNewPolicy

        known = paramiko.RSAKey.generate(1024)
        host_keys = paramiko.HostKeys()
        host_keys.add("example.test", known.get_name(), known)
        policy = AcceptNewPolicy("example.test", 22)

        policy.missing_host_key(_HostKeyClient(host_keys), "example.test", known)

    def test_config_save_is_atomic_and_private(self) -> None:
        import portfwd.config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_file = config_module.CONFIG_DIR, config_module.CONFIG_FILE
            try:
                config_module.CONFIG_DIR = Path(tmp) / "portfwd"
                config_module.CONFIG_FILE = config_module.CONFIG_DIR / "config.json"
                config = config_module.Config()
                config.add(config_module.ConnectionDef(
                    name="private", host="example.test", password="secret"
                ))
                config.save()

                self.assertEqual(
                    stat.S_IMODE(config_module.CONFIG_FILE.stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE(config_module.CONFIG_DIR.stat().st_mode), 0o700
                )
                data = json.loads(config_module.CONFIG_FILE.read_text())
                self.assertEqual(data["connections"][0]["name"], "private")
                self.assertFalse(list(config_module.CONFIG_DIR.glob(".config.*.tmp")))
            finally:
                config_module.CONFIG_DIR, config_module.CONFIG_FILE = old_dir, old_file

    def test_invalid_config_root_is_ignored(self) -> None:
        import portfwd.config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_file = config_module.CONFIG_DIR, config_module.CONFIG_FILE
            try:
                config_module.CONFIG_DIR = Path(tmp)
                config_module.CONFIG_FILE = Path(tmp) / "config.json"
                config_module.CONFIG_FILE.write_text("[]", encoding="utf-8")
                config = config_module.Config()
                self.assertEqual(config.connections, [])
            finally:
                config_module.CONFIG_DIR, config_module.CONFIG_FILE = old_dir, old_file

    def test_keychain_password_is_not_written_to_json(self) -> None:
        import portfwd.config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_file = config_module.CONFIG_DIR, config_module.CONFIG_FILE
            try:
                config_module.CONFIG_DIR = Path(tmp)
                config_module.CONFIG_FILE = Path(tmp) / "config.json"
                config = config_module.Config()
                config.add(config_module.ConnectionDef(
                    name="keychain", host="example.test", password="secret"
                ))
                with patch.object(config_module.secrets, "available", return_value=True), \
                     patch.object(config_module.secrets, "set", return_value=True) as store:
                    config.save()
                store.assert_called_once_with("keychain", "secret")
                data = json.loads(config_module.CONFIG_FILE.read_text())
                self.assertEqual(data["connections"][0]["password"], "")
            finally:
                config_module.CONFIG_DIR, config_module.CONFIG_FILE = old_dir, old_file

    def test_load_pkey_reads_modern_openssh_key(self) -> None:
        from portfwd.config import load_pkey

        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "id_rsa"
            expected = paramiko.RSAKey.generate(1024)
            expected.write_private_key_file(str(key_path))
            loaded = load_pkey(str(key_path))
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.get_name(), "ssh-rsa")

    def test_ssh_config_port_precedence(self) -> None:
        from portfwd.config import ssh_config_fill

        with tempfile.TemporaryDirectory() as tmp:
            ssh_dir = Path(tmp) / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "config").write_text(
                "Host alias\n  HostName example.test\n  Port 2201\n  User tester\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": tmp}, clear=False):
                resolved = ssh_config_fill("alias", port=0)
                explicit = ssh_config_fill("alias", port=2202)
            self.assertEqual(resolved["port"], 2201)
            self.assertEqual(explicit["port"], 2202)


if __name__ == "__main__":
    unittest.main()
