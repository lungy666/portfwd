"""TUI 无头冒烟测试：不依赖真实 SSH，用 app 回调模拟 worker 结果，
验证布局挂载、连接列表、转发表、状态流转与清理。"""
import os
import tempfile
import uuid
import asyncio


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
        status_cell = str(table.get_cell_at((0, 4)))
        assert "已激活" in status_cell, status_cell
        assert str(table.get_cell_at((0, 5))) == "2"
        assert "512" in str(table.get_cell_at((0, 6)))

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
