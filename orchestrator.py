#!/usr/bin/env python3
"""
Main orchestration CLI for Claude runner.

Subcommands:
  dispatch         Create a planned run record and optionally notify
  mark-acp-started Record that ACP session has started
  run-local        Execute a task locally (with optional watcher + notifications)
  fallback         Mark ACP run as failed and execute local fallback
  status           Show latest run status

All state mutations use run_record directly (no subprocess round-trips).
Notifications use notifier directly (no subprocess round-trips).
Execution uses executor directly (single subprocess: claude CLI).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── ensure local imports work regardless of CWD ──────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

import run_record as rr
from run_record import DEFAULT_STATUS_DIR, now_iso
from executor import Executor, DEFAULT_WORKDIR, DEFAULT_CLAUDE_BIN, DEFAULT_SETTINGS_FILE
from notifier import notify_card, notify_text, DEFAULT_CHAT_ID


# ── helpers ───────────────────────────────────────────────────────────────────

def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _card(
    notify: bool,
    label: str,
    run_id: str,
    stage: str,
    detail: str = "",
    chat_id: str = DEFAULT_CHAT_ID,
    status_dir: str = DEFAULT_STATUS_DIR,
    *,
    card_message_id: Optional[str] = None,
    pid: Optional[int] = None,
) -> Optional[str]:
    if not notify:
        return None
    try:
        return notify_card(
            label, run_id, stage, detail, chat_id,
            card_message_id=card_message_id,
            status_dir=status_dir,
            pid=pid,
        )
    except Exception as e:
        print(f"[orchestrator] notify error: {e}", file=sys.stderr)
        return None


def _start_watcher(
    label: str,
    run_id: str,
    chat_id: str,
    status_dir: str,
    interval: int = 10,
    notify_interval: int = 60,
    max_duration: int = 0,
    auto_fallback: bool = False,
    fallback_task_file: str = "",
    fallback_stall_threshold: int = 120,
    claude_bin: str = "",
) -> Optional[subprocess.Popen]:
    """Start watcher.py as a detached background process."""
    watcher = str(Path(__file__).parent / "watcher.py")
    cmd = [
        sys.executable, watcher,
        "--label", label,
        "--run-id", run_id,
        "--chat-id", chat_id,
        "--status-dir", status_dir,
        "--interval", str(interval),
        "--notify-interval", str(notify_interval),
    ]
    if max_duration > 0:
        cmd += ["--max-duration", str(max_duration)]
    if auto_fallback and fallback_task_file:
        cmd += [
            "--auto-fallback",
            "--fallback-task-file", fallback_task_file,
            "--fallback-stall-threshold", str(fallback_stall_threshold),
            "--orchestrator-path", __file__,
        ]
        if claude_bin:
            cmd += ["--fallback-claude-bin", claude_bin]
    try:
        return subprocess.Popen(cmd, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[orchestrator] failed to start watcher: {e}", file=sys.stderr)
        return None


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_dispatch(args: argparse.Namespace) -> int:
    rec = rr.create_planned(
        label=args.label,
        task_file=args.task_file,
        mode=args.mode,
        interactive=args.interactive,
        reason=args.reason,
        status_dir=args.status_dir,
    )
    msg_id = _card(args.notify, rec.label, rec.run_id, "dispatch",
                   f"mode={args.mode}", args.chat_id, args.status_dir)
    _print({"dispatch": asdict(rec), "notification": {"message_id": msg_id}})
    return 0


def cmd_mark_acp_started(args: argparse.Namespace) -> int:
    extra = {
        "acp_session_key": args.acp_session_key,
        "acp_session_id": args.acp_session_id,
        "acp_agent": args.acp_agent,
        "acp_started_at": now_iso(),
    }
    rec = rr.update(
        args.run_id,
        status="acp_running",
        summary=args.summary or "ACP session started",
        source_transport="acp",
        source_session_id=args.acp_session_id,
        extra=extra,
        status_dir=args.status_dir,
    )
    msg_id = _card(args.notify, rec.label, rec.run_id, "acp_started",
                   f"session_id={args.acp_session_id or '-'}", args.chat_id, args.status_dir,
                   card_message_id=rec.card_message_id)
    _print({"run": asdict(rec), "notification": {"message_id": msg_id}})
    return 0


def _spawn_detached_run(args: argparse.Namespace, run_id: str) -> None:
    """Re-invoke this script as a detached background process to run the task."""
    cmd = [
        sys.executable, __file__, "run-local",
        "--label", args.label,
        "--task-file", args.task_file,
        "--run-id", run_id,
        "--workdir", args.workdir,
        "--status-dir", args.status_dir,
        "--timeout", str(args.timeout),
        "--stall-after", str(args.stall_after),
    ]
    if args.claude_bin:
        cmd += ["--claude-bin", args.claude_bin]
    settings_file = getattr(args, "settings_file", "") or os.environ.get("CLAUDE_SETTINGS", "")
    if not settings_file and Path(DEFAULT_SETTINGS_FILE).exists():
        settings_file = DEFAULT_SETTINGS_FILE
    if settings_file:
        cmd += ["--settings-file", settings_file]
    if getattr(args, "notify", False):
        # Note: do NOT pass --notify to the detached child; notifications are handled by watcher.
        cmd += ["--chat-id", args.chat_id]
    if args.fallback_from:
        cmd += ["--fallback-from", args.fallback_from]
    if args.parent_run_id:
        cmd += ["--parent-run-id", args.parent_run_id]
    if args.source_session_id:
        cmd += ["--source-session-id", args.source_session_id]
    if args.source_transport:
        cmd += ["--source-transport", args.source_transport]
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cmd_run_local(args: argparse.Namespace) -> int:
    effective_run_id = args.run_id or rr._run_id(args.label)

    settings_file = getattr(args, "settings_file", "") or os.environ.get("CLAUDE_SETTINGS", "")
    if not settings_file and Path(DEFAULT_SETTINGS_FILE).exists():
        settings_file = DEFAULT_SETTINGS_FILE

    ex = Executor(
        label=args.label,
        task_file=args.task_file,
        run_id=effective_run_id,
        transport="local_wrapper",
        workdir=args.workdir,
        claude_bin=args.claude_bin or DEFAULT_CLAUDE_BIN,
        status_dir=args.status_dir,
        timeout=args.timeout,
        stall_after=args.stall_after,
        settings_file=settings_file,
        fallback_from=args.fallback_from,
        parent_run_id=args.parent_run_id,
        source_session_id=args.source_session_id,
        source_transport=args.source_transport,
    )
    ex._save()

    # --detach: notify, start watcher, re-invoke self in background, return immediately
    if getattr(args, "detach", False):
        _card(args.notify, args.label, effective_run_id, "local_start",
              "本地 wrapper 启动（异步）", args.chat_id, args.status_dir)
        if args.notify:
            _start_watcher(args.label, effective_run_id, args.chat_id, args.status_dir,
                           interval=10, max_duration=args.timeout + 120)
        _spawn_detached_run(args, effective_run_id)
        _print({"detached": True, "run_id": effective_run_id, "label": args.label,
                "status_dir": args.status_dir})
        return 0

    # Synchronous path (default)
    msg_id = _card(args.notify, args.label, effective_run_id, "local_start",
                   "本地 wrapper 启动", args.chat_id, args.status_dir)
    watcher_proc = None
    if args.notify:
        watcher_proc = _start_watcher(
            args.label, effective_run_id, args.chat_id, args.status_dir, interval=10
        )

    exit_code = ex.run()

    # Terminate watcher
    if watcher_proc:
        try:
            watcher_proc.terminate()
            watcher_proc.wait(timeout=5)
        except Exception:
            pass

    rec = ex.record
    end_stage = "done" if rec.status == "ok" else "failed"
    _card(args.notify, rec.label, rec.run_id, end_stage, rec.summary,
          args.chat_id, args.status_dir, card_message_id=rec.card_message_id, pid=rec.pid)

    # Send result card (card 2) — same as watcher does in detach mode
    if args.notify and rec.log_file and Path(rec.log_file).exists():
        try:
            from notifier import build_result_card, _send_card as _feishu_send
            from watcher import _tail
            output = _tail(rec.log_file, lines=60).strip()
            if output:
                result_card = build_result_card(
                    rec.label, rec.run_id, output, success=(rec.status == "ok"),
                    pid=rec.pid,
                )
                _feishu_send(args.chat_id, result_card)
        except Exception as e:
            print(f"[orchestrator] result card error: {e}", file=sys.stderr)

    _print({"local_run": asdict(rec), "exit_code": exit_code})
    return exit_code


def cmd_fallback(args: argparse.Namespace) -> int:
    # 1. Mark original ACP run as fallback_started
    try:
        old_rec = rr.update(
            args.run_id,
            status="fallback_started",
            summary=args.fail_summary or "ACP failed, switching to local wrapper",
            source_transport="acp",
            fallback_from="acp",
            source_session_id=args.failed_session_id,
            extra={
                "acp_session_key": args.failed_session_key,
                "acp_failure_reason": args.failure_reason,
                "fallback_requested_at": now_iso(),
            },
            status_dir=args.status_dir,
        )
        old_card_msg_id = old_rec.card_message_id
    except FileNotFoundError:
        # ACP run record doesn't exist (e.g. ACP never wrote one) — create minimal stub
        old_rec = None
        old_card_msg_id = None

    # Notify fallback on the original card
    _card(args.notify, args.label, args.run_id, "fallback",
          f"failed_session_id={args.failed_session_id}", args.chat_id, args.status_dir,
          card_message_id=old_card_msg_id)

    # 2. Run local fallback
    fallback_run_id = f"{args.run_id}-fallback"
    settings_file = getattr(args, "settings_file", "") or os.environ.get("CLAUDE_SETTINGS", "")
    ex = Executor(
        label=args.label,
        task_file=args.task_file,
        run_id=fallback_run_id,
        transport="local_wrapper",
        workdir=args.workdir,
        claude_bin=args.claude_bin or DEFAULT_CLAUDE_BIN,
        status_dir=args.status_dir,
        timeout=args.timeout,
        stall_after=args.stall_after,
        settings_file=settings_file,
        fallback_from="acp",
        parent_run_id=args.run_id,
        source_session_id=args.failed_session_id,
        source_transport="acp",
    )
    ex._save()

    local_msg_id = _card(args.notify, args.label, fallback_run_id, "local_start",
                         f"parent_run_id={args.run_id}", args.chat_id, args.status_dir)

    watcher_proc = None
    if args.notify:
        watcher_proc = _start_watcher(
            args.label, fallback_run_id, args.chat_id, args.status_dir, interval=10
        )

    exit_code = ex.run()

    if watcher_proc:
        try:
            watcher_proc.terminate()
            watcher_proc.wait(timeout=5)
        except Exception:
            pass

    rec = ex.record
    end_stage = "done" if rec.status == "ok" else "failed"
    _card(args.notify, rec.label, rec.run_id, end_stage, rec.summary,
          args.chat_id, args.status_dir, card_message_id=rec.card_message_id)

    _print({
        "old_run": asdict(old_rec) if old_rec else {"run_id": args.run_id, "status": "fallback_started"},
        "local_fallback": asdict(rec),
        "lineage": f"acp -> local_wrapper | failed_session={args.failed_session_id} | new_run={fallback_run_id}",
        "exit_code": exit_code,
    })
    return exit_code


def cmd_status(args: argparse.Namespace) -> int:
    rec = rr.load_latest(args.label, args.status_dir)
    if not rec:
        _print({"error": "NO_RUNS"})
        return 1
    _print(asdict(rec))
    return 0


# ── argument parser ───────────────────────────────────────────────────────────

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--notify", action="store_true", help="Send Feishu notifications")
    p.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    p.add_argument("--status-dir", default=DEFAULT_STATUS_DIR)


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Claude orchestration CLI")
    sub = root.add_subparsers(dest="cmd", required=True)

    # dispatch
    p = sub.add_parser("dispatch", help="Create a planned run record")
    p.add_argument("--label", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--mode", choices=["auto", "acp", "local"], default="auto")
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--reason", default="")
    _add_common(p)
    p.set_defaults(func=cmd_dispatch)

    # mark-acp-started
    p = sub.add_parser("mark-acp-started", help="Record ACP session start")
    p.add_argument("--run-id", required=True)
    p.add_argument("--acp-session-key", default="")
    p.add_argument("--acp-session-id", default="")
    p.add_argument("--acp-agent", default="claude")
    p.add_argument("--summary", default="")
    _add_common(p)
    p.set_defaults(func=cmd_mark_acp_started)

    # run-local
    p = sub.add_parser("run-local", help="Execute task locally")
    p.add_argument("--label", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--run-id", default="")
    p.add_argument("--parent-run-id", default="")
    p.add_argument("--source-session-id", default="")
    p.add_argument("--source-transport", default="")
    p.add_argument("--fallback-from", default="")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR)
    p.add_argument("--claude-bin", default="")
    p.add_argument("--settings-file", default="",
                   help="Path to Claude settings JSON. Falls back to CLAUDE_SETTINGS env var.")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--stall-after", type=int, default=300)
    p.add_argument("--detach", action="store_true",
                   help="Fire-and-forget: start task in background and return run_id immediately.")
    _add_common(p)
    p.set_defaults(func=cmd_run_local)

    # fallback
    p = sub.add_parser("fallback", help="Mark ACP as failed and run local fallback")
    p.add_argument("--run-id", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--failed-session-id", default="")
    p.add_argument("--failed-session-key", default="")
    p.add_argument("--failure-reason", default="")
    p.add_argument("--fail-summary", default="")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR)
    p.add_argument("--claude-bin", default="")
    p.add_argument("--settings-file", default="",
                   help="Path to Claude settings JSON. Falls back to CLAUDE_SETTINGS env var.")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--stall-after", type=int, default=300)
    _add_common(p)
    p.set_defaults(func=cmd_fallback)

    # status
    p = sub.add_parser("status", help="Show latest run status")
    p.add_argument("--label", required=True)
    _add_common(p)
    p.set_defaults(func=cmd_status)

    return root


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
