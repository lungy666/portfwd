"""TUI 无头冒烟测试：不依赖真实 SSH，用 app 回调模拟 worker 结果，
验证布局挂载、连接列表、转发表、状态流转与清理。"""
import asyncio
import os
import queue
import tempfile
import uuid


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="portfwd-ui-")
    os.environ["PORTFWD_HOME"] = tmp  # 隔离配置
    os.environ["PORTFWD_DISABLE_KEYCHAIN"] = "1"

    from portfwd.app import PortFwdApp

    app = PortFwdApp()
    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()

        # 1) 布局存在
        assert app.query_one("#conn-list") is not None
        assert app.query_one("#fwd-table") is not None
        assert len(app.query("Button")) >= 8

        # 2) 模拟添加连接 + 一条转发
        from portfwd.config import ConnectionDef
        from portfwd.models import PortForward, FwdStatus

        conn = ConnectionDef(name="fake", host="127.0.0.1", user="tester")
        app._config.add(conn)
        fwd = PortForward(
            id=uuid.uuid4().hex[:8], name="web", local_port=8080,
            remote_host="127.0.0.1", remote_port=3000, conn="fake",
        )
        app._forwards[fwd.id] = fwd
        app._selected_conn = "fake"
        app._selected_fwd = fwd.id
        await app._render_connections()
        await app._render_forwards()
        await pilot.pause()

        table = app.query_one("#fwd-table")
        assert table.row_count == 1, f"转发表应有 1 行，实际 {table.row_count}"

        # 3) 模拟引擎事件：转发激活 + 流量更新
        app._handle_event({"type": "fwd_active", "conn": "fake", "fwd_id": fwd.id})
        fwd.connections = 2
        fwd.bytes_total = 1024 * 512
        app._update_fwd_row(fwd)
        await pilot.pause()
        assert fwd.status == FwdStatus.ACTIVE
        # 列序：转发/连接/方向/本地地址/远程目标/状态/连接数/流量
        status_cell = str(table.get_cell_at((0, 5)))
        assert "已激活" in status_cell, status_cell
        assert str(table.get_cell_at((0, 6))) == "2"
        assert "512" in str(table.get_cell_at((0, 7)))

        # 4) 模拟断连事件
        app._handle_event({"type": "disconnected", "conn": "fake",
                           "error": "SSH 连接已断开"})
        assert fwd.status == FwdStatus.STOPPED
        assert fwd.error == "SSH 连接已断开"

        # 5) 新建转发屏幕：端口冲突校验（fwd 已占用 8080）
        from portfwd.app import ForwardScreen
        screen = ForwardScreen("fake", 9090)
        app.push_screen(screen, callback=lambda d: app._post_async(app._on_fwd_saved, d))
        await pilot.pause()
        assert app.screen is not None
        from textual.widgets import Input
        li = screen.query_one("#fs-local", Input)
        li.value = "8080"
        await pilot.pause()
        screen._save()
        await pilot.pause()
        assert app.screen is screen, "端口冲突时应保持屏幕"
        li.value = "9090"
        await pilot.pause()
        screen._save()
        await pilot.pause()
        assert app.screen is not screen, "保存后应关闭屏幕"

        # 5b) 键盘输入回归：预填值 + restrict 后仍可键入数字（Textual restrict 是正则）
        kscreen = ForwardScreen("fake", 7777)
        app.push_screen(kscreen, callback=lambda d: None)
        await pilot.pause()
        k_local = kscreen.query_one("#fs-local", Input)
        k_local.focus()
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        assert k_local.value == "77771", f"append 失败: {k_local.value!r}"
        k_local.cursor_position = 0
        await pilot.press("5")
        await pilot.pause()
        assert k_local.value == "577771", f"insert 失败: {k_local.value!r}"
        await pilot.press("x")
        await pilot.pause()
        assert k_local.value == "577771", "非数字应被 reject"
        kscreen.dismiss(None)
        await pilot.pause()

        # 6) 连接屏幕：必填校验
        from portfwd.app import ConnectScreen
        cscreen = ConnectScreen()
        app.push_screen(cscreen, callback=lambda c: app._post_async(app._on_conn_saved, c))
        await pilot.pause()
        cscreen._save()
        await pilot.pause()
        assert app.screen is cscreen, "空表单时应保持屏幕"
        cscreen.query_one("#cs-name", Input).value = "fake2"
        cscreen.query_one("#cs-host", Input).value = "example.com"
        await pilot.pause()
        cscreen._save()
        await pilot.pause()
        assert app.screen is not cscreen

        # 6b) 编辑连接支持改名，不留下旧条目
        app._selected_conn = "fake2"
        await pilot.press("e")
        await pilot.pause()
        rename_screen = app.screen
        rename_screen.query_one("#cs-name", Input).value = "fake2-renamed"
        rename_screen._save()
        await pilot.pause()
        assert app._config.find("fake2") is None
        assert app._config.find("fake2-renamed") is not None

        # 7) 删除转发/连接后清理（先删掉步骤 5 中保存的 9090 转发）
        app._stop_forward_worker = lambda *a, **k: None  # 防止真起线程
        app._connect_ok = lambda *a, **k: None  # 防止自动重启转发
        saved = [f for f in app._forwards.values() if f.local_port == 9090]
        for f in saved:
            app._selected_fwd = f.id
            await app.action_fwd_delete()
        await pilot.pause()
        app._selected_fwd = fwd.id
        app._config.find("fake").port_forwards.append(fwd.to_dict())
        await app.action_fwd_delete()
        await pilot.pause()
        assert table.row_count == 0, f"row_count={table.row_count}"
        assert fwd.id not in app._forwards

        app._stop_conn_worker = lambda *a, **k: None
        # 直接删除全部连接并重新渲染（模拟多次 action_conn_delete）
        names = list(app._config.connections)
        app._config.connections = []
        for name in names:
            for fwd_id in [f.id for f in app._forwards.values() if f.conn == name]:
                app._forwards.pop(fwd_id, None)
        app._selected_conn = None
        app._config.save()
        await app._render_connections()
        await app._render_forwards()
        await pilot.pause()
        assert app._config.find("fake") is None
        assert app._config.find("fake2-renamed") is None
        lv_items = [w for w in app.query_one("#conn-list").children if w.id]
        assert len(lv_items) == 0, f"conn-list 仍有条目: {lv_items}"

    # 8) 配置文件已写入隔离目录
    from portfwd.config import CONFIG_FILE
    assert CONFIG_FILE.exists(), "配置应已持久化到 PORTFWD_HOME"

    print("TUI SMOKE TEST PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())


def test_ui_smoke():
    asyncio.run(main())


# ---------------------------------------------------------------------------
# 反向转发：保存链路 + 方向分派 + 表格方向列
# ---------------------------------------------------------------------------
class RecordingSession:
    """记录 open_forward/open_reverse 调用的假会话（验证方向分派）。"""

    def __init__(self, events):
        self.events = events
        self.connected_now = True
        # 非 None 表示 paramiko 客户端已建立（worker 会检查该属性）
        self.client = object()
        self.calls: list[tuple] = []
        self._running: set[str] = set()

    @property
    def connected(self):
        return self.connected_now

    def _emit_active(self, fwd_id: str) -> None:
        try:
            self.events.put_nowait(
                {"type": "fwd_active", "conn": "fake", "fwd_id": fwd_id})
        except queue.Full:
            pass

    def open_forward_blocking(self, fwd_id, local_port, remote_host,
                              remote_port, bind_ip="127.0.0.1"):
        self.calls.append(("fwd", fwd_id, local_port, remote_host, remote_port))
        self._running.add(fwd_id)
        self._emit_active(fwd_id)

    def open_reverse_forward_blocking(self, fwd_id, remote_host,
                                      remote_port, local_host, local_port):
        self.calls.append(("rev", fwd_id, remote_host, remote_port,
                           local_host, local_port))
        self._running.add(fwd_id)
        self._emit_active(fwd_id)

    def close_forward_blocking(self, fwd_id):
        self._running.discard(fwd_id)

    def close_blocking(self):
        self.connected_now = False
        self._running.clear()

    def traffic(self, fwd_id):
        return (0, 0)

    def running_forwards(self):
        return set(self._running)


async def _reverse_flow_tui() -> None:
    tmp = tempfile.mkdtemp(prefix="portfwd-ui-rev-")
    os.environ["PORTFWD_HOME"] = tmp
    os.environ["PORTFWD_DISABLE_KEYCHAIN"] = "1"

    from textual.widgets import Button, Input, Select

    from portfwd.app import PortFwdApp
    from portfwd.config import ConnectionDef
    from portfwd.models import FwdDirection, FwdStatus, PortForward

    app = PortFwdApp()
    async with app.run_test(size=(110, 28)) as pilot:
        await pilot.pause()
        conn = ConnectionDef(name="fake", host="127.0.0.1")
        app._config.add(conn)
        app._selected_conn = "fake"
        fake = RecordingSession(app._manager.events)
        app._manager.sessions["fake"] = fake

        # 端口冲突语义：反向规则不占本机监听端口，但同连接内禁止重复远程监听
        rev0 = PortForward(
            id=uuid.uuid4().hex[:8], name="proxy0", local_port=7890,
            remote_host="127.0.0.1", remote_port=17890,
            direction=FwdDirection.REVERSE, conn="fake")
        app._forwards[rev0.id] = rev0
        assert app._local_port_taken(7890) is False, "反向不占本机监听端口"
        assert app._rev_bind_taken("fake", "127.0.0.1", 17890) is True
        assert app._rev_bind_taken("other", "127.0.0.1", 17890) is False

        # 新转发屏幕：切换到反向，字段随之变化
        app.action_fwd_new()
        await pilot.pause()
        screen = app.screen
        assert screen.__class__.__name__ == "ForwardScreen"
        dir_sel = screen.query_one("#fs-dir", Select)
        dir_sel.value = "reverse"
        screen._apply_direction()
        await pilot.pause()
        assert screen.query_one("#fs-lhost", Input).display is True, \
            "反向应显示本机目标地址字段"
        assert screen.query_one("#fs-local", Input).value == "", \
            "切到反向后本机目标端口应由用户填写"
        assert screen.query_one("#fs-rhost", Input).disabled is True, \
            "反向远程监听地址应锁定 127.0.0.1"
        assert screen.query_one("#fs-disc", Button).disabled is True, \
            "反向无需发现远程端口"

        # 重复远程监听 -> 保持屏幕；换端口 -> 保存成功
        screen.query_one("#fs-name", Input).value = "proxy"
        screen.query_one("#fs-local", Input).value = "7891"
        screen.query_one("#fs-rport", Input).value = "17890"
        screen._save()
        await pilot.pause()
        assert app.screen is screen, "重复远程监听端口应被拒绝"
        screen.query_one("#fs-rport", Input).value = "17891"
        screen._save()
        await pilot.pause()
        assert app.screen is not screen, "保存成功后应关闭屏幕"

        saved = [f for f in app._forwards.values() if f.name == "proxy"]
        assert saved, "反向规则应已保存"
        saved = saved[0]
        assert saved.direction is FwdDirection.REVERSE
        assert saved.local_host == "127.0.0.1" and saved.local_port == 7891
        assert saved.remote_host == "127.0.0.1" and saved.remote_port == 17891
        assert saved.url == "" and saved.can_open_in_browser is False
        assert any(d.get("id") == saved.id and d.get("direction") == "reverse"
                   and d.get("local_host") == "127.0.0.1"
                   for d in conn.port_forwards), "方向应持久化进配置"

        # 连接已启动 -> worker 按 direction 分派到 open_reverse_forward_blocking
        await pilot.pause(0.8)
        assert ("rev", saved.id, "127.0.0.1", 17891, "127.0.0.1", 7891) \
            in fake.calls, fake.calls
        assert saved.status == FwdStatus.ACTIVE

        # 表格方向列：反向行显示"服务器访问本机"
        table = app.query_one("#fwd-table")
        row = list(app._forwards.keys()).index(saved.id)
        assert str(table.get_cell_at((row, 2))) == "服务器访问本机"

        # 再切回正向：字段恢复
        app.action_fwd_new()
        await pilot.pause()
        f2 = app.screen
        d2 = f2.query_one("#fs-dir", Select)
        d2.value = "reverse"
        f2._apply_direction()
        d2.value = "forward"
        f2._apply_direction()
        await pilot.pause()
        assert f2.query_one("#fs-lhost", Input).display is False
        assert f2.query_one("#fs-disc", Button).disabled is False
        f2.dismiss(None)
        await pilot.pause()

    print("TUI REVERSE FLOW TEST PASSED ✓")


def test_ui_reverse():
    asyncio.run(_reverse_flow_tui())


# ---------------------------------------------------------------------------
# 回归：连接名以数字开头/含点号时不能作为 Textual widget id（BadIdentifier）
# ---------------------------------------------------------------------------
async def _numeric_conn_name_tui() -> None:
    tmp = tempfile.mkdtemp(prefix="portfwd-ui-num-")
    os.environ["PORTFWD_HOME"] = tmp
    os.environ["PORTFWD_DISABLE_KEYCHAIN"] = "1"

    from textual.widgets import ListItem, ListView

    from portfwd.app import PortFwdApp
    from portfwd.config import ConnectionDef

    app = PortFwdApp()
    # 挂载前预置：on_mount -> _render_connections 即真实崩溃路径
    app._config.add(ConnectionDef(name="3090", host="127.0.0.1"))
    app._config.add(ConnectionDef(name="10.0.0.5", host="127.0.0.1"))

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        lv = app.query_one("#conn-list", ListView)
        items = [w for w in lv.children if isinstance(w, ListItem)]
        assert len(items) == 2, f"应有 2 个条目，实际 {len(items)}"
        app._refresh_conn_marks()
        assert app._conn_marks.get("3090") == "○ 3090", app._conn_marks
        # 选择事件应正确映射回连接名
        app.on_list_view_highlighted(ListView.Highlighted(lv, items[0]))
        assert app._selected_conn == "3090", app._selected_conn
        app.on_list_view_highlighted(ListView.Highlighted(lv, items[1]))
        assert app._selected_conn == "10.0.0.5", app._selected_conn

    print("TUI NUMERIC NAME TEST PASSED ✓")


def test_ui_numeric_name():
    asyncio.run(_numeric_conn_name_tui())
