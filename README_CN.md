<div align="center">

# Agent Orchestrator

**随时知道每个 coding agent 在做什么、哪个最重要，以及如何把工作完整找回来。**

[![CI](https://github.com/YAMY1234/agent-orchestrator-public/actions/workflows/ci.yml/badge.svg)](https://github.com/YAMY1234/agent-orchestrator-public/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![macOS and Linux](https://img.shields.io/badge/macOS%20%7C%20Linux-local--first-24292f)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](README.md)

</div>

![Agent Orchestrator 同时管理六个 Codex 和 Claude Code TTY sessions](docs/assets/dashboard-hero.webp)

<p align="center"><sub>六个真实 TTY 风格 pane，内容来自虚构 demo tasks；截图中不包含用户数据。</sub></p>

Codex 和 Claude Code 在单个 terminal 里很强大。真正困难的是同时跑五个、十个
terminal 之后：tab 名称开始失去意义，重要任务埋在窗口里，idle 的 agent 看起来
和 busy 的一样，而一旦关闭窗口，之后可能连这个 session 在哪里都想不起来。

Agent Orchestrator 把这些 terminal sessions 变成一个可持续恢复、可视化的 task
board。每个 agent 都有自己的名称、优先级、实时状态、workspace、关联文件和
恢复入口。

## Terminal tabs 无法提供的掌控感

| 只使用 terminal | Agent Orchestrator |
| --- | --- |
| 每个 tab 看起来都差不多 | 为 task 设置容易记住的自定义标签 |
| 紧急任务和普通任务混在一起 | 使用 `P0`、`P1`、`P2` 排序和分组 |
| 无法快速判断“它还在工作吗？” | 直接查看 busy、idle 时长、watching、blocked 和 done |
| 输出在 terminal，task 文件却散落在别处 | 将项目文件夹、单个文件和参考 URL 绑定到 session |
| 关闭 terminal 后失去自己的记忆索引 | 捕获原生 resume metadata，并搜索已结束 sessions |
| 重启后原来的工作区布局消失 | 保存 active sessions，并把支持恢复的任务放回原 pane |

## 为真实复杂工作准备的指挥中心

从一个专注 pane 到高密度 `5x3` 布局都可以自由选择。实用的 `3x2` 布局能同时
容纳六个 Codex 和 Claude Code 复杂任务，并让输出和操作按钮保持清晰。

- 使用完整交互式 TTY，或更轻量的纯文本流。
- 不用切换 terminal，就能直接向任意 agent 发送输入。
- 每个 pane 都能放大、重连、停止、关闭或打开关联文件。
- 在不同 slot 之间拖动 task，不会重启底层 session。
- 将 Codex、Claude Code 和 Cursor Agent 放在同一个视图中管理。

浏览器只是控制面；即使关闭页面，后台 tmux sessions 仍会继续运行。

## 一眼看懂优先级和实时状态

![优先级、自定义标签、状态颜色、idle 跟踪和 goal 状态](docs/assets/priority-status.webp)

Sidebar 的目标是在你阅读 terminal 输出之前，先回答“我现在应该看哪里”：

- **P0 — 红色：** 紧急任务，或者正在阻塞关键决策的任务。
- **P1 — 黄色：** 重要且需要持续关注的工作。
- **P2 — 蓝色：** 可以稳定放在后台推进的常规工作。
- **Watching — 绿色：** 正在推进，目前不需要人工介入。
- **Blocked — 紫色：** 等待输入、权限或外部依赖。
- **Done — 弱化/绿色：** 已完成，但仍然保持清晰可识别。

自定义标签会把难记的 session ID 变成 “Auth migration” 或 “Release
automation” 这样的任务名称。Idle badge 会显示 pane 已经安静了多久；busy
检测要求输出持续变化，因此一行偶然的 terminal 噪声不会让 task 看起来一直在
工作。

Pane 边框、优先级标签和 terminal 底部状态会形成统一的视觉语言：先扫描整个
grid 里的红、黄、蓝、绿，再打开真正需要关注的 task。

## Terminal 关掉，工作仍然找得回来

Terminal agent 最常见的问题并不是进程崩溃，而是人已经忘了哪个 tab、哪个
目录、哪个 resume command 对应哪个 task。

Agent Orchestrator 会保留多层恢复信息：

1. 记录 task 标签、agent 类型、workspace、logs 和本地 metadata。
2. 当 agent CLI 暴露原生 session ID 时，自动捕获对应的 Codex、Claude Code
   或 Cursor resume command。
3. **Save active** 保存当前 pane 布局和可以恢复的 active sessions。
4. 机器重启或 Dashboard 重启后，**Restore saved** 会在后台 tmux 中重新创建
   支持恢复的 session，并把它们放回保存时的 slot。
5. 已结束 sessions 仍然可以搜索，并可从创建 session 的流程中恢复。

由于不同 agent CLI 暴露的 metadata 不完全一致，恢复能力是 best-effort；但
Dashboard 会明确展示这些状态，而不是让它们消失在 terminal scrollback 里。

## 每个 task 都有自己的 Linked Items

![Linked Items 展示 task workspace、文件树和 Markdown 状态报告](docs/assets/linked-items.webp)

一个 task 不只是 terminal transcript。它通常还有项目文件夹、计划、测试证据、
结果表格、截图和几个参考网页。Linked Items 会把这些上下文直接绑定到 session。

- 绑定整个项目或 task 文件夹，直接在 Dashboard 中浏览目录树。
- 当 task 跨越多个位置时，可以单独绑定文件或 URL。
- 预览 Markdown、源码、图片、CSV 数据和报告。
- 让实现笔记、验证证据和发布产出始终靠近产生它们的 agent。
- 几天后恢复工作时，可以快速找回完整上下文。

Dashboard 不会复制一份新的项目。它记住真正的 workspace，让每个 task 都有一个
稳定的入口，方便你持续 track 它的工作和产出。

## 快速开始

需要 macOS 或 Linux、`tmux`、Python 3.10+，以及至少一个支持的 agent CLI
（`codex`、`claude` 或 `agent`）。安装 `ttyd` 后即可使用截图中的完整交互式
terminal 体验。

```bash
git clone https://github.com/YAMY1234/agent-orchestrator-public.git
cd agent-orchestrator-public

PYTHON=python3.11  # 可替换为任意已安装的 Python 3.10+
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python orchestrator.py dashboard
```

打开 [http://127.0.0.1:7860](http://127.0.0.1:7860)，创建 session、设置标签和
优先级，然后选择 **Start in Background**。

如果希望当前 shell 里的命令更短：

```bash
ORCH_REPO="$PWD"
orch() { "$ORCH_REPO/.venv/bin/python" "$ORCH_REPO/orchestrator.py" "$@"; }
```

## 一个实用的日常工作流

1. 创建后台 session，并给它一个人能记住的标签。
2. 设置 `P0`、`P1` 或 `P2`，让它自动进入正确分组。
3. 绑定 task 或项目文件夹，让产出始终容易找到。
4. 观察 busy 和 idle 时长，不再反复打开每一个 pane 检查。
5. 只在需要决策、权限或补充信息时向 agent 发送输入。
6. 重启前保存 active sessions；完成后停止任务，同时保留 resume metadata。

## 从 CLI 启动 sessions

```bash
orch run                              # Cursor Agent
orch run claude                       # Claude Code
orch run codex                        # OpenAI Codex CLI
orch run codex investigate /path/to/project
```

浏览器和 CLI 工作流使用相同的本地 sessions 和 metadata。

## 在 macOS 后台常驻

受管安装器会创建隔离运行环境、安装依赖、生成私有 token，并注册用户级
LaunchAgent：

```bash
./launchd/deploy.sh --install  # 首次安装
./launchd/deploy.sh            # 后续代码更新
./launchd/deploy.sh --dry-run  # 预览更新
```

部署时会保留 outputs、关联 projects、证书、本地配置和私有 task recipes。
LaunchAgent 默认只监听 `127.0.0.1`。

常用覆盖项：

```bash
ORCH_PYTHON=/path/to/python3.12 ./launchd/deploy.sh --install
ORCH_DASHBOARD_PORT=9000 ./launchd/deploy.sh --install
ORCH_DASHBOARD_HOST=0.0.0.0 ./launchd/deploy.sh --install
```

## 远程访问

非 loopback bind 强制要求认证。通过 LAN 或 VPN 访问时，应同时使用 token 和
HTTPS：

```bash
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 0.0.0.0 --https
```

URL helper 会检测正在运行的 Dashboard 协议和 bind 地址：

```bash
orch url            # 打印并复制最佳认证 URL
orch url -q         # 只输出 URL
orch url --json     # 检查所有可访问候选地址
```

## Local-first 安全模型

Agent Orchestrator 可以向本地 terminal sessions 发送输入，应当把它视为一个
高权限开发者工具。

- 默认只监听 localhost。
- 非 loopback 访问必须使用 token。
- Token 保存在 tracked source tree 之外，并使用仅当前用户可读的权限。
- Runtime 数据保留在本机，其中可能包含 prompts、transcripts、本地路径和
  resume metadata。
- 绝不要发布 `outputs/`、`projects/`、`.dashboard-certs/`、本地配置或私有
  task recipes。

部署和漏洞报告说明见 [SECURITY.md](SECURITY.md)，开发检查和贡献指南见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 当前范围

Dashboard-first 是主要支持的体验。由于各 agent CLI 暴露的 session metadata
不同，resume 能力是 best-effort。项目面向可信的本地开发者机器，而不是托管式
多用户部署。旧版 YAML recipe runner 仍为高级用户保留。

Agent Orchestrator 采用 [MIT License](LICENSE)。
