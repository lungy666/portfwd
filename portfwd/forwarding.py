"""SSH 连接与端口转发引擎。

对齐 VS Code Remote-SSH 的 PORTS 面板语义：
  在本机 127.0.0.1:<local_port> 起监听；每来一个本地连接，
  就打开一条 direct-tcpip SSH channel 连到远程 <remote_host>:<remote_port>，
  双向泵送数据。于是浏览器访问本地端口即可打开远程服务。

线程模型（重要）：
  - 本模块所有 *_blocking 方法都会阻塞（socket / SSH I/O），
    调用方必须在 worker 线程里调用，绝不能在 Textual UI 线程里同步调用。
  - 引擎线程通过共享的 events 队列（queue.Queue）向 UI 推送状态变化；
    流量/连接数则由 UI 定时轮询 traffic() 获取，避免队列被高频打爆。
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import paramiko
from paramiko import SSHClient

from .config import ConnectionDef, load_pkey, ssh_config_fill
from .models import FwdStatus, PortForward

log = logging.getLogger("portfwd")


class ConnectionAuthError(ConnectionError):
    """认证失败（需要密钥或密码）。"""


class AcceptNewPolicy(paramiko.MissingHostKeyPolicy):
    """对齐 ssh 的 StrictHostKeyChecking=accept-new：

    - known_hosts 里已有该主机且密钥匹配 -> 通过；
    - 已有但密钥不同（疑似中间人攻击）-> 拒绝并报错；
    - 首次见到的主机 -> 自动接受并追加写入 ~/.ssh/known_hosts
      （按"用户输入的主机名"记录，与 `ssh` 行为一致）。
    """

    def __init__(self, host: str, port: int, alias: str = "") -> None:
        self._host = host
        self._port = port
        # 写 known_hosts 时优先用用户输入的别名，其次才是解析后的 host
        self._record = alias or host

    def missing_host_key(self, client, hostname, key) -> None:
        import paramiko as _p

        host_keys = client.get_host_keys()
        candidates = [hostname]
        if self._port != 22 and not hostname.startswith("["):
            candidates.append(f"[{hostname}]:{self._port}")
        if self._record not in candidates:
            candidates.append(self._record)
        if self._port != 22 and not self._record.startswith("["):
            candidates.append(f"[{self._record}]:{self._port}")
        for name in candidates:
            entries = host_keys.get(name)
            if entries:
                known = entries.get(key.get_name())
                if known is not None and known != key:
                    raise _p.SSHException(
                        f"主机密钥不匹配 {hostname}（可能遭遇中间人攻击，已拒绝连接）"
                    )
                if known is not None:
                    return
                break
        host_keys.add(hostname, key.get_name(), key)
        records = [self._record]
        if self._host not in records:
            records.append(self._host)
        for record in records:
            _append_known_hosts(record, key, self._port)
        log.info("已接受新主机密钥并写入 known_hosts: %s", ", ".join(records))


def _append_known_hosts(host: str, key, port: int) -> None:
    """把新主机密钥追加到 ~/.ssh/known_hosts（非 22 端口用 [host]:port 形式，已存在则跳过）。"""
    from pathlib import Path

    line_host = f"[{host}]:{port}" if port != 22 else host
    line = line_host + " " + key.get_name() + " " + key.get_base64()
    path = Path.home() / ".ssh" / "known_hosts"
    try:
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        key_name = key.get_name()
        key_data = key.get_base64()
        if any(
            len(parts := l.split()) >= 3
            and parts[0] == line_host
            and parts[1] == key_name
            and parts[2] == key_data
            for l in existing
            if l and not l.startswith("#")
        ):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        log.warning("写入 known_hosts 失败: %s", e)


# ---------------------------------------------------------------------------
# 事件结构：引擎线程 -> UI。UI 用 poll_events() 无阻塞拉取。
# 形如 {"type": "...", "conn": name, "fwd_id": str, ...}
#   type: connected | disconnected | fwd_active | fwd_error
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 单个转发的流量计数
# ---------------------------------------------------------------------------
class _TrafficMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._bytes = 0

    def start(self, cid: str) -> None:
        with self._lock:
            self._active.add(cid)

    def end(self, cid: str) -> None:
        with self._lock:
            self._active.discard(cid)

    def add_bytes(self, n: int) -> None:
        if n > 0:
            with self._lock:
                self._bytes += n

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return len(self._active), self._bytes


class _Forward:
    """一条运行期转发的状态（不持久化）。"""

    def __init__(self) -> None:
        self.stop_evt = threading.Event()
        self.meter = _TrafficMeter()
        self.listener: Optional[socket.socket] = None
        self.seq = 0
        self.seq_lock = threading.Lock()
        self.pumps: set[Any] = set()  # 活跃 pump 的 channel，关闭转发时统一关闭
        self.sockets: set[socket.socket] = set()
        self.pumps_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 单个 SSH 连接 + 其上的转发
# ---------------------------------------------------------------------------
class Session:
    """管理一个 SSH 连接及其所有端口转发。所有阻塞方法都在 worker 里调。"""

    def __init__(self, conn: ConnectionDef, events: "queue.Queue[dict]") -> None:
        self.conn = conn
        self.events = events
        self.session_id = uuid.uuid4().hex
        self.client: Optional[SSHClient] = None
        self.resolved = ssh_config_fill(conn.host, conn.user, conn.port,
                                        conn.identity_file)
        self._forwards: dict[str, _Forward] = {}
        self._fwd_lock = threading.Lock()
        self._state = FwdStatus.STOPPED  # 连接级状态
        self._hb: Optional[threading.Thread] = None
        self._closed_evt = threading.Event()

    # -- 状态 ---------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.conn.name

    @property
    def connected(self) -> bool:
        if self._closed_evt.is_set() or self.client is None:
            return False
        try:
            tr = self.client.get_transport()
            return bool(
                tr is not None
                and tr.is_authenticated()
                and tr.is_active()
            )
        except Exception:
            return False

    def _push(self, **ev: Any) -> None:
        ev.setdefault("conn", self.name)
        ev.setdefault("session", self.session_id)
        try:
            self.events.put_nowait(ev)
        except queue.Full:
            pass

    # -- 连接 ---------------------------------------------------------------
    def connect_blocking(self, password: Optional[str] = None) -> None:
        """阻塞建立 SSH 连接；失败抛异常（含 ConnectionAuthError）。"""
        os_ = os
        host = self.resolved["host"]
        port = int(self.resolved["port"])
        user = self.resolved["user"] or os_.getenv("USER") or "root"
        identity = self.resolved.get("identity_file") or ""
        if identity:
            identity = os_.path.expanduser(identity)

        client = SSHClient()
        # Paramiko does not load user host keys automatically. Loading them is
        # required for accept-new to reject a changed key instead of accepting it.
        try:
            client.load_system_host_keys()
        except OSError:
            pass
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(AcceptNewPolicy(host, port, self.conn.host))

        pkey = None
        key_filename = None
        if identity:
            pkey = load_pkey(identity)
            if pkey is None:
                key_filename = identity  # 让 paramiko 自己读（可带 passphrase 处理）

        try:
            client.connect(
                hostname=host,
                port=port,
                username=user,
                password=password,
                pkey=pkey,
                key_filename=key_filename,
                allow_agent=(self.conn.auth in ("auto", "key")),
                look_for_keys=(self.conn.auth == "auto"),
                timeout=15,
                banner_timeout=20,
                auth_timeout=20,
            )
        except paramiko.AuthenticationException as e:
            _close(client)
            raise ConnectionAuthError(
                f"认证失败 {user}@{host}:{port}（需要密钥或密码）"
            ) from e
        except OSError as e:
            _close(client)
            raise ConnectionError(f"无法连接 {host}:{port}：{e}") from e
        except paramiko.SSHException as e:
            _close(client)
            raise ConnectionError(f"SSH 连接失败 {user}@{host}:{port}：{e}") from e
        except Exception:
            _close(client)
            raise
        self.client = client
        transport = client.get_transport()
        if transport is None:
            _close(client)
            self.client = None
            raise ConnectionError(f"SSH 连接失败 {user}@{host}:{port}：未建立 Transport")
        transport.set_keepalive(30)
        self._push(type="connected")

        self._hb = threading.Thread(
            target=self._heartbeat, daemon=True, name=f"portfwd-hb-{self.name}"
        )
        self._hb.start()
        log.info("已连接 %s@%s:%s", user, host, port)

    def _heartbeat(self) -> None:
        was_up = True
        while not self._closed_evt.is_set():
            if self._closed_evt.wait(2):
                break
            up = self.connected
            if up and not was_up:
                self._push(type="connected")
            if not up and was_up:
                was_up = False
                self._teardown_forwards()
                self._push(type="disconnected", error="SSH 连接已断开")
                break
            was_up = up

    # -- 转发 ---------------------------------------------------------------
    def _transport(self) -> Any:
        if self.client is None:
            return None
        return self.client.get_transport()

    def open_forward_blocking(self, fwd_id: str, local_port: int,
                              remote_host: str, remote_port: int,
                              bind_ip: "str" = "127.0.0.1") -> None:
        """阻塞创建本地监听，启动 accept 循环。失败抛异常。"""
        tr = self._transport()
        if tr is None:
            raise RuntimeError("SSH 未连接")

        with self._fwd_lock:
            if fwd_id in self._forwards:
                raise RuntimeError("该转发已在运行")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((bind_ip, local_port))
            except OSError as e:
                listener.close()
                raise RuntimeError(f"本地端口 {local_port} 被占用：{e}")
            listener.listen(64)
            listener.settimeout(0.5)
            f = _Forward()
            f.listener = listener
            self._forwards[fwd_id] = f

        threading.Thread(
            target=self._accept_loop,
            args=(fwd_id, remote_host, remote_port, f),
            daemon=True,
            name=f"portfwd-accept-{fwd_id}",
        ).start()
        self._push(type="fwd_active", fwd_id=fwd_id)
        log.info("转发 %s 已激活 %s -> %s:%s", fwd_id, local_port,
                 remote_host, remote_port)

    def _accept_loop(self, fwd_id: str, remote_host: str, remote_port: int,
                     f: _Forward) -> None:
        while not f.stop_evt.is_set():
            try:
                conn_sock, _addr = f.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener 关闭

            tr = self._transport()
            if tr is None:
                _close(conn_sock)
                continue
            # A client-originated local forward is a direct-tcpip channel.
            try:
                channel = tr.open_channel(
                    "direct-tcpip",
                    (remote_host, remote_port),
                    ("127.0.0.1", 0),
                )
            except Exception as e:
                log.warning("tunnel 打开失败 %s -> %s:%s: %s",
                            fwd_id, remote_host, remote_port, e)
                _close(conn_sock)
                continue

            with f.seq_lock:
                f.seq += 1
                cid = f"{fwd_id}#{f.seq}"
            f.meter.start(cid)
            threading.Thread(
                target=self._pump,
                args=(conn_sock, channel, f.meter, cid, f),
                daemon=True,
                name=f"portfwd-pump-{cid}",
            ).start()

    def _pump(self, sock: socket.socket, channel: Any, meter: _TrafficMeter,
              cid: str, f: "_Forward") -> None:
        f_pumps = (f.pumps, f.pumps_lock)
        with f.pumps_lock:
            f.pumps.add(channel)
            f.sockets.add(sock)

        def drain(recv, send, eof) -> None:
            try:
                while True:
                    data = recv(16384)
                    if not data:
                        break
                    send(data)
                    meter.add_bytes(len(data))
            except Exception:
                pass
            finally:
                try:
                    eof()
                except Exception:
                    pass

        to_remote = threading.Thread(
            target=drain, args=(sock.recv, channel.sendall,
                                lambda: channel.shutdown_write()), daemon=True)
        to_local = threading.Thread(
            target=drain, args=(channel.recv, sock.sendall,
                                lambda: sock.shutdown(socket.SHUT_WR)), daemon=True)
        to_remote.start()
        to_local.start()
        to_remote.join()
        to_local.join()
        _close(channel)
        _close(sock)
        meter.end(cid)
        if f_pumps is not None:
            with f_pumps[1]:
                f_pumps[0].discard(channel)
                f.sockets.discard(sock)

    def close_forward_blocking(self, fwd_id: str) -> None:
        with self._fwd_lock:
            f = self._forwards.pop(fwd_id, None)
        if f is None:
            return
        f.stop_evt.set()
        _close(f.listener)
        # 同时关闭远端 channel 和本地 socket，确保双向 pump 不会长期阻塞。
        with f.pumps_lock:
            channels = list(f.pumps)
            sockets = list(f.sockets)
        for ch in channels:
            try:
                ch.close()
            except Exception:
                pass
        for sock in sockets:
            _close(sock)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not f.pumps:
                break
            time.sleep(0.02)

    def traffic(self, fwd_id: str) -> tuple[int, int]:
        f = self._forwards.get(fwd_id)
        if f is None:
            return 0, 0
        return f.meter.snapshot()

    def running_forwards(self) -> set[str]:
        with self._fwd_lock:
            return set(self._forwards.keys())

    def _teardown_forwards(self) -> None:
        with self._fwd_lock:
            ids = list(self._forwards.keys())
        for i in ids:
            self.close_forward_blocking(i)

    # -- 远程端口发现 -------------------------------------------------------
    def list_remote_ports(self, timeout: float = 8.0) -> list[tuple[str, str, int]]:
        """拉取远程监听端口 -> [(ip, proto, port)]；都不行抛 RuntimeError。"""
        if self.client is None:
            raise RuntimeError("未连接")
        for cmd in (
            "ss -Hltn 2>/dev/null || true",
            "netstat -ltn 2>/dev/null || true",
            "lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || true",
        ):
            try:
                _in, out, _err = self.client.exec_command(cmd, timeout=timeout)
                text = out.read().decode("utf-8", "replace")
            except Exception:
                continue
            rows = self._parse_ports(text)
            if rows:
                return rows
        raise RuntimeError("无法读取远程端口（ss/netstat/lsof 均不可用）")

    @staticmethod
    def _parse_ports(text: str) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        seen: set[tuple[str, int]] = set()
        for line in text.splitlines():
            low = line.lower()
            if low.startswith(("local", "proto", "command", "receive",
                               "state", "address", "sl")):
                continue
            for tok in line.split():
                if ":" in tok and tok.rsplit(":", 1)[-1].isdigit():
                    addr, ps = tok.rsplit(":", 1)
                    port = int(ps)
                    ip = addr.strip("[]")
                    if ip == "*":
                        ip = "0.0.0.0"
                    proto = "udp" if ("udp" in low and "tcp" not in low) else "tcp"
                    key = (ip, port)
                    if key not in seen:
                        seen.add(key)
                        rows.append((ip, proto, port))
                    break
        return rows

    # -- 关闭 ---------------------------------------------------------------
    def close_blocking(self) -> None:
        self._closed_evt.set()
        self._teardown_forwards()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self._state = FwdStatus.STOPPED


def _close(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 会话注册表（跨连接）
# ---------------------------------------------------------------------------
class SessionManager:
    """按名字管理 Session；持有共享事件队列，供 TUI 轮询。"""

    def __init__(self) -> None:
        self.events: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self.sessions: dict[str, Session] = {}

    def get(self, name: str) -> Optional[Session]:
        return self.sessions.get(name)

    def make(self, conn: ConnectionDef) -> Session:
        s = Session(conn, self.events)
        self.sessions[conn.name] = s
        return s

    def drop(self, name: str) -> None:
        s = self.sessions.pop(name, None)
        if s is not None:
            s.close_blocking()

    def poll_events(self) -> list[dict]:
        out: list[dict] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    def close_all(self) -> None:
        for name in list(self.sessions):
            self.drop(name)
