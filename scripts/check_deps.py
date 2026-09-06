#!/usr/bin/env python3
"""Verify every runtime dependency of the panel is installed.

Detects third-party imports across the runtime source tree, cross-checks
them against requirements.txt (presence and version), and import-tests
anything not covered by an installed distribution. Exits 1 with a report
so start.bat can pip install and retry.
"""
from __future__ import annotations

import ast
import re
import sys
from importlib import import_module, metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = (ROOT, ROOT / "webui", ROOT / "email_providers", ROOT / "scripts")
SKIP_PARTS = {
    ".venv", "venv", "__pycache__", ".git", ".codex-artifacts", ".github",
    "docs", "deploy", "tests", "accounts", "log", "auth_out",
    "cpa_auth", "grok2api_auth", "node_modules",
}

# Minimal fallback for interpreters without sys.stdlib_module_names (<3.10).
_STDLIB_FALLBACK = frozenset(
    """abc argparse array ast asyncio base64 binascii bisect builtins bytevars
    calendar cmath cmd collections concurrent configparser contextlib contextvars
    copy cProfile csv ctypes dataclasses datetime decimal difflib dis doctest
    email enum errno faulthandler fileinput fnmatch fractions ftplib functools
    gc getopt getpass gettext glob graphlib gzip hashlib heapq hmac html http
    importlib inspect io ipaddress itertools json keyword linecache locale
    logging lzma marshal math mimetypes mmap multiprocessing numbers operator
    os pathlib pickle pkgutil platform plistlib poplib posix printlib profile
    pstats pty pwd py_compile queue quopri random re readline reprlib resource
    runpy sched secrets select selectors shelve shlex shutil signal site smtpd
    socket socketserver sqlite3 ssl stat statistics string stringprep struct
    subprocess symtable sys sysconfig tarfile tempfile textwrap threading time
    timeit tkinter token tokenize trace traceback tracemalloc tty types
    typing unicodedata unittest urllib uuid venv warnings wave weakref webbrowser
    wsgiref xml xmlrpc zipapp zipfile zipimport zlib zoneinfo __future__ _thread
    fcntl msvcrt winreg posixterm termios tty ntpwd ntpath genericpath posixpath
    os2emxpath macpath ntpath""".split()
)


def stdlib_names() -> frozenset:
    names = getattr(sys, "stdlib_module_names", None)
    return names or _STDLIB_FALLBACK


def normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def iter_py_files():
    seen = set()
    for base in SCAN_DIRS:
        if not base.is_dir():
            continue
        pattern = "*.py" if base == ROOT else "**/*.py"
        for path in base.glob(pattern):
            rel = path.relative_to(ROOT)
            if any(part in SKIP_PARTS for part in rel.parts):
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            yield path


def local_module_names() -> set:
    names = set()
    for path in iter_py_files():
        names.add(path.stem)
    for child in ROOT.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            if child.name not in SKIP_PARTS:
                names.add(child.name)
    return names


def top_level_imports(path: Path) -> set:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — always local
                continue
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def parse_requirements(path: Path):
    reqs = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return reqs
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[\[<>=!~;@ ]", line, 1)[0].strip()
        if not name:
            continue
        ops = re.findall(r"(==|>=|<=|~=|!=|<|>)\s*([0-9A-Za-z._-]+)", line)
        reqs.append((name, ops))
    return reqs


def vkey(version: str):
    main, _, _local = version.strip().partition("+")
    parts = []
    for seg in re.split(r"[._-]", main):
        if seg.isdigit():
            parts.append((1, int(seg)))
        else:
            m = re.match(r"(\d+)", seg)
            parts.append((1, int(m.group(1))) if m else (0, seg))
    return parts


def satisfies(installed: str, op: str, want: str) -> bool:
    if op == "==":
        return vkey(installed) == vkey(want)
    if op == "!=":
        return vkey(installed) != vkey(want)
    if op in (">", ">="):
        return vkey(installed) >= vkey(want) if op == ">=" else vkey(installed) > vkey(want)
    if op in ("<", "<="):
        return vkey(installed) <= vkey(want) if op == "<=" else vkey(installed) < vkey(want)
    return True  # ~= and unknown specifiers: presence is enough


def installed_dists() -> dict:
    out = {}
    try:
        for dist in metadata.distributions():
            name = (dist.metadata or {}).get("Name") if dist.metadata else None
            name = name or dist.name or ""
            if name and dist.version:
                out.setdefault(normalize_dist(name), dist.version)
    except Exception:
        pass
    return out


def module_dist_map() -> dict:
    try:
        return metadata.packages_distributions() or {}
    except Exception:
        return {}


def main() -> int:
    missing = []
    dists = installed_dists()

    req_path = ROOT / "requirements.txt"
    for name, ops in parse_requirements(req_path):
        installed = dists.get(normalize_dist(name))
        if installed is None:
            missing.append(f"{name}: not installed (required by requirements.txt)")
            continue
        for op, want in ops:
            if not satisfies(installed, op, want):
                missing.append(f"{name}: {installed} installed but requirement is '{op}{want}'")

    stdlib = stdlib_names()
    local = local_module_names()
    mod2dist = module_dist_map()

    third_party = set()
    for path in iter_py_files():
        for mod in top_level_imports(path):
            if mod in stdlib or mod in local or mod == "__main__":
                continue
            third_party.add(mod)

    for mod in sorted(third_party):
        mapped = mod2dist.get(mod) or []
        if any(normalize_dist(d) in dists for d in mapped):
            continue
        try:
            import_module(mod)
        except Exception as exc:
            missing.append(f"{mod}: import failed ({type(exc).__name__}: {exc})")

    if missing:
        print("[deps] Missing or broken dependencies detected:")
        for item in sorted(set(missing)):
            print(f"  - {item}")
        return 1
    print(f"[deps] All {len(dists)} installed distributions and "
          f"{len(third_party)} imported modules check out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
