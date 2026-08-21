# portfwd

SSH 端口转发管理工具，风格对齐 VS Code Remote-SSH 的 PORTS 面板（TUI + GUI 双界面）：
把远程主机上的端口通过 SSH 隧道暴露到本机 `127.0.0.1`，本地浏览器直接访问即可打开远程服务。

## 功能

- **连接管理**：保存任意多个 SSH 连接；支持直接输入 IP/域名，或输入
  `~/.ssh/config` 里的别名（自动解析 host / port / user / 密钥，与 `ssh` 命令行为一致）
- **端口转发**：每个连接下可保存多条转发规则，随时启停；本地端口冲突检测
- **远程端口发现**：一键拉取远程 `ss`/`netstat`/`lsof` 监听端口列表，选中自动填入
- **流量统计**：每条转发实时显示活跃连接数与累计流量
- **自动恢复**：重启 portfwd 后自动重连并恢复已保存的转发
- **认证**：自动（agent/默认密钥 → 失败弹密码框）/ 仅密钥 / 密码（可保存到配置）
- **主机密钥**：`accept-new` 策略（首次自动记录到 known_hosts，密钥不匹配则拒绝）

## 安装

```bash
cd ~/portfwd
.venv/bin/pip install -e .     # 已装好可跳过
alias portfwd="~/portfwd/.venv/bin/portfwd"   # 或写入 ~/.zshrc
```

## 使用

```bash
portfwd           # 启动 TUI
portfwd -v        # TUI 带调试日志
portfwd --gui     # 启动 GUI（图形窗口，Flet）
portfwd-gui       # 同上，独立入口
```

### GUI 界面

左侧连接列表（● 已连接 / ○ 未连接），每行带 连接/断开、编辑、删除；
右侧转发行：状态点、名称、本地地址（点击在浏览器打开）、远程目标、状态、
连接数、流量、启停、删除。顶栏可保存配置、退出；左下开关控制「启动时自动恢复」。
新建转发时可点「发现远程端口」，下拉选中即自动填入。

**操作**（快捷键见窗口底部 Footer）：

| 键 | 动作 |
| :- | :- |
| `c` | 新建连接（支持填 `~/.ssh/config` 别名） |
| `e` | 编辑选中连接 |
| `x` | 连接 / 断开选中连接 |
| `f` | 给选中连接新建转发 |
| `r` | 启 / 停选中转发 |
| `d` | 删除选中转发（表中回车也会切换） |
| `s` | 保存配置到 `~/.portfwd/config.json` |
| `q` | 退出（自动清理所有隧道） |

新建转发时可点「发现远程端口」，在弹出的列表里选中端口即可自动填入；
远程主机填 `127.0.0.1` 表示 SSH 会话所在主机，填其它 IP 则经隧道二次转发
（VS Code 同款高级用法）。

## 配置

- 配置目录：`~/.portfwd/`（环境变量 `PORTFWD_HOME` 可覆盖）
- `config.json` 和配置目录分别为 0600/0700；macOS 有 `security` 命令时，密码会迁移到
  Keychain，JSON 中不再保存密码。其它平台保留 0600 文件回退。

## 测试

```bash
.venv/bin/python tests/test_engine.py   # 引擎集成测试（本地 echo，FakeTransport）
.venv/bin/python tests/test_ui.py       # TUI 无头冒烟测试
.venv/bin/python tests/test_gui.py      # GUI 无头冒烟测试（FakePage，无需显示）
.venv/bin/python tests/test_buttons.py  # TUI 按钮链路回归
.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py'
# 安装开发依赖后，也可运行：.venv/bin/python -m pytest
```

## macOS 应用

需要在 macOS 上预先安装固定版本的构建 CLI 和完整 Xcode（Command Line Tools 不包含
`xcodebuild`）：

```bash
.venv/bin/pip install -e '.[build]'
xcode-select --switch /Applications/Xcode.app/Contents/Developer
./scripts/build_macos.sh
```

脚本默认按当前机器架构构建（Apple Silicon 为 `arm64`，Intel 为 `x64`），也可用
`MACOS_ARCH` 覆盖。产物位于 `build/macos/portfwd.app`。可用 `BUILD_VERSION` 和递增的
`BUILD_NUMBER` 覆盖版本信息。签名和公证需要在发布机上另行配置 Developer ID 证书。

## 实现说明

- `portfwd/models.py` — 纯数据模型与序列化
- `portfwd/config.py` — 配置持久化 + `~/.ssh/config` 解析（paramiko SSHConfig）
- `portfwd/forwarding.py` — SSH 连接/转发引擎：本地监听 → SSH channel → 双向泵；
  心跳监测断线；使用标准 `direct-tcpip` channel；加载 `known_hosts` 并执行
  `accept-new` 主机密钥策略
- `portfwd/app.py` — Textual TUI；阻塞 SSH 操作全部走 worker 线程，
  引擎事件经队列推送，流量由 0.5s 定时器轮询
- `portfwd/gui.py` — Flet GUI（`portfwd --gui` / `portfwd-gui`）；与 TUI 共用
  引擎与配置，阻塞操作走 `page.run_thread`，状态经队列 + 0.5s `run_task` 轮询，
  窗口关闭和 `atexit` 都会清理 SSH 会话
