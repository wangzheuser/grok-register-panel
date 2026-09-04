from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone

import pytest
import requests

from webui import grok2api_sync as sync


FUTURE = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, lines=None, line_error=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {"Content-Type": "application/json"}
        self.lines = list(lines or [])
        self.line_error = line_error
        self.closed = False

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload

    def iter_lines(self, **_kwargs):
        for line in self.lines:
            yield line
        if self.line_error:
            raise self.line_error

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


def login_response(token="access-1"):
    return FakeResponse(
        200,
        {
            "data": {
                "admin": {"id": "1", "username": "admin"},
                "tokens": {
                    "accessToken": token,
                    "accessTokenExpiresAt": FUTURE,
                    "refreshTokenExpiresAt": FUTURE,
                },
            }
        },
    )


def refresh_response(token="access-2"):
    return FakeResponse(
        200,
        {
            "data": {
                "accessToken": token,
                "accessTokenExpiresAt": FUTURE,
                "refreshTokenExpiresAt": FUTURE,
            }
        },
    )


def me_response():
    return FakeResponse(200, {"data": {"id": "1", "username": "admin"}})


def sse_response(payload, *, event="complete"):
    return FakeResponse(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        lines=[": connected", f"event: {event}", f"data: {json.dumps(payload)}", ""],
    )


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(sync, "CONFIG_PATH", path)
    monkeypatch.setattr(sync, "LOCK_PATH", tmp_path / "config.json.lock")
    return path


@pytest.fixture
def reset_runtime(monkeypatch):
    monkeypatch.setattr(sync, "_runtime_client", None)
    monkeypatch.setattr(sync, "_runtime_client_signature", None)


def test_config_defaults_validation_and_legacy_migration(isolated_config):
    isolated_config.write_text(
        json.dumps({"grok2api_sync_app_key": "legacy-secret"}), encoding="utf-8"
    )
    state = sync.read_sync_config()
    assert state["username"] == "admin"
    assert state["password_configured"] is False
    assert state["legacy_app_key_configured"] is True
    assert "legacy-secret" not in json.dumps(state)

    with pytest.raises(sync.Grok2APISyncConfigError):
        sync.save_sync_config(True, "", "admin", "password")
    with pytest.raises(sync.Grok2APISyncConfigError):
        sync.save_sync_config(True, "https://grok.example", "admin", "")

    state = sync.save_sync_config(True, "https://grok.example/", "operator", "password value")
    raw = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert state == {
        "ok": True,
        "enabled": True,
        "url": "https://grok.example",
        "username": "operator",
        "password_configured": True,
        "legacy_app_key_configured": False,
        "config_exists": True,
    }
    assert raw["grok2api_sync_password"] == "password value"
    assert "grok2api_sync_app_key" not in raw


def test_password_blank_preserves_and_explicit_clear_removes(isolated_config):
    sync.save_sync_config(False, "https://grok.example", "admin", "secret")
    state = sync.save_sync_config(False, "https://grok.example", "admin", "")
    assert state["password_configured"] is True
    state = sync.save_sync_config(False, "", "admin", "", clear_password=True)
    assert state["password_configured"] is False


def test_config_rejects_invalid_or_credentialed_url(isolated_config):
    for value in (
        "grok.example",
        "ftp://grok.example",
        "https://u:p@grok.example",
        "https://grok.example?a=1",
    ):
        with pytest.raises(sync.Grok2APISyncConfigError):
            sync.save_sync_config(False, value)


def test_login_verify_logout_contract_uses_direct_session(monkeypatch):
    session = FakeSession([login_response(), me_response(), FakeResponse(200, {"data": {"loggedOut": True}})])
    monkeypatch.setattr(sync.requests, "Session", lambda: session)
    result = sync.verify_connection("https://grok.example/", "admin", "password")
    assert result["ok"] is True
    assert session.trust_env is False
    assert [call[1] for call in session.calls] == [
        "https://grok.example/api/admin/v1/auth/login",
        "https://grok.example/api/admin/v1/me",
        "https://grok.example/api/admin/v1/auth/logout",
    ]
    assert session.calls[0][2]["json"] == {"username": "admin", "password": "password"}
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer access-1"
    assert session.closed is True


@pytest.mark.parametrize(
    ("status", "detail"),
    [(401, "用户名或密码错误"), (429, "登录请求过于频繁"), (500, "暂时不可用")],
)
def test_login_failure_statuses_are_specific(monkeypatch, status, detail):
    session = FakeSession([FakeResponse(status)])
    monkeypatch.setattr(sync.requests, "Session", lambda: session)
    result = sync.verify_connection("https://grok.example", "admin", "secret-password")
    assert result["ok"] is False
    assert detail in result["detail"]
    assert "secret" not in json.dumps(result)
    assert len(session.calls) == 1


def test_import_contract_and_successful_sse():
    response = sse_response({"created": 1, "updated": 0, "skipped": 0, "failed": 0, "synced": 1, "syncFailed": 0})
    session = FakeSession([login_response(), response])
    client = sync.Grok2APIAdminClient("https://grok.example", "admin", "password", session=session)
    result = client.import_web_account("session-token", "user@example.com")
    assert result["ok"] is True
    assert result["created"] == 1
    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url == "https://grok.example/api/admin/v1/accounts/web/import"
    assert kwargs["stream"] is True
    assert kwargs["headers"] == {"Authorization": "Bearer access-1", "Accept": "text/event-stream"}
    name, document, content_type = kwargs["files"]["files"]
    assert name == "grok-web-account.json"
    assert content_type == "application/json"
    assert json.loads(document) == {
        "provider": "grok_web",
        "accounts": [{"sso_token": "session-token", "email": "user@example.com"}],
    }
    assert response.closed is True


def test_runtime_client_reuses_login_for_multiple_accounts(monkeypatch, reset_runtime):
    session = FakeSession(
        [
            login_response(),
            sse_response({"created": 1, "failed": 0}),
            sse_response({"skipped": 1, "failed": 0}),
        ]
    )
    monkeypatch.setattr(sync.requests, "Session", lambda: session)
    first = sync.sync_sso("https://grok.example", "admin", "password", "sso=one; other=x", email="one@example.com")
    second = sync.sync_sso("https://grok.example", "admin", "password", "two")
    assert first["ok"] is True
    assert second["detail"] == "账号已存在，已跳过"
    assert [url for _, url, _ in session.calls].count("https://grok.example/api/admin/v1/auth/login") == 1
    first_document = json.loads(session.calls[1][2]["files"]["files"][1])
    assert first_document["accounts"][0]["sso_token"] == "one"


def test_expiring_access_token_refreshes_before_import():
    session = FakeSession(
        [
            login_response(),
            refresh_response(),
            sse_response({"updated": 1, "failed": 0}),
        ]
    )
    client = sync.Grok2APIAdminClient("https://grok.example", "admin", "password", session=session)
    client.login()
    client.access_expires_at = 0
    result = client.import_web_account("token")
    assert result["ok"] is True
    assert [url for _, url, _ in session.calls][1] == "https://grok.example/api/admin/v1/auth/refresh"
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer access-2"


def test_import_401_refreshes_and_retries_once():
    first_import = FakeResponse(401, {"error": {"code": "adminUnauthorized"}})
    session = FakeSession(
        [
            login_response(),
            first_import,
            refresh_response(),
            sse_response({"created": 1, "failed": 0}),
        ]
    )
    client = sync.Grok2APIAdminClient("https://grok.example", "admin", "password", session=session)
    result = client.import_web_account("token")
    assert result["ok"] is True
    assert first_import.closed is True
    assert [url for _, url, _ in session.calls].count("https://grok.example/api/admin/v1/accounts/web/import") == 2


def test_invalid_refresh_session_falls_back_to_login():
    session = FakeSession(
        [
            login_response("access-1"),
            FakeResponse(401),
            FakeResponse(401),
            login_response("access-2"),
            sse_response({"created": 1, "failed": 0}),
        ]
    )
    client = sync.Grok2APIAdminClient("https://grok.example", "admin", "password", session=session)
    result = client.import_web_account("token")
    assert result["ok"] is True
    urls = [url for _, url, _ in session.calls]
    assert urls.count("https://grok.example/api/admin/v1/auth/login") == 2
    assert urls.count("https://grok.example/api/admin/v1/auth/refresh") == 1


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"skipped": 1, "failed": 0}, "账号已存在，已跳过"),
        ({"created": 1, "failed": 0, "syncFailed": 1}, "账号已入库，但初始化同步失败"),
    ],
)
def test_import_success_outcomes(payload, detail):
    response = sse_response(payload)
    assert sync._parse_import_stream(response, started_at=time.monotonic())["detail"] == detail


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (sse_response({"created": 0, "skipped": 0, "failed": 1}), "未接受"),
        (sse_response({"code": "authImportFailed"}, event="error"), "authImportFailed"),
        (FakeResponse(200, headers={"Content-Type": "text/event-stream"}, lines=["event: complete", "data: not-json", ""]), "无效 JSON"),
        (FakeResponse(200, headers={"Content-Type": "text/event-stream"}, lines=[]), "完成事件前结束"),
    ],
)
def test_sse_failure_outcomes(response, detail):
    with pytest.raises(Exception) as caught:
        sync._parse_import_stream(response, started_at=time.monotonic())
    assert detail in str(caught.value)


def test_sse_idle_and_total_timeout():
    idle = FakeResponse(
        200,
        headers={"Content-Type": "text/event-stream"},
        line_error=requests.ReadTimeout("secret response"),
    )
    with pytest.raises(Exception, match="长时间没有新数据"):
        sync._parse_import_stream(idle, started_at=time.monotonic())
    total = FakeResponse(200, headers={"Content-Type": "text/event-stream"}, lines=[": heartbeat"])
    with pytest.raises(Exception, match="超过总时限"):
        sync._parse_import_stream(total, started_at=time.monotonic() - 121)


def test_non_sse_and_network_failures_are_redacted(monkeypatch, reset_runtime):
    session = FakeSession([login_response(), FakeResponse(200, {"data": {}}, headers={"Content-Type": "application/json"})])
    monkeypatch.setattr(sync.requests, "Session", lambda: session)
    result = sync.sync_sso("https://grok.example", "admin", "secret-password", "secret-sso")
    assert result["ok"] is False
    assert "非 SSE" in result["detail"]
    assert "secret" not in json.dumps(result)

    monkeypatch.setattr(sync, "_runtime_client", None)
    monkeypatch.setattr(sync, "_runtime_client_signature", None)
    monkeypatch.setattr(sync.requests, "Session", lambda: FakeSession([requests.Timeout("secret")]))
    result = sync.sync_sso("https://grok.example", "admin", "secret-password", "secret-sso")
    assert result == {"ok": False, "status_code": None, "detail": "Grok2API 请求超时"}


def test_test_config_uses_stored_credentials(isolated_config, monkeypatch):
    sync.save_sync_config(True, "https://grok.example", "operator", "stored password")
    captured = []
    monkeypatch.setattr(
        sync,
        "verify_connection",
        lambda url, username, password: captured.append((url, username, password)) or {"ok": True},
    )
    assert sync.test_sync_config("", "", "") == {"ok": True}
    assert captured == [("https://grok.example", "operator", "stored password")]


def test_enqueue_is_disabled_or_runs_in_single_background_worker(monkeypatch):
    assert sync.enqueue_sync({}, "token") is None
    logs = []

    def fake_sync(_url, _username, _password, _sso, *, email=""):
        time.sleep(0.02)
        assert email == "user@example.com"
        return {"ok": False, "detail": "Grok2API 网络请求失败"}

    monkeypatch.setattr(sync, "sync_sso", fake_sync)
    future = sync.enqueue_sync(
        {
            "grok2api_sync_enabled": True,
            "grok2api_sync_url": "https://grok.example",
            "grok2api_sync_username": "admin",
            "grok2api_sync_password": "password",
        },
        "token",
        email="user@example.com",
        log_callback=logs.append,
    )
    assert future is not None
    assert future.result(timeout=2)["ok"] is False
    assert logs == ["[Grok2API] [!] Grok2API 网络请求失败: user@example.com"]
    assert sync._get_executor()._max_workers == 1


def test_real_http_session_login_multipart_and_sse(reset_runtime):
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.path == "/api/admin/v1/auth/login":
                captured["login"] = json.loads(body)
                payload = json.dumps({
                    "data": {
                        "admin": {"id": "1", "username": "admin"},
                        "tokens": {
                            "accessToken": "real-access",
                            "accessTokenExpiresAt": FUTURE,
                            "refreshTokenExpiresAt": FUTURE,
                        },
                    }
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "grok2api_admin_refresh=refresh; Path=/api/admin/v1/auth; HttpOnly")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/api/admin/v1/accounts/web/import":
                captured["authorization"] = self.headers.get("Authorization")
                captured["content_type"] = self.headers.get("Content-Type")
                captured["body"] = body
                payload = b'event: complete\ndata: {"created":1,"updated":0,"skipped":0,"failed":0,"synced":1,"syncFailed":0}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = sync.sync_sso(
            f"http://127.0.0.1:{server.server_port}",
            "admin",
            "password",
            "sso=real-token",
            email="real@example.com",
        )
        assert result["ok"] is True
        assert captured["login"] == {"username": "admin", "password": "password"}
        assert captured["authorization"] == "Bearer real-access"
        assert captured["content_type"].startswith("multipart/form-data; boundary=")
        assert b'name="files"; filename="grok-web-account.json"' in captured["body"]
        assert b'"sso_token":"real-token"' in captured["body"]
        assert b'"email":"real@example.com"' in captured["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
