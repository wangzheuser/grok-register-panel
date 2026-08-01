"""Process discovery and termination scoped to one project root."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
import psutil

try:
    from secure_files import atomic_write_text
except ImportError:  # running from webui/
    import sys

    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from secure_files import atomic_write_text


def _cmdline(pid: int) -> list[str]:
    try:
        proc = psutil.Process(pid)
        return proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
        return []


def _cwd(pid: int) -> Path | None:
    try:
        proc = psutil.Process(pid)
        cwd_str = proc.cwd()
        if cwd_str:
            return Path(cwd_str).resolve()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
        pass
    return None


def _resolved_arg(arg: str, cwd: Path) -> Path | None:
    if not arg or arg.startswith("-"):
        return None
    candidate = Path(arg)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def process_matches(
    pid: int,
    root: str | os.PathLike[str],
    script_names: tuple[str, ...] | list[str],
) -> bool:
    project_root = Path(root).resolve()
    process_cwd = _cwd(pid)
    if process_cwd != project_root:
        return False
    expected = {(project_root / name).resolve() for name in script_names}
    return any(
        _resolved_arg(arg, process_cwd) in expected
        for arg in _cmdline(pid)
    )


def find_managed_processes(
    root: str | os.PathLike[str],
    script_names: tuple[str, ...] | list[str],
) -> list[dict]:
    found = []
    for proc in psutil.process_iter(['pid', 'cmdline', 'cwd', 'create_time']):
        try:
            pid = proc.info['pid']
            if not process_matches(pid, root, script_names):
                continue
            cmdline = proc.info['cmdline'] or []
            
            create_time = proc.info['create_time']
            elapsed = int(time.time() - create_time) if create_time else 0
            if elapsed < 3600:
                etime = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            else:
                etime = f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            
            pgid = None
            if hasattr(os, "getpgid"):
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    pass
            
            found.append(
                {
                    "pid": pid,
                    "pgid": pgid,
                    "etime": etime,
                    "cmd": " ".join(cmdline)[:240],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            continue
    return sorted(found, key=lambda item: item["pid"])


def write_pid_file(
    path: str | os.PathLike[str],
    pid: int,
) -> None:
    atomic_write_text(path, f"{int(pid)}\n")


def read_verified_pid_file(
    path: str | os.PathLike[str],
    root: str | os.PathLike[str],
    script_names: tuple[str, ...] | list[str],
) -> int | None:
    try:
        pid = int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if process_matches(pid, root, script_names) else None


def terminate_managed_processes(
    root: str | os.PathLike[str],
    script_names: tuple[str, ...] | list[str],
    *,
    grace_seconds: float = 2.0,
) -> list[int]:
    processes = find_managed_processes(root, script_names)
    pids = {item["pid"] for item in processes}
    if not pids:
        return []

    for pid in pids:
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            parent.terminate()
        except psutil.NoSuchProcess:
            continue

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not find_managed_processes(root, script_names):
            break
        time.sleep(0.1)

    remaining = find_managed_processes(root, script_names)
    for item in remaining:
        pid = item["pid"]
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass
    return sorted(pids)
