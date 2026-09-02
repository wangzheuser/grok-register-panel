from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

from webui import grok2api_sync
from webui import monitor


def request(url: str, *, token: str = "", method: str = "GET", payload=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=5)
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_monitor_grok2api_sync_config_auth_and_redaction(tmp_path):
    token = "monitor-test-token"
    secret = "grok2api-admin-secret"
    old_token = os.environ.get("MONITOR_TOKEN")
    old_paths = grok2api_sync.CONFIG_PATH, grok2api_sync.LOCK_PATH
    grok2api_sync.CONFIG_PATH = tmp_path / "config.json"
    grok2api_sync.LOCK_PATH = tmp_path / "config.json.lock"
    os.environ["MONITOR_TOKEN"] = token
    server = monitor.ThreadingHTTPServer(("127.0.0.1", 0), monitor.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert request(base + "/api/grok2api-sync")[0] == 401
        status, body = request(
            base + "/api/grok2api-sync",
            token=token,
            method="POST",
            payload={
                "enabled": True,
                "url": "https://grok.example/",
                "app_key": secret,
            },
        )
        assert status == 200
        assert secret.encode() not in body
        saved = json.loads(body)
        assert saved["enabled"] is True
        assert saved["url"] == "https://grok.example"
        assert saved["app_key_configured"] is True

        status, body = request(base + "/api/grok2api-sync", token=token)
        assert status == 200
        assert secret.encode() not in body
        assert json.loads(body)["app_key_configured"] is True

        status, body = request(
            base + "/api/grok2api-sync",
            token=token,
            method="POST",
            payload={"enabled": False, "url": "https://grok.example", "app_key": ""},
        )
        assert status == 200
        assert json.loads(body)["app_key_configured"] is True

        status, body = request(
            base + "/api/grok2api-sync",
            token=token,
            method="POST",
            payload={"enabled": False, "url": "", "clear_app_key": True},
        )
        assert status == 200
        assert json.loads(body)["app_key_configured"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        grok2api_sync.CONFIG_PATH, grok2api_sync.LOCK_PATH = old_paths
        if old_token is None:
            os.environ.pop("MONITOR_TOKEN", None)
        else:
            os.environ["MONITOR_TOKEN"] = old_token


def test_monitor_grok2api_connection_test_is_protected_and_redacted(monkeypatch):
    token = "monitor-test-token"
    secret = "grok2api-admin-secret"
    old_token = os.environ.get("MONITOR_TOKEN")
    os.environ["MONITOR_TOKEN"] = token
    calls = []

    def fake_test(url, app_key, *, clear_app_key=False):
        calls.append((url, app_key, clear_app_key))
        return {"ok": True, "status_code": 200, "detail": "连接正常"}

    monkeypatch.setattr(monitor, "test_grok2api_sync_config", fake_test)
    server = monitor.ThreadingHTTPServer(("127.0.0.1", 0), monitor.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = {"url": "https://grok.example", "app_key": secret}
        assert request(base + "/api/grok2api-sync/test", method="POST", payload=payload)[0] == 401
        status, body = request(
            base + "/api/grok2api-sync/test",
            token=token,
            method="POST",
            payload=payload,
        )
        assert status == 200
        assert secret.encode() not in body
        assert calls == [("https://grok.example", secret, False)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if old_token is None:
            os.environ.pop("MONITOR_TOKEN", None)
        else:
            os.environ["MONITOR_TOKEN"] = old_token

