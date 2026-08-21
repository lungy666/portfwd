"""按钮功能回归：无头模式下逐个按钮走一遍，验证每个按钮的副作用。

FakeSession 模拟已连接的 SSH 会话，记录 connect/disconnect/open_forward/
close_forward 调用；转发启动走真实 worker 线程（本地端口 bind + 事件推送），
验证完整的按钮 -> worker -> 事件 -> 表格刷新 链路。
"""
import os
import tempfile
import uuid


class FakeChannel:
    def close(self):
        pass


class FakeSession:
    """记录所有操作的假会话；open_forward 真实 bind 本地端口并推送 fwd_active。"""

    def __init__(self, events):
        self.events = events
        self.connected_now = True
        # 非 None 表示 paramiko 客户端已建立（app worker 会检查该属性）
        self.client = object()
        self.opened: list[tuple[str, int, str, int]] = []
        self.opened_rev: list[tuple[str, str, int, str, int]] = []
        self.closed: list[str] = []
        self.close_calls = 0
        self._listeners = {}
        self._rev_active: set[str] = set()
        import socket
        self._socket_mod = socket

    @property
    def connected(self):
        return self.connected_now

    def _emit_active(self, fwd_id: str) -> None:
        try:
            self.events.put_nowait(
                {"type": "fwd_active", "conn": "fake", "fwd_id": fwd_id})
        except Exception:
            pass

    def open_forward_blocking(self, fwd_id, local_port, remote_host,
                              remote_port, bind_ip="127.0.0.1"):
        self.opened.append((fwd_id, local_port, remote_host, remote_port))
        listener = self._socket_mod.socket(self._socket_mod.AF_INET,
                                           self._socket_mod.SOCK_STREAM)
        listener.setsockopt(self._socket_mod.SOL_SOCKET,
                            self._socket_mod.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", local_port))
        listener.listen(4)
        self._listeners[fwd_id] = listener
        self._emit_active(fwd_id)

    def open_reverse_forward_blocking(self, fwd_id, remote_host, remote_port,
                                      local_host, local_port):
        # 反向：服务器端监听，本地不 bind；只记录并推送 fwd_active
        self.opened_rev.append((fwd_id, remote_host, remote_port,
                                local_host, local_port))
        self._rev_active.add(fwd_id)
        self._emit_active(fwd_id)

    def close_forward_blocking(self, fwd_id):
        self.closed.append(fwd_id)
        self._rev_active.discard(fwd_id)
        listener = self._listeners.pop(fwd_id, None)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def close_blocking(self):
        self.close_calls += 1
        self.connected_now = False
        for f in list(self._listeners):
            self.close_forward_blocking(f)
        self._rev_active.clear()

    def traffic(self, fwd_id):
        return (0, 64) if fwd_id in self._listeners else (0, 0)

    def running_forwards(self):
        return set(self._listeners.keys()) | self._rev_active


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="portfwd-btn-")
    os.environ["PORTFWD_HOME"] = tmp
    os.environ["PORTFWD_DISABLE_KEYCHAIN"] = "1"

    from portfwd.app import PortFwdApp
    from portfwd.config import ConnectionDef
    from portfwd.models import PortForward, FwdStatus
    from textual.widgets import Button, Input

    app = PortFwdApp()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        fake = FakeSession(app._manager.events)
        app._manager.sessions["fake"] = fake
        conn = ConnectionDef(name="fake", host="127.0.0.1")
        app._config.add(conn)

        # ================= 连接区按钮 =================

        # [新连接 c] 打开 ConnectScreen，填写后保存 -> 写入 config
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConnectScreen"
        app.screen.query_one("#cs-name", Input).value = "newconn"
        app.screen.query_one("#cs-host", Input).value = "1.2.3.4"
        await pilot.pause()
        app.screen._save()
        await pilot.pause()
        assert app._config.find("newconn") is not None
        print("[新连接 c] OK: 已保存 newconn")

        # [编辑 e] 选中连接后打开 ConnectScreen 且带旧值
        app._selected_conn = "newconn"
        await pilot.press("e")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConnectScreen"
        assert app.screen.query_one("#cs-host", Input).value == "1.2.3.4"
        app.screen.dismiss(None)
        await pilot.pause()
        print("[编辑 e] OK: 带出旧值")

        # [连接/断开 x] 选中 fake -> 走 _stop_conn_worker -> manager.drop
        app._selected_conn = "fake"
        before_close = fake.close_calls
        await pilot.press("x")
        await pilot.pause(1.0)
        assert "fake" not in app._manager.sessions, "drop 应移除会话"
        print("[连接/断开 x] OK: 已断开并清理会话")
        # 恢复 fake 会话供后续测试
        app._manager.sessions["fake"] = fake
        fake.connected_now = True

        # [删除连接] 删除 newconn 及其转发
        fwd_orphan = PortForward(
            id=uuid.uuid4().hex[:8], name="orphan", local_port=9701,
            remote_host="127.0.0.1", remote_port=1, conn="newconn")
        app._forwards[fwd_orphan.id] = fwd_orphan
        app._config.find("newconn").port_forwards.append(fwd_orphan.to_dict())
        app._selected_conn = "newconn"
        await pilot.pause()
        # 点"删除连接"按钮
        await pilot.click("#btn-conn-del")
        await pilot.pause()
        assert app._config.find("newconn") is None
        assert fwd_orphan.id not in app._forwards
        print("[删除连接] OK: 连接与转发一并删除")

        # ================= 转发区按钮 =================
        # 准备一条 STOPPED 转发
        fwd = PortForward(
            id=uuid.uuid4().hex[:8], name="web", local_port=18761,
            remote_host="127.0.0.1", remote_port=8080, conn="fake")
        app._forwards[fwd.id] = fwd
        app._selected_conn = "fake"
        app._selected_fwd = fwd.id
        await app._render_forwards()
        await pilot.pause()

        # [新转发 f] 打开 ForwardScreen，发现端口列表由 worker 填充
        await pilot.press("f")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ForwardScreen"
        app.screen.query_one("#fs-rport", Input).value = "9999"
        app.screen.query_one("#fs-local", Input).value = "18762"
        await pilot.pause()
        app.screen._save()
        await pilot.pause()
        assert app.screen.__class__.__name__ != "ForwardScreen"
        new_fwd = [f for f in app._forwards.values() if f.local_port == 18762]
        assert new_fwd, "新转发应已加入"
        print("[新转发 f] OK: 保存并加入表格")
        # 会话已"连接"，_on_fwd_saved 会尝试启动；fake 会真实 bind 18762
        await pilot.pause(0.5)
        assert ("18762" in [str(o[1]) for o in fake.opened]), fake.opened
        print("[新转发 f] OK: 已自动启动（fake.opened 有记录）")

        # [启/停 r] 对已 active 的 18762 转发按 r -> 停止
        target = new_fwd[0]
        app._selected_fwd = target.id
        await pilot.press("r")
        await pilot.pause(0.5)
        assert target.id in fake.closed, f"应已关闭: {fake.closed}"
        assert target.status == FwdStatus.STOPPED
        print("[启/停 r] OK: 已停止并回写状态")

        # 再按 r -> 重新启动
        await pilot.press("r")
        await pilot.pause(0.5)
        assert (target.id, 18762, "127.0.0.1", 9999) in fake.opened
        assert target.status == FwdStatus.ACTIVE
        print("[启/停 r] OK: 重新启动成功")

        # [删除转发 d]
        await pilot.press("d")
        await pilot.pause()
        assert target.id not in app._forwards
        assert target.id in fake.closed
        print("[删除转发 d] OK: 删除并清理运行态")

        # [保存 s]
        await pilot.press("s")
        await pilot.pause()
        from portfwd.config import CONFIG_FILE
        assert CONFIG_FILE.exists()
        data = __import__("json").loads(CONFIG_FILE.read_text())
        assert any(c["name"] == "fake" for c in data["connections"])
        print("[保存 s] OK: 配置已落盘")

        # [删除转发 d] 选中不存在 -> 提示不崩
        app._selected_fwd = "nonexistent"
        await pilot.press("d")
        await pilot.pause()
        print("[删除转发 d] OK: 空选择安全")

        # ================= 边界：空状态 =================
        app._forwards.clear()
        app._selected_fwd = None
        app._selected_conn = None
        await app._render_forwards()
        await pilot.pause()
        # 无选中连接时按 f / e / x / 删除连接 -> 只提示，不崩溃
        for key in ("f", "e", "x", "d"):
            await pilot.press(key)
            await pilot.pause()
        print("空状态边界 OK: 无崩溃")

    # 收尾：所有 fake 监听已关
    assert not fake._listeners, f"残留监听: {fake._listeners}"
    print("BUTTON FUNCTION TEST PASSED ✓")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


def test_button_smoke():
    import asyncio
    asyncio.run(main())
