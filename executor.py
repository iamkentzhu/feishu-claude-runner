#!/usr/bin/env python3
"""
Local Claude executor with run-record tracking and watchdog.

Replaces run_local_claude_task.py as an importable module.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from run_record import (
    RunRecord,
    DEFAULT_STATUS_DIR,
    _run_id,
    now_iso,
    save,
)

if TYPE_CHECKING:
    pass

DEFAULT_WORKDIR = os.environ.get("CLAUDE_RUNNER_WORKDIR", os.getcwd())
DEFAULT_CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")


class Executor:
    """
    Runs a Claude task as a subprocess.
    Tracks status via RunRecord, writes stdout+stderr to a log file,
    and runs a watchdog thread to detect stalls.
    """

    def __init__(
        self,
        label: str,
        task_file: str,
        *,
        run_id: str = "",
        transport: str = "local_wrapper",
        workdir: str = DEFAULT_WORKDIR,
        claude_bin: str = DEFAULT_CLAUDE_BIN,
        status_dir: str = DEFAULT_STATUS_DIR,
        timeout: int = 1800,
        stall_after: int = 300,
        settings_file: str = "",
        # Lineage
        fallback_from: str = "",
        parent_run_id: str = "",
        source_session_id: str = "",
        source_transport: str = "",
    ):
        self.status_dir = status_dir
        self.claude_bin = claude_bin
        self.settings_file = settings_file
        self.workdir = str(Path(workdir).expanduser().resolve())
        effective_run_id = run_id or _run_id(label)
        log_file = str(Path(status_dir).expanduser().resolve() / f"{effective_run_id}.log")

        # Build the actual command we'll run (for metadata)
        _cmd = [claude_bin, "--print", "--permission-mode", "bypassPermissions"]
        if settings_file:
            _cmd.extend(["--settings", settings_file])

        self.record = RunRecord(
            run_id=effective_run_id,
            label=label,
            task_file=str(Path(task_file).expanduser().resolve()),
            transport=transport,
            status="created",
            created_at=now_iso(),
            workdir=self.workdir,
            log_file=log_file,
            timeout_seconds=timeout,
            stall_after_seconds=stall_after,
            fallback_from=fallback_from or None,
            parent_run_id=parent_run_id or None,
            source_session_id=source_session_id or None,
            source_transport=source_transport or None,
            metadata={
                "command": _cmd,
                "invoked_by": "executor.py",
            },
        )
        self._proc: Optional[subprocess.Popen] = None
        self._stop = False
        self._terminate_signal: Optional[int] = None
        self._prev_signal_handlers: dict[int, signal.Handlers] = {}

    # ── internal ─────────────────────────────────────────────────────────────

    def _save(self) -> None:
        # Preserve card_message_id from existing disk record to avoid overwriting
        # what orchestrator wrote when it sent the initial card.
        if self.record.card_message_id is None:
            try:
                from run_record import load as _load
                existing = _load(self.record.run_id, self.status_dir)
                if existing.card_message_id:
                    self.record.card_message_id = existing.card_message_id
            except Exception:
                pass
        save(self.record, self.status_dir)

    def _touch_progress(self) -> None:
        log = self.record.log_file
        if not log or not Path(log).exists():
            return
        mtime = Path(log).stat().st_mtime
        ts = __import__("datetime").datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        self.record.last_output_at = ts
        self.record.last_progress_at = ts
        self.record.stalled = False
        self._save()

    def _watchdog(self) -> None:
        stall_after = self.record.stall_after_seconds
        while not self._stop and self._proc and self._proc.poll() is None:
            time.sleep(5)
            log = self.record.log_file
            if not log or not Path(log).exists():
                continue
            try:
                self._touch_progress()
            except Exception:
                pass
            if stall_after > 0:
                age = time.time() - Path(log).stat().st_mtime
                if age > stall_after and not self.record.stalled:
                    self.record.stalled = True
                    self.record.summary = f"No output for {int(age)}s"
                    self._save()

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._prev_signal_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle_termination_signal)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._prev_signal_handlers.items():
            signal.signal(sig, handler)
        self._prev_signal_handlers.clear()

    def _handle_termination_signal(self, signum: int, _frame) -> None:
        if self._terminate_signal is not None:
            return
        self._terminate_signal = signum
        self._stop = True
        signame = signal.Signals(signum).name
        self.record.summary = f"Received {signame}; cleaning up"
        self._save()
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _finalize_interrupted_run(self, signum: int) -> int:
        signame = signal.Signals(signum).name
        exit_code = 128 + signum

        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        self.record.finished_at = now_iso()
        self.record.exit_code = exit_code
        self.record.status = "cancelled"
        self.record.summary = f"Interrupted by {signame}"
        try:
            self._touch_progress()
        except Exception:
            self._save()
        return exit_code

    # ── public ────────────────────────────────────────────────────────────────

    def run(self) -> int:
        """Execute Claude and return exit code."""
        # Build direct Claude CLI command (no bash wrapper middleman)
        cmd = [self.claude_bin, "--print", "--permission-mode", "bypassPermissions"]
        if self.settings_file:
            cmd.extend(["--settings", self.settings_file])

        # Read task file content up front (bytes to preserve encoding exactly)
        task_bytes = Path(self.record.task_file).read_bytes()

        self.record.status = "running"
        self.record.started_at = now_iso()
        self.record.summary = "Local Claude runner started"
        self._save()

        log_path = Path(self.record.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        self._install_signal_handlers()
        try:
            with log_path.open("wb") as f:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=self.workdir,
                    stdin=subprocess.PIPE,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )
                self.record.pid = self._proc.pid
                self._save()

                # Feed task via a daemon thread to avoid blocking if Claude
                # produces output before consuming all stdin (shouldn't happen
                # in --print mode, but belt-and-suspenders).
                def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
                    try:
                        proc.stdin.write(data)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass

                feeder = threading.Thread(
                    target=_feed_stdin, args=(self._proc, task_bytes), daemon=True
                )
                feeder.start()

                t = threading.Thread(target=self._watchdog, daemon=True)
                t.start()

                timeout = self.record.timeout_seconds
                deadline = time.time() + timeout if timeout > 0 else None

                while True:
                    if self._terminate_signal is not None:
                        return self._finalize_interrupted_run(self._terminate_signal)

                    if deadline is not None:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            try:
                                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            self.record.finished_at = now_iso()
                            self.record.exit_code = 124
                            self.record.status = "timeout"
                            self.record.summary = f"Timed out after {self.record.timeout_seconds}s"
                            self._save()
                            return 124
                        wait_timeout = min(1.0, remaining)
                    else:
                        wait_timeout = 1.0

                    try:
                        code = self._proc.wait(timeout=wait_timeout)
                        break
                    except subprocess.TimeoutExpired:
                        continue

            self._stop = True
            self.record.finished_at = now_iso()
            self.record.exit_code = code
            self.record.status = "ok" if code == 0 else "failed"
            self.record.summary = f"Process exited with code {code}"
            try:
                self._touch_progress()
            except Exception:
                self._save()
            return code
        finally:
            self._stop = True
            self._restore_signal_handlers()


# ── CLI entry point (backward-compatible with run_local_claude_task.py) ──────

def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Run Claude local wrapper with run-record + watchdog")
    p.add_argument("--task-file", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--run-id", default="")
    p.add_argument("--transport", default="local_wrapper")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR)
    p.add_argument("--claude-bin", default=DEFAULT_CLAUDE_BIN)
    p.add_argument("--settings-file", default="",
                   help="Path to Claude settings JSON (maps to --settings). "
                        "Falls back to CLAUDE_SETTINGS env var if not set.")
    p.add_argument("--status-dir", default=DEFAULT_STATUS_DIR)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--stall-after", type=int, default=300)
    p.add_argument("--fallback-from", default="")
    p.add_argument("--parent-run-id", default="")
    p.add_argument("--source-session-id", default="")
    p.add_argument("--source-transport", default="")
    args = p.parse_args()

    settings_file = args.settings_file or os.environ.get("CLAUDE_SETTINGS", "")

    ex = Executor(
        label=args.label,
        task_file=args.task_file,
        run_id=args.run_id,
        transport=args.transport,
        workdir=args.workdir,
        claude_bin=args.claude_bin,
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
    # Print record path first (consumed by orchestrator)
    record_path = Path(args.status_dir).expanduser().resolve() / f"{ex.record.run_id}.json"
    print(str(record_path))
    return ex.run()


if __name__ == "__main__":
    import sys
    sys.exit(main())
