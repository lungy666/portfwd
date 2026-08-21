"""配置持久化 + ~/.ssh/config 读取。

- Config：把"连接定义 + 每条连接的转发列表"存到 ~/.portfwd/config.json
- ssh_config_fill：把用户输入的别名/主机，用 paramiko 内置的 SSHConfig
  解析出 host / port / user / key，等价于命令行 `ssh` 的行为，
  用户无需重复配置密钥路径。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import paramiko.config
from paramiko.pkey import PKey

import logging

from . import secrets

log = logging.getLogger("portfwd")


CONFIG_DIR = Path(os.environ.get("PORTFWD_HOME", str(Path.home() / ".portfwd")))
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class ConnectionDef:
    """一个可保存的 SSH 连接（相当于 VS Code Remote-SSH 的远端主机）。"""

    name: str            # 在左侧列表里显示的名字
    host: str            # host 或 ~/.ssh/config 里的别名
    user: str = ""       # 留空则走 ssh config / 当前用户
    port: int = 22
    identity_file: str = ""
    # 认证方式：auto / key / password
    #   auto     - 先试 agent/默认密钥/identity_file，不行再要密码
    #   key      - 只用密钥
    #   password - 只用密码（仍允许 agent）
    auth: str = "auto"
    password: str = ""   # 可选：明文保存的密码（文件权限 0600）
    port_forwards: list[dict[str, Any]] = field(default_factory=list)
    # [{name,local_port,remote_host,remote_port}]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.host, str):
            raise ValueError("连接名称和主机必须是文本")
        self.name = self.name.strip()
        self.host = self.host.strip()
        if not self.name or not self.host:
            raise ValueError("连接名称和主机不能为空")
        try:
            self.port = int(self.port)
        except (TypeError, ValueError) as e:
            raise ValueError("SSH 端口无效") from e
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH 端口超出范围")
        if self.auth not in {"auto", "key", "password"}:
            self.auth = "auto"
        if not isinstance(self.port_forwards, list):
            self.port_forwards = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "identity_file": self.identity_file,
            "auth": self.auth,
            "password": self.password,
            "port_forwards": self.port_forwards,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConnectionDef":
        """从字典构建；忽略未知字段。缺少 name/host 等必需字段时抛 TypeError。"""
        if not isinstance(raw, dict):
            raise TypeError("连接配置必须是对象")
        raw = dict(raw)
        known = {f.name for f in fields(cls)}
        for k in list(raw):
            if k not in known:
                raw.pop(k)
        raw.setdefault("port_forwards", [])
        return cls(**raw)


class Config:
    """极简 JSON 配置容器。"""

    def __init__(self) -> None:
        self.connections: list[ConnectionDef] = []
        self.autostart: bool = True  # 启动时自动恢复/启动已保存的转发
        self._load()

    def _load(self) -> None:
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            log.warning("配置文件根节点不是对象，忽略配置")
            return
        self.autostart = bool(data.get("autostart", True))
        # 逐条容错：个别损坏的条目跳过而不是让整个应用起不来
        connections = data.get("connections", [])
        if not isinstance(connections, list):
            log.warning("配置中的 connections 不是列表，忽略连接配置")
            return
        for c in connections:
            if not isinstance(c, dict):
                log.warning("跳过损坏的连接配置条目: %r", c)
                continue
            try:
                conn = ConnectionDef.from_dict(c)
                if not conn.password:
                    keychain_password = secrets.get(conn.name)
                    if keychain_password:
                        conn.password = keychain_password
                self.connections.append(conn)
            except (TypeError, ValueError):
                log.warning("跳过缺少必需字段(name/host)的连接配置: %r", c)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass
        connections = []
        for conn in self.connections:
            item = conn.to_dict()
            if secrets.available():
                if conn.password and secrets.set(conn.name, conn.password):
                    item["password"] = ""
                elif not conn.password:
                    secrets.delete(conn.name)
                else:
                    log.warning("Keychain 保存失败，保留 0600 配置文件中的密码: %s", conn.name)
            connections.append(item)
        data = {
            "autostart": self.autostart,
            "connections": connections,
        }
        try:
            # mkstemp 创建时即为 0600，随后原子替换，避免明文配置短暂暴露。
            fd, tmp_name = tempfile.mkstemp(
                prefix=".config.", suffix=".tmp", dir=str(CONFIG_DIR)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, CONFIG_FILE)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            log.exception("保存配置失败: %s", CONFIG_FILE)
            raise

    def find(self, name: str) -> Optional[ConnectionDef]:
        for c in self.connections:
            if c.name == name:
                return c
        return None

    def add(self, conn: ConnectionDef) -> None:
        self.connections.append(conn)

    def remove(self, name: str) -> None:
        if secrets.available():
            secrets.delete(name)
        self.connections = [c for c in self.connections if c.name != name]


def ssh_config_fill(host: str, user: str = "", port: int = 0,
                    identity_file: str = "") -> dict[str, Any]:
    """用 ~/.ssh/config 补全连接参数。

    返回 dict：host / port / user / identity_file（均为解析后的最终值）。
    用户显式传入的 user/port/identity_file 优先级高于 ssh config。
    """
    out: dict[str, Any] = {
        "host": host,
        "port": port or 22,
        "user": user,
        "identity_file": identity_file,
    }
    try:
        sshcfg = paramiko.config.SSHConfig()
        # paramiko 会默认读 ~/.ssh/config；这里显式再指一次以防 HOME 变化
        path = Path.home() / ".ssh" / "config"
        if path.exists():
            import io
            sshcfg.parse(io.StringIO(path.read_text(encoding="utf-8")))
        options = sshcfg.lookup(host)
    except Exception:
        return out

    merged_host = options.get("hostname")
    if merged_host:
        out["host"] = merged_host
    # ConnectionDef historically stores the UI default (22), so treat 22 as
    # unspecified for SSH aliases while still honoring every non-default port.
    if (not port or port == 22) and options.get("port"):
        try:
            parsed_port = int(options["port"])
            if 1 <= parsed_port <= 65535:
                out["port"] = parsed_port
        except (TypeError, ValueError):
            log.warning("忽略 SSH config 中的无效端口: %r", options.get("port"))
    # user 优先级：显式传入 > ssh config > 当前系统用户
    if not out["user"]:
        out["user"] = options.get("user") or os.environ.get("USER", "")
    # 密钥：显式传入的优先
    if not out["identity_file"] and options.get("identityfile"):
        out["identity_file"] = options["identityfile"][0]
    return out


def load_pkey(identity_file: str) -> Optional[PKey]:
    """尝试从 identity_file 加载私钥；失败返回 None。"""
    if not identity_file:
        return None
    path = Path(os.path.expanduser(identity_file))
    if not path.exists():
        return None
    try:
        # Paramiko 4 supports the modern OpenSSH private-key container through
        # from_path(); the legacy PKey class method cannot parse it reliably.
        from_path = getattr(PKey, "from_path", None)
        if from_path is not None:
            return from_path(str(path))
        return PKey.from_private_key_file(str(path))
    except Exception:
        return None
