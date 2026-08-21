"""命令行入口：`python -m portfwd` 或安装后的 `portfwd` / `portfwd-gui`。"""
from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="portfwd",
        description="SSH 端口转发管理工具（VS Code Remote-SSH PORTS 面板风格）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--gui", action="store_true",
        help="启动图形界面（Flet；等效于 portfwd-gui）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.gui:
        from .gui import run_gui

        run_gui()
        return 0
    from .app import PortFwdApp

    PortFwdApp().run()
    return 0


def gui_main() -> int:
    """`portfwd-gui` 入口：只走 GUI。"""
    parser = argparse.ArgumentParser(
        prog="portfwd-gui",
        description="SSH 端口转发管理工具（图形界面）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    from .gui import run_gui

    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
