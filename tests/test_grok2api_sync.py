from __future__ import annotations

import json
import time

import pytest
import requests

from webui import grok2api_sync as sync


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, invalid_json=False):
        self.status_code = status_code
        self.payload = payload
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("invalid json")
        return self.payload


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(sync, "CONFIG_PATH", path)
    monkeypatch.setattr(sync, "LOCK_PATH", tmp_path / "config.json.lock")
    return path


def test_config_defaults_validation_and_secret_lifecycle(isolated_config):
    assert sync.read_sync_config() == {
        "ok": True,
        "enabled": False,
        "url": "",
        "app_key_configured": False,
        "config_exists": False,
    }
    with pytest.raises(sync.Grok2APISyncConfigError):
        sync.save_sync_config(True, "", "secret")
    with pytest.raises(sync.Grok2APISyncConfigError):
        sync.save_sync_config(True, "https://grok.example", "")

    state = sync.save_sync_config(True, "https://grok.example/", "secret-value")
    assert state["enabled"] is True
    assert state["url"] == "https://grok.example"
    assert state["app_key_configured"] is True
    assert "secret-value" not in json.dumps(state)

    state = sync.save_sync_config(False, "https://grok.example", "")
    assert state["app_key_configured"] is True
    assert json.loads(isolated_config.read_text(encoding="utf-8"))["grok2api_sync_app_key"] == "secret-value"

    state = sync.save_sync_config(False, "", "", clear_app_key=True)
    assert state["app_key_configured"] is False
    assert json.loads(isolated_config.read_text(encoding="utf-8"))["grok2api_sync_app_key"] == ""


def test_config_rejects_invalid_or_credentialed_url(isolated_config):
    for value in ("grok.example", "ftp://grok.example", "https://u:p@grok.example", "https://grok.example?a=1"):
        with pytest.raises(sync.Grok2APISyncConfigError):
            sync.save_sync_config(False, value)


def test_sync_request_contract_uses_direct_session(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self):
            self.trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def request(self, method, url, **kwargs):
            calls.append((self.trust_env, method, url, kwargs))
            return FakeResponse(200, {"status": "success", "count": 1, "skipped": 0})

    monkeypatch.setattr(sync.requests, "Session", FakeSession)
    result = sync.sync_sso("https://grok.example/", "admin-key", "sso=session-token")
    assert result["ok"] is True
    trust_env, method, url, kwargs = calls[0]
    assert trust_env is False
    assert method == "POST"
    assert url == "https://grok.example/admin/api/tokens/add"
    assert kwargs["headers"] == {"Authorization": "Bearer admin-key"}
    assert kwargs["timeout"] == 10
    assert kwargs["json"] == {"tokens": ["session-token"]}


def test_verify_connection_contract(monkeypatch):
    calls = []

    def fake_request(method, url, key, **kwargs):
        calls.append((method, url, key, kwargs))
        return FakeResponse(200, {"status": "success"})

    monkeypatch.setattr(sync, "_direct_request", fake_request)
    result = sync.verify_connection("https://grok.example", "admin-key")
    assert result["ok"] is True
    assert calls == [("GET", "https://grok.example/admin/api/verify", "admin-key", {})]


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (FakeResponse(200, {"status": "success", "count": 0, "skipped": 1}), "账号已存在"),
        (FakeResponse(401, {}), "鉴权失败"),
        (FakeResponse(404, {}), "接口不存在"),
        (FakeResponse(500, {}), "暂时不可用"),
        (FakeResponse(200, None, invalid_json=True), "无法识别"),
    ],
)
def test_sync_response_outcomes(monkeypatch, response, detail):
    monkeypatch.setattr(sync, "_direct_request", lambda *_args, **_kwargs: response)
    result = sync.sync_sso("https://grok.example", "key", "token")
    assert detail in result["detail"]


def test_sync_network_failure_does_not_expose_credentials(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.Timeout("request timed out for secret-token")

    monkeypatch.setattr(sync, "_direct_request", fail)
    result = sync.sync_sso("https://grok.example", "admin-key", "secret-token")
    assert result == {
        "ok": False,
        "status_code": None,
        "detail": "网络请求失败或超时",
    }
    assert "secret" not in json.dumps(result)


def test_test_config_uses_stored_key(isolated_config, monkeypatch):
    sync.save_sync_config(True, "https://grok.example", "stored-key")
    captured = []
    monkeypatch.setattr(
        sync,
        "verify_connection",
        lambda url, key: captured.append((url, key)) or {"ok": True},
    )
    assert sync.test_sync_config("", "") == {"ok": True}
    assert captured == [("https://grok.example", "stored-key")]


def test_enqueue_is_disabled_or_runs_in_background(monkeypatch):
    assert sync.enqueue_sync({}, "token") is None
    logs = []

    def fake_sync(_url, _key, _sso):
        time.sleep(0.05)
        return {"ok": False, "detail": "网络请求失败或超时"}

    monkeypatch.setattr(sync, "sync_sso", fake_sync)
    future = sync.enqueue_sync(
        {
            "grok2api_sync_enabled": True,
            "grok2api_sync_url": "https://grok.example",
            "grok2api_sync_app_key": "key",
        },
        "token",
        email="user@example.com",
        log_callback=logs.append,
    )
    assert future is not None
    assert future.result(timeout=2)["ok"] is False
    assert logs == ["[Grok2API] [!] 网络请求失败或超时: user@example.com"]

