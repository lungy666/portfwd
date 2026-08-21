"""GUI 无头冒烟测试：不依赖真实 SSH / 显示，用 FakePage 驱动 PortFwdGui，
验证布局构建、连接/转发列表渲染、状态流转、流量轮询、表单校验与清理。

运行：.venv/bin/python tests/test_gui.py
"""
import asyncio
import os
import queue
import tempfile
import threading
import uuid

import flet as ft
from flet.controls.base_control import BaseControl

# 无头环境：控件 update() 不真正推送客户端，只计数（真实实现要求控件挂在
# 有会话的 Page 上，这里统一打桩）。
_UPDATES = {"n": 0}


def _fake_update(self):
    _UPDATES["n"] += 1


BaseControl.update = _fake_update


class _FakeWindow:
    width = height = min_width = min_height = 0


class FakePage:
    """最小 ft.Page 替身：记录调用；worker 内联执行，协程同步跑完。"""

    def __init__(self) -> None:
        self.controls: list = []
        self.dialogs: list = []
        self.threads = 0
        self.title = ""
        self.theme_mode = None
        self.window = _FakeWindow()

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self, *controls) -> None:
        pass

    def run_thread(self, fn, *args, **kwargs) -> None:
        self.threads += 1
        fn(*args, **kwargs)  # 测试里内联执行

    def run_task(self, fn, *args, **kwargs) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(fn(*args, **kwargs))
        finally:
            loop.close()

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            return self.dialogs.pop()
        return None


class FakeSession:
    """鸭子类型的 Session：提供 SessionManager.drop / _tick_once /
    worker 方向分派用到的接口，并记录 open/close 调用。"""

    def __init__(self, name: str, fwd_ids=(), traffic=(0, 0), up: bool = True,
                 events=None) -> None:
        self.name = name
        self.client = None
        self._closed_evt = threading.Event()
        self._fwd_ids = set(fwd_ids)
        self._traffic = traffic
        self._up = up
        self._events = events
        self.opened: list[tuple] = []
        self.opened_rev: list[tuple] = []
        self.closed: list[str] = []
        self.close_calls = 0

    @property
    def connected(self) -> bool:
        return self._up

    def _teardown_forwards(self) -> None:
        pass

    def _emit_active(self, fwd_id: str) -> None:
        if self._events is not None:
            try:
                self._events.put_nowait(
                    {"type": "fwd_active", "conn": self.name, "fwd_id": fwd_id})
            except queue.Full:
                pass

    def open_forward_blocking(self, fwd_id, local_port, remote_host,
                              remote_port, bind_ip="127.0.0.1"):
        self.opened.append((fwd_id, local_port, remote_host, remote_port))
        self._fwd_ids.add(fwd_id)
        self._emit_active(fwd_id)

    def open_reverse_forward_blocking(self, fwd_id, remote_host,
                                      remote_port, local_host, local_port):
        self.opened_rev.append((fwd_id, remote_host, remote_port,
                                local_host, local_port))
        self._fwd_ids.add(fwd_id)
        self._emit_active(fwd_id)

    def close_forward_blocking(self, fwd_id):
        self.closed.append(fwd_id)
        self._fwd_ids.discard(fwd_id)

    def close_blocking(self):
        self.close_calls += 1
        self._up = False
        self._fwd_ids.clear()

    def running_forwards(self) -> set:
        return set(self._fwd_ids)

    def traffic(self, fwd_id: str) -> tuple:
        return self._traffic


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="portfwd-gui-")
    os.environ["PORTFWD_HOME"] = tmp  # 隔离配置（须在 import portfwd 前）
    os.environ["PORTFWD_DISABLE_KEYCHAIN"] = "1"

    from portfwd.config import CONFIG_FILE, ConnectionDef
    from portfwd.gui import NUM, PortFwdGui, parse_port
    from portfwd.models import FwdStatus, PortForward

    page = FakePage()
    gui = PortFwdGui()
    gui.attach(page)

    # 1) 布局构建
    assert page.controls, "attach() 应向 page 添加根控件"
    assert gui._conn_list is not None and gui._fwd_table is not None
    assert gui._conn_list.item_extent == 64
    assert _UPDATES["n"] > 0, "渲染应触发控件 update()"

    # 2) 添加连接 + 转发并渲染
    conn = ConnectionDef(name="fake", host="127.0.0.1", user="tester")
    gui._config.add(conn)
    fwd = PortForward(
        id=uuid.uuid4().hex[:8], name="web", local_port=8080,
        remote_host="127.0.0.1", remote_port=3000, conn="fake",
    )
    gui._forwards[fwd.id] = fwd
    gui._selected_conn = "fake"
    gui._selected_fwd = fwd.id
    gui._rebuild_connections()
    gui._rebuild_forwards()
    assert len(gui._conn_list.controls) == 1, f"连接列表应有 1 行: {len(gui._conn_list.controls)}"
    assert gui._conn_list.controls[0].title.value == "fake"
    assert gui._conn_list.controls[0].height == 64
    assert gui._conn_list.controls[0].subtitle.max_lines == 1
    assert len(gui._fwd_table.rows) == 1, f"转发表应有 1 行: {len(gui._fwd_table.rows)}"

    # 3) 端口解析（纯逻辑）
    assert parse_port("")[0] is None, "空端口应被拒绝"
    assert parse_port("99999")[0] is None, "超范围端口应被拒绝"
    p, err = parse_port("2222")
    assert err is None and p == 2222
    p, err = parse_port("", default="22")
    assert err is None and p == 22

    # 4) 引擎事件：转发激活 -> 状态与行内文本刷新
    gui._handle_event({"type": "fwd_active", "conn": "fake", "fwd_id": fwd.id})
    assert fwd.status == FwdStatus.ACTIVE, fwd.status
    assert "已激活" in gui._fwd_status_texts[fwd.id].value

    # 5) 流量轮询：_tick_once 从 session 拉取并刷新行内文本
    gui._manager.sessions["fake"] = FakeSession(
        "fake", fwd_ids={fwd.id}, traffic=(3, 2048))
    gui._tick_once()
    assert fwd.connections == 3 and fwd.bytes_total == 2048
    assert gui._fwd_conns_texts[fwd.id].value == "3"
    assert gui._fwd_bytes_texts[fwd.id].value == "2.0 KB"

    # 6) 断连事件：该连接下转发全部停止并带错误
    gui._handle_event({"type": "disconnected", "conn": "fake",
                       "error": "SSH 连接已断开"})
    assert fwd.status == FwdStatus.STOPPED
    assert fwd.error == "SSH 连接已断开"
    gui._manager.sessions.clear()

    # 7) 对话框可构建（连接新建 / 转发新建 / 密码 / 确认删除）
    gui._conn_new()
    assert len(page.dialogs) == 1, "连接对话框应已展示"
    conn_dialog = page.dialogs[-1]
    assert conn_dialog.content.width == 460
    assert conn_dialog.content.height is None
    assert conn_dialog.actions == []
    conn_fields = conn_dialog.content.content.controls
    assert conn_fields[2].controls[1].width == 120
    auth_field = conn_fields[4].controls[0]
    assert all(control.expand is None for control in conn_fields[4].controls)
    assert all(isinstance(option, ft.DropdownOption) for option in auth_field.options)
    assert auth_field.value == "auto"
    assert conn_fields[-1].height == 40
    assert NUM.regex_string == r"^[0-9]*$"
    gui._fwd_new()
    assert len(page.dialogs) == 2, "转发对话框应已展示"
    gui._show_password_dialog("fake", "需要密码登录")
    assert len(page.dialogs) == 3, "密码对话框应已展示"
    gui._confirm("删除？", lambda: None)
    assert len(page.dialogs) == 4, "确认对话框应已展示"
    for _ in range(4):
        page.pop_dialog()

    # 8) 保存连接（config + 渲染全链路）
    gui._save_conn(ConnectionDef(name="conn2", host="example.com"), existing=None)
    assert gui._config.find("conn2") is not None
    assert gui._selected_conn == "conn2", "新建连接应被选中"
    assert len(gui._conn_list.controls) == 2
    dup = gui._save_conn(ConnectionDef(name="conn2", host="other.com"), existing=None)
    assert dup is False, "重名连接应被拒绝"
    assert gui._save_conn(
        ConnectionDef(name="conn2-renamed", host="example.net"),
        existing=gui._config.find("conn2"),
    )
    assert gui._config.find("conn2") is None
    assert gui._config.find("conn2-renamed") is not None

    # 8b) 连接对话框保存：先关闭表单，再显示成功提示，避免提示层挡住表单
    gui._selected_conn = "fake"
    gui._conn_new()
    save_dialog = page.dialogs[-1]
    save_fields = save_dialog.content.content.controls
    save_fields[0].value = "saved"
    save_fields[1].value = "example.org"
    save_fields[-1].controls[1].on_click(None)
    assert save_dialog not in page.dialogs
    assert gui._config.find("saved") is not None
    assert gui._conn_tiles["saved"].title.value == "saved"
    page.pop_dialog()  # remove success SnackBar from the fake page

    # 9) 删除转发（先入配置再删，走全链路）
    conn.port_forwards.append(fwd.to_dict())
    gui._selected_fwd = fwd.id
    gui._do_delete_fwd(fwd.id)
    assert fwd.id not in gui._forwards
    assert not conn.port_forwards, "配置中的转发记录应同步移除"
    assert len(gui._fwd_table.rows) == 0

    # 10) 删除连接（其转发一并移除）
    gui._selected_conn = "fake"
    gui._do_delete_conn("fake")
    assert gui._config.find("fake") is None
    assert gui._selected_conn is not None and gui._config.find(gui._selected_conn) is not None

    # 11) 配置已写入隔离目录
    assert CONFIG_FILE.exists(), "配置应已持久化到 PORTFWD_HOME"

    # 12) 清理：cleanup 关闭所有会话
    gui._manager.sessions["x"] = FakeSession("x")
    gui._cleanup()
    assert "x" not in gui._manager.sessions

    print("GUI SMOKE TEST PASSED ✓")


if __name__ == "__main__":
    main()


def test_gui_smoke():
    main()


# ---------------------------------------------------------------------------
# 反向转发：对话框方向切换 / 保存链路 / 方向分派 / 不显示浏览器打开
# ---------------------------------------------------------------------------
def _gui_reverse_flow() -> None:
    tmp = tempfile.mkdtemp(prefix="portfwd-gui-rev-")
    os.environ["PORTFWD_HOME"] = tmp
    os.environ["PORTFWD_DISABLE_KEYCHAIN"] = "1"

    from portfwd.config import ConnectionDef
    from portfwd.gui import PortFwdGui
    from portfwd.models import FwdDirection, FwdStatus, PortForward

    page = FakePage()
    gui = PortFwdGui()
    gui.attach(page)
    conn = ConnectionDef(name="fake", host="127.0.0.1", user="tester")
    gui._config.add(conn)
    gui._selected_conn = "fake"

    fake = FakeSession("fake", events=gui._manager.events)
    fake.client = object()  # worker 检查该属性判断客户端已建立
    gui._manager.sessions["fake"] = fake

    # 新转发对话框：方向默认正向，与旧版一致
    gui._fwd_new()
    dlg = page.dialogs[-1]
    controls = dlg.content.content.controls
    f_name, f_dir = controls[0], controls[1]
    f_local, f_rport = controls[2].controls[0], controls[2].controls[1]
    f_lhost, f_rhost = controls[3], controls[4]
    assert f_dir.value == "forward"
    assert f_lhost.visible is False, "正向不显示本机目标地址字段"
    assert f_rhost.disabled is False

    # 切到反向：字段标签/默认值/可见性随之变化
    f_dir.value = "reverse"
    f_dir.on_change(None)
    assert f_lhost.visible is True, "反向应显示本机目标地址字段"
    assert f_rhost.disabled is True and f_rhost.value == "127.0.0.1", \
        "反向远程监听地址应锁定 127.0.0.1"
    assert f_local.label == "本机目标端口" and f_local.value == "", \
        "切到反向后本机目标端口应由用户填写"
    assert f_rport.value == "18080", "反向应给出非默认的建议监听端口"

    # 保存反向规则：走全链路（config + 渲染 + worker 分派）
    f_name.value = "proxy"
    f_local.value = "7890"
    f_rport.value = "17890"
    dlg.actions[1].on_click(None)  # 保存并启动
    assert dlg not in page.dialogs, "保存成功后应关闭对话框"

    saved = [f for f in gui._forwards.values() if f.name == "proxy"]
    assert saved, "反向规则应已保存"
    saved = saved[0]
    assert saved.direction is FwdDirection.REVERSE
    assert (saved.local_host, saved.local_port) == ("127.0.0.1", 7890)
    assert (saved.remote_host, saved.remote_port) == ("127.0.0.1", 17890)
    assert any(d.get("id") == saved.id and d.get("direction") == "reverse"
               and d.get("local_host") == "127.0.0.1"
               for d in conn.port_forwards), "方向应持久化进配置"
    # 连接已启动 -> worker 按 direction 分派到 open_reverse_forward_blocking
    assert (saved.id, "127.0.0.1", 17890, "127.0.0.1", 7890) in fake.opened_rev, \
        fake.opened_rev

    # 事件流：fwd_active -> 行内状态刷新（与正向一致）
    gui._handle_event({"type": "fwd_active", "conn": "fake", "fwd_id": saved.id})
    assert saved.status == FwdStatus.ACTIVE

    # 反向行不显示"浏览器打开"按钮；正向行显示
    def open_icons(row) -> list:
        return [c.icon for c in row.controls if isinstance(c, ft.IconButton)
                and c.icon == ft.Icons.OPEN_IN_NEW]

    assert not open_icons(gui._actions_row(saved)), \
        "反向规则不能打开浏览器"
    gui._fwd_open(saved.id)  # 反向直接调用也应安全忽略

    fwd_f = PortForward(
        id=uuid.uuid4().hex[:8], name="web", local_port=8080,
        remote_host="127.0.0.1", remote_port=3000, conn="fake")
    fwd_f.status = FwdStatus.ACTIVE
    gui._forwards[fwd_f.id] = fwd_f
    assert open_icons(gui._actions_row(fwd_f)), "正向规则应保留浏览器打开按钮"

    # 冲突语义：反向不占本机监听端口；同连接内重复远程监听被拒
    assert gui._local_port_taken(7890) is False
    assert gui._rev_bind_taken("fake", "127.0.0.1", 17890) is True
    assert gui._rev_bind_taken("other", "127.0.0.1", 17890) is False

    # 表格渲染：方向列显示"服务器访问本机"
    gui._rebuild_forwards()
    rows = gui._fwd_table.rows
    assert len(rows) == 2
    cells = rows[0].cells
    assert cells[2].content.value == "服务器访问本机"

    print("GUI REVERSE FLOW TEST PASSED ✓")


def test_gui_reverse():
    _gui_reverse_flow()
