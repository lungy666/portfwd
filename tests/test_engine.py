"""引擎集成测试：不依赖真实 sshd，用 FakeTransport 打通
本地监听 -> open_channel -> 双向数据泵 -> 计数 -> 清理 的完整通路。"""
import socket
import threading
import time

from portfwd.config import ConnectionDef
from portfwd.forwarding import Session


class FakeChannel:
    """把一个到本地 echo server 的 socket 伪装成 paramiko Channel。"""

    def __init__(self, sock):
        self.sock = sock

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
        try:
            self.sock.close()
        except OSError:
            pass


class FakeTransport:
    """open_channel 直接 socket 连到 127.0.0.1:<dest_port>。"""

    def __init__(self):
        self.active = True
        self.authenticated = True
        self.kinds = []

    def open_channel(self, kind, dest, local):
        self.kinds.append(kind)
        assert kind == "direct-tcpip"
        host, port = dest
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        return FakeChannel(s)

    def is_active(self):
        return self.active

    def is_authenticated(self):
        return self.authenticated


class FakeClient:
    def __init__(self, transport):
        self._t = transport

    def get_transport(self):
        return self._t

    def close(self):
        pass


def start_echo(port, ready):
    """极简 echo：收到啥原样返回，'\r\n' 结束。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))

    def run():
        srv.listen(8)
        ready.set()
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_echo, args=(c,), daemon=True).start()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return srv, thread


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
        # 真实远程服务会在连接结束时主动关闭，模拟之
        try:
            sock.close()
        except OSError:
            pass


def main():
    # 1) 起本地 echo server
    ready = threading.Event()
    srv, _ = start_echo(0, ready)
    echo_port = srv.getsockname()[1]
    assert ready.wait(5), "echo server 没起来"
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    local_port = free.getsockname()[1]
    free.close()

    # 2) 造一个假 Session（不真连接）
    conn = ConnectionDef(name="fake", host="127.0.0.1")
    import queue
    session = Session(conn, queue.Queue())
    transport = FakeTransport()
    session.client = FakeClient(transport)
    assert session.connected, "活动 Transport 应显示为已连接"
    transport.active = False
    assert not session.connected, "失效 Transport 不应显示为已连接"
    transport.active = True

    # 3) 打开转发 127.0.0.1:local_port -> 127.0.0.1:echo_port
    session.open_forward_blocking("fwd1", local_port, "127.0.0.1", echo_port)
    time.sleep(0.2)

    # 4) 通过本地端口发请求，验证 echo 回来 + 流量/连接计数
    client = socket.create_connection(("127.0.0.1", local_port), timeout=5)
    client.sendall(b"hello portfwd \r\n")
    got = b""
    deadline = time.time() + 5
    while time.time() < deadline and len(got) < len(b"hello portfwd \r\n"):
        chunk = client.recv(4096)
        if not chunk:
            break
        got += chunk
    assert got == b"hello portfwd \r\n", f"echo 内容不符: {got!r}"

    # 活跃连接应 >=1
    time.sleep(0.1)
    active, total = session.traffic("fwd1")
    print(f"traffic after echo: active={active} bytes={total}")
    assert total >= len(b"hello portfwd \r\n") * 2, "字节计数应含双向"
    assert active >= 1, "应有活跃连接"

    # 5) 关闭客户端
    client.close()
    time.sleep(0.5)
    active2, total2 = session.traffic("fwd1")
    print(f"traffic after close: active={active2} bytes={total2}")
    assert active2 == 0, "连接关闭后活跃数应为 0"
    assert transport.kinds == ["direct-tcpip"], transport.kinds

    # 6) 关闭转发，本地端口应不可再连
    session.close_forward_blocking("fwd1")
    time.sleep(0.3)
    try:
        socket.create_connection(("127.0.0.1", local_port), timeout=1)
        raise AssertionError("转发关闭后本地端口仍可达")
    except OSError:
        pass
    print("本地端口已关闭 ✓")

    srv.close()
    print("ENGINE INTEGRATION TEST PASSED ✓")


if __name__ == "__main__":
    main()


def test_engine_integration():
    main()
