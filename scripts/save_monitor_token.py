#!/usr/bin/env python3
"""Persist the panel token (MONITOR_TOKEN) into .env for start.bat.

Reads the token from the MONITOR_TOKEN environment variable so the value
never travels through the command line. Replaces an existing entry or
appends one, leaving every other line of .env untouched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from secure_files import atomic_write_text  # noqa: E402

KEY = "MONITOR_TOKEN"


def main() -> int:
    token = str(os.environ.get(KEY, "") or "").strip()
    if not token:
        print(f"[Config] {KEY} is empty; nothing to save.")
        return 1

    env_path = ROOT / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == KEY:
                out.append(f"{KEY}={token}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{KEY}={token}")

    content = "\n".join(out) + "\n"
    if env_path.exists() and env_path.read_text(encoding="utf-8") == content:
        print(f"[Config] {KEY} is already saved in {env_path.name}.")
        return 0

    atomic_write_text(env_path, content)
    print(f"[Config] {KEY} saved to {env_path.name}; it will be reused on next start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
