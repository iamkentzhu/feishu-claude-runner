#!/usr/bin/env python3
"""
Watcher: monitors a running Claude task and keeps the Feishu card updated.

Key improvements over the old watch_and_update_card.py:
1. Reads card_message_id from run record (no shared state file).
2. ACP stall/timeout auto-triggers fallback via orchestrator — no manual intervention needed.
3. Structured progress: only reports lines prefixed "# PROGRESS:" for quality output;
   falls back to keyword heuristics for legacy tasks.
4. Shorter poll interval default (10s) and optional inotify-style trigger via mtime.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from run_record import RunRecord, load, load_latest, DEFAULT_STATUS_DIR
from notifier import notify_card, DEFAULT_CHAT_ID

PROGRESS_PREFIX = "# PROGRESS:"
KEYWORDS = [
    "完成", "成功", "失败", "错误", "开始", "正在", "已",
    "Creating", "Reading", "Writing", "Error", "Success",
]
def _tool_label(name: str) -> str:
    mapping = {
        "Read": "Claude 正在读取文件",
        "Edit": "Claude 正在修改代码",
        "Write": "Claude 正在写入文件",
        "Bash": "Claude 正在执行命令",
        "Grep": "Claude 正在搜索代码",
        "Glob": "Claude 正在查找文件",
        "WebFetch": "Claude 正在抓取网页",
        "TodoWrite": "Claude 正在更新任务清单",
    }
    return mapping.get(name, f"Claude 正在调用工具：{name}")


def _summarize_stream_event(line: str) -> str:
    try:
        event = json.loads(line)
    except Exception:
        return ""

    etype = event.get("type")

    if etype == "assistant":
        message = event.get("message") or {}
        content = message.get("content") or []
        if not content:
            return "Claude 正在思考"
        block = content[0] or {}
        btype = block.get("type")
        if btype == "thinking":
            thinking = (block.get("thinking") or "").strip()
            if not thinking:
                return "Claude 正在分析任务"
            if "Inspecting files" in thinking or "read" in thinking.lower():
                return "Claude 正在阅读代码和文件"
            if "Finalizing" in thinking or "final" in thinking.lower():
                return "Claude 正在整理结果"
            if "tool" in thinking.lower():
                return "Claude 正在调用工具处理任务"
            return "Claude 正在分析任务"
        if btype == "tool_use":
            tool_name = block.get("name", "")
            return _tool_label(tool_name)
        if btype == "text":
            text = (block.get("text") or "").strip()
            return text[:120] if text else "Claude 正在输出结果"
        return "Claude 正在处理任务"

    if etype == "user":
        return "Claude 已收到任务输入"

    if etype == "result":
        subtype = event.get("subtype") or ""
        if subtype == "success":
            return "Claude 已完成任务"
        if subtype == "error_max_turns":
            return "Claude 达到最大轮数限制"
        is_error = event.get("is_error")
        if is_error:
            return "Claude 执行失败"
        return "Claude 已返回结果"

    if etype == "system":
        subtype = event.get("subtype") or ""
        if subtype == "init":
            return "Claude 运行环境已初始化"

    return ""


# ── log parsing ───────────────────────────────────────────────────────────────

def _tail(log_file: str, lines: int = 50) -> str:
    try:
        result = subprocess.run(["tail", "-n", str(lines), log_file], capture_output=True, text=True, timeout=5)
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _extract_links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s)\]}>\"]+", text or "")


def _fallback_log_summary(log_file: str, lines: int = 40) -> str:
    tail = _tail(log_file, lines=lines)
    if not tail:
        return ""

    readable: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            summary = _summarize_stream_event(stripped)
            if summary:
                readable.append(summary)
            continue
        readable.append(stripped)
    return "\n".join(readable[-8:])[:1200]


def extract_final_result(log_file: str) -> dict:
    """Extract the user-readable final result from a Claude stream-json log."""
    final_event: Optional[dict] = None
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    event = json.loads(stripped)
                except Exception:
                    continue
                if event.get("type") == "result":
                    final_event = event
    except Exception as e:
        fallback = f"无法读取任务日志：{e}"
        return {"ok": False, "result": "", "error": fallback, "raw_event_found": False, "links": []}

    if final_event:
        is_success = final_event.get("subtype") == "success" and not final_event.get("is_error")
        result = (final_event.get("result") or "").strip()
        error = (final_event.get("error") or final_event.get("message") or "").strip()
        if is_success:
            return {
                "ok": True,
                "result": result or "任务已完成，但 Claude final result 为空。",
                "error": "",
                "raw_event_found": True,
                "links": _extract_links(result),
            }
        summary = error or result or "Claude 返回失败结果。"
        return {
            "ok": False,
            "result": "",
            "error": summary,
            "raw_event_found": True,
            "links": _extract_links(summary),
        }

    summary = _fallback_log_summary(log_file)
    return {
        "ok": False,
        "result": summary or "未在日志中找到 Claude final result。",
        "error": "未在日志中找到 Claude final result。",
        "raw_event_found": False,
        "links": _extract_links(summary),
    }


def extract_progress(log_file: str) -> str:
    """
    Extract a short progress summary from the log.
    Prefers structured "# PROGRESS: ..." lines; falls back to stream-json events,
    then keyword heuristics.
    """
    tail = _tail(log_file, lines=80)
    if not tail:
        return ""

    # Structured: prefer last PROGRESS line
    progress_lines = [l.strip() for l in tail.splitlines() if l.strip().startswith(PROGRESS_PREFIX)]
    if progress_lines:
        return progress_lines[-1][len(PROGRESS_PREFIX):].strip()[:200]

    # stream-json fallback
    jsonish = [l.strip() for l in tail.splitlines() if l.strip().startswith('{')]
    if jsonish:
        for line in reversed(jsonish):
            summary = _summarize_stream_event(line)
            if summary:
                return summary[:200]

    # Heuristic fallback
    lines = [l.strip() for l in tail.splitlines() if l.strip()]
    relevant = [l for l in lines[-20:] if any(k in l for k in KEYWORDS)]
    if relevant:
        return relevant[-1][:200]
    return lines[-1][:200] if lines else ""


# ── watcher loop ──────────────────────────────────────────────────────────────

def watch(
    label: str,
    run_id: str = "",
    *,
    status_dir: str = DEFAULT_STATUS_DIR,
    chat_id: str = DEFAULT_CHAT_ID,
    interval: int = 10,
    notify_interval: int = 60,
    max_duration: int = 1800,
    # ACP auto-fallback: if set, trigger fallback when ACP stalls/timeouts
    auto_fallback: bool = False,
    fallback_task_file: str = "",
    fallback_label: str = "",
    fallback_claude_bin: str = "",
    fallback_stall_threshold: int = 120,   # seconds of no output → trigger fallback
    orchestrator_path: str = "",
) -> None:
    """
    Watch a run until it reaches a terminal state, updating the Feishu card at each milestone.

    interval: how often to check run state and stall condition (seconds).
    notify_interval: minimum seconds between progress card updates (throttle).
      Terminal states (done/failed/stalled) always notify immediately regardless.

    auto_fallback: when True and the watched run is an ACP run that stalls/fails,
    automatically triggers local fallback via orchestrator.py.
    """
    start_time = time.time()
    last_progress = ""
    fallback_triggered = False
    last_notify_at: float = 0

    while time.time() - start_time < max_duration:
        # Load record
        try:
            rec = load(run_id, status_dir) if run_id else load_latest(label, status_dir)
        except Exception:
            rec = None

        if not rec:
            time.sleep(interval)
            continue

        effective_run_id = rec.run_id
        status = rec.status
        card_msg_id = rec.card_message_id

        # Wait for card_message_id to be written before sending any updates.
        # Without this, the first _notify call would _send_card (create new) instead of patching.
        if not card_msg_id and status not in {"ok", "done", "failed", "timeout"}:
            time.sleep(interval)
            continue

        def _notify(stage: str, detail: str = "") -> None:
            try:
                notify_card(
                    rec.label, effective_run_id, stage, detail,
                    chat_id, card_message_id=card_msg_id, status_dir=status_dir,
                    pid=rec.pid,
                )
            except Exception as e:
                print(f"[watcher] notify error: {e}", file=sys.stderr)

        # ── Terminal states ───────────────────────────────────────────────────
        if status in {"ok", "done"}:
            _notify("done", rec.summary)
            # Send a separate result card with Claude's final user-facing result
            if rec.log_file and Path(rec.log_file).exists():
                try:
                    from notifier import build_result_card, _send_card
                    final = extract_final_result(rec.log_file)
                    result_text = final["result"] if final["ok"] else final.get("error", "")
                    if result_text:
                        result_card = build_result_card(
                            rec.label,
                            effective_run_id,
                            result_text,
                            success=final["ok"],
                            pid=rec.pid,
                            log_file=rec.log_file,
                        )
                        _send_card(chat_id, result_card)
                except Exception as e:
                    print(f"[watcher] result card error: {e}", file=sys.stderr)
            return
        if status == "failed":
            _notify("failed", rec.summary)
            if rec.log_file and Path(rec.log_file).exists():
                try:
                    from notifier import build_result_card, _send_card
                    final = extract_final_result(rec.log_file)
                    result_text = final.get("error") or final.get("result") or rec.summary
                    if result_text:
                        result_card = build_result_card(
                            rec.label,
                            effective_run_id,
                            result_text,
                            success=False,
                            pid=rec.pid,
                            log_file=rec.log_file,
                        )
                        _send_card(chat_id, result_card)
                except Exception as e:
                    print(f"[watcher] failed result card error: {e}", file=sys.stderr)
            return
        if status == "timeout":
            _notify("timeout", rec.summary)
            return
        if status == "fallback_started":
            # The ACP run handed off; watcher job done for this run
            return

        # ── Stall detection & ACP auto-fallback ──────────────────────────────
        if rec.stalled and not fallback_triggered:
            _notify("stalled", rec.summary or "No output")
            if auto_fallback and rec.transport == "acp" and fallback_task_file:
                _trigger_fallback(
                    rec, fallback_task_file, fallback_label or label,
                    fallback_claude_bin, chat_id, status_dir, orchestrator_path,
                )
                fallback_triggered = True
                return
            # For local runs: don't exit — keep polling until process finishes,
            # then send a result card so the user sees the actual output (even
            # if it's just "I don't have this tool").
            # We only notify stalled once (fallback_triggered acts as the flag).
            fallback_triggered = True  # suppress duplicate stalled notifications

        # ── ACP stall by log mtime (for ACP runs that log to file) ───────────
        if (
            auto_fallback
            and not fallback_triggered
            and rec.transport == "acp"
            and rec.log_file
            and Path(rec.log_file).exists()
            and fallback_task_file
        ):
            age = time.time() - Path(rec.log_file).stat().st_mtime
            if age > fallback_stall_threshold:
                rec.stalled = True
                rec.summary = f"ACP no output for {int(age)}s — auto fallback"
                from run_record import save as save_rec
                save_rec(rec, status_dir)
                _notify("stalled", rec.summary)
                _trigger_fallback(
                    rec, fallback_task_file, fallback_label or label,
                    fallback_claude_bin, chat_id, status_dir, orchestrator_path,
                )
                fallback_triggered = True
                return

        # ── Progress update from log (throttled by notify_interval) ─────────
        if rec.log_file and Path(rec.log_file).exists():
            progress = extract_progress(rec.log_file)
            now = time.time()
            if progress and progress != last_progress and (now - last_notify_at) >= notify_interval:
                _notify("local_start", progress)
                last_progress = progress
                last_notify_at = now

        time.sleep(interval)


def _trigger_fallback(
    rec: RunRecord,
    task_file: str,
    label: str,
    claude_bin: str,
    chat_id: str,
    status_dir: str,
    orchestrator_path: str,
) -> None:
    """Invoke orchestrator.py fallback subcommand as a subprocess."""
    import os
    orch = orchestrator_path or str(Path(__file__).parent / "orchestrator.py")
    cmd = [
        sys.executable, orch, "fallback",
        "--run-id", rec.run_id,
        "--label", label,
        "--task-file", task_file,
        "--failed-session-id", rec.source_session_id or "",
        "--failure-reason", "auto-detected stall by watcher",
        "--fail-summary", rec.summary or "ACP stalled",
        "--notify",
        "--chat-id", chat_id,
        "--status-dir", status_dir,
    ]
    if claude_bin:
        cmd.extend(["--claude-bin", claude_bin])

    print(f"[watcher] triggering auto-fallback for {rec.run_id}", file=sys.stderr)
    subprocess.Popen(cmd, start_new_session=True)


# ── CLI entry point (backward compat with watch_and_update_card.py) ───────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Watch Claude run and update progress card")
    p.add_argument("--label", required=True)
    p.add_argument("--run-id", default="")
    p.add_argument("--status-dir", default=DEFAULT_STATUS_DIR)
    p.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    p.add_argument("--interval", type=int, default=10,
                   help="State check interval in seconds (stall detection frequency)")
    p.add_argument("--notify-interval", type=int, default=60,
                   help="Min seconds between progress card updates (throttle). Terminal states always notify immediately.")
    p.add_argument("--max-duration", type=int, default=1800)
    # ACP auto-fallback args
    p.add_argument("--auto-fallback", action="store_true",
                   help="Auto-trigger local fallback when ACP run stalls")
    p.add_argument("--fallback-task-file", default="")
    p.add_argument("--fallback-label", default="")
    p.add_argument("--fallback-claude-bin", default="")
    p.add_argument("--fallback-stall-threshold", type=int, default=120)
    p.add_argument("--orchestrator-path", default="")
    args = p.parse_args()

    watch(
        label=args.label,
        run_id=args.run_id,
        status_dir=args.status_dir,
        chat_id=args.chat_id,
        interval=args.interval,
        notify_interval=args.notify_interval,
        max_duration=args.max_duration,
        auto_fallback=args.auto_fallback,
        fallback_task_file=args.fallback_task_file,
        fallback_label=args.fallback_label,
        fallback_claude_bin=args.fallback_claude_bin,
        fallback_stall_threshold=args.fallback_stall_threshold,
        orchestrator_path=args.orchestrator_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
