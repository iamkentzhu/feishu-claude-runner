# feishu-claude-runner

Run Claude Code CLI tasks from Feishu — with async dispatch, real-time progress cards, and result notifications.

通过飞书触发 Claude Code 本地任务执行，支持异步派发、实时进度卡片和结果通知。

---

## What is this / 这是什么

A lightweight Python orchestration layer that lets an AI agent (like [OpenClaw](https://openclaw.ai)) dispatch tasks to a local [Claude Code](https://claude.ai/code) CLI process and report progress back via Feishu interactive cards.

一个轻量级 Python 编排层，让 AI Agent（如 OpenClaw）把任务派发到本地 Claude Code CLI 执行，并通过飞书互动卡片实时回传进度。

**Two Feishu messages per run / 每次任务发送两条飞书消息：**

- **Card 1 (progress)**: Created on dispatch, patched in-place as the task moves through stages → `任务创建` → `本地执行` → `完成/失败`
- **Card 2 (result)**: Sent after completion with Claude's actual output

- **卡片 1（进度卡片）**：任务发起时创建，随阶段 patch 更新
- **卡片 2（结果卡片）**：任务完成后新发一条，包含 Claude 的实际输出

---

## Background / 背景

This project was built through a conversation between [Kent](https://github.com/iamkentzhu) and Claude (Anthropic). Here's how it happened:

这个项目诞生于 Kent 和 Claude 的一次完整协作过程，记录如下：

**1. Problem statement / 提出需求**

Kent runs [OpenClaw](https://openclaw.ai) — an AI agent platform — connected to Feishu (Lark). The goal: send a task from Feishu, have a local Claude Code CLI process execute it, get real-time progress updates, and receive the result back in Feishu. Simple in concept, but the naive implementation hit a hard wall immediately.

Kent 在本地跑了一套 OpenClaw AI Agent，连接飞书。需求很直接：在飞书发一条消息，触发本地 Claude Code CLI 执行任务，实时看进度，最后把结果发回飞书。听起来简单，但第一版实现就卡死了。

**2. Root cause analysis / 定位根因**

The first attempt used `nohup` to background Claude and redirect its output to a file — Claude never produced any output. A week of debugging narrowed it down: `claude --print` reads its prompt from stdin via `proc.communicate()`. That works fine in a foreground terminal. But when backgrounded through Node.js (OpenClaw's gateway), the stdin/stdout pipe chain broke silently — Claude sat waiting for input that never arrived, and was eventually killed.

第一版用 `nohup` 后台运行 Claude，输出重定向到文件——Claude 始终无输出。排查发现：`claude --print` 通过 `proc.communicate()` 从 stdin 读 prompt，在前台终端没问题，但经过 Node.js（OpenClaw 网关）后台派发时，stdin/stdout 的 pipe 传递链断裂，Claude 卡在等待输入，最终被超时杀掉。

**3. Design session / 方案讨论（Opus 模型）**

Kent switched to Claude Opus for the design phase. The two went back and forth on architecture: whether to use the ACP/acpx protocol (OpenClaw's preferred route), or build a direct local runner as a reliable fallback. The conclusion: build both. A direct Python executor as the primary path, with the ACP route as an option for interactive sessions. The key insight was to have the executor own the subprocess directly — no bash wrapper, no heredoc, just `subprocess.Popen` with `stdin=PIPE` and a feeder thread, so the pipe relationship is fully controlled.

Kent 切换到 Opus 模型讨论方案。两人来回讨论了架构：是走 ACP/acpx 协议（OpenClaw 的标准链路），还是直接本地执行作为可靠的兜底路径。结论是两条都要做。Python Executor 直接管理子进程作为主路径，ACP 作为交互场景的选项。核心洞察是让 executor 完全控制子进程——不走 bash 封装，不走 heredoc，直接用 `subprocess.Popen` 配合 stdin feeder 线程，彻底消除 pipe 断链问题。

**4. Implementation / 编码实现（Sonnet 模型）**

Kent switched to Claude Sonnet for execution. The 8 loosely coupled shell scripts that existed were replaced with 5 focused Python modules. Along the way, several real issues surfaced and were fixed in-conversation: the ACP `claude-agent-acp` package had been deprecated and renamed (causing "Internal error" on every session creation); Claude's stall timeout was too aggressive at 120s; the whole system was synchronous — Lucie had to sit and wait for Claude to finish before replying to Feishu. Each issue was diagnosed from actual logs and fixed.

Kent 切换到 Sonnet 模型执行编码。把原来 8 个松散的 shell 脚本重构成 5 个聚焦的 Python 模块。过程中发现并修复了几个真实问题：ACP 的 `claude-agent-acp` 包已被废弃改名（导致每次建 session 都报 Internal error）；Claude stall 超时 120s 太激进；整个系统是同步的——Lucie 必须等 Claude 跑完才能回飞书消息。每个问题都从实际日志里定位，逐一修掉。

**5. Open source / 开源发布**

After the system was running stably, Kent decided to open source the runner layer. Claude audited all 5 files for hardcoded paths, credentials, and personal identifiers, replaced them with environment variables, wrote this README, and published to GitHub — including one more bug fix caught during the process (missing `--settings` flag that caused Claude to start with no API provider).

系统跑稳之后，Kent 决定把 runner 层开源。Claude 对 5 个文件做了完整审计，清理了所有硬编码路径、凭证、个人标识符，替换为环境变量，写了这份 README，推送到 GitHub——过程中还顺手修了一个新发现的 bug（缺少 `--settings` 参数导致 Claude 启动时没有 API provider 配置）。

---

## Architecture / 架构

```
Feishu message
     │
     ▼
 AI Agent (e.g. OpenClaw/Lucie)
     │
     ▼  claude-runner run-local --notify --detach
     │
     ├──► orchestrator.py   Main CLI entry point
     │         │
     │         ├──► executor.py      Manages Claude subprocess + watchdog
     │         │        │
     │         │        └──► ~/.local/bin/claude --print   (Claude Code CLI)
     │         │
     │         └──► watcher.py       Background monitor (detached process)
     │                   │
     │                   └──► notifier.py   Feishu card send/patch
     │
     └──► run_record.py   JSON state files in CLAUDE_RUNNER_STATUS_DIR/
```

---

## Quick Start / 快速开始

### 1. Install Claude Code CLI / 安装 Claude Code

```bash
npm install -g @anthropic-ai/claude-code
# Verify / 验证:
~/.local/bin/claude --version
```

### 2. Configure Feishu / 配置飞书

Create a Feishu app at [open.feishu.cn](https://open.feishu.cn), enable the **im:message** permission, and get your `appId` and `appSecret`.

在[飞书开放平台](https://open.feishu.cn)创建自建应用，开通 **im:message** 权限，拿到 `appId` 和 `appSecret`。

```bash
# Method A: env vars / 方式 A：环境变量
export FEISHU_APP_ID=cli_xxxxxxxxxxxx
export FEISHU_APP_SECRET=your_app_secret
export FEISHU_CHAT_ID=oc_xxxxxxxxxxxx   # target chat / 目标会话

# Method B: config file / 方式 B：配置文件
mkdir -p ~/.claude-runner
cp config.example.json ~/.claude-runner/config.json
# edit ~/.claude-runner/config.json with your appId and appSecret
```

### 3. Set working directory / 设置工作目录

```bash
export CLAUDE_RUNNER_WORKDIR=~/your-project   # where Claude will run
# Run records default to ~/.claude-runner/runs/
```

### 4. Run a task / 执行任务

```bash
echo "Fix the failing test in src/utils.py" > /tmp/task.txt

python3 orchestrator.py run-local \
  --label my-fix \
  --task-file /tmp/task.txt \
  --notify \
  --detach        # returns immediately, watcher handles Feishu updates
```

### 5. Check status / 查看状态

```bash
python3 orchestrator.py status --label my-fix
```

---

## Configuration / 配置

All configuration is via environment variables. No config file required unless you prefer it for Feishu credentials.

所有配置通过环境变量控制，飞书凭证支持配置文件作为替代。

| Variable | Default | Description |
|----------|---------|-------------|
| `FEISHU_APP_ID` | — | Feishu app ID (required for `--notify`) |
| `FEISHU_APP_SECRET` | — | Feishu app secret (required for `--notify`) |
| `FEISHU_CHAT_ID` | — | Default Feishu chat ID to send cards to |
| `FEISHU_CONFIG_PATH` | `~/.claude-runner/config.json` | Path to JSON config with credentials |
| `CLAUDE_RUNNER_WORKDIR` | `$PWD` | Default working directory for Claude |
| `CLAUDE_RUNNER_STATUS_DIR` | `~/.claude-runner/runs` | Directory for run records and logs |

**Config file format (flat, recommended):**

```json
{ "appId": "cli_xxx", "appSecret": "xxx" }
```

**Also supports OpenClaw-compatible format:**

```json
{ "channels": { "feishu": { "appId": "cli_xxx", "appSecret": "xxx" } } }
```

---

## CLI Usage / 命令行

### `run-local` — Execute a task

```bash
python3 orchestrator.py run-local \
  --label <name>         \   # short identifier for this task
  --task-file <path>     \   # text file with the prompt/task for Claude
  [--notify]             \   # send Feishu card updates
  [--detach]             \   # fire-and-forget (return run_id immediately)
  [--chat-id <id>]       \   # override default FEISHU_CHAT_ID
  [--timeout 1800]       \   # max seconds before killing Claude (default 1800)
  [--stall-after 300]    \   # seconds of no output before marking stalled (default 300)
  [--workdir <path>]     \   # override CLAUDE_RUNNER_WORKDIR
  [--claude-bin <path>]      # path to claude CLI (default ~/.local/bin/claude)
```

**`--detach` output:**
```json
{"detached": true, "run_id": "my-fix-20260401-120000-ab1234", "label": "my-fix", "status_dir": "..."}
```

### `status` — Check run state

```bash
python3 orchestrator.py status --label <name>
```

### `dispatch` — Create a record without running

```bash
python3 orchestrator.py dispatch --label <name> --task-file <path>
```

### `fallback` — Switch ACP run to local execution

```bash
python3 orchestrator.py fallback \
  --run-id <acp-run-id> \
  --label <name> \
  --task-file <path> \
  --notify
```

---

## Run Record / 运行记录

Each run creates two files in `CLAUDE_RUNNER_STATUS_DIR/`:

```
<run_id>.json    # state machine record (status, timestamps, card_message_id, ...)
<run_id>.log     # raw Claude stdout+stderr
```

Status transitions: `created` → `running` → `ok` / `failed` / `timeout` / `cancelled`

Progress lines in the task or Claude output prefixed with `# PROGRESS:` are extracted and shown in the Feishu card:

```
# PROGRESS: Analyzing test failures...
# PROGRESS: Fix applied, running tests...
```

---

## Feishu App Setup / 飞书应用配置

1. Go to [open.feishu.cn/app](https://open.feishu.cn/app) → Create custom app / 创建自建应用
2. Add permission: **发送消息** (`im:message:send_as_bot`)
3. Publish the app and install it to your workspace
4. Get `appId` and `appSecret` from the app credentials page
5. Get `chat_id`: in Feishu, open a chat → Settings → Copy link, the `open_id` at the end is your `oc_xxx` chat ID

---

## Requirements / 依赖

- Python 3.9+
- [Claude Code CLI](https://claude.ai/code) (`~/.local/bin/claude`)
- A Feishu custom app with messaging permissions (only required for `--notify`)

---

## License

MIT
