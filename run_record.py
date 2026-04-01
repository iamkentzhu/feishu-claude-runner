#!/usr/bin/env python3
"""
Shared data model and CRUD for Claude runner records.

Replaces dispatch_task.py, update_run.py, check_run.py as importable module.
All state is stored as JSON files: <status_dir>/<run_id>.json
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_STATUS_DIR = os.environ.get(
    "CLAUDE_RUNNER_STATUS_DIR",
    str(Path.home() / ".claude-runner" / "runs"),
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run_id(label: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"{label}-{ts}-{short}"


@dataclass
class RunRecord:
    run_id: str
    label: str
    task_file: str
    transport: str          # "acp" | "local_wrapper"
    status: str             # planned | running | acp_running | fallback_started | ok | failed | timeout | cancelled
    created_at: str

    # Execution
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    workdir: Optional[str] = None
    log_file: Optional[str] = None
    last_output_at: Optional[str] = None
    last_progress_at: Optional[str] = None
    stalled: bool = False
    timeout_seconds: int = 1800
    stall_after_seconds: int = 300
    summary: str = ""

    # ACP state
    card_message_id: Optional[str] = None      # Feishu card message_id for this run

    # Lineage (ACP → local fallback)
    fallback_from: Optional[str] = None        # e.g. "acp"
    parent_run_id: Optional[str] = None
    source_session_id: Optional[str] = None
    source_transport: Optional[str] = None

    metadata: dict[str, Any] = field(default_factory=dict)


# ── persistence ──────────────────────────────────────────────────────────────

def _record_path(run_id: str, status_dir: str) -> Path:
    return Path(status_dir).expanduser().resolve() / f"{run_id}.json"


def save(record: RunRecord, status_dir: str = DEFAULT_STATUS_DIR) -> Path:
    path = _record_path(record.run_id, status_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2))
    return path


def load(run_id: str, status_dir: str = DEFAULT_STATUS_DIR) -> RunRecord:
    path = _record_path(run_id, status_dir)
    if not path.exists():
        raise FileNotFoundError(f"Run record not found: {path}")
    data = json.loads(path.read_text())
    return RunRecord(**{k: v for k, v in data.items() if k in RunRecord.__dataclass_fields__})


def load_latest(label: str = "", status_dir: str = DEFAULT_STATUS_DIR) -> Optional[RunRecord]:
    """Return the most recently modified record, optionally filtered by label prefix."""
    d = Path(status_dir).expanduser().resolve()
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if label:
        files = [f for f in files if f.stem.startswith(f"{label}-")]
    if not files:
        return None
    data = json.loads(files[0].read_text())
    return RunRecord(**{k: v for k, v in data.items() if k in RunRecord.__dataclass_fields__})


# ── factory ──────────────────────────────────────────────────────────────────

def _transport(mode: str, interactive: bool) -> str:
    if mode == "acp":
        return "acp"
    if mode == "local":
        return "local_wrapper"
    return "acp" if interactive else "local_wrapper"


def create_planned(
    label: str,
    task_file: str,
    mode: str = "auto",
    interactive: bool = False,
    reason: str = "",
    status_dir: str = DEFAULT_STATUS_DIR,
) -> RunRecord:
    """Create a new 'planned' record and persist it."""
    rec = RunRecord(
        run_id=_run_id(label),
        label=label,
        task_file=str(Path(task_file).expanduser().resolve()),
        transport=_transport(mode, interactive),
        status="planned",
        created_at=now_iso(),
        summary="Dispatcher created run plan",
        metadata={
            "dispatch_mode": mode,
            "interactive": interactive,
            "dispatch_reason": reason or ("interactive task" if interactive else "non-interactive task"),
        },
    )
    save(rec, status_dir)
    return rec


# ── mutation helpers ──────────────────────────────────────────────────────────

_TERMINAL = {"failed", "ok", "timeout", "cancelled", "fallback_started"}


def update(
    run_id: str,
    *,
    status: str = "",
    summary: str = "",
    source_session_id: str = "",
    source_transport: str = "",
    fallback_from: str = "",
    parent_run_id: str = "",
    card_message_id: str = "",
    extra: dict[str, Any] | None = None,
    status_dir: str = DEFAULT_STATUS_DIR,
) -> RunRecord:
    """Load, mutate, and save a run record."""
    rec = load(run_id, status_dir)
    if status:
        rec.status = status
        if status in _TERMINAL:
            rec.finished_at = now_iso()
    if summary:
        rec.summary = summary
    if source_session_id:
        rec.source_session_id = source_session_id
    if source_transport:
        rec.source_transport = source_transport
    if fallback_from:
        rec.fallback_from = fallback_from
    if parent_run_id:
        rec.parent_run_id = parent_run_id
    if card_message_id:
        rec.card_message_id = card_message_id
    if extra:
        rec.metadata.update(extra)
    save(rec, status_dir)
    return rec


# ── CLI shims (for backward compat / direct invocation) ──────────────────────

def _cli_dispatch() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Create dispatcher record")
    p.add_argument("--label", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--mode", choices=["auto", "acp", "local"], default="auto")
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--status-dir", default=DEFAULT_STATUS_DIR)
    p.add_argument("--reason", default="")
    args = p.parse_args()
    rec = create_planned(args.label, args.task_file, args.mode, args.interactive, args.reason, args.status_dir)
    print(json.dumps(asdict(rec), ensure_ascii=False, indent=2))
    return 0


def _cli_update() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Update run record")
    p.add_argument("--run-id", required=True)
    p.add_argument("--status-dir", default=DEFAULT_STATUS_DIR)
    p.add_argument("--status", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--source-session-id", default="")
    p.add_argument("--source-transport", default="")
    p.add_argument("--fallback-from", default="")
    p.add_argument("--parent-run-id", default="")
    p.add_argument("--extra", default="")
    args = p.parse_args()
    extra = json.loads(args.extra) if args.extra else None
    rec = update(
        args.run_id,
        status=args.status,
        summary=args.summary,
        source_session_id=args.source_session_id,
        source_transport=args.source_transport,
        fallback_from=args.fallback_from,
        parent_run_id=args.parent_run_id,
        extra=extra,
        status_dir=args.status_dir,
    )
    print(json.dumps(asdict(rec), ensure_ascii=False, indent=2))
    return 0


def _cli_check() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Inspect latest run record")
    p.add_argument("--status-dir", default=DEFAULT_STATUS_DIR)
    p.add_argument("--label", default="")
    args = p.parse_args()
    rec = load_latest(args.label, args.status_dir)
    if not rec:
        print("NO_RUNS")
        return 1
    print(json.dumps(asdict(rec), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    # Allow: python run_record.py dispatch|update|check ...
    if len(sys.argv) < 2:
        print("Usage: run_record.py dispatch|update|check ...")
        sys.exit(1)
    cmd = sys.argv.pop(1)
    if cmd == "dispatch":
        sys.exit(_cli_dispatch())
    elif cmd == "update":
        sys.exit(_cli_update())
    elif cmd == "check":
        sys.exit(_cli_check())
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
