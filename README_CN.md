# Agent Orchestrator

[English README](README.md)

Agent Orchestrator 是一个本地 Dashboard，用来同时运行和管理多个 CLI 编程
agent。它会把 Cursor Agent、Claude Code、OpenAI Codex 等 agent 放到 tmux
session 里运行，持续捕获输出，并通过浏览器界面进行多窗口查看、输入、停止和
恢复。

当前推荐的使用方式是 Dashboard 优先：

1. 启动 Dashboard。
2. 在浏览器里创建 session。
3. 默认使用后台启动，避免打开大量 terminal 窗口。
4. 通过 Dashboard 查看、输入、停止和恢复 agent。

旧的 YAML recipe runner 仍然保留，但不是当前主要支持路径。新使用场景建议优先
使用 Dashboard 管理后台 session。

## 支持能力

- 支持 Cursor Agent CLI、Claude Code、OpenAI Codex CLI。
- 支持后台启动 session，也可以选择打开 terminal 后启动。
- 支持多 pane 布局：`1`、`2`、`3`、`2x2`、`3x2`、`3x3`、`4x3`、`5x3`。
- 可以从左侧 sidebar 拖拽 session 到任意 pane。
- 每个 pane 有独立输入框、复制、Esc、关闭和 mode 控制。
- 支持纯文本日志流，也支持可选的 ttyd 真终端模式。
- Stop 时会尽量保存 resume metadata。
- 如果能恢复出 agent CLI 的 resume 命令，可以从 ended session 里恢复。
- 支持本机、局域网、VPN 或 tunnel 访问，并支持 token 鉴权。

## 依赖

- macOS 或 Linux，并安装 `tmux`
- Python 3.10+
- 至少安装一种 agent CLI：
  - Cursor Agent CLI: `agent`
  - Claude Code: `claude`
  - OpenAI Codex CLI: `codex`
- `requirements.txt` 里的 Python 依赖
- 可选：`ttyd`，用于浏览器内完整终端交互

创建隔离环境并安装 Python 依赖：

```bash
python3.11 -m venv .venv  # 任意 Python 3.10+ 均可
.venv/bin/python -m pip install -r requirements.txt
```

下面的例子默认你已经把 `orch` 指向本仓库的 `orchestrator.py`。如果没有这个
alias 或 wrapper，也可以在仓库根目录运行 `.venv/bin/python orchestrator.py ...`。

## 快速开始

启动 Dashboard：

```bash
.venv/bin/python orchestrator.py dashboard
```

默认只监听 `127.0.0.1`，因此这条命令只能在本机访问。如果已经有缓存 token，
会继续使用；否则本机模式不需要鉴权。

打开：

```text
http://127.0.0.1:7860
```

如果需要局域网、VPN 或 tunnel 访问，建议加 token：

```bash
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 0.0.0.0 --https
```

进入 Dashboard 后，用左上角的新建 session 控件选择 agent 并启动。推荐默认选择
**Start in Background**。只有明确需要本地 terminal 窗口时，再选择
**Open Terminal and Start**。

### 本地 Dashboard 配置

把 `dashboard.local.example.json` 复制成 `dashboard.local.json`，即可配置
当前机器专用的快捷链接和路径，而不会把它们提交到 Git。这个本地文件已被忽略，
支持 `notes_url`、`projects_browser_url`、`git_status_url` 和 `projects_root`。

环境变量 `ORCH_NOTES_URL`、`ORCH_PROJECTS_BROWSER_URL`、
`ORCH_GIT_STATUS_URL` 和 `ORCH_PROJECTS_ROOT` 会覆盖对应的文件配置；设置
`ORCH_DASHBOARD_CONFIG` 可以从其他位置加载 JSON 配置。

### 本地运行目录

运行数据默认写入源码目录旁的 `outputs/`。如果希望把 session metadata 和
日志放到别处，可以把 `ORCH_OUTPUTS_DIR` 设为绝对路径。Dashboard、session
launcher、continue/link 命令和 YAML runner 都会使用同一个配置。对于
Dashboard 进程，显式传入的 `orch dashboard --outputs PATH` 优先级更高。

`orch organize` 默认把归档后的 session 内容写入 output 目录旁的
`projects/`。如果需要单独指定归档位置，可以设置 `ORCH_PROJECTS_DIR`。

## Dashboard 工作流

### 创建 Session

你可以在浏览器里创建 Cursor、Claude 或 Codex session。每个 session 都会对应
一个 tmux session、一个 output 目录和一个持续刷新的日志文件。

通常更推荐后台启动，因为这样不会在本地创建一堆 terminal 窗口。即使后台启动，
Dashboard 仍然可以和 tmux session 交互。

### 排列 Pane

可以用顶部布局选择器在不同 grid 之间切换。把左侧 sidebar 中的 session 拖到
目标 pane，就能直接替换或放入对应窗口。

### 发送输入

每个 pane 都有独立输入框。Enter 发送，Shift+Enter 换行。Dashboard 会通过
tmux 把文本发送给底层 agent。

### Stop 和 Resume

Stop 会尝试干净地停止 agent，并保存可恢复信息。是否能恢复取决于具体 CLI：

- Codex: `codex resume <session-id>`
- Claude Code: `claude --resume <session-id>`
- Cursor Agent: `agent --resume <chat-id>`

如果成功找到 resume 命令，新建 session 时就可以从 Resume UI 里选择之前结束的
session。

## CLI 单 Session 模式

也可以从 terminal 直接启动一个 session：

```bash
# 默认 Cursor Agent
orch run

# Claude Code
orch run claude

# OpenAI Codex CLI
orch run codex

# 自定义 label 和工作目录
orch run cursor fix-bug /path/to/project
orch run claude review-pr /path/to/project
orch run codex investigate-bug /path/to/project
```

常用快捷参数包括 `--model`、`--effort`（仅 Claude Code）、`--fast`、
`--think`、`--opus`、`--sonnet`、`--codex`、`--codex-high`。

## 远程访问

只要 Dashboard 监听地址超出 localhost，就必须启用 token。如果没有配置
token，CLI 会拒绝使用非 loopback 的 `--host`。

```bash
# 局域网或 Tailscale
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 0.0.0.0 --https

# Cloudflare Tunnel
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 127.0.0.1
cloudflared tunnel --url http://localhost:7860

# ngrok
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 127.0.0.1
ngrok http 7860
```

浏览器在非 localhost 来源下使用剪贴板 API 时需要 secure context。局域网或 VPN
建议用 `--https`，公网 tunnel 通常会自带 HTTPS。

## URL Helper

```bash
orch url            # 自动探测 HTTP/HTTPS，打印并复制最佳 URL
orch url -q         # 只打印 URL
orch url --json     # 打印所有候选网络接口
orch url --no-copy  # 不复制到剪贴板
```

该命令会先读取 `ORCH_DASHBOARD_TOKEN`，然后读取
`~/.config/agent-orchestrator/dashboard-token` 中的本地 token。没有 token 时只会
返回 localhost URL。可以用 `ORCH_DASHBOARD_TOKEN_FILE` 指定其他缓存路径。只有在
Dashboard 当前未运行、需要覆盖 fallback 协议时，才需要使用 `--https` 或
`--no-https`。

如果启动 Dashboard 时使用 `--publish-icloud`，当前 URL 也会写到：

```text
~/iCloud Drive/orch-dashboard.txt
```

## macOS LaunchAgent

`launchd/` 里的脚本可以把 Dashboard 安装成用户级 LaunchAgent：

```bash
./launchd/deploy.sh --install
./launchd/deploy.sh
./launchd/deploy.sh --dry-run
```

首次安装会在 live 目录中创建独立 `.venv` 并安装所需 Python 包。如果
`python3` 低于 3.10，脚本还会查找 `python3.10` 到 `python3.14`；也可以用
`ORCH_PYTHON` 明确指定解释器。

首次安装会自动生成高强度随机 token，并以仅当前用户可读的权限保存到本地
token 缓存。如果希望使用指定 token，可以在安装时设置
`ORCH_DASHBOARD_TOKEN`。

LaunchAgent 默认只监听 `127.0.0.1`。如果明确需要 LAN 或 VPN 访问，请使用
`ORCH_DASHBOARD_HOST=0.0.0.0 ./launchd/deploy.sh --install`；token 认证仍然是
强制要求。

`deploy.sh` 会把仓库同步到一个更适合 launchd 读取的 live 目录，例如
`~/projects/agent-orchestrator/`。这么做是为了避开 macOS TCC 对 LaunchAgent
后台进程读取 `~/Documents`、`~/Desktop`、`~/Downloads` 的限制。

## 高级功能：YAML Recipe Runner

仓库里仍然保留旧的 YAML 编排模式：

```bash
orch start tasks/example.yaml
orch resume outputs/example-20260515-120000
orch status
```

这个模式可以跑多任务 recipe 和依赖关系，但最近的主要开发不在这条路径上。新使用
场景建议优先使用 Dashboard 管理后台 session。

`tasks/example.yaml` 保持与机器无关。包含本机路径、内部 prompt 或日志引用的
recipe 应放在 Git 已忽略的 `tasks/private/` 目录中，不要提交。

## 架构

- `dashboard.py`：FastAPI 后端、tmux 集成、session 发现、resume metadata 恢复、
  ttyd proxy。
- `static/index.html`：单文件浏览器 UI。
- `run.sh`：Dashboard 创建 session 时使用的轻量启动脚本。
- `outputs/`：运行日志、session metadata 和 Dashboard 状态。
- `tmux`：进程管理和 terminal capture 层。

## 安全说明

- 如果 Dashboard 可以被远程访问，它就可以向本地 tmux session 发送输入。
- 非 loopback 监听必须设置 `--token` 或 `ORCH_DASHBOARD_TOKEN`。
- 运行目录里可能包含 transcript、prompt、日志和本机路径。不要公开 `outputs/`、
  `projects/`、`.dashboard-certs/`。

漏洞报告和部署安全说明见 [SECURITY.md](SECURITY.md)。

## 贡献

开发环境、检查命令和 Pull Request 要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

Agent Orchestrator 使用 [MIT License](LICENSE)。

## 已知限制

- Resume 是 best-effort，依赖具体 agent CLI 暴露的本地 metadata。
- ttyd 渲染仍可能受到不同 agent CLI terminal UI 行为影响。
- YAML 编排是 legacy 路径，成熟度低于当前 Dashboard 工作流。
- 当前项目主要面向本地开发机，不是托管式多用户服务。
