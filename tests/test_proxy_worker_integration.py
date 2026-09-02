# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grok_register_ttk as register
from webui import proxy_store


def test_worker_hot_reload_only_changes_next_account_proxy():
    previous_paths = (
        proxy_store.STATE_PATH,
        proxy_store.LOCK_PATH,
        proxy_store.LEGACY_PATH,
        proxy_store.CONFIG_PATH,
    )
    previous_proxy = register.config.get("proxy")
    previous_workers = register.config.get("register_workers")
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        try:
            proxy_store.STATE_PATH = base / "log" / "proxy_pool.json"
            proxy_store.LOCK_PATH = base / "log" / "proxy_pool.json.lock"
            proxy_store.LEGACY_PATH = base / "proxies.txt"
            proxy_store.CONFIG_PATH = base / "config.json"
            register.config["proxy"] = "http://legacy.example:7890"
            proxy_store.CONFIG_PATH.write_text(
                json.dumps({"proxy": register.config["proxy"]}), encoding="utf-8"
            )
            register.config["register_workers"] = 2

            assert register.load_proxy_pool() == ["http://legacy.example:7890"]

            imported = proxy_store.import_proxies(
                "a.example:8000:user:pass\nb.example:8001:user:pass"
            )
            assert register.load_proxy_pool() == []
            try:
                register.pick_proxy_for_worker(0, 0)
            except RuntimeError as exc:
                assert "没有健康且启用的代理" in str(exc)
            else:
                raise AssertionError("unknown managed proxies must not reach workers")

            for offset, item in enumerate(imported["items"]):
                proxy_store._apply_probe_result(
                    item["id"],
                    {
                        "ok": True,
                        "exit_ip": f"198.51.100.{10 + offset}",
                        "asn": 64510 + offset,
                        "asn_org": "Worker Test",
                        "latency_ms": 100 + offset,
                        "checked_at": "2026-07-30T00:00:00Z",
                    },
                )

            current = register.pick_proxy_for_worker(0, 0)
            register.set_thread_proxy(current)
            assert "a.example:8000" in current
            proxy_store.record_proxy_result(current, "risk", "policy deny")

            # State changes do not mutate the current account's bound proxy.
            assert register.get_thread_proxy() == current
            next_account = register.pick_proxy_for_worker(0, 1)
            assert next_account != current
            assert "b.example:8001" in next_account
        finally:
            (
                proxy_store.STATE_PATH,
                proxy_store.LOCK_PATH,
                proxy_store.LEGACY_PATH,
                proxy_store.CONFIG_PATH,
            ) = previous_paths
            register.config["proxy"] = previous_proxy
            register.config["register_workers"] = previous_workers


def test_explicit_proxy_modes_and_resin_account_stickiness():
    previous_paths = (
        proxy_store.STATE_PATH,
        proxy_store.LOCK_PATH,
        proxy_store.LEGACY_PATH,
        proxy_store.CONFIG_PATH,
    )
    previous_proxy = register.config.get("proxy")
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        try:
            proxy_store.STATE_PATH = base / "log" / "proxy_pool.json"
            proxy_store.LOCK_PATH = base / "log" / "proxy_pool.json.lock"
            proxy_store.LEGACY_PATH = base / "proxies.txt"
            proxy_store.CONFIG_PATH = base / "config.json"
            register.config["proxy"] = "http://legacy.example:7890"
            proxy_store.CONFIG_PATH.write_text(
                json.dumps({"proxy": register.config["proxy"]}), encoding="utf-8"
            )

            proxy_store.save_proxy_config("direct")
            assert register.load_proxy_pool() == []
            assert register.pick_proxy_for_worker(0, 0) == ""
            register.set_thread_proxy("")
            assert register.get_proxies() == {}

            imported = proxy_store.import_proxies("static.example:8000:user:pass")
            proxy_store._apply_probe_result(
                imported["imported_ids"][0],
                {
                    "ok": True,
                    "exit_ip": "198.51.100.20",
                    "asn": 64520,
                    "asn_org": "Static",
                    "latency_ms": 80,
                    "checked_at": "2026-07-30T00:00:00Z",
                },
            )
            template = "http://temp.{uuid}:pass@127.0.0.1:9200"
            proxy_store.save_proxy_config("resin", resin_template=template)
            assert register.load_proxy_pool() == [template]
            first = register.pick_proxy_for_worker(0, 0)
            second = register.pick_proxy_for_worker(0, 1)
            assert first != second
            assert "{uuid}" not in first
            assert "static.example" not in first

            register.set_thread_proxy(first)
            assert register.get_proxies() == {"http": first, "https": first}
            assert register.get_proxies()["https"] == first
        finally:
            (
                proxy_store.STATE_PATH,
                proxy_store.LOCK_PATH,
                proxy_store.LEGACY_PATH,
                proxy_store.CONFIG_PATH,
            ) = previous_paths
            register.config["proxy"] = previous_proxy
            for name in ("proxy", "proxy_assigned"):
                if hasattr(register._proxy_tls, name):
                    delattr(register._proxy_tls, name)


if __name__ == "__main__":
    test_worker_hot_reload_only_changes_next_account_proxy()
    test_explicit_proxy_modes_and_resin_account_stickiness()
    print("OK proxy worker integration")
