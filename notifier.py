#!/usr/bin/env python3
"""
Feishu notification layer for Claude runner.

Replaces notify_card.py and notify_run_event.py as an importable module.
Card state (message_id) is stored per-run inside the run record itself,
not in a shared global state file.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from run_record import RunRecord

# Config path: env var → ~/.claude-runner/config.json
_CONFIG_PATH_DEFAULT = Path.home() / ".claude-runner" / "config.json"
CONFIG_PATH = Path(os.environ.get("FEISHU_CONFIG_PATH", str(_CONFIG_PATH_DEFAULT)))

# Default chat_id: env var → empty string (notifications skipped if not set)
DEFAULT_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "")

TIMEOUT_SECONDS = 15


# ── Feishu auth ───────────────────────────────────────────────────────────────

def _serialize(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _load_feishu_credentials() -> tuple[str, str]:
    """
    Load Feishu appId and appSecret. Priority:
    1. Environment variables: FEISHU_APP_ID + FEISHU_APP_SECRET
    2. Config file at CONFIG_PATH.
       Supports two formats:
         - Flat: {"appId": "...", "appSecret": "..."}
         - OpenClaw-compatible: {"channels": {"feishu": {"appId": "...", "appSecret": "..."}}}
    """
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and app_secret:
        return app_id, app_secret

    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Feishu credentials not found. Set FEISHU_APP_ID + FEISHU_APP_SECRET env vars, "
            f"or create {CONFIG_PATH} with {{\"appId\": \"...\", \"appSecret\": \"...\"}}."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Flat format
    if "appId" in cfg:
        return cfg["appId"], cfg["appSecret"]
    # OpenClaw-compatible format
    feishu = cfg.get("channels", {}).get("feishu", {})
    return feishu["appId"], feishu["appSecret"]


def get_tenant_token() -> str:
    app_id, app_secret = _load_feishu_credentials()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=_serialize({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        data = json.loads(r.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu auth failed: {data}")
    return data["tenant_access_token"]


# ── Card builder ──────────────────────────────────────────────────────────────

_STAGE_DEFS: dict[str, dict] = {
    "dispatch":    {"name": "任务创建",    "icon": "✅"},
    "acp_started": {"name": "ACP 启动",   "icon": "⏳"},
    "fallback":    {"name": "ACP 切换",   "icon": "⚠️"},
    "local_start": {"name": "本地执行",   "icon": "⏳"},
    "stalled":     {"name": "执行卡住",   "icon": "❌"},
    "timeout":     {"name": "执行超时",   "icon": "❌"},
    "done":        {"name": "完成",        "icon": "✅"},
    "failed":      {"name": "失败",        "icon": "❌"},
}


def build_result_card(label: str, run_id: str, output: str, success: bool = True) -> dict:
    """Build a result card with Claude's actual output."""
    status_icon = "✅" if success else "❌"
    status_text = "执行成功" if success else "执行失败"
    header_color = "green" if success else "red"
    # Trim if too long
    if len(output) > 2000:
        output = "…（输出已截断，显示末尾部分）\n\n" + output[-1800:]
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**任务：**{label}　{status_icon} {status_text}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**run_id：**`{run_id}`"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"```\n{output}\n```"}},
    ]
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "📋 任务结果"},
            "template": header_color,
        },
        "body": {"elements": elements},
    }


def build_card(label: str, run_id: str, stages: list[dict]) -> dict:
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**任务：**{label}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**run_id：**`{run_id}`"}},
        {"tag": "hr"},
    ]
    for s in stages:
        icon = s.get("icon", "⏸️")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"{icon} **{s['name']}**"}})
        if s.get("detail"):
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": s["detail"]}})
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "🔧 Claude 任务执行"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


# ── Low-level send/update ─────────────────────────────────────────────────────

def _send_card(chat_id: str, card: dict) -> str:
    token = get_tenant_token()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=_serialize({
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": _serialize(card),
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        data = json.loads(r.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu send card failed: {data}")
    return data["data"]["message_id"]


def _update_card(message_id: str, card: dict) -> None:
    token = get_tenant_token()
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
        data=_serialize({"content": _serialize(card)}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        data = json.loads(r.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu update card failed: {data}")


def _send_text(chat_id: str, text: str) -> dict:
    token = get_tenant_token()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=_serialize({
            "receive_id": chat_id,
            "msg_type": "text",
            "content": _serialize({"text": text}),
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        data = json.loads(r.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu send text failed: {data}")
    return data


# ── In-memory stage state (per notify_card call chain) ───────────────────────
# Keyed by run_id → list of stage dicts accumulated so far.
# Lives only in the calling process; card_message_id is persisted in run record.
_stage_history: dict[str, list[dict]] = {}


def _get_stages(run_id: str) -> list[dict]:
    return _stage_history.setdefault(run_id, [])


def _upsert_stage(run_id: str, stage_key: str, detail: str) -> list[dict]:
    defn = _STAGE_DEFS.get(stage_key, {"name": stage_key, "icon": "⏸️"})
    entry = {"name": defn["name"], "icon": defn["icon"], "detail": detail}
    stages = _get_stages(run_id)
    idx = next((i for i, s in enumerate(stages) if s["name"] == entry["name"]), None)
    if idx is not None:
        stages[idx] = entry
    else:
        stages.append(entry)
    return stages


# ── Public API ────────────────────────────────────────────────────────────────

def notify_card(
    label: str,
    run_id: str,
    stage: str,
    detail: str = "",
    chat_id: str = DEFAULT_CHAT_ID,
    *,
    card_message_id: Optional[str] = None,
    status_dir: str = "",
) -> str:
    """
    Send or update a Feishu progress card for a run.

    card_message_id: if provided, updates existing card; otherwise creates new.
    Returns the message_id (create or existing).

    If status_dir is given, the returned message_id is persisted into the run record.
    """
    stages = _upsert_stage(run_id, stage, detail)
    card = build_card(label, run_id, stages)

    if card_message_id:
        _update_card(card_message_id, card)
        msg_id = card_message_id
    else:
        msg_id = _send_card(chat_id, card)

    # Persist message_id back into run record so future calls can update the same card
    if status_dir:
        try:
            from run_record import update as update_record
            update_record(run_id, card_message_id=msg_id, status_dir=status_dir)
        except Exception:
            pass

    return msg_id


def notify_text(
    label: str,
    run_id: str,
    status: str,
    summary: str = "",
    transport: str = "",
    extra: str = "",
    chat_id: str = DEFAULT_CHAT_ID,
    title: str = "",
    message: str = "",
) -> str:
    """Send a plain-text notification and return message_id."""
    if message:
        text = message
    else:
        parts = []
        if title:
            parts.append(title)
        if label:
            parts.append(f"任务：{label}")
        if run_id:
            parts.append(f"run_id：{run_id}")
        if transport:
            parts.append(f"链路：{transport}")
        if status:
            parts.append(f"状态：{status}")
        if summary:
            parts.append(summary)
        if extra:
            parts.append(extra)
        text = "\n".join(parts)
    if not text.strip():
        raise ValueError("Empty notification message")
    result = _send_text(chat_id, text)
    return result.get("data", {}).get("message_id", "")


# ── CLI entry points (backward compat) ───────────────────────────────────────

def _cli_card() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Send/update claude runner progress card")
    p.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    p.add_argument("--label", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--detail", default="")
    p.add_argument("--status-dir", default="")
    args = p.parse_args()

    # Try to load existing card_message_id from run record
    card_message_id = None
    if args.status_dir:
        try:
            from run_record import load
            rec = load(args.run_id, args.status_dir)
            card_message_id = rec.card_message_id
        except Exception:
            pass

    msg_id = notify_card(
        args.label,
        args.run_id,
        args.stage,
        args.detail,
        args.chat_id,
        card_message_id=card_message_id,
        status_dir=args.status_dir,
    )
    print(json.dumps({"message_id": msg_id, "stage": args.stage}, ensure_ascii=False, indent=2))
    return 0


def _cli_text() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Send text notification to Feishu")
    p.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    p.add_argument("--title", default="")
    p.add_argument("--label", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--transport", default="")
    p.add_argument("--status", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--extra", default="")
    p.add_argument("--message", default="")
    args = p.parse_args()
    msg_id = notify_text(
        label=args.label,
        run_id=args.run_id,
        status=args.status,
        summary=args.summary,
        transport=args.transport,
        extra=args.extra,
        chat_id=args.chat_id,
        title=args.title,
        message=args.message,
    )
    print(json.dumps({"message_id": msg_id}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: notifier.py card|text ...")
        sys.exit(1)
    cmd = sys.argv.pop(1)
    if cmd == "card":
        sys.exit(_cli_card())
    elif cmd == "text":
        sys.exit(_cli_text())
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
