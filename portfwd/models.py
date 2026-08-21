"""数据模型：转发状态、连接与转发的序列化结构。

这里只做"纯数据 + 序列化"，不触碰任何 SSH/IO，方便持久化和测试。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Optional


class FwdStatus(str, Enum):
    """转发规则的生命周期状态。"""

    STOPPED = "stopped"      # 已保存但未启动
    STARTING = "starting"    # 正在建立 SSH 监听
    ACTIVE = "active"        # 本地端口已就绪，可接受连接
    ERROR = "error"          # 启动失败或运行中出错
    STOPPING = "stopping"    # 正在关闭


# 状态到终端显示文案/颜色提示的映射（TUI 渲染用）
STATUS_LABEL: dict[FwdStatus, str] = {
    FwdStatus.ACTIVE: "已激活",
    FwdStatus.STARTING: "启动中",
    FwdStatus.STOPPING: "停止中",
    FwdStatus.ERROR: "错误",
    FwdStatus.STOPPED: "已停止",
}


@dataclass
class PortForward:
    """一条 SSH 远程端口转发规则。

    语义对齐 VS Code Remote-SSH：把**远程主机上的端口**通过 SSH 隧道
    暴露到**本机**端口，用本地浏览器访问即可打开远程服务。

    若 remote_host 为 "127.0.0.1" 则指向 SSH 会话所在主机本身；
    若指定其它地址（如远程内网某台机器的 IP），则通过 SSH 通道
    二次转发到该地址（VS Code 的高级用法）。
    """

    id: str
    name: str
    local_port: int
    remote_host: str
    remote_port: int
    status: FwdStatus = FwdStatus.STOPPED
    error: str = ""
    connections: int = 0
    bytes_total: int = 0
    # 所属连接名（非持久化，由 TUI 分配）
    conn: str = ""

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        self.name = str(self.name).strip()
        self.remote_host = str(self.remote_host).strip() or "127.0.0.1"
        if not self.name:
            raise ValueError("转发名称不能为空")
        try:
            self.local_port = int(self.local_port)
            self.remote_port = int(self.remote_port)
        except (TypeError, ValueError) as e:
            raise ValueError("转发端口无效") from e
        if not 1 <= self.local_port <= 65535:
            raise ValueError("本地端口超出范围")
        if not 1 <= self.remote_port <= 65535:
            raise ValueError("远程端口超出范围")
        if isinstance(self.status, str):
            try:
                self.status = FwdStatus(self.status)
            except ValueError:
                self.status = FwdStatus.STOPPED

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    @property
    def address(self) -> str:
        """表格首列展示的本地访问地址。"""
        return f"127.0.0.1:{self.local_port}"

    @property
    def display_remote(self) -> str:
        if self.remote_host in ("127.0.0.1", "localhost", ""):
            return f":{self.remote_port}"
        return f"{self.remote_host}:{self.remote_port}"

    def to_dict(self) -> dict[str, Any]:
        """持久化用的字典（剔除纯运行期字段）。"""
        d = asdict(self)
        drop = {"error", "connections", "bytes_total", "conn", "local_endpoint"}
        for k in list(d):
            if k in drop:
                d.pop(k)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PortForward":
        if not isinstance(raw, dict):
            raise TypeError("转发配置必须是对象")
        raw = dict(raw)
        status = raw.pop("status", FwdStatus.STOPPED.value)
        try:
            raw["status"] = FwdStatus(status)
        except ValueError:
            raw["status"] = FwdStatus.STOPPED
        # 兼容旧配置里可能出现的多余字段
        known = {f for f in cls.__dataclass_fields__}
        for k in list(raw):
            if k not in known:
                raw.pop(k)
        return cls(**raw)

    def label(self) -> str:
        """转发目标的简短展示，例如 :3000 / 10.0.0.5:8080。"""
        if self.remote_host in ("127.0.0.1", "localhost", ""):
            return f":{self.remote_port}"
        return f"{self.remote_host}:{self.remote_port}"


def human_bytes(n: float) -> str:
    """字节数 -> 人类可读（TUI/GUI 共用）。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"
