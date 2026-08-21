"""Textual TUI：VS Code Remote-SSH PORTS 面板风格的 SSH 端口转发管理器。

布局：
  左侧  - 连接列表（● 已连接 / ○ 未连接）
  右侧  - 转发表（名称/所属连接/本地地址/远程目标/状态/连接数/流量）
  底部  - 操作按钮 + Footer 快捷键提示

线程模型（与 forwarding.py 对齐）：
  - 所有阻塞 SSH/socket 操作都通过 @work(thread=True) 在 worker 线程执行，
    完成后用 call_from_thread 把结果抛回 UI 线程。
  - 引擎状态变化走 SessionManager 的事件队列，UI 用 0.5s 定时器拉取；
    流量/连接数同样由定时器轮询 session.traffic() 更新，避免高频刷表。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Select,
)
from textual.widgets.option_list import Option

from .config import CONFIG_FILE, Config, ConnectionDef
from .forwarding import ConnectionAuthError, SessionManager, open_forward_for_session
from .models import STATUS_LABEL, FwdDirection, FwdStatus, PortForward, human_bytes

log = logging.getLogger("portfwd")

# 表格列定义：(key, 表头, 宽度)。
# 注意："本地地址"在正向是本机监听端，反向是本机目标端；
# "远程目标"在正向是远程目标，反向是远程监听端（方向列消歧）。
COLUMNS: list[tuple[str, str, int]] = [
    ("name", "转发", 18),
    ("conn", "连接", 12),
    ("direction", "方向", 17),
    ("local", "本地地址", 15),
    ("remote", "远程目标", 18),
    ("status", "状态", 32),
    ("conns", "连接数", 7),
    ("bytes", "流量", 10),
]

STATUS_COLOR = {
    FwdStatus.ACTIVE: "green",
    FwdStatus.STARTING: "yellow",
    FwdStatus.STOPPING: "yellow",
    FwdStatus.ERROR: "red",
    FwdStatus.STOPPED: "86",
}


# ---------------------------------------------------------------------------
# 模态屏：新建/编辑连接
# ---------------------------------------------------------------------------
class ConnectScreen(ModalScreen[Optional[ConnectionDef]]):
    """新建或编辑一个 SSH 连接定义。"""

    CSS = """
    #cs-panel {
        width: 68;
        height: auto;
        max-height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    #cs-panel > Input, #cs-panel > Select { margin: 0 0 1 0; }
    #cs-title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    #cs-user { width: 1fr; }
    #cs-port { width: 8; }
    #cs-auth { width: 1fr; }
    #cs-pass { width: 1fr; }
    #cs-buttons { layout: horizontal; margin: 1 0 0 0; }
    #cs-buttons .spacer { width: 1fr; }
    """

    def __init__(self, existing: Optional[ConnectionDef] = None) -> None:
        super().__init__()
        self._existing = existing

    def compose(self) -> ComposeResult:
        ex = self._existing
        with Vertical(id="cs-panel"):
            yield Label("编辑连接" if ex else "新建连接", id="cs-title")
            yield Input(select_on_focus=False, 
                value=ex.name if ex else "",
                placeholder="连接名称（显示在左侧列表）",
                id="cs-name",
            )
            yield Input(select_on_focus=False, 
                value=ex.host if ex else "",
                placeholder="主机 IP/域名，或 ~/.ssh/config 里的别名",
                id="cs-host",
            )
            with Horizontal():
                yield Input(select_on_focus=False, 
                    value=ex.user if ex else "",
                    placeholder="用户（留空自动）",
                    id="cs-user",
                )
                yield Input(select_on_focus=False, 
                    value=str(ex.port) if ex else "22",
                    id="cs-port",
                    restrict=r"[0-9]*",
                )
            yield Input(select_on_focus=False, 
                value=ex.identity_file if ex else "",
                placeholder="密钥文件（留空自动，如 ~/.ssh/id_ed25519）",
                id="cs-key",
            )
            with Horizontal():
                yield Select(
                    [
                        ("自动（先密钥后密码）", "auto"),
                        ("仅密钥", "key"),
                        ("密码", "password"),
                    ],
                    value=ex.auth if ex else "auto",
                    prompt="认证方式",
                    id="cs-auth",
                )
                yield Input(select_on_focus=False, 
                    value=ex.password if ex else "",
                    password=True,
                    placeholder="密码（可选，保存到配置后免输入）",
                    id="cs-pass",
                )
            with Horizontal(id="cs-buttons"):
                yield Label("", classes="spacer")
                yield Button("取消", id="cs-cancel")
                yield Button("保存", variant="primary", id="cs-save")

    def on_mount(self) -> None:
        self.query_one("#cs-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cs-cancel":
            self.dismiss(None)
        elif event.button.id == "cs-save":
            self._save()

    def _save(self) -> None:
        name = self.query_one("#cs-name", Input).value.strip()
        host = self.query_one("#cs-host", Input).value.strip()
        if not name or not host:
            self.app.notify("名称和主机不能为空", severity="error")
            return
        port_text = self.query_one("#cs-port", Input).value.strip() or "22"
        if not (port_text.isdigit() and 1 <= int(port_text) <= 65535):
            self.app.notify("端口无效", severity="error")
            return
        conn = ConnectionDef(
            name=name,
            host=host,
            user=self.query_one("#cs-user", Input).value.strip(),
            port=int(port_text),
            identity_file=self.query_one("#cs-key", Input).value.strip(),
            auth=self.query_one("#cs-auth", Select).value or "auto",
            password=self.query_one("#cs-pass", Input).value,
        )
        if self._existing is not None:
            conn.port_forwards = self._existing.port_forwards
        self.dismiss(conn)


# ---------------------------------------------------------------------------
# 模态屏：新建转发
# ---------------------------------------------------------------------------
class ForwardScreen(ModalScreen[Optional[dict[str, Any]]]):
    """给选中连接新建一条转发。

    方向切换：
      - 访问服务器端口（正向，默认）：本机 127.0.0.1:本地端口 监听
        -> SSH -> 远程目标；可一键发现远程监听端口并填入。
      - 服务器访问本机（反向）：远程 127.0.0.1:远程监听端口 -> SSH
        -> 本机目标 host:port（如本机代理）；不暴露 0.0.0.0，
        也不需要发现远程端口。
    """

    CSS = """
    #fs-panel {
        width: 74;
        height: auto;
        max-height: 92%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    #fs-panel > Input, #fs-panel > Select { margin: 0 0 1 0; }
    #fs-title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    #fs-dir { width: 1fr; }
    #fs-rhost { width: 1fr; }
    #fs-rport { width: 8; }
    #fs-hint { color: $text-muted; }
    #fs-ports { height: 8; margin: 1 0; border: round $accent 50%; }
    #fs-buttons { layout: horizontal; margin: 1 0 0 0; }
    #fs-buttons .spacer { width: 1fr; }
    """

    def __init__(self, conn_name: str, local_port: int) -> None:
        super().__init__()
        self._conn_name = conn_name
        self._local_port = local_port

    def compose(self) -> ComposeResult:
        with Vertical(id="fs-panel"):
            yield Label(f"新建转发 · 连接：{self._conn_name}", id="fs-title")
            yield Input(select_on_focus=False, placeholder="转发名称（如 web-ui）", id="fs-name")
            yield Select(
                [
                    ("访问服务器端口（本机 → 服务器）", "forward"),
                    ("服务器访问本机（服务器 → 本机）", "reverse"),
                ],
                value="forward",
                prompt="方向",
                id="fs-dir",
            )
            yield Input(select_on_focus=False,
                value=str(self._local_port),
                placeholder="本地端口（在本机 127.0.0.1 监听）",
                id="fs-local",
                restrict=r"[0-9]*",
            )
            lhost_input = Input(select_on_focus=False,
                value="127.0.0.1",
                placeholder="本机目标地址（本机上的服务地址）",
                id="fs-lhost",
            )
            lhost_input.display = False
            yield lhost_input
            with Horizontal():
                yield Input(select_on_focus=False,
                    value="127.0.0.1",
                    placeholder="远程主机（127.0.0.1 = SSH 会话所在主机）",
                    id="fs-rhost",
                )
                yield Input(select_on_focus=False,
                    value="80",
                    placeholder="远程端口",
                    id="fs-rport",
                    restrict=r"[0-9]*",
                )
            with Horizontal():
                yield Button("发现远程端口", variant='default', id="fs-disc")
                yield Label("（需该连接已启动；选中条目自动填入上方）", id="fs-hint")
            yield OptionList(id="fs-ports")
            with Horizontal(id="fs-buttons"):
                yield Label("", classes="spacer")
                yield Button("取消", id="fs-cancel")
                yield Button("保存并启动", variant="primary", id="fs-save")

    def on_mount(self) -> None:
        self._apply_direction()
        self.query_one("#fs-name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "fs-dir":
            self._apply_direction()

    def _direction(self) -> FwdDirection:
        try:
            value = self.query_one("#fs-dir", Select).value
        except NoMatches:
            return FwdDirection.FORWARD
        try:
            return FwdDirection(str(value or "forward"))
        except ValueError:
            return FwdDirection.FORWARD

    def _apply_direction(self) -> None:
        """按当前方向切换字段标签/默认值/可见性（正向与旧版行为一致）。"""
        rev = self._direction() is FwdDirection.REVERSE
        lhost = self.query_one("#fs-lhost", Input)
        lhost.display = rev
        local = self.query_one("#fs-local", Input)
        rhost = self.query_one("#fs-rhost", Input)
        rport = self.query_one("#fs-rport", Input)
        if rev:
            # 切到反向：本机端改成"目标端口"（清空初始建议值），
            # 远程端固定 127.0.0.1 监听，远程端口给一个不冲突的建议值
            if local.value == str(self._local_port):
                local.value = ""
            local.placeholder = "本机目标端口（本机上的服务，如代理 7890）"
            rhost.value = "127.0.0.1"
            rhost.placeholder = "远程监听地址（仅支持 127.0.0.1）"
            rhost.disabled = True
            if rport.value in ("80", ""):
                rport.value = str(self.app._next_remote_port(self._conn_name))
            rport.placeholder = "远程监听端口"
        else:
            if not local.value:
                local.value = str(self._local_port)
            local.placeholder = "本地端口（在本机 127.0.0.1 监听）"
            rhost.value = "127.0.0.1"
            rhost.placeholder = "远程主机（127.0.0.1 = SSH 会话所在主机）"
            rhost.disabled = False
            rport.placeholder = "远程端口"
        self.query_one("#fs-disc", Button).disabled = rev
        self.query_one("#fs-ports", OptionList).display = not rev
        self.query_one("#fs-hint", Label).update(
            "（反向转发无需发现端口：远程监听 127.0.0.1:端口 → 本机目标）"
            if rev else "（需该连接已启动；选中条目自动填入上方）"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fs-cancel":
            self.dismiss(None)
        elif event.button.id == "fs-save":
            self._save()
        elif event.button.id == "fs-disc":
            self.app.notify("正在读取远程监听端口…", severity="information")
            self.app._list_ports_worker(self._conn_name, self)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id or ""
        host, _, port = option_id.rpartition(":")
        if host and port.isdigit():
            self.query_one("#fs-rhost", Input).value = host
            self.query_one("#fs-rport", Input).value = port

    def _save(self) -> None:
        app = self.app
        name = self.query_one("#fs-name", Input).value.strip() or "portfwd"
        direction = self._direction()
        local = self.query_one("#fs-local", Input).value.strip()
        remote_port = self.query_one("#fs-rport", Input).value.strip()
        if direction is FwdDirection.REVERSE:
            local_host = self.query_one("#fs-lhost", Input).value.strip() or "127.0.0.1"
            remote_host = "127.0.0.1"
            if not (local.isdigit() and 1 <= int(local) <= 65535):
                app.notify("本机目标端口无效", severity="error")
                return
            if not (remote_port.isdigit() and 1 <= int(remote_port) <= 65535):
                app.notify("远程监听端口无效", severity="error")
                return
            if app._rev_bind_taken(self._conn_name, remote_host, int(remote_port)):
                app.notify(
                    f"远程监听 127.0.0.1:{remote_port} 已被同一连接的反向转发占用",
                    severity="error",
                )
                return
        else:
            local_host = "127.0.0.1"
            remote_host = self.query_one("#fs-rhost", Input).value.strip() or "127.0.0.1"
            if not (local.isdigit() and 1 <= int(local) <= 65535):
                app.notify("本地端口无效", severity="error")
                return
            if not (remote_port.isdigit() and 1 <= int(remote_port) <= 65535):
                app.notify("远程端口无效", severity="error")
                return
            if app._local_port_taken(int(local)):
                app.notify(f"本地端口 {local} 已被其它转发占用", severity="error")
                return
        self.dismiss(
            {
                "name": name,
                "direction": direction.value,
                "local_host": local_host,
                "local_port": int(local),
                "remote_host": remote_host,
                "remote_port": int(remote_port),
            }
        )


# ---------------------------------------------------------------------------
# 模态屏：密码输入
# ---------------------------------------------------------------------------
class PasswordScreen(ModalScreen[Optional[str]]):
    """认证失败后弹出，让用户输入密码重试。"""

    CSS = """
    #pw-panel {
        width: 58;
        height: auto;
        border: heavy $warning;
        background: $surface;
        padding: 1 2;
    }
    #pw-msg { margin: 0 0 1 0; }
    #pw-input { margin: 0 0 1 0; }
    #pw-buttons { layout: horizontal; }
    #pw-buttons .spacer { width: 1fr; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="pw-panel"):
            yield Label(self._prompt, id="pw-msg")
            yield Input(select_on_focus=False, 
                placeholder="输入密码（留空则放弃）",
                password=True,
                id="pw-input",
            )
            with Horizontal(id="pw-buttons"):
                yield Label("", classes="spacer")
                yield Button("取消", id="pw-cancel")
                yield Button("登录", variant="primary", id="pw-ok")

    def on_mount(self) -> None:
        self.query_one("#pw-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pw-cancel":
            self.dismiss(None)
        elif event.button.id == "pw-ok":
            self.dismiss(self.query_one("#pw-input", Input).value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.input.value)


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class PortFwdApp(App):
    """portfwd 主窗口。"""

    TITLE = "portfwd"
    SUB_TITLE = "SSH 端口转发管理"

    CSS = """
    Screen {
        layout: vertical;
    }
    #body { height: 1fr; }
    #sidebar {
        width: 30;
        height: 1fr;
        border: round $primary 60%;
        padding: 1 1 0 1;
    }
    #sidebar-title { text-style: bold; color: $primary; padding: 0 0 1 0; }
    #conn-list { height: 1fr; }
    #conn-list > ListItem { height: 1; }
    #main { layout: vertical; height: 1fr; width: 1fr; }
    #fwd-table { height: 1fr; border: round $accent 60%; }
    #action-bar { height: auto; padding: 0 0 0 1; }
    #action-bar Button { margin: 1 1 1 0; }
    #action-bar-2 { height: auto; padding: 0 0 0 1; }
    #action-bar-2 Button { margin: 1 1 1 0; }
    """

    BINDINGS = [
        ("c", "conn_new", "新连接"),
        ("e", "conn_edit", "编辑连接"),
        ("x", "conn_toggle", "启/断连接"),
        ("f", "fwd_new", "新转发"),
        ("r", "fwd_toggle", "启/停转发"),
        ("d", "fwd_delete", "删除转发"),
        ("s", "save", "保存配置"),
        ("q", "quit", "退出"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config = Config()
        self._manager = SessionManager()
        # 全部已保存转发：fwd_id -> PortForward
        self._forwards: dict[str, PortForward] = {}
        self._selected_conn: Optional[str] = None
        self._selected_fwd: Optional[str] = None
        self._editing_conn_name: Optional[str] = None
        # 每个连接是否已弹过密码框（避免死循环）
        self._auth_prompted: dict[str, bool] = {}
        # 左侧列表最后写入的文案（避免无谓刷新）
        self._conn_marks: dict[str, str] = {}
        self._load_forwards()

    # -- 初始化 --------------------------------------------------------------
    def _load_forwards(self) -> None:
        for conn in self._config.connections:
            for raw in conn.port_forwards:
                try:
                    fwd = PortForward.from_dict(raw)
                except Exception:
                    log.warning("忽略损坏的转发配置: %r", raw)
                    continue
                if not fwd.id:
                    fwd.id = uuid.uuid4().hex[:8]
                fwd.conn = conn.name
                fwd.status = FwdStatus.STOPPED
                self._forwards[fwd.id] = fwd

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("连接", id="sidebar-title")
                yield ListView(id="conn-list")
            with Vertical(id="main"):
                yield DataTable(id="fwd-table")
                with Horizontal(id="action-bar"):
                    yield Button("新连接 c", id="btn-conn-new")
                    yield Button("编辑 e", id="btn-conn-edit")
                    yield Button("连接/断开 x", id="btn-conn-toggle")
                    yield Button("删除连接", variant="error", id="btn-conn-del")
                with Horizontal(id="action-bar-2"):
                    yield Button("新转发 f", variant="primary", id="btn-fwd-new")
                    yield Button("启/停 r", id="btn-fwd-toggle")
                    yield Button("删除转发 d", variant="error", id="btn-fwd-del")
                    yield Button("保存 s", id="btn-save")
        yield Footer()

    async def on_mount(self) -> None:
        dt = self.query_one("#fwd-table", DataTable)
        for key, label, width in COLUMNS:
            dt.add_column(label, key=key, width=width)
        dt.cursor_type = "row"
        await self._render_connections()
        await self._render_forwards()
        self.set_interval(0.5, self._tick)
        if self._config.autostart:
            for conn in list(self._config.connections):
                if conn.port_forwards:
                    self._connect_worker(conn.name)

    def on_unmount(self) -> None:
        try:
            self._manager.close_all()
        except Exception:
            pass

    # -- 渲染 ----------------------------------------------------------------
    async def _render_connections(self) -> None:
        lv = self.query_one("#conn-list", ListView)
        await lv.clear()
        self._conn_marks.clear()
        items: list[ListItem] = []
        for conn in self._config.connections:
            mark = "●" if self._conn_up(conn.name) else "○"
            text = f"{mark} {conn.name}"
            self._conn_marks[conn.name] = text
            items.append(ListItem(Label(text), id=conn.name))
        if items:
            await lv.extend(items)
        target: Optional[int] = None
        if self._selected_conn is not None:
            for idx, conn in enumerate(self._config.connections):
                if conn.name == self._selected_conn:
                    target = idx
                    break
        if target is None and items:
            target = 0
        if target is not None:
            lv.index = target
            self._selected_conn = self._config.connections[target].name

    async def _render_forwards(self) -> None:
        dt = self.query_one("#fwd-table", DataTable)
        rows = list(dt.ordered_rows)
        for row in rows:
            dt.remove_row(row.key)
        for fwd in self._forwards.values():
            dt.add_row(
                fwd.name,
                fwd.conn,
                fwd.direction_label,
                fwd.address,
                fwd.display_remote,
                self._status_cell(fwd),
                str(fwd.connections),
                human_bytes(fwd.bytes_total),
                key=fwd.id,
            )
        if self._forwards:
            if self._selected_fwd not in self._forwards:
                self._selected_fwd = next(iter(self._forwards))
            row = list(self._forwards.keys()).index(self._selected_fwd)
            dt.move_cursor(row=row, column=0)
        else:
            self._selected_fwd = None

    def _status_cell(self, fwd: PortForward) -> Text:
        text = STATUS_LABEL[fwd.status]
        if fwd.error:
            text = f"{text}：{fwd.error}"
        return Text(text, style=STATUS_COLOR.get(fwd.status, ""))

    def _update_fwd_row(self, fwd: PortForward) -> None:
        dt = self.query_one("#fwd-table", DataTable)
        try:
            dt.update_cell(fwd.id, "status", self._status_cell(fwd))
            dt.update_cell(fwd.id, "conns", str(fwd.connections))
            dt.update_cell(fwd.id, "bytes", human_bytes(fwd.bytes_total))
        except Exception:
            pass  # 行可能已被移除

    def _refresh_conn_marks(self) -> None:
        lv = self.query_one("#conn-list", ListView)
        for item in list(lv.children):
            if not isinstance(item, ListItem) or item.id is None:
                continue
            conn = self._config.find(item.id)
            if conn is None:
                continue
            mark = "●" if self._conn_up(conn.name) else "○"
            text = f"{mark} {conn.name}"
            if self._conn_marks.get(conn.name) == text:
                continue
            self._conn_marks[conn.name] = text
            try:
                item.query_one(Label).update(text)
            except Exception:
                pass

    # -- UI 定时器 -------------------------------------------------------------
    def _tick(self) -> None:
        for ev in self._manager.poll_events():
            self._handle_event(ev)
        for _name, session in list(self._manager.sessions.items()):
            for fwd_id in session.running_forwards():
                fwd = self._forwards.get(fwd_id)
                if fwd is None:
                    continue
                active, total = session.traffic(fwd_id)
                if (active, total) != (fwd.connections, fwd.bytes_total):
                    fwd.connections = active
                    fwd.bytes_total = total
                    self._update_fwd_row(fwd)
        self._refresh_conn_marks()

    def _handle_event(self, ev: dict[str, Any]) -> None:
        etype = ev.get("type")
        conn_name = ev.get("conn", "")
        event_session = ev.get("session")
        current_session = self._manager.get(conn_name)
        if (
            event_session
            and current_session is not None
            and event_session != getattr(current_session, "session_id", None)
        ):
            return
        if etype == "connected":
            self._refresh_conn_marks()
        elif etype == "disconnected":
            for fwd in self._forwards.values():
                if fwd.conn == conn_name and fwd.status != FwdStatus.STOPPED:
                    fwd.status = FwdStatus.STOPPED
                    fwd.error = ev.get("error") or "SSH 连接已断开"
                    fwd.connections = 0
                    self._update_fwd_row(fwd)
            self._refresh_conn_marks()
        elif etype == "fwd_active":
            fwd = self._forwards.get(ev.get("fwd_id", ""))
            if fwd is not None:
                fwd.status = FwdStatus.ACTIVE
                fwd.error = ""
                self._update_fwd_row(fwd)
        elif etype == "fwd_error":
            fwd = self._forwards.get(ev.get("fwd_id", ""))
            if fwd is not None:
                fwd.status = FwdStatus.ERROR
                fwd.error = ev.get("error") or "未知错误"
                self._update_fwd_row(fwd)

    # -- worker 线程辅助 ---------------------------------------------------------
    def _post_async(self, coro_func: Any, *args: Any) -> None:
        """在 UI 线程调度一个 async 方法（push_screen 回调已在 UI 线程）。"""
        import asyncio
        asyncio.create_task(coro_func(*args))

    def _ui(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        """把回调抛回 UI 线程（应用关闭后安全忽略）。"""
        try:
            self.call_from_thread(callback, *args, **kwargs)
        except Exception:
            pass

    # -- worker：连接 ------------------------------------------------------------
    @work(thread=True, exclusive=False, exit_on_error=False)
    def _connect_worker(self, conn_name: str, password: Optional[str] = None) -> None:
        conn = self._config.find(conn_name)
        if conn is None:
            return
        session = self._manager.get(conn_name)
        if session is None:
            session = self._manager.make(conn)
        try:
            client = getattr(session, "client", None)
            if client is not None and not session.connected:
                # A closed Session cannot be safely reused: its heartbeat has
                # already been stopped and its closed event is set.
                self._manager.drop(conn_name)
                session = self._manager.make(conn)
                client = None
            if client is None:
                pwd = password if password is not None else (conn.password or None)
                session.connect_blocking(pwd)
            self._ui(self._connect_ok, conn_name)
        except ConnectionAuthError as e:
            self._ui(self._auth_failed, conn_name, str(e))
        except Exception as e:
            self._ui(self._connect_failed, conn_name, str(e))

    def _connect_ok(self, conn_name: str) -> None:
        self.notify(f"已连接 {conn_name}", severity="information")
        self._refresh_conn_marks()
        for fwd in list(self._forwards.values()):
            if fwd.conn == conn_name and fwd.status == FwdStatus.STOPPED:
                self._start_forward_worker(conn_name, fwd.id)

    def _connect_failed(self, conn_name: str, msg: str) -> None:
        self.notify(f"连接 {conn_name} 失败：{msg}", severity="error")
        self._refresh_conn_marks()

    def _auth_failed(self, conn_name: str, msg: str) -> None:
        conn = self._config.find(conn_name)
        if conn is None or conn.auth not in ("auto", "password"):
            self.notify(msg, severity="error")
            return
        if self._auth_prompted.get(conn_name):
            self.notify(
                f"{conn_name}：认证失败（可在连接编辑里保存密码）",
                severity="error",
            )
            return
        self._auth_prompted[conn_name] = True
        self.push_screen(
            PasswordScreen(f"{msg}\n{conn_name} 需要密码登录，请输入："),
            callback=lambda pwd: self._on_password(conn_name, pwd),
        )

    def _on_password(self, conn_name: str, pwd: Optional[str]) -> None:
        if not pwd:
            self._auth_prompted.pop(conn_name, None)
            return
        self._connect_worker(conn_name, pwd)

    # -- worker：转发 ------------------------------------------------------------
    @work(thread=True, exclusive=False, exit_on_error=False)
    def _start_forward_worker(self, conn_name: str, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None or fwd.status in (FwdStatus.STARTING, FwdStatus.ACTIVE):
            return
        fwd.status = FwdStatus.STARTING
        fwd.error = ""
        self._ui(self._update_fwd_row, fwd)
        conn = self._config.find(conn_name)
        if conn is None:
            self._ui(self._fwd_error, fwd_id, "所属连接不存在")
            return
        session = self._manager.get(conn_name)
        if session is None:
            session = self._manager.make(conn)
        try:
            client = getattr(session, "client", None)
            if client is not None and not session.connected:
                self._manager.drop(conn_name)
                session = self._manager.make(conn)
                client = None
            if client is None:
                session.connect_blocking(conn.password or None)
            open_forward_for_session(session, fwd)
        except ConnectionAuthError as e:
            self._ui(self._auth_failed, conn_name, str(e))
            self._ui(self._fwd_error, fwd_id, str(e))
        except Exception as e:
            self._ui(self._fwd_error, fwd_id, str(e))

    @work(thread=True, exclusive=False, exit_on_error=False)
    def _stop_forward_worker(self, conn_name: str, fwd_id: str) -> None:
        session = self._manager.get(conn_name)
        if session is not None:
            try:
                session.close_forward_blocking(fwd_id)
            except Exception:
                pass
        fwd = self._forwards.get(fwd_id)
        if fwd is not None:
            fwd.status = FwdStatus.STOPPED
            fwd.error = ""
            fwd.connections = 0
            self._ui(self._update_fwd_row, fwd)

    def _fwd_error(self, fwd_id: str, msg: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None:
            return
        fwd.status = FwdStatus.ERROR
        fwd.error = msg
        self._update_fwd_row(fwd)

    # -- worker：断开连接 ----------------------------------------------------------
    @work(thread=True, exclusive=False, exit_on_error=False)
    def _stop_conn_worker(self, conn_name: str) -> None:
        self._manager.drop(conn_name)
        for fwd in self._forwards.values():
            if fwd.conn == conn_name and fwd.status != FwdStatus.STOPPED:
                fwd.status = FwdStatus.STOPPED
                fwd.connections = 0
                fwd.error = ""
                self._ui(self._update_fwd_row, fwd)
        self._ui(self._refresh_conn_marks)
        self._ui(self.notify, f"已断开 {conn_name}", severity="information")

    # -- worker：远程端口发现 --------------------------------------------------------
    @work(thread=True, exclusive=False, exit_on_error=False)
    def _list_ports_worker(self, conn_name: str, screen: ForwardScreen) -> None:
        session = self._manager.get(conn_name)
        try:
            if session is None or not session.connected:
                raise RuntimeError("SSH 尚未连接，请先启动该连接")
            rows = session.list_remote_ports()
        except Exception as e:
            self._ui(self.notify, f"读取远程端口失败：{e}", severity="error")
            return
        self._ui(self._fill_remote_ports, screen, rows)

    def _fill_remote_ports(self, screen: ForwardScreen, rows: list[tuple[str, str, int]]) -> None:
        try:
            ol = screen.query_one("#fs-ports", OptionList)
        except Exception:
            return  # 屏幕已关闭
        ol.clear_options()
        for ip, proto, port in rows:
            ol.add_option(Option(f"{proto:<3} {ip}:{port}", id=f"{ip}:{port}"))
        self.notify(f"发现 {len(rows)} 个远程监听端口", severity="information")

    # -- 选择事件 -----------------------------------------------------------------
    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.id:
            self._selected_conn = event.item.id

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is not None and event.item.id:
            self._selected_conn = event.item.id
            self._post_async(self.action_conn_toggle)

    def _fwd_id_at_row(self, row: int) -> Optional[str]:
        """行号 -> fwd_id（与 _render_forwards 的插入顺序一致）。"""
        ids = list(self._forwards.keys())
        if 0 <= row < len(ids):
            return ids[row]
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_fwd = self._fwd_id_at_row(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_fwd = self._fwd_id_at_row(event.cursor_row)
        self.action_fwd_toggle()

    # -- 按钮 -------------------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-conn-new":
            self.action_conn_new()
        elif button_id == "btn-conn-edit":
            self.action_conn_edit()
        elif button_id == "btn-conn-toggle":
            self.action_conn_toggle()
        elif button_id == "btn-conn-del":
            self._post_async(self.action_conn_delete)
        elif button_id == "btn-fwd-new":
            self.action_fwd_new()
        elif button_id == "btn-fwd-toggle":
            self.action_fwd_toggle()
        elif button_id == "btn-fwd-del":
            self.action_fwd_delete()
        elif button_id == "btn-save":
            self.action_save()

    # -- 连接操作 -----------------------------------------------------------------
    def action_conn_new(self) -> None:
        self._editing_conn_name = None
        self.push_screen(
            ConnectScreen(),
            callback=lambda c: self._post_async(self._on_conn_saved, c),
        )

    def action_conn_edit(self) -> None:
        conn = (
            self._config.find(self._selected_conn)
            if self._selected_conn
            else None
        )
        if conn is None:
            self.notify("请先在左侧选择一个连接", severity="warning")
            return
        self._editing_conn_name = conn.name
        self.push_screen(
            ConnectScreen(conn),
            callback=lambda c: self._post_async(self._on_conn_saved, c),
        )

    def action_conn_toggle(self) -> None:
        conn_name = self._selected_conn
        if not conn_name or self._config.find(conn_name) is None:
            self.notify("请先在左侧选择一个连接", severity="warning")
            return
        session = self._manager.get(conn_name)
        if session is not None and session.connected:
            self._stop_conn_worker(conn_name)
        else:
            self._auth_prompted.pop(conn_name, None)
            self.notify(f"正在连接 {conn_name} …", severity="information")
            self._connect_worker(conn_name)

    async def action_conn_delete(self) -> None:
        conn_name = self._selected_conn
        conn = self._config.find(conn_name) if conn_name else None
        if conn is None:
            self.notify("请先在左侧选择一个连接", severity="warning")
            return
        self._stop_conn_worker(conn_name)
        self._config.remove(conn_name)
        for fwd_id in [f.id for f in self._forwards.values() if f.conn == conn_name]:
            self._forwards.pop(fwd_id, None)
        self._selected_conn = None
        self._config.save()
        await self._render_connections()
        await self._render_forwards()
        self.notify(f"已删除连接 {conn_name} 及其转发", severity="information")

    async def _on_conn_saved(self, conn: Optional[ConnectionDef]) -> None:
        if conn is None:
            self._editing_conn_name = None
            return
        old_name = self._editing_conn_name
        self._editing_conn_name = None
        old = self._config.find(old_name) if old_name else None
        if old is None and self._config.find(conn.name) is not None:
            self.notify(f"连接名 {conn.name} 已存在", severity="error")
            return
        if old is not None:
            if conn.name != old.name and self._config.find(conn.name) is not None:
                self.notify(f"连接名 {conn.name} 已存在", severity="error")
                return
            conn.port_forwards = old.port_forwards
            connection_changed = (
                old.host, old.user, old.port, old.identity_file,
                old.auth, old.password,
            ) != (
                conn.host, conn.user, conn.port, conn.identity_file,
                conn.auth, conn.password,
            )
            if conn.name != old.name:
                for fwd in self._forwards.values():
                    if fwd.conn == old.name:
                        fwd.conn = conn.name
                self._stop_conn_worker(old.name)
                self._config.remove(old.name)
                self._config.add(conn)
            else:
                if connection_changed:
                    self._stop_conn_worker(old.name)
                for idx, c in enumerate(self._config.connections):
                    if c.name == conn.name:
                        self._config.connections[idx] = conn
                        break
        else:
            self._config.add(conn)
        self._config.save()
        await self._render_connections()
        self.notify(f"已保存连接 {conn.name}", severity="information")

    # -- 转发操作 -----------------------------------------------------------------
    def action_fwd_new(self) -> None:
        conn = (
            self._config.find(self._selected_conn)
            if self._selected_conn
            else None
        )
        if conn is None:
            self.notify("请先在左侧选择一个连接", severity="warning")
            return
        self.push_screen(
            ForwardScreen(conn.name, self._next_local_port()),
            callback=lambda d: self._post_async(self._on_fwd_saved, d),
        )

    async def _on_fwd_saved(self, data: Optional[dict[str, Any]]) -> None:
        if data is None:
            return
        conn = (
            self._config.find(self._selected_conn)
            if self._selected_conn
            else None
        )
        if conn is None:
            # 连接可能已删除（例如用户在删除连接后延迟关闭了转发屏幕），静默丢弃
            log.info("忽略转发保存：连接 %r 不存在", self._selected_conn)
            return
        fwd = PortForward(
            id=uuid.uuid4().hex[:8],
            name=data["name"],
            local_port=data["local_port"],
            remote_host=data["remote_host"],
            remote_port=data["remote_port"],
            direction=data.get("direction", FwdDirection.FORWARD.value),
            local_host=data.get("local_host", "127.0.0.1"),
            conn=conn.name,
        )
        self._forwards[fwd.id] = fwd
        conn.port_forwards.append(fwd.to_dict())
        self._config.save()
        self._selected_fwd = fwd.id
        await self._render_forwards()
        session = self._manager.get(conn.name)
        if session is not None and session.connected:
            self._start_forward_worker(conn.name, fwd.id)
        else:
            self.notify(
                f"已添加转发 {fwd.name}（连接未启动，启动连接时会自动生效）",
                severity="information",
            )

    def action_fwd_toggle(self) -> None:
        fwd = self._forwards.get(self._selected_fwd or "")
        if fwd is None:
            self.notify("请先在表中选择一条转发", severity="warning")
            return
        if fwd.status in (FwdStatus.ACTIVE, FwdStatus.STARTING):
            self._stop_forward_worker(fwd.conn, fwd.id)
        else:
            self._start_forward_worker(fwd.conn, fwd.id)

    async def action_fwd_delete(self) -> None:
        fwd = self._forwards.get(self._selected_fwd or "")
        if fwd is None:
            self.notify("请先在表中选择一条转发", severity="warning")
            return
        if fwd.status in (FwdStatus.ACTIVE, FwdStatus.STARTING):
            self._stop_forward_worker(fwd.conn, fwd.id)
        conn = self._config.find(fwd.conn)
        if conn is not None:
            conn.port_forwards = [
                d for d in conn.port_forwards if d.get("id") != fwd.id
            ]
        self._forwards.pop(fwd.id, None)
        self._selected_fwd = None
        self._config.save()
        await self._render_forwards()
        self.notify(f"已删除转发 {fwd.name}", severity="information")

    # -- 其它 -------------------------------------------------------------------
    def action_save(self) -> None:
        self._config.save()
        self.notify(f"配置已保存到 {CONFIG_FILE}", severity="information")

    def _conn_up(self, name: str) -> bool:
        session = self._manager.get(name)
        return bool(session is not None and session.connected)

    def _local_port_taken(self, port: int) -> bool:
        # 只有正向规则在本机起监听，反向的 local 端是目标不占端口
        return any(
            f.local_port == port and f.direction is FwdDirection.FORWARD
            for f in self._forwards.values()
        )

    def _rev_bind_taken(self, conn_name: str, host: str, port: int) -> bool:
        # 反向：同一 SSH 连接内不允许重复的远程监听 host:port
        return any(
            f.direction is FwdDirection.REVERSE
            and f.conn == conn_name
            and f.remote_host == host
            and f.remote_port == port
            for f in self._forwards.values()
        )

    def _next_local_port(self) -> int:
        taken = {
            f.local_port for f in self._forwards.values()
            if f.direction is FwdDirection.FORWARD
        }
        port = 8080
        while port in taken:
            port += 1
        return port

    def _next_remote_port(self, conn_name: str) -> int:
        taken = {
            f.remote_port for f in self._forwards.values()
            if f.direction is FwdDirection.REVERSE and f.conn == conn_name
        }
        port = 18080
        while port in taken:
            port += 1
        return port
