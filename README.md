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
