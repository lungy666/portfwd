"""Flet GUI：图形化 SSH 端口转发管理器（VS Code Remote-SSH PORTS 面板风格）。

布局：
  顶栏    - 新连接 / 新转发 / 保存
  左侧    - 连接列表（点击选中；● 已连接 / ○ 未连接，行内 连接/编辑/删除）
  右侧    - 转发表（名称 / 所属连接 / 本地地址 / 远程目标 / 状态 / 连接数 / 流量 / 操作）

线程模型（与 forwarding.py 对齐）：
  - 所有阻塞 SSH 操作经 page.run_thread 在 worker 线程执行，
    结果用 page.run_task 抛回 UI 事件循环（run_coroutine_threadsafe，线程安全）。
  - 引擎事件与流量由 0.5s 异步轮询任务拉取（无 page.timer，用 run_task + asyncio.sleep）。
  - 退出（关窗 / 进程退出）时 atexit 统一关闭所有 SSH 会话并释放本地端口。
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import uuid
from typing import Any, Optional

import flet as ft

from .config import CONFIG_FILE, ConnectionDef, Config
from .forwarding import ConnectionAuthError, SessionManager
from .models import FwdStatus, PortForward, STATUS_LABEL, human_bytes

log = logging.getLogger("portfwd")

NUM = ft.NumbersOnlyInputFilter()

STATUS_COLOR = {
    FwdStatus.ACTIVE: ft.Colors.GREEN,
    FwdStatus.STARTING: ft.Colors.ORANGE,
    FwdStatus.STOPPING: ft.Colors.ORANGE,
    FwdStatus.ERROR: ft.Colors.RED,
    FwdStatus.STOPPED: ft.Colors.GREY,
}

# 认证方式：(显示文案, 存储值)
AUTH_OPTIONS = [
    ("自动（先密钥后密码）", "auto"),
    ("仅密钥", "key"),
    ("密码", "password"),
]
_AUTH_DISPLAY = {code: disp for disp, code in AUTH_OPTIONS}


def _auth_display(code: str) -> str:
    return _AUTH_DISPLAY.get(code, AUTH_OPTIONS[0][0])


def parse_port(text: str, default: str = "") -> tuple[Optional[int], Optional[str]]:
    """解析并校验端口号，返回 (端口, 错误文案)；至多一个非 None。"""
    t = (text or "").strip() or default
    if not (t.isdigit() and 1 <= int(t) <= 65535):
        return None, "端口无效"
    return int(t), None


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class PortFwdGui:
    """portfwd GUI 主控制器（UI 事件循环线程内操作控件，阻塞操作走 worker）。"""

    def __init__(self) -> None:
        self._config = Config()
        self._manager = SessionManager()
        # 全部已保存转发：fwd_id -> PortForward
        self._forwards: dict[str, PortForward] = {}
        self._selected_conn: Optional[str] = None
        self._selected_fwd: Optional[str] = None
        # 每个连接是否已弹过密码框（避免死循环）
        self._auth_prompted: dict[str, bool] = {}

        self._page: Optional[ft.Page] = None
        self._stopping = False
        self._conn_list: Optional[ft.ListView] = None
        self._fwd_table: Optional[ft.DataTable] = None
        # 行内可增量刷新的控件引用（避免整表重建导致闪烁/丢选择）
        self._fwd_status_texts: dict[str, ft.Text] = {}
        self._fwd_conns_texts: dict[str, ft.Text] = {}
        self._fwd_bytes_texts: dict[str, ft.Text] = {}
        self._fwd_action_cells: dict[str, ft.DataCell] = {}
        self._fwd_rows: dict[str, ft.DataRow] = {}
        self._conn_tiles: dict[str, ft.ListTile] = {}

    # -- 入口 ---------------------------------------------------------------
    def main(self, page: ft.Page) -> None:
        """`ft.app(target=main)` 入口：构建 UI 并启动轮询/自动恢复。"""
        self.attach(page)
        self.start()

    def attach(self, page: ft.Page) -> None:
        """绑定页面并构建 UI（不含后台任务；无头测试可只调到这里）。"""
        self._page = page
        self._stopping = False
        atexit.register(self._cleanup)
        page.on_close = lambda _e: self._on_page_close()
        self._load_forwards()

        page.title = "portfwd — SSH 端口转发"
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.window.width = 1100
        page.window.height = 700
        page.window.min_width = 880
        page.window.min_height = 540

        self._build_ui()

    def start(self) -> None:
        """启动流量/事件轮询，并按配置自动恢复转发。"""
        assert self._page is not None
        self._page.run_task(self._tick_loop)
        if self._config.autostart:
            for conn in list(self._config.connections):
                if conn.port_forwards:
                    self._run(self._connect_worker, conn.name)

    def _cleanup(self) -> None:
        self._stopping = True
        try:
            self._manager.close_all()
        except Exception:
            pass

    def _on_page_close(self) -> None:
        self._cleanup()

    def _close_window(self) -> None:
        self._cleanup()
        page = self._page
        if page is None:
            return
        try:
            page.window.close()
        except Exception:
            try:
                page.close()
            except Exception:
                pass

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

    # -- UI 构建 -------------------------------------------------------------
    def _build_ui(self) -> None:
        assert self._page is not None
        page = self._page

        self._conn_list = ft.ListView(
            controls=[], spacing=4, item_extent=64, expand=True,
            padding=ft.Padding(left=8, top=4, right=8, bottom=8),
        )
        self._fwd_table = ft.DataTable(
            columns=[
                ft.DataColumn(label=h)
                for h in ("转发", "连接", "本地地址", "远程目标", "状态", "连接数", "流量", "操作")
            ],
            column_spacing=28,
            border=ft.Border(
                top=ft.BorderSide(1, ft.Colors.GREY),
                bottom=ft.BorderSide(1, ft.Colors.GREY),
                left=ft.BorderSide(1, ft.Colors.GREY),
                right=ft.BorderSide(1, ft.Colors.GREY),
            ),
            border_radius=8,
            data_row_min_height=40,
        )

        autostart = ft.Switch(
            label="启动时恢复",
            value=self._config.autostart,
            on_change=lambda e: self._set_autostart(bool(e.control.value)),
        )
        toolbar = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.LAN, color=ft.Colors.BLUE_GREY),
                    ft.Text("portfwd", size=16, weight=ft.FontWeight.BOLD),
                    ft.Text("SSH 端口转发", size=12, color=ft.Colors.GREY),
                    ft.Container(expand=True),
                    ft.ElevatedButton("新连接", icon=ft.Icons.ADD,
                                      on_click=lambda _e: self._conn_new()),
                    ft.ElevatedButton("新转发", icon=ft.Icons.ADD,
                                      on_click=lambda _e: self._fwd_new()),
                    ft.OutlinedButton("保存", icon=ft.Icons.SAVE,
                                      on_click=lambda _e: self._save()),
                    autostart,
                    ft.IconButton(ft.Icons.CLOSE, tooltip="退出",
                                  on_click=lambda _e: self._close_window()),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=16, top=10, right=16, bottom=10),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.GREY)),
        )
        sidebar = ft.Container(
            width=280,
            border=ft.Border(right=ft.BorderSide(1, ft.Colors.GREY)),
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Text("连接", size=13, weight=ft.FontWeight.BOLD,
                                        color=ft.Colors.GREY),
                        padding=ft.Padding(left=16, top=12, bottom=4),
                    ),
                    ft.Container(expand=True, content=self._conn_list),
                ],
            ),
        )
        main_area = ft.Container(
            expand=True,
            padding=ft.Padding(left=16, top=12, right=16, bottom=16),
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text("转发", size=15, weight=ft.FontWeight.BOLD),
                            ft.Container(expand=True),
                            ft.Text("选中连接后点「新转发」；行内按钮 启停 / 打开 / 删除",
                                    size=12, color=ft.Colors.GREY),
                        ]
                    ),
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            controls=[self._fwd_table],
                            scroll=ft.ScrollMode.AUTO,
                            spacing=10,
                        ),
                    ),
                ],
                spacing=10,
            ),
        )
        page.add(
            ft.Column(
                controls=[
                    toolbar,
                    ft.Row(controls=[sidebar, main_area], expand=True, spacing=0),
                ],
                expand=True,
                spacing=0,
            )
        )
        self._rebuild_connections()
        self._rebuild_forwards()

    # -- 渲染 ----------------------------------------------------------------
    def _conn_tile(self, conn: ConnectionDef) -> ft.ListTile:
        up = self._conn_up(conn.name)
        selected = self._selected_conn == conn.name
        return ft.ListTile(
            dense=True,
            height=64,
            min_height=64,
            selected=selected,
            leading=ft.Icon(
                ft.Icons.CIRCLE if up else ft.Icons.RADIO_BUTTON_UNCHECKED,
                color=ft.Colors.GREEN if up else ft.Colors.GREY,
                size=14,
            ),
            title=ft.Text(conn.name, size=14, max_lines=1, no_wrap=True,
                          overflow=ft.TextOverflow.ELLIPSIS,
                          weight=ft.FontWeight.BOLD if selected else None),
            subtitle=ft.Text(
                f"{conn.user or '自动'}@{conn.host}:{conn.port}",
                size=12,
                color=ft.Colors.GREY,
                max_lines=1,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            trailing=ft.Row(
                controls=[
                    ft.IconButton(
                        ft.Icons.LINK_OFF if up else ft.Icons.LINK,
                        icon_size=18,
                        icon_color=ft.Colors.RED if up else ft.Colors.GREEN,
                        tooltip="断开" if up else "连接",
                        on_click=lambda _e, n=conn.name: self._conn_toggle(n),
                    ),
                    ft.IconButton(ft.Icons.EDIT, icon_size=18, tooltip="编辑",
                                  on_click=lambda _e, n=conn.name: self._conn_edit(n)),
                    ft.IconButton(ft.Icons.DELETE, icon_size=18, tooltip="删除",
                                  icon_color=ft.Colors.GREY,
                                  on_click=lambda _e, n=conn.name: self._conn_delete(n)),
                ],
                spacing=0,
                tight=True,
            ),
            on_click=lambda _e, n=conn.name: self._select_conn(n),
        )

    def _rebuild_connections(self) -> None:
        lv = self._conn_list
        if lv is None:
            return
        tiles = []
        self._conn_tiles.clear()
        for conn in self._config.connections:
            tile = self._conn_tile(conn)
            self._conn_tiles[conn.name] = tile
            tiles.append(tile)
        lv.controls = tiles
        lv.update()
        if self._selected_conn is None and self._config.connections:
            self._selected_conn = self._config.connections[0].name
            self._rebuild_connections()

    def _actions_row(self, fwd: PortForward) -> ft.Control:
        running = fwd.status in (FwdStatus.ACTIVE, FwdStatus.STARTING)
        btns = [
            ft.IconButton(
                ft.Icons.STOP if running else ft.Icons.PLAY_ARROW,
                icon_size=18,
                icon_color=ft.Colors.RED if running else ft.Colors.GREEN,
                tooltip="停止" if running else "启动",
                on_click=lambda _e, i=fwd.id: self._fwd_toggle(i),
            ),
        ]
        if fwd.status == FwdStatus.ACTIVE:
            btns.append(
                ft.IconButton(ft.Icons.OPEN_IN_NEW, icon_size=18, tooltip="浏览器打开",
                              on_click=lambda _e, i=fwd.id: self._fwd_open(i))
            )
        btns.append(
            ft.IconButton(ft.Icons.DELETE, icon_size=18, icon_color=ft.Colors.GREY,
                          tooltip="删除",
                          on_click=lambda _e, i=fwd.id: self._fwd_delete(i))
        )
        return ft.Row(controls=btns, spacing=2)

    @staticmethod
    def _status_text(fwd: PortForward) -> str:
        text = STATUS_LABEL[fwd.status]
        if fwd.error:
            text = f"{text}：{fwd.error}"
        return text

    def _rebuild_forwards(self) -> None:
        table = self._fwd_table
        if table is None:
            return
        rows = []
        self._fwd_status_texts.clear()
        self._fwd_conns_texts.clear()
        self._fwd_bytes_texts.clear()
        self._fwd_action_cells.clear()
        self._fwd_rows.clear()
        for fwd in self._forwards.values():
            st = ft.Text(self._status_text(fwd), size=13,
                         color=STATUS_COLOR[fwd.status])
            ct = ft.Text(str(fwd.connections), size=13)
            bt = ft.Text(human_bytes(fwd.bytes_total), size=13)
            cell_act = ft.DataCell(content=self._actions_row(fwd))
            self._fwd_status_texts[fwd.id] = st
            self._fwd_conns_texts[fwd.id] = ct
            self._fwd_bytes_texts[fwd.id] = bt
            self._fwd_action_cells[fwd.id] = cell_act
            row = ft.DataRow(
                cells=[
                    ft.DataCell(content=ft.Text(fwd.name, size=13)),
                    ft.DataCell(content=ft.Text(fwd.conn, size=13)),
                    ft.DataCell(content=ft.Text(fwd.address, size=13,
                                                font_family="monospace")),
                    ft.DataCell(content=ft.Text(fwd.display_remote, size=13,
                                                font_family="monospace")),
                    ft.DataCell(content=st),
                    ft.DataCell(content=ct),
                    ft.DataCell(content=bt),
                    cell_act,
                ],
                selected=self._selected_fwd == fwd.id,
                on_select_change=lambda _e, i=fwd.id: self._select_fwd(i),
            )
            self._fwd_rows[fwd.id] = row
            rows.append(row)
        table.rows = rows
        table.update()
        if self._forwards and self._selected_fwd not in self._forwards:
            self._selected_fwd = next(iter(self._forwards))
            self._refresh_fwd_selection()

    def _refresh_fwd_selection(self) -> None:
        for fwd_id, row in self._fwd_rows.items():
            want = fwd_id == self._selected_fwd
            if row.selected != want:
                row.selected = want
                row.update()

    def _refresh_traffic(self, fwd: PortForward) -> None:
        ct = self._fwd_conns_texts.get(fwd.id)
        bt = self._fwd_bytes_texts.get(fwd.id)
        if ct is not None and ct.value != str(fwd.connections):
            ct.value = str(fwd.connections)
            ct.update()
        if bt is not None:
            new = human_bytes(fwd.bytes_total)
            if bt.value != new:
                bt.value = new
                bt.update()

    def _refresh_fwd(self, fwd: PortForward) -> None:
        """状态/操作变化后的行内增量刷新；控件引用失效时退回整表重建。"""
        try:
            st = self._fwd_status_texts.get(fwd.id)
            if st is None:
                self._rebuild_forwards()
                return
            st.value = self._status_text(fwd)
            st.color = STATUS_COLOR[fwd.status]
            st.update()
            cell = self._fwd_action_cells.get(fwd.id)
            if cell is not None:
                cell.content = self._actions_row(fwd)
                cell.update()
            self._refresh_traffic(fwd)
        except Exception:
            self._rebuild_forwards()

    # -- 选择 -----------------------------------------------------------------
    def _select_conn(self, name: str) -> None:
        if self._selected_conn == name:
            return
        self._selected_conn = name
        self._rebuild_connections()

    def _select_fwd(self, fwd_id: str) -> None:
        if self._selected_fwd == fwd_id:
            return
        self._selected_fwd = fwd_id
        self._refresh_fwd_selection()

    # -- 轮询 -----------------------------------------------------------------
    async def _tick_loop(self) -> None:
        while not self._stopping:
            try:
                self._tick_once()
            except Exception:
                log.exception("轮询失败")
            await asyncio.sleep(0.5)

    def _set_autostart(self, enabled: bool) -> None:
        self._config.autostart = enabled
        self._config.save()

    def _tick_once(self) -> None:
        """单次轮询：拉引擎事件 + 各运行中转发的流量（UI 线程调用）。"""
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
                    self._refresh_traffic(fwd)

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
            self._rebuild_connections()
        elif etype == "disconnected":
            for fwd in self._forwards.values():
                if fwd.conn == conn_name and fwd.status != FwdStatus.STOPPED:
                    fwd.status = FwdStatus.STOPPED
                    fwd.error = ev.get("error") or "SSH 连接已断开"
                    fwd.connections = 0
                    self._refresh_fwd(fwd)
            self._rebuild_connections()
        elif etype == "fwd_active":
            fwd = self._forwards.get(ev.get("fwd_id", ""))
            if fwd is not None:
                fwd.status = FwdStatus.ACTIVE
                fwd.error = ""
                self._refresh_fwd(fwd)
        elif etype == "fwd_error":
            fwd = self._forwards.get(ev.get("fwd_id", ""))
            if fwd is not None:
                fwd.status = FwdStatus.ERROR
                fwd.error = ev.get("error") or "未知错误"
                self._refresh_fwd(fwd)

    # -- worker 线程辅助 ---------------------------------------------------------
    def _run(self, fn: Any, *args: Any) -> None:
        """在 worker 线程执行阻塞函数（应用关闭后安全忽略）。"""
        page = self._page
        if page is None:
            return
        try:
            page.run_thread(fn, *args)
        except Exception:
            pass

    def _ui(self, coro_fn: Any, *args: Any) -> None:
        """把协程抛回 UI 事件循环（应用关闭后安全忽略）。"""
        page = self._page
        if page is None:
            return
        try:
            page.run_task(coro_fn, *args)
        except Exception:
            pass

    # -- worker：连接 ------------------------------------------------------------
    def _connect_worker(self, conn_name: str, password: Optional[str] = None) -> None:
        conn = self._config.find(conn_name)
        if conn is None:
            return
        try:
            session = self._manager.get(conn_name) or self._manager.make(conn)
            client = getattr(session, "client", None)
            if client is not None and not session.connected:
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

    async def _connect_ok(self, conn_name: str) -> None:
        self._notify_sync(f"已连接 {conn_name}")
        self._rebuild_connections()
        for fwd in list(self._forwards.values()):
            if fwd.conn == conn_name and fwd.status == FwdStatus.STOPPED:
                self._run(self._start_fwd_worker, conn_name, fwd.id)

    async def _connect_failed(self, conn_name: str, msg: str) -> None:
        self._notify_sync(f"连接 {conn_name} 失败：{msg}", ft.Colors.RED)
        self._rebuild_connections()

    async def _auth_failed(self, conn_name: str, msg: str) -> None:
        conn = self._config.find(conn_name)
        if conn is None or conn.auth not in ("auto", "password"):
            self._notify_sync(msg, ft.Colors.RED)
            return
        if self._auth_prompted.get(conn_name):
            self._notify_sync(
                f"{conn_name}：认证失败（可在连接编辑里保存密码）", ft.Colors.RED
            )
            return
        self._auth_prompted[conn_name] = True
        self._show_password_dialog(conn_name, msg)

    # -- worker：转发 ------------------------------------------------------------
    def _start_fwd_worker(self, conn_name: str, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None or fwd.status in (FwdStatus.STARTING, FwdStatus.ACTIVE):
            return
        fwd.status = FwdStatus.STARTING
        fwd.error = ""
        self._ui(self._fwd_starting, fwd)
        conn = self._config.find(conn_name)
        if conn is None:
            self._ui(self._fwd_error, fwd_id, "所属连接不存在")
            return
        try:
            session = self._manager.get(conn_name) or self._manager.make(conn)
            client = getattr(session, "client", None)
            if client is not None and not session.connected:
                self._manager.drop(conn_name)
                session = self._manager.make(conn)
                client = None
            if client is None:
                session.connect_blocking(conn.password or None)
            session.open_forward_blocking(
                fwd_id, fwd.local_port, fwd.remote_host, fwd.remote_port
            )
        except ConnectionAuthError as e:
            self._ui(self._auth_failed, conn_name, str(e))
            self._ui(self._fwd_error, fwd_id, str(e))
        except Exception as e:
            self._ui(self._fwd_error, fwd_id, str(e))

    def _stop_fwd_worker(self, conn_name: str, fwd_id: str) -> None:
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
        self._ui(self._fwd_stopped, fwd_id)

    # -- worker：断开连接 ----------------------------------------------------------
    def _stop_conn_worker(self, conn_name: str) -> None:
        try:
            self._manager.drop(conn_name)
        except Exception:
            pass
        for fwd in self._forwards.values():
            if fwd.conn == conn_name and fwd.status != FwdStatus.STOPPED:
                fwd.status = FwdStatus.STOPPED
                fwd.connections = 0
                fwd.error = ""
        self._ui(self._conn_stopped, conn_name)

    # -- worker：远程端口发现 --------------------------------------------------------
    def _list_ports_worker(self, conn_name: str, dd: ft.Dropdown,
                           hint: ft.Text) -> None:
        session = self._manager.get(conn_name)
        try:
            if session is None or not session.connected:
                raise RuntimeError("SSH 尚未连接，请先启动该连接")
            rows = session.list_remote_ports()
        except Exception as e:
            self._ui(self._notify, f"读取远程端口失败：{e}", ft.Colors.RED)
            return
        self._ui(self._fill_ports, dd, hint, rows)

    # -- worker 结果（UI 循环） ------------------------------------------------------
    async def _notify(self, msg: str, color: Optional[ft.Colors] = None) -> None:
        self._notify_sync(msg, color)

    def _notify_sync(self, msg: str, color: Optional[ft.Colors] = None) -> None:
        page = self._page
        if page is None:
            return
        try:
            page.show_dialog(ft.SnackBar(ft.Text(msg, size=13), bgcolor=color))
        except Exception:
            pass

    async def _fwd_starting(self, fwd: PortForward) -> None:
        self._refresh_fwd(fwd)

    async def _fwd_error(self, fwd_id: str, msg: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None:
            return
        fwd.status = FwdStatus.ERROR
        fwd.error = msg
        self._refresh_fwd(fwd)

    async def _fwd_stopped(self, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is not None:
            self._refresh_fwd(fwd)

    async def _conn_stopped(self, conn_name: str) -> None:
        self._rebuild_connections()
        self._rebuild_forwards()
        self._notify_sync(f"已断开 {conn_name}")

    async def _fill_ports(self, dd: ft.Dropdown, hint: ft.Text,
                          rows: list[tuple[str, str, int]]) -> None:
        dd.options = [
            ft.DropdownOption(key=f"{ip}:{port} · {proto}")
            for ip, proto, port in rows
        ]
        dd.update()
        hint.value = f"发现 {len(rows)} 个监听端口，选中自动填入"
        hint.update()
        self._notify_sync(f"发现 {len(rows)} 个远程监听端口")

    # -- 连接操作 -----------------------------------------------------------------
    def _conn_new(self) -> None:
        self._show_conn_dialog(None)

    def _conn_edit(self, name: str) -> None:
        conn = self._config.find(name)
        if conn is None:
            return
        self._show_conn_dialog(conn)

    def _conn_toggle(self, name: str) -> None:
        self._select_conn(name)
        session = self._manager.get(name)
        if session is not None and session.connected:
            self._run(self._stop_conn_worker, name)
        else:
            self._auth_prompted.pop(name, None)
            self._notify_sync(f"正在连接 {name} …")
            self._run(self._connect_worker, name)

    def _conn_delete(self, name: str) -> None:
        self._confirm(f"删除连接 {name} 及其全部转发？",
                      lambda: self._do_delete_conn(name))

    def _do_delete_conn(self, conn_name: str) -> None:
        self._run(self._stop_conn_worker, conn_name)
        self._config.remove(conn_name)
        for fwd_id in [f.id for f in self._forwards.values() if f.conn == conn_name]:
            self._forwards.pop(fwd_id, None)
        if self._selected_conn == conn_name:
            self._selected_conn = None
        self._config.save()
        self._rebuild_connections()
        self._rebuild_forwards()
        self._notify_sync(f"已删除连接 {conn_name} 及其转发")

    def _save_conn(self, conn: ConnectionDef,
                   existing: Optional[ConnectionDef],
                   notify: bool = True) -> bool:
        if existing is not None:
            if conn.name != existing.name and self._config.find(conn.name) is not None:
                self._notify_sync(f"连接名 {conn.name} 已存在", ft.Colors.RED)
                return False
            conn.port_forwards = existing.port_forwards
            connection_changed = (
                existing.host, existing.user, existing.port,
                existing.identity_file, existing.auth, existing.password,
            ) != (
                conn.host, conn.user, conn.port,
                conn.identity_file, conn.auth, conn.password,
            )
            if existing.name != conn.name:
                # 改名：同步转发引用，并断开旧会话（会话按连接名管理）
                for f in self._forwards.values():
                    if f.conn == existing.name:
                        f.conn = conn.name
                self._run(self._stop_conn_worker, existing.name)
                self._config.remove(existing.name)
                self._config.add(conn)
            else:
                if connection_changed:
                    self._run(self._stop_conn_worker, existing.name)
                for idx, c in enumerate(self._config.connections):
                    if c.name == conn.name:
                        self._config.connections[idx] = conn
                        break
        else:
            if self._config.find(conn.name) is not None:
                self._notify_sync(f"连接名 {conn.name} 已存在", ft.Colors.RED)
                return False
            self._config.add(conn)
        self._config.save()
        if existing is None:
            self._selected_conn = conn.name
        elif self._selected_conn == existing.name:
            self._selected_conn = conn.name  # 改名后跟随选中
        elif self._config.find(self._selected_conn) is None:
            self._selected_conn = conn.name
        self._rebuild_connections()
        self._rebuild_forwards()
        if notify:
            self._notify_sync(f"已保存连接 {conn.name}")
        return True

    def _show_conn_dialog(self, existing: Optional[ConnectionDef]) -> None:
        page = self._page
        assert page is not None
        is_edit = existing is not None
        dialog_width = 460
        split_gap = 12
        f_name = ft.TextField(label="连接名称", width=dialog_width,
                              value=existing.name if existing else "")
        f_host = ft.TextField(label="主机", hint_text="IP/域名，或 ~/.ssh/config 里的别名",
                              width=dialog_width,
                              value=existing.host if existing else "")
        f_user = ft.TextField(label="用户", width=dialog_width - 120 - split_gap,
                              hint_text="留空自动",
                              value=existing.user if existing else "")
        f_port = ft.TextField(label="端口", width=120, input_filter=NUM,
                              value=str(existing.port if existing else 22))
        f_key = ft.TextField(label="密钥文件", width=dialog_width,
                             hint_text="留空自动，如 ~/.ssh/id_ed25519",
                             value=existing.identity_file if existing else "")
        f_auth = ft.Dropdown(
            label="认证方式", width=(dialog_width - split_gap) / 2,
            options=[ft.DropdownOption(key=code, text=disp) for disp, code in AUTH_OPTIONS],
            value=existing.auth if existing else "auto",
        )
        f_pass = ft.TextField(label="密码（可选）", password=True,
                              width=(dialog_width - split_gap) / 2,
                              value=existing.password if existing else "")

        def do_save(_e: Any) -> None:
            name = f_name.value.strip()
            host = f_host.value.strip()
            if not name or not host:
                self._notify_sync("名称和主机不能为空", ft.Colors.RED)
                return
            port, _ = parse_port(f_port.value, default="22")
            if port is None:
                self._notify_sync("端口无效", ft.Colors.RED)
                return
            auth_code = f_auth.value or "auto"
            conn = ConnectionDef(
                name=name, host=host,
                user=f_user.value.strip(), port=port,
                identity_file=f_key.value.strip(),
                auth=auth_code, password=f_pass.value,
            )
            if self._save_conn(conn, existing, notify=False):
                page.pop_dialog()
                self._notify_sync(f"已保存连接 {conn.name}")

        dlg = ft.AlertDialog(
            title=ft.Text("编辑连接" if is_edit else "新建连接"),
            content=ft.Container(
                width=dialog_width,
                content=ft.Column(
                    controls=[
                        f_name, f_host,
                        ft.Row(controls=[f_user, f_port], spacing=12),
                        f_key,
                        ft.Row(controls=[f_auth, f_pass], spacing=12),
                        ft.Row(
                            controls=[
                                ft.TextButton(
                                    "取消", on_click=lambda _e: page.pop_dialog(),
                                    height=40,
                                ),
                                ft.Button("保存", on_click=do_save, height=40),
                            ],
                            alignment=ft.MainAxisAlignment.END,
                            spacing=8,
                            height=40,
                        ),
                    ],
                    spacing=8,
                    tight=True,
                ),
            ),
            title_padding=ft.Padding(left=24, top=20, right=24, bottom=8),
            content_padding=ft.Padding(left=24, top=0, right=24, bottom=8),
            actions=[],
        )
        page.show_dialog(dlg)

    def _show_password_dialog(self, conn_name: str, msg: str) -> None:
        page = self._page
        assert page is not None
        f_pwd = ft.TextField(label="密码", password=True, autofocus=True,
                             hint_text="留空则放弃")

        def do_login(_e: Any) -> None:
            pwd = f_pwd.value
            page.pop_dialog()
            if pwd:
                self._run(self._connect_worker, conn_name, pwd)

        def do_cancel(_e: Any) -> None:
            self._auth_prompted.pop(conn_name, None)
            page.pop_dialog()

        f_pwd.on_submit = do_login
        dlg = ft.AlertDialog(
            title=ft.Text("需要密码"),
            content=ft.Container(
                width=420,
                content=ft.Column(
                    controls=[ft.Text(msg, size=13), f_pwd], spacing=12, tight=True
                ),
            ),
            actions=[
                ft.TextButton("放弃", on_click=do_cancel),
                ft.ElevatedButton("登录", on_click=do_login),
            ],
        )
        page.show_dialog(dlg)

    # -- 转发操作 -----------------------------------------------------------------
    def _fwd_new(self) -> None:
        conn = self._config.find(self._selected_conn) if self._selected_conn else None
        if conn is None:
            self._notify_sync("请先在左侧选择一个连接", ft.Colors.ORANGE)
            return
        self._show_fwd_dialog(conn.name)

    def _show_fwd_dialog(self, conn_name: str) -> None:
        page = self._page
        assert page is not None
        f_name = ft.TextField(label="转发名称", hint_text="如 web-ui")
        f_local = ft.TextField(label="本地端口", expand=True, input_filter=NUM,
                               value=str(self._next_local_port()),
                               hint_text="在本机 127.0.0.1 监听")
        f_rport = ft.TextField(label="远程端口", expand=True, input_filter=NUM,
                               value="80")
        f_rhost = ft.TextField(label="远程主机", value="127.0.0.1",
                               hint_text="127.0.0.1 = SSH 会话所在主机")
        dd = ft.Dropdown(expand=True, options=[],
                         hint_text="远程监听端口（先点「发现」）")
        hint = ft.Text("选中条目自动填入上方远程主机/端口", size=12,
                       color=ft.Colors.GREY)

        def do_discover(_e: Any) -> None:
            self._notify_sync("正在读取远程监听端口…")
            self._run(self._list_ports_worker, conn_name, dd, hint)

        def do_pick(e: Any) -> None:
            val = (e.control.value or "").split(" · ")[0].strip()
            host, _, port = val.rpartition(":")
            if host and port.isdigit():
                f_rhost.value = host
                f_rport.value = port
                f_rhost.update()
                f_rport.update()

        dd.on_select = do_pick

        def do_save(_e: Any) -> None:
            name = f_name.value.strip() or "portfwd"
            local, _ = parse_port(f_local.value)
            if local is None:
                self._notify_sync("本地端口无效", ft.Colors.RED)
                return
            rhost = f_rhost.value.strip() or "127.0.0.1"
            remote, _ = parse_port(f_rport.value)
            if remote is None:
                self._notify_sync("远程端口无效", ft.Colors.RED)
                return
            if self._local_port_taken(local):
                self._notify_sync(f"本地端口 {local} 已被其它转发占用", ft.Colors.RED)
                return
            self._add_forward(conn_name, name, local, rhost, remote)
            page.pop_dialog()

        dlg = ft.AlertDialog(
            title=ft.Text(f"新建转发 · {conn_name}"),
            content=ft.Container(
                width=470,
                content=ft.Column(
                    controls=[
                        f_name,
                        ft.Row(controls=[f_local, f_rport], spacing=12),
                        f_rhost,
                        ft.Row(
                            controls=[
                                ft.OutlinedButton("发现远程端口", icon=ft.Icons.SEARCH,
                                                  on_click=do_discover),
                                hint,
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=12,
                        ),
                        dd,
                    ],
                    spacing=12,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _e: page.pop_dialog()),
                ft.ElevatedButton("保存并启动", on_click=do_save),
            ],
        )
        page.show_dialog(dlg)

    def _add_forward(self, conn_name: str, name: str, local_port: int,
                     remote_host: str, remote_port: int) -> None:
        conn = self._config.find(conn_name)
        if conn is None:
            log.info("忽略转发保存：连接 %r 不存在", conn_name)
            return
        fwd = PortForward(
            id=uuid.uuid4().hex[:8],
            name=name, local_port=local_port,
            remote_host=remote_host, remote_port=remote_port,
            conn=conn.name,
        )
        self._forwards[fwd.id] = fwd
        conn.port_forwards.append(fwd.to_dict())
        self._config.save()
        self._selected_fwd = fwd.id
        self._rebuild_forwards()
        session = self._manager.get(conn.name)
        if session is not None and session.connected:
            self._run(self._start_fwd_worker, conn.name, fwd.id)
        else:
            self._notify_sync(f"已添加转发 {fwd.name}（连接未启动，启动连接时会自动生效）")

    def _fwd_toggle(self, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None:
            return
        if fwd.status in (FwdStatus.ACTIVE, FwdStatus.STARTING):
            self._run(self._stop_fwd_worker, fwd.conn, fwd.id)
        else:
            self._run(self._start_fwd_worker, fwd.conn, fwd.id)

    def _fwd_delete(self, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None:
            return
        self._confirm(f"删除转发 {fwd.name}（{fwd.address} → {fwd.display_remote}）？",
                      lambda: self._do_delete_fwd(fwd_id))

    def _do_delete_fwd(self, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None:
            return
        if fwd.status in (FwdStatus.ACTIVE, FwdStatus.STARTING):
            self._run(self._stop_fwd_worker, fwd.conn, fwd.id)
        conn = self._config.find(fwd.conn)
        if conn is not None:
            conn.port_forwards = [d for d in conn.port_forwards if d.get("id") != fwd.id]
        self._forwards.pop(fwd.id, None)
        if self._selected_fwd == fwd.id:
            self._selected_fwd = None
        self._config.save()
        self._rebuild_forwards()
        self._notify_sync(f"已删除转发 {fwd.name}")

    def _fwd_open(self, fwd_id: str) -> None:
        fwd = self._forwards.get(fwd_id)
        if fwd is None or self._page is None:
            return
        try:
            self._page.run_task(self._page.url_launcher.launch_url, fwd.url)
        except Exception:
            self._notify_sync(f"请手动在浏览器打开：{fwd.url}")

    # -- 其它 -------------------------------------------------------------------
    def _save(self) -> None:
        self._config.save()
        self._notify_sync(f"配置已保存到 {CONFIG_FILE}")

    def _confirm(self, message: str, on_yes) -> None:
        page = self._page
        assert page is not None
        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Container(width=380, content=ft.Text(message, size=14)),
            actions=[
                ft.TextButton("取消", on_click=lambda _e: page.pop_dialog()),
                ft.ElevatedButton(
                    "删除", bgcolor=ft.Colors.RED, color=ft.Colors.WHITE,
                    on_click=lambda _e: (page.pop_dialog(), on_yes()),
                ),
            ],
        )
        page.show_dialog(dlg)

    def _conn_up(self, name: str) -> bool:
        session = self._manager.get(name)
        return bool(session is not None and session.connected)

    def _local_port_taken(self, port: int) -> bool:
        return any(f.local_port == port for f in self._forwards.values())

    def _next_local_port(self) -> int:
        taken = {f.local_port for f in self._forwards.values()}
        port = 8080
        while port in taken:
            port += 1
        return port


def run_gui() -> int:
    """GUI 入口：`portfwd --gui` 或 `portfwd-gui`。"""
    gui = PortFwdGui()
    ft.app(target=gui.main)
    return 0
