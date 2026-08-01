#!/usr/bin/env python3
"""Generate the organize prompt: classify unarchived sessions into project folders."""

from __future__ import annotations

import argparse
import os
import re
import time
from typing import Optional

from session_safety import get_active_dirs, get_archived_timestamps

SUBFOLDER_THRESHOLD = 15


def _get_session_last_modified(session_dir: str) -> float:
    """Get the most recent mtime of any file in the session directory."""
    latest = os.path.getmtime(session_dir)
    for root, _dirs, files in os.walk(session_dir):
        for f in files:
            fp = os.path.join(root, f)
            try:
                mt = os.path.getmtime(fp)
                if mt > latest:
                    latest = mt
            except OSError:
                pass
    return latest


def find_pending_sessions(outputs_dir: str, projects_dir: str, self_session: str,
                          stale_seconds: Optional[float] = None) -> list[dict]:
    """Find all non-empty, non-active, non-archived sessions.

    If stale_seconds is set, also skip sessions whose most recent file modification
    is within that many seconds (i.e. only include sessions that haven't been
    updated for at least stale_seconds).
    """
    archived = get_archived_timestamps(projects_dir)
    active = get_active_dirs(outputs_dir)
    now = time.time()

    sessions = []
    for entry in sorted(os.listdir(outputs_dir)):
        full = os.path.join(outputs_dir, entry)
        if not os.path.isdir(full):
            continue
        if entry == self_session:
            continue
        if entry.startswith("organize-"):
            continue
        if entry in active:
            continue

        m = re.search(r"(\d{8}-\d{6})", entry)
        ts = m.group(1) if m else None
        if ts and ts in archived:
            continue

        logdir = os.path.join(full, "logs")
        if not os.path.isdir(logdir):
            continue
        max_size = 0
        log_file = None
        for lf in os.listdir(logdir):
            lfp = os.path.join(logdir, lf)
            if os.path.isfile(lfp):
                sz = os.path.getsize(lfp)
                if sz > max_size:
                    max_size = sz
                    log_file = lfp
        if max_size <= 512:
            continue

        if stale_seconds is not None:
            last_mod = _get_session_last_modified(full)
            if now - last_mod < stale_seconds:
                continue

        sessions.append({
            "dir": entry,
            "ts": ts or "unknown",
            "log": log_file,
            "size": max_size,
        })

    return sessions


def _count_logs(path: str) -> int:
    """Count .log files directly under path (non-recursive)."""
    if not os.path.isdir(path):
        return 0
    return sum(1 for f in os.listdir(path) if f.endswith(".log") and os.path.isfile(os.path.join(path, f)))


def _count_logs_recursive(path: str) -> int:
    """Count .log files recursively under path."""
    total = 0
    for _root, _dirs, files in os.walk(path):
        total += sum(1 for f in files if f.endswith(".log"))
    return total


def get_existing_projects(projects_dir: str) -> dict:
    """Return nested structure: {project_name: {"files": [...], "subdirs": {subdir: [files]}}}."""
    result = {}
    if not os.path.isdir(projects_dir):
        return result
    for project in sorted(os.listdir(projects_dir)):
        pdir = os.path.join(projects_dir, project)
        if not os.path.isdir(pdir):
            continue
        info = {"files": [], "subdirs": {}}
        for entry in sorted(os.listdir(pdir)):
            entry_path = os.path.join(pdir, entry)
            if os.path.isfile(entry_path) and entry.endswith(".log"):
                info["files"].append(entry)
            elif os.path.isdir(entry_path):
                sub_files = sorted(
                    f for f in os.listdir(entry_path)
                    if f.endswith(".log") and os.path.isfile(os.path.join(entry_path, f))
                )
                if sub_files:
                    info["subdirs"][entry] = sub_files
        result[project] = info
    return result


def find_overgrown_projects(projects_dir: str, pending_count: int) -> list[dict]:
    """Find projects that are at or above the subfolder threshold after new sessions land.

    Returns list of {name, path, total_logs, files_at_root} for projects where
    the root-level .log count (current + worst-case incoming) >= SUBFOLDER_THRESHOLD.
    Only projects WITHOUT existing subdirectories are flagged (already-split ones
    are managed by the agent placing files into the right subfolder).
    """
    overgrown = []
    if not os.path.isdir(projects_dir):
        return overgrown
    for project in sorted(os.listdir(projects_dir)):
        pdir = os.path.join(projects_dir, project)
        if not os.path.isdir(pdir):
            continue
        has_subdirs = any(
            os.path.isdir(os.path.join(pdir, e)) for e in os.listdir(pdir)
        )
        if has_subdirs:
            continue
        root_logs = _count_logs(pdir)
        if root_logs + pending_count >= SUBFOLDER_THRESHOLD:
            log_files = sorted(
                f for f in os.listdir(pdir)
                if f.endswith(".log") and os.path.isfile(os.path.join(pdir, f))
            )
            overgrown.append({
                "name": project,
                "path": pdir,
                "total_logs": root_logs,
                "files": log_files,
            })
    return overgrown


def _format_projects(projects: dict) -> str:
    """Format projects dict into readable markdown lines."""
    lines = []
    for name, info in projects.items():
        total = len(info["files"]) + sum(len(v) for v in info["subdirs"].values())
        lines.append(f"- **{name}/** ({total} logs)")
        if info["subdirs"]:
            for subdir, sub_files in info["subdirs"].items():
                lines.append(f"  - **{subdir}/** ({len(sub_files)} logs)")
                for f in sub_files[-3:]:
                    lines.append(f"    - {f}")
                if len(sub_files) > 3:
                    lines.append(f"    - ... and {len(sub_files) - 3} more")
        if info["files"]:
            shown = info["files"][-5:]
            for f in shown:
                lines.append(f"  - {f}")
            if len(info["files"]) > 5:
                lines.append(f"  - ... and {len(info['files']) - 5} more")
    return "\n".join(lines) if lines else "(无已有 projects，需要全部新建)"


def _format_overgrown_section(overgrown: list[dict]) -> str:
    """Format the overgrown projects section for the prompt."""
    if not overgrown:
        return ""
    lines = [
        "",
        "## 需要拆分子文件夹的 projects（当前文件数 ≥ {threshold} 或加上待处理后将 ≥ {threshold}）".format(
            threshold=SUBFOLDER_THRESHOLD
        ),
        "",
        "以下 project 的根目录 .log 文件过多，归档新 session 前需先拆分子文件夹：",
    ]
    for proj in overgrown:
        lines.append(f"- **{proj['name']}/** ({proj['total_logs']} logs at root)")
        for f in proj["files"]:
            lines.append(f"  - {f}")
    return "\n".join(lines)


def _format_protected_section(active_dirs: set[str], self_session: str) -> str:
    """Format the never-touch section listing all currently active dirs.

    This list is computed at prompt-generation time. The cleanup step
    MUST also re-check tmux state immediately before moving anything —
    new sessions can spawn between prompt generation and execution.
    """
    items = sorted(active_dirs | {self_session})
    lines = [
        "",
        "## 受保护的目录（绝对不能 rm / mv / 改名！）",
        "",
        "以下目录有对应的活跃 tmux session 在跑（或就是本 organize session 自己），",
        "在任何情况下都**不允许移动、改名、删除、清理它们**：",
        "",
    ]
    for d in items:
        lines.append(f"- `{d}/`")
    lines.append("")
    lines.append("**额外硬性要求**：清理这一步**严禁使用 `rm -rf`**，请调用")
    lines.append("`bash scripts/prune.sh --dry-run` 先检查，再调用 `bash scripts/prune.sh`。")
    lines.append("该脚本会重新读取 tmux/session.json/state.json 并把目录移到 `~/.Trash/`。")
    return "\n".join(lines)


PROMPT_TEMPLATE = """\
你是一个 session 整理助手。请将以下未归档的 agent session 日志按 project 归类到 projects/ 文件夹。

## 路径配置
- outputs 目录: {outputs_dir}
- projects 目录: {projects_dir}
- 子文件夹阈值: 每个 project 根目录下 .log 文件不超过 {threshold} 个，超过需拆分子文件夹
{protected_section}

## 已有 projects（归类时优先匹配已有的）
{existing_projects}
{overgrown_section}

## 待处理 sessions（共 {count} 个，都是非空、非活跃、未归档的）

下面这份列表已经在 prompt 生成时排除了所有活跃 session。但请仍然在执行删除前再做一次 tmux 二次校验（见步骤 3）——新的 session 可能在生成 prompt 之后被启动。
{session_list}

## 执行步骤

### 0. 拆分过大的 project（如有标记）
如果上方列出了"需要拆分子文件夹的 projects"，**先处理拆分**再归档新 session：
1. 读取该 project 下所有 .log 文件的前 50 行，理解每个文件的主题
2. 按主题分组，每组起一个简短英文 snake_case 子文件夹名（如 `pr_and_ci`、`debug_and_fix`）
3. 同时**审查分类正确性**：如果某个文件明显不属于当前 project，将它移到正确的 project（已有或新建）
4. 用 `mkdir -p` 创建子文件夹，用 `mv` 移动文件
5. 确保拆分后每个子文件夹内文件数 ≤ {threshold}

### 1. 逐个处理每个 session
对每个待处理 session：
1. 读取日志文件的前 200 行和后 200 行（文件不长就全读）
2. 根据对话内容判断属于哪个已有 project（注意区分不同类型的工作——**开发调试类** vs **文档写作类** vs **实验运行类**不应混为一个 project）
3. 匹配不上则创建新 project（简短英文 snake_case 命名）
4. 如果目标 project 已有子文件夹，将文件放入最匹配的子文件夹；如果没有子文件夹，放到 project 根目录
5. **复制日志文件**到 `{projects_dir}/<project_name>/[subfolder/]` 下，文件命名格式：
   `<YYYYMMDD-HHMMSS>-<short_summary>.log`
   - 时间戳取自原始 session 目录名
   - short_summary 是对 session 内容的简短英文概括（snake_case，3-5 个词）

### 2. 归档后检查是否需要新拆分
归档完成后，检查所有 project 根目录的 .log 文件数：
- 如果某个 project 根目录下 .log 文件数 > {threshold}，执行与步骤 0 相同的拆分流程

### 3. 清理已归档的 session 目录（**用 prune.sh，强制 tmux 二次校验**）

**绝对禁止使用 `rm -rf`**。不要自己手写清理循环；统一调用仓库自带的
`scripts/prune.sh`，它会在脚本层重新检查 tmux/session.json/state.json，并把通过校验的
目录移动到 `~/.Trash/`，方便误删后从废纸篓恢复。

#### 3a. 安全校验（每次清理前先做）

1. 拿当前 tmux 活跃 session 列表：

   ```bash
   tmux ls 2>/dev/null | grep -oE "^orch-[^:]+" | grep -v -- "-web$"
   ```

2. 对每个准备清理的目录 `<dir>`，如果 `<dir>` 匹配 `<task>-YYYYMMDD-HHMMSS`，
   解出 `<task>`，然后检查上面列表里**没有任何** `orch-<task>-<pid>` 与之对应；
   只有确认没有对应活跃 tmux 才能清理。

3. 同样的检查也用在受保护清单（见上方"受保护的目录"段）：列表中的任何目录
   都**绝对不允许** mv / rm / 改名。

#### 3b. 使用脚本而非手工删除

先 dry-run 看候选清单：

```bash
bash scripts/prune.sh --dry-run
```

确认没有活跃 session 出现在候选清单后，再执行：

```bash
bash scripts/prune.sh
```

#### 3c. 清理三类目录

`prune.sh` 只会把以下三类搬进 Trash：

- 已经成功归档的 session 目录
- `organize-*` 目录中**不是当前 self_session 也没有活跃 tmux** 的（绝不要清理自己！）
- 空 session 目录（日志文件 ≤ 512 bytes 或无日志文件的）且无活跃 tmux

如果发现某个目录"应该归档"但其实有活跃 tmux（说明 prompt 生成期间状态变了），
直接跳过它、在最终报告里列出来即可，**不要尝试搬运它的元数据/log**。

### 4. 输出报告
整理完成后输出简要报告：
- 处理了多少 session
- 每个 project 归入了哪些 session（列出 dirname 和生成的文件名）
- 拆分/重归类了哪些文件（如有）
- 搬进 Trash 了多少个目录

然后回复"任务已完成"。

## 注意事项
- 一个 session 只归入一个 project（归入主要相关的）
- **严格区分分类**：不要把所有技术相关的 session 塞进同一个 project。例如：
  - 代码开发/PR/CI/bug修复 → 以项目名命名的 `xxx_dev` project
  - 文档/研究/演示文稿撰写 → 以用途命名的 `xxx_docs` project
  - 实验运行/benchmark → 以工具名命名的 `xxx_benchmark` 或 `xxx_slurm` project
- 不确定归属时创建新 project，命名要准确反映内容
- 只复制日志文件（不需要复制 state.json 等）
- 如果日志只有 agent header 没有实质对话，跳过归档；后续让 `prune.sh` 在确认无活跃 tmux 后搬进 Trash"""


def _parse_duration(s: str) -> float:
    """Parse a human-friendly duration string into seconds.

    Examples: '3h' -> 10800, '30m' -> 1800, '1d' -> 86400, '90' -> 5400 (treated as minutes).
    Supports combined forms like '1h30m'.
    """
    if not s:
        return 0
    s = s.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0.0
    buf = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            buf += ch
        elif ch in units:
            total += float(buf or "0") * units[ch]
            buf = ""
        else:
            raise ValueError(f"Unknown duration unit: '{ch}' in '{s}'")
    if buf:
        total += float(buf) * 60  # bare number → minutes
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs-dir", required=True)
    parser.add_argument("--projects-dir", required=True)
    parser.add_argument("--self-session", required=True)
    parser.add_argument("--stale", default="",
                        help="Only organize sessions not updated for this duration "
                             "(e.g. '3h', '30m', '1d'). Omit to organize all inactive sessions.")
    args = parser.parse_args()

    stale_seconds = _parse_duration(args.stale) if args.stale else None
    sessions = find_pending_sessions(args.outputs_dir, args.projects_dir, args.self_session,
                                     stale_seconds=stale_seconds)
    projects = get_existing_projects(args.projects_dir)
    active_now = get_active_dirs(args.outputs_dir)

    if not sessions:
        if stale_seconds:
            dur = args.stale
            print(f"没有需要整理的 session。所有非空 session 都已归档、正在活跃中、或最近 {dur} 内有更新。")
        else:
            print("没有需要整理的 session。所有非空 session 都已归档或正在活跃中。")
        return

    existing_str = _format_projects(projects)

    overgrown = find_overgrown_projects(args.projects_dir, len(sessions))
    overgrown_str = _format_overgrown_section(overgrown)

    protected_str = _format_protected_section(active_now, args.self_session)

    sess_lines = []
    for s in sessions:
        sess_lines.append(f"- `{s['dir']}/` — 日志: `{s['log']}` ({s['size']} bytes)")
    session_str = "\n".join(sess_lines)

    prompt = PROMPT_TEMPLATE.format(
        outputs_dir=args.outputs_dir,
        projects_dir=args.projects_dir,
        threshold=SUBFOLDER_THRESHOLD,
        protected_section=protected_str,
        existing_projects=existing_str,
        overgrown_section=overgrown_str,
        count=len(sessions),
        session_list=session_str,
    )
    print(prompt, end="")


if __name__ == "__main__":
    main()
