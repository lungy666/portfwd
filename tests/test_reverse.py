"""反向转发测试：数据模型兼容/loopback 安全校验、引擎生命周期
（FakeTransport/FakeChannel + 本机 echo，不依赖外部服务器）、
request/cancel 调用、启动失败回滚、停止清理与竞态、方向分派。"""
import queue
import socket
import threading
import time

import paramiko
import pytest

from portfwd.config import ConnectionDef
from portfwd.forwarding import Session, open_forward_for_session
from portfwd.models import FwdDirection, PortForward


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
def test_old_config_defaults_to_forward():
    fwd = PortForward.from_dict({
        "id": "a1", "name": "web", "local_port": 8080,
        "remote_host": "10.0.0.5", "remote_port": 9000,
    })
    assert fwd.direction is FwdDirection.FORWARD
    assert fwd.local_host == "127.0.0.1"
    assert fwd.address == "127.0.0.1:8080"
    assert fwd.display_remote == "10.0.0.5:9000"
    assert fwd.url == "http://127.0.0.1:8080"
    assert fwd.can_open_in_browser is True


def test_reverse_serialization_roundtrip():
    fwd = PortForward(
        id="r1", name="proxy", local_port=7890,
        remote_host="localhost", remote_port=17890,
        direction="reverse", local_host="127.0.0.1",
    )
    assert fwd.remote_host == "127.0.0.1", "反向监听端应规范化为 127.0.0.1"
    d = fwd.to_dict()
    assert d["direction"] == "reverse"
    assert d["local_host"] == "127.0.0.1"
    for runtime in ("error", "connections", "bytes_total", "conn"):
        assert runtime not in d, f"运行态字段 {runtime} 不应持久化"
    fwd2 = PortForward.from_dict(d)
    assert fwd2.direction is FwdDirection.REVERSE
    assert fwd2.local_host == "127.0.0.1"
    assert (fwd2.local_port, fwd2.remote_host, fwd2.remote_port) == (7890, "127.0.0.1", 17890)
    assert fwd2.link == ":17890 → 127.0.0.1:7890"
    assert fwd2.url == "", "反向规则没有可打开的本机地址"
    assert fwd2.can_open_in_browser is False


def test_direction_labels_and_link():
    f = PortForward(id="f", name="web", local_port=8080,
                    remote_host="10.0.0.5", remote_port=3000)
    assert f.direction_label == "访问服务器"
    assert f.link == "127.0.0.1:8080 → 10.0.0.5:3000"
    r = PortForward(id="r", name="p", local_port=7890,
                    remote_host="127.0.0.1", remote_port=17890,
                    direction=FwdDirection.REVERSE)
    assert r.direction_label == "服务器访问本机"
    assert r.link == ":17890 → 127.0.0.1:7890"


def test_reverse_bind_loopback_only():
    for bad in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(ValueError):
            PortForward(id="x", name="x", local_port=1,
                        remote_host=bad, remote_port=2, direction="reverse")
    # 正向规则不受 loopback 限制（远程目标可以是任意地址）
    PortForward(id="y", name="y", local_port=1,
                remote_host="0.0.0.0", remote_port=2)
    # 缺少 direction 才兼容为正向；非法值不能静默改变规则语义
    for bad_direction in ("sideways", None):
        with pytest.raises(ValueError):
            PortForward.from_dict({
                "id": "z", "name": "z", "local_port": 1,
                "remote_host": "127.0.0.1", "remote_port": 2,
                "direction": bad_direction,
            })
    with pytest.raises(ValueError):
        PortForward(id="local-public", name="unsafe", local_port=1,
                    local_host="0.0.0.0", remote_host="127.0.0.1", remote_port=2)


# ---------------------------------------------------------------------------
# 引擎：FakeTransport / FakeChannel + 本机 echo
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self, transport):
        self._t = transport

    def get_transport(self):
        return self._t

    def close(self):
        pass


class FakeChannel:
    """把一个 socket 端伪装成 paramiko Channel（模拟服务器侧）。"""

    def __init__(self, sock):
        self.sock = sock
        self.closed = False

    def sendall(self, data):
        self.sock.sendall(data)

    def recv(self, n):
        return self.sock.recv(n)

    def shutdown_write(self):
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class FakeTransport:
    """模拟 tcpip-forward：记录 request/cancel，deliver() 触发 handler。"""

    def __init__(self, refuse: bool = False):
        self.refuse = refuse
        self.requested: list[tuple[str, int]] = []
        self.cancelled: list[tuple[str, int]] = []
        self.global_requests: list[tuple[str, tuple[str, int], bool]] = []
        self._handler = None

    def is_active(self):
        return True

    def is_authenticated(self):
        return True

    def request_port_forward(self, address, port, handler=None):
        if self.refuse:
            raise paramiko.SSHException("TCP forwarding request denied")
        self.requested.append((address, int(port)))
        self._handler = handler
        return int(port)

    def cancel_port_forward(self, address, port):
        self.cancelled.append((address, int(port)))
        self._handler = None

    def global_request(self, kind, data, wait=True):
        self.global_requests.append((kind, tuple(data), wait))
        return True

    def deliver(self, channel, dest, origin=("10.0.0.9", 54321)):
        if self._handler is None:
            channel.close()
            return
        self._handler(channel, origin, dest)


def start_local_echo(ready: threading.Event):
    """本机目标端 echo 服务：收到啥原样返回。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))

    def run():
        srv.listen(16)
        ready.set()
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_echo, args=(c,), daemon=True).start()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return srv


def _echo(sock):
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                return
            sock.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def make_session(transport) -> Session:
    conn = ConnectionDef(name="fake", host="127.0.0.1")
    session = Session(conn, queue.Queue())
    session.client = FakeClient(transport)
    return session


def recv_until(sock, n: int, timeout: float = 5.0) -> bytes:
    got = b""
    deadline = time.monotonic() + timeout
    while len(got) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        chunk = sock.recv(4096)
        if not chunk:
            break
        got += chunk
    return got


# ---------------------------------------------------------------------------
# 引擎：反向转发数据通路
# ---------------------------------------------------------------------------
def test_reverse_echo_bidirectional_and_cancel():
    ready = threading.Event()
    srv = start_local_echo(ready)
    assert ready.wait(5), "echo 服务没起来"
    echo_port = srv.getsockname()[1]
    transport = FakeTransport()
    session = make_session(transport)

    session.open_reverse_forward_blocking(
        "rev1", "127.0.0.1", 17890, "127.0.0.1", echo_port)
    assert transport.requested == [("127.0.0.1", 17890)]
    assert "rev1" in session.running_forwards()

    # 模拟服务器侧经 SSH 打进来的一条 forwarded-tcpip 连接
    a, b = socket.socketpair()
    ch = FakeChannel(a)
    transport.deliver(ch, ("127.0.0.1", 17890))
    payload = b"hello reverse \r\n"
    b.sendall(payload)
    got = recv_until(b, len(payload))
    assert got == payload, f"echo 内容不符: {got!r}"

    time.sleep(0.15)
    active, total = session.traffic("rev1")
    assert active >= 1, "应有活跃连接"
    assert total >= len(payload) * 2, "字节计数应含双向"
    f = session._forwards["rev1"]
    with f.pumps_lock:
        assert f.sockets and all(sock.gettimeout() is None for sock in f.sockets), \
            "连接超时只应用于建连阶段，已建立的隧道必须恢复阻塞模式"

    b.close()
    deadline = time.monotonic() + 5
    while session.traffic("rev1")[0] != 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert session.traffic("rev1")[0] == 0, "连接关闭后活跃数应为 0"

    # 停止：取消远端监听 + 从 _forwards 移除
    session.close_forward_blocking("rev1")
    assert transport.cancelled == [("127.0.0.1", 17890)]
    assert "rev1" not in session.running_forwards()
    srv.close()


def test_reverse_local_unreachable_keeps_rule_active():
    # 本机目标端口上没有任何服务
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    dead_port = free.getsockname()[1]
    free.close()
    transport = FakeTransport()
    session = make_session(transport)

    session.open_reverse_forward_blocking(
        "rev2", "127.0.0.1", 17891, "127.0.0.1", dead_port)

    a, b = socket.socketpair()
    ch = FakeChannel(a)
    transport.deliver(ch, ("127.0.0.1", 17891))
    deadline = time.monotonic() + 5
    while not ch.closed and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ch.closed, "本机目标不可达时应关闭该 channel"
    # 监听规则保持 active（只是这一条连接失败）
    assert "rev2" in session.running_forwards()
    assert session.traffic("rev2") == (0, 0)
    b.close()
    session.close_forward_blocking("rev2")
    assert transport.cancelled == [("127.0.0.1", 17891)]


def test_reverse_stop_closes_late_channel():
    ready = threading.Event()
    srv = start_local_echo(ready)
    assert ready.wait(5)
    echo_port = srv.getsockname()[1]
    transport = FakeTransport()
    session = make_session(transport)

    session.open_reverse_forward_blocking(
        "rev3", "127.0.0.1", 17892, "127.0.0.1", echo_port)
    old_handler = transport._handler
    session.close_forward_blocking("rev3")

    # 停止之后才到达的 channel（竞态）必须被立即关闭
    a, b = socket.socketpair()
    ch = FakeChannel(a)
    old_handler(ch, ("10.0.0.9", 54321), ("127.0.0.1", 17892))
    deadline = time.monotonic() + 5
    while not ch.closed and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ch.closed, "停止后新到的 channel 应立即关闭"
    b.close()
    srv.close()


def test_cancelling_one_reverse_keeps_other_handler_active():
    ready = threading.Event()
    srv = start_local_echo(ready)
    assert ready.wait(5)
    echo_port = srv.getsockname()[1]
    transport = FakeTransport()
    session = make_session(transport)

    session.open_reverse_forward_blocking(
        "first", "127.0.0.1", 17901, "127.0.0.1", echo_port)
    session.open_reverse_forward_blocking(
        "second", "127.0.0.1", 17902, "127.0.0.1", echo_port)
    session.close_forward_blocking("first")

    assert transport.cancelled == []
    assert transport.global_requests == [
        ("cancel-tcpip-forward", ("127.0.0.1", 17901), True)
    ]
    assert transport._handler is not None

    a, b = socket.socketpair()
    transport.deliver(FakeChannel(a), ("127.0.0.1", 17902))
    payload = b"second rule still works"
    b.sendall(payload)
    assert recv_until(b, len(payload)) == payload
    b.close()

    session.close_forward_blocking("second")
    assert transport.cancelled == [("127.0.0.1", 17902)]
    srv.close()


def test_reverse_duplicate_bind_rejected_by_engine():
    session = make_session(FakeTransport())
    session.open_reverse_forward_blocking(
        "first", "127.0.0.1", 17903, "127.0.0.1", 7890)
    with pytest.raises(RuntimeError, match="已被"):
        session.open_reverse_forward_blocking(
            "duplicate", "127.0.0.1", 17903, "127.0.0.1", 7891)
    assert session.running_forwards() == {"first"}
    session.close_forward_blocking("first")


def test_reverse_concurrent_connections_and_traffic():
    ready = threading.Event()
    srv = start_local_echo(ready)
    assert ready.wait(5)
    echo_port = srv.getsockname()[1]
    transport = FakeTransport()
    session = make_session(transport)

    session.open_reverse_forward_blocking(
        "rev4", "127.0.0.1", 17893, "127.0.0.1", echo_port)

    pairs = []
    for i in range(4):
        a, b = socket.socketpair()
        ch = FakeChannel(a)
        transport.deliver(ch, ("127.0.0.1", 17893))
        pairs.append((b, f"conn-{i} \r\n".encode()))
    for b, payload in pairs:
        b.sendall(payload)
    for b, payload in pairs:
        got = recv_until(b, len(payload))
        assert got == payload
        b.close()

    deadline = time.monotonic() + 5
    total = 0
    while time.monotonic() < deadline:
        active, total = session.traffic("rev4")
        if active == 0:
            break
        time.sleep(0.05)
    expected = sum(len(p) for _, p in pairs)
    assert total >= expected * 2, f"双向流量不足: {total} < {expected * 2}"
    session.close_forward_blocking("rev4")
    srv.close()


def test_reverse_start_failure_rolls_back():
    transport = FakeTransport(refuse=True)
    session = make_session(transport)
    with pytest.raises(RuntimeError) as ei:
        session.open_reverse_forward_blocking(
            "rev5", "127.0.0.1", 17894, "127.0.0.1", 7890)
    msg = str(ei.value)
    assert "拒绝" in msg, f"服务端拒绝应给出清晰中文错误: {msg}"
    assert "rev5" not in session.running_forwards(), "失败必须回滚 _forwards"


def test_reverse_non_loopback_rejected_by_engine():
    session = make_session(FakeTransport())
    with pytest.raises(RuntimeError):
        session.open_reverse_forward_blocking(
            "rev6", "0.0.0.0", 17895, "127.0.0.1", 7890)
    assert "rev6" not in session.running_forwards()


# ---------------------------------------------------------------------------
# 方向分派（GUI/TUI worker 共用的入口）
# ---------------------------------------------------------------------------
class DispatchFake:
    def __init__(self):
        self.calls: list[tuple] = []

    def open_forward_blocking(self, fwd_id, local_port, remote_host,
                              remote_port, bind_ip="127.0.0.1"):
        self.calls.append(("fwd", fwd_id, local_port, remote_host, remote_port))

    def open_reverse_forward_blocking(self, fwd_id, remote_host,
                                      remote_port, local_host, local_port):
        self.calls.append(("rev", fwd_id, remote_host, remote_port,
                           local_host, local_port))


def test_open_forward_for_session_dispatches_by_direction():
    s = DispatchFake()
    fwd_f = PortForward(id="a", name="web", local_port=8080,
                        remote_host="127.0.0.1", remote_port=80)
    fwd_r = PortForward(id="b", name="proxy", local_port=7890,
                        remote_host="127.0.0.1", remote_port=17890,
                        direction=FwdDirection.REVERSE, local_host="127.0.0.1")
    open_forward_for_session(s, fwd_f)
    open_forward_for_session(s, fwd_r)
    assert s.calls == [
        ("fwd", "a", 8080, "127.0.0.1", 80),
        ("rev", "b", "127.0.0.1", 17890, "127.0.0.1", 7890),
    ]


if __name__ == "__main__":
    import sys

    _fns = [obj for name, obj in sorted(globals().items())
            if name.startswith("test_") and callable(obj)]
    for _fn in _fns:
        _fn()
        print(f"{_fn.__name__} ✓")
    print("REVERSE TESTS PASSED ✓")
    sys.exit(0)
