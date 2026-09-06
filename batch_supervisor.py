"""Supervise headless registration batches and recover crashed browser drivers."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence
import psutil

from secure_files import atomic_write_json, exclusive_file_lock


PROGRESS_ENV = "GROK_BATCH_PROGRESS_FILE"
DEFAULT_IDLE_TIMEOUT = 360
DEFAULT_MAX_RESTARTS = 8
DRAIN_FILE = Path(__file__).resolve().parent / "log" / "batch-drain.json"
DRAIN_FILE_MAX_AGE_SEC = 6 * 3600


def drain_requested() -> bool:
    """编排器轮次到期写入该文件；worker 完成在跑任务后退出，supervisor 不再重启。"""
    try:
        st = DRAIN_FILE.stat()
    except OSError:
        return False
    if time.time() - st.st_mtime > DRAIN_FILE_MAX_AGE_SEC:
        return False
    try:
        data = json.loads(DRAIN_FILE.read_text(encoding="utf-8") or "{}")
        expire_at = float(data.get("expire_at") or 0)
        if expire_at and time.time() > expire_at:
            return False
    except (OSError, ValueError, TypeError):
        pass
    return True

_PROGRESS_LOCK = threading.Lock()
_DRIVER_CRASH_MARKERS = (
    "Cannot read properties of undefined (reading '_getChildFrames')",
    "Cannot read properties of undefined (reading 'childFrames')",
    "Connection closed while reading from the driver",
    "Playwright driver unexpectedly exited",
)


def is_driver_crash_line(line: str) -> bool:
    text = str(line or "")
    return any(marker in text for marker in _DRIVER_CRASH_MARKERS)


def read_completed(path: str | os.PathLike[str]) -> int:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return max(0, int(data.get("completed", 0) or 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def initialize_progress(path: str | os.PathLike[str], target: int) -> Path:
    progress_path = Path(path)
    atomic_write_json(
        progress_path,
        {
            "completed": 0,
            "target": max(0, int(target)),
            "updated_at": time.time(),
        },
    )
    return progress_path


def mark_slot_completed(slots: int = 1) -> None:
    """Persist completed task slots for the supervising parent process."""
    raw_path = str(os.environ.get(PROGRESS_ENV, "") or "").strip()
    if not raw_path:
        return
    increment = max(0, int(slots or 0))
    if increment <= 0:
        return

    path = Path(raw_path)
    lock_path = path.with_name(f"{path.name}.lock")
    with _PROGRESS_LOCK:
        with exclusive_file_lock(lock_path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                data = {}
            completed = max(0, int(data.get("completed", 0) or 0)) + increment
            target = max(0, int(data.get("target", 0) or 0))
            if target:
                completed = min(completed, target)
            atomic_write_json(
                path,
                {
                    "completed": completed,
                    "target": target,
                    "updated_at": time.time(),
                },
            )


def _terminate_process_group(process: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
    except psutil.NoSuchProcess:
        return
    try:
        process.wait(timeout=max(0.1, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except psutil.NoSuchProcess:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_supervisor(
    count: int,
    workers: int,
    child_command_builder: Callable[[int, int], Sequence[str]],
    *,
    progress_file: str | os.PathLike[str],
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    child_env: Mapping[str, str] | None = None,
) -> int:
    """Run a batch child and restart the remaining work after a driver crash."""
    target = max(1, int(count))
    worker_count = max(1, min(24, int(workers), target))
    progress_path = initialize_progress(progress_file, target)
    stop_requested = False
    active_process: subprocess.Popen | None = None
    restarts = 0

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    can_install_signals = threading.current_thread() is threading.main_thread()
    previous_handlers: dict[int, object] = {}
    if can_install_signals:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    try:
        while not stop_requested:
            completed = read_completed(progress_path)
            remaining = max(0, target - completed)
            if remaining <= 0:
                print(
                    f"[supervisor] batch complete completed={completed}/{target} restarts={restarts}",
                    flush=True,
                )
                return 0
            if restarts > max(0, int(max_restarts)):
                print(
                    f"[supervisor] restart limit reached remaining={remaining} restarts={restarts}",
                    flush=True,
                )
                return 1

            command = [str(part) for part in child_command_builder(remaining, worker_count)]
            env = {**os.environ, **dict(child_env or {})}
            env[PROGRESS_ENV] = str(progress_path)
            print(
                f"[supervisor] starting child remaining={remaining} workers={worker_count} restart={restarts}",
                flush=True,
            )
            active_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=env,
            )
            assert active_process.stdout is not None
            q = queue.Queue()
            
            def enqueue_output(out, q):
                for line in iter(out.readline, ''):
                    if not line:
                        break
                    q.put(line)
                q.put(None)
                
            t = threading.Thread(target=enqueue_output, args=(active_process.stdout, q))
            t.daemon = True
            t.start()
            
            last_output = time.monotonic()
            restart_reason = ""

            while not stop_requested:
                try:
                    line = q.get(timeout=1.0)
                    if line is None:
                        break
                    last_output = time.monotonic()
                    print(line, end="", flush=True)
                    if is_driver_crash_line(line):
                        restart_reason = "playwright driver crashed"
                        break
                except queue.Empty:
                    pass

                # 收尾（drain）期间：worker 可能在跑长任务，暂停 idle 判定，
                # 由编排器的 drain 宽限期兜底强杀。
                if drain_requested():
                    last_output = time.monotonic()

                if restart_reason:
                    break
                
                return_code = active_process.poll()
                if return_code is not None:
                    while not q.empty():
                        try:
                            line = q.get_nowait()
                            if line is not None:
                                print(line, end="", flush=True)
                        except queue.Empty:
                            break
                    break
                
                if time.monotonic() - last_output > max(1.0, float(idle_timeout)):
                    restart_reason = f"no child output for {int(idle_timeout)}s"
                    break

            if stop_requested:
                _terminate_process_group(active_process)
                return 130

            return_code = active_process.poll()
            if restart_reason:
                _terminate_process_group(active_process)
                if drain_requested():
                    print(
                        "[supervisor] drain requested; stop without restart",
                        flush=True,
                    )
                    return 0
                restarts += 1
                remaining = max(0, target - read_completed(progress_path))
                print(
                    f"[supervisor] {restart_reason}; restarting remaining={remaining} attempt={restarts}/{max_restarts}",
                    flush=True,
                )
                time.sleep(min(1.0 * restarts, 5.0))
                continue

            completed = read_completed(progress_path)
            if return_code == 0 and completed >= target:
                print(
                    f"[supervisor] child exited cleanly completed={completed}/{target}",
                    flush=True,
                )
                return 0

            if drain_requested():
                print(
                    f"[supervisor] drain requested; stop without restart completed={completed}/{target}",
                    flush=True,
                )
                return 0

            restarts += 1
            remaining = max(0, target - completed)
            print(
                f"[supervisor] child exited rc={return_code} completed={completed}/{target}; "
                f"restarting remaining={remaining} attempt={restarts}/{max_restarts}",
                flush=True,
            )
            time.sleep(min(1.0 * restarts, 5.0))
    finally:
        if active_process is not None and active_process.poll() is None:
            _terminate_process_group(active_process)
        if can_install_signals:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        for path in (progress_path, progress_path.with_name(f"{progress_path.name}.lock")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    return 130
