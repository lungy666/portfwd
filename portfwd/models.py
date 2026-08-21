"""数据模型：转发状态、连接与转发的序列化结构。

这里只做"纯数据 + 序列化"，不触碰任何 SSH/IO，方便持久化和测试。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class FwdStatus(str, Enum):
    """转发规则的生命周期状态。"""

    STOPPED = "stopped"      # 已保存但未启动
    STARTING = "starting"    # 正在建立 SSH 监听
    ACTIVE = "active"        # 本地端口已就绪，可接受连接
    ERROR = "error"          # 启动失败或运行中出错
    STOPPING = "stopping"    # 正在关闭


class FwdDirection(str, Enum):
    """转发方向：两端 host:port 的角色随方向相反。

    - FORWARD（正向，默认）：本机 local_host:local_port 是本机**监听**端，
      远程 remote_host:remote_port 是远程**目标**（本机访问服务器端口）。
    - REVERSE（反向）：远程 remote_host:remote_port 是远程**监听**端，
      本机 local_host:local_port 是本机**目标**（服务器访问本机，
      典型用法：服务器经本机 HTTP/SOCKS 代理出网）。
    """

    FORWARD = "forward"
    REVERSE = "reverse"


# 状态到终端显示文案/颜色提示的映射（TUI 渲染用）
STATUS_LABEL: dict[FwdStatus, str] = {
    FwdStatus.ACTIVE: "已激活",
    FwdStatus.STARTING: "启动中",
    FwdStatus.STOPPING: "停止中",
    FwdStatus.ERROR: "错误",
    FwdStatus.STOPPED: "已停止",
}

# 方向到列表/表格展示文案的映射
DIRECTION_LABEL: dict[FwdDirection, str] = {
    FwdDirection.FORWARD: "访问服务器",
    FwdDirection.REVERSE: "服务器访问本机",
}


def is_loopback_host(host: Any) -> bool:
    """反向转发的远程监听端只允许 loopback（127.0.0.1 / localhost）。"""
    return str(host).strip().lower() in ("127.0.0.1", "localhost")


@dataclass
class PortForward:
    """一条 SSH 端口转发规则（支持正向 / 反向两种方向）。

    正向语义对齐 VS Code Remote-SSH：把**远程主机上的端口**通过 SSH 隧道
    暴露到**本机**端口，用本地浏览器访问即可打开远程服务。若 remote_host
    为 "127.0.0.1" 则指向 SSH 会话所在主机本身；若指定其它地址（如远程内网
    某台机器的 IP），则通过 SSH 通道二次转发到该地址（VS Code 的高级用法）。

    反向则相反：远程 127.0.0.1:remote_port 起监听，连进来时经 SSH 回到
    本机 local_host:local_port（如本机代理 127.0.0.1:7890），供服务器使用
    本机代理。反向的远程监听端仅允许 loopback，绝不默认暴露 0.0.0.0。
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
    # 所属连接名（非持久化，由 UI 分配）
    conn: str = ""
    # 方向：旧配置缺省该字段时一律视为正向
    direction: FwdDirection = FwdDirection.FORWARD
    # 本机端地址：正向是本机监听地址（127.0.0.1），反向是本机目标服务地址
    local_host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("转发名称不能为空")
        try:
            self.direction = FwdDirection(self.direction)
        except (TypeError, ValueError) as e:
            raise ValueError(f"未知的转发方向：{self.direction!r}") from e
        self.local_host = str(self.local_host).strip() or "127.0.0.1"
        self.remote_host = str(self.remote_host).strip() or "127.0.0.1"
        if self.direction is FwdDirection.REVERSE:
            # 反向远程监听端仅允许 loopback，且规范化为 127.0.0.1 后持久化
            if not is_loopback_host(self.remote_host):
                raise ValueError(
                    f"反向转发的远程监听地址仅支持 127.0.0.1/localhost，收到：{self.remote_host!r}"
                )
            self.remote_host = "127.0.0.1"
        else:
            if not is_loopback_host(self.local_host):
                raise ValueError(
                    f"正向转发的本机监听地址仅支持 127.0.0.1/localhost，收到：{self.local_host!r}"
                )
            self.local_host = "127.0.0.1"
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
        """正向规则可用的浏览器地址；反向规则没有可打开的本机地址，返回空串。"""
        if self.direction is FwdDirection.REVERSE:
            return ""
        return f"http://{self.local_host}:{self.local_port}"

    @property
    def can_open_in_browser(self) -> bool:
        return self.direction is FwdDirection.FORWARD

    @property
    def address(self) -> str:
        """本机端地址：正向是本机监听端，反向是本机目标端。"""
        return f"{self.local_host}:{self.local_port}"

    @property
    def display_remote(self) -> str:
        """远程端：正向是远程目标，反向是远程监听端。"""
        if self.remote_host in ("127.0.0.1", "localhost", ""):
            return f":{self.remote_port}"
        return f"{self.remote_host}:{self.remote_port}"

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABEL[self.direction]

    @property
    def link(self) -> str:
        """完整链路展示（方向不同，两端含义相反）。"""
        if self.direction is FwdDirection.FORWARD:
            return f"{self.address} → {self.display_remote}"
        return f"{self.display_remote} → {self.address}"

    def to_dict(self) -> dict[str, Any]:
        """持久化用的字典（剔除纯运行期字段）。"""
        d = asdict(self)
        drop = {"error", "connections", "bytes_total", "conn", "local_endpoint"}
        for k in list(d):
            if k in drop:
                d.pop(k)
        d["status"] = self.status.value
        d["direction"] = self.direction.value
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
