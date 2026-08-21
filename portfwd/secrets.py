"""平台凭据存储。

macOS 使用系统 ``security`` 命令访问用户 Keychain；其它平台返回不可用，
由配置层保留现有的 0600 文件回退，以便 TUI 和无头测试继续工作。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import os

SERVICE = "com.portfwd.desktop"


def available() -> bool:
    return (
        os.environ.get("PORTFWD_DISABLE_KEYCHAIN") != "1"
        and sys.platform == "darwin"
        and shutil.which("security") is not None
    )


def get(account: str) -> str | None:
    if not available():
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.rstrip("\r\n")
    return value or None


def set(account: str, password: str) -> bool:
    if not available():
        return False
    try:
        subprocess.run(
            [
                "security", "add-generic-password", "-U",
                "-s", SERVICE, "-a", account, "-w", password,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def delete(account: str) -> bool:
    if not available():
        return False
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
