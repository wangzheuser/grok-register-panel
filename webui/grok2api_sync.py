"""Go Grok2API admin-session config and best-effort Web-account delivery."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

import requests

from secure_files import atomic_write_json, exclusive_file_lock


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(
    os.environ.get("GROK2API_SYNC_CONFIG_FILE", str(ROOT / "config.json"))
)
LOCK_PATH = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".lock")
CONNECT_TIMEOUT = 10
STREAM_IDLE_TIMEOUT = 60
IMPORT_TOTAL_TIMEOUT = 120
ACCESS_REFRESH_SKEW = 30

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_runtime_client: "Grok2APIAdminClient | None" = None
_runtime_client_signature: tuple[str, str, str] | None = None
_runtime_client_lock = threading.Lock()


class Grok2APISyncConfigError(ValueError):
    """Raised when cloud-sync configuration is incomplete or invalid."""


class _ClientError(RuntimeError):
    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _normalize_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise Grok2APISyncConfigError("Grok2API 服务地址必须是有效的 HTTP/HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Grok2APISyncConfigError("Grok2API 服务地址不能包含凭据、查询参数或片段")
    return url


def _normalize_sso(value: object) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("sso="):
        token = token[4:]
    token = token.split(";", 1)[0]
    return token.replace("\r", "").replace("\n", "").replace("\x00", "").strip()


def _read_unlocked() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise Grok2APISyncConfigError("config.json 读取失败") from exc
    if not isinstance(value, dict):
        raise Grok2APISyncConfigError("config.json 顶层必须是对象")
    return value


def _public_state(raw: Mapping[str, object]) -> dict:
    return {
        "ok": True,
        "enabled": bool(raw.get("grok2api_sync_enabled", False)),
        "url": str(raw.get("grok2api_sync_url", "") or "").strip(),
        "username": str(raw.get("grok2api_sync_username", "admin") or "admin").strip() or "admin",
        "password_configured": bool(str(raw.get("grok2api_sync_password", "") or "")),
        "legacy_app_key_configured": bool(str(raw.get("grok2api_sync_app_key", "") or "").strip()),
        "config_exists": CONFIG_PATH.exists(),
    }


def read_sync_config() -> dict:
    with exclusive_file_lock(LOCK_PATH):
        return _public_state(_read_unlocked())


def save_sync_config(
    enabled: object,
    url: object,
    username: object = "admin",
    password: object = "",
    *,
    clear_password: bool = False,
) -> dict:
    normalized_url = _normalize_url(url)
    normalized_username = str(username or "").strip() or "admin"
    with exclusive_file_lock(LOCK_PATH):
        raw = _read_unlocked()
        current_password = str(raw.get("grok2api_sync_password", "") or "")
        supplied_password = str(password or "")
        effective_password = "" if clear_password else (supplied_password or current_password)
        is_enabled = bool(enabled)
        if is_enabled and not normalized_url:
            raise Grok2APISyncConfigError("启用云同步时必须填写 Grok2API 服务地址")
        if is_enabled and not normalized_username:
            raise Grok2APISyncConfigError("启用云同步时必须填写 Grok2API 管理员用户名")
        if is_enabled and not effective_password:
            raise Grok2APISyncConfigError("启用云同步时必须填写 Grok2API 管理员密码")
        updated = dict(raw)
        updated.pop("grok2api_sync_app_key", None)
        updated.update(
            {
                "grok2api_sync_enabled": is_enabled,
                "grok2api_sync_url": normalized_url,
                "grok2api_sync_username": normalized_username,
                "grok2api_sync_password": effective_password,
            }
        )
        atomic_write_json(CONFIG_PATH, updated)
        return _public_state(updated)


def _parse_expiry(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        raise _ClientError("Grok2API 登录响应缺少访问令牌有效期")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _ClientError("Grok2API 登录响应中的访问令牌有效期无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _response_data(response) -> dict:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise _ClientError("Grok2API 返回无法识别的 JSON 响应", response.status_code) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise _ClientError("Grok2API 返回无法识别的响应结构", response.status_code)
    return payload["data"]


def _http_error(status_code: int, *, login: bool = False) -> _ClientError:
    if status_code == 401:
        detail = "管理员用户名或密码错误" if login else "管理员会话已失效"
    elif status_code == 404:
        detail = "接口不存在，请检查服务根地址和 Grok2API 版本"
    elif status_code == 429:
        detail = "管理员登录请求过于频繁，请稍后重试"
    elif status_code >= 500:
        detail = f"Grok2API 服务暂时不可用（HTTP {status_code}）"
    else:
        detail = f"Grok2API 请求失败（HTTP {status_code}）"
    return _ClientError(detail, status_code)


def _result_from_error(exc: BaseException) -> dict:
    if isinstance(exc, _ClientError):
        return {"ok": False, "status_code": exc.status_code, "detail": exc.detail}
    if isinstance(exc, requests.Timeout):
        detail = "Grok2API 请求超时"
    elif isinstance(exc, requests.RequestException):
        detail = "Grok2API 网络请求失败"
    else:
        detail = "Grok2API 同步失败"
    return {"ok": False, "status_code": None, "detail": detail}


class Grok2APIAdminClient:
    """A single-session client for the Go Grok2API admin API."""

    def __init__(self, url: str, username: str, password: str, *, session=None):
        self.url = _normalize_url(url)
        self.username = str(username or "").strip() or "admin"
        self.password = str(password or "")
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.access_token = ""
        self.access_expires_at = 0.0
        self.has_refresh_session = False

    def _request(self, method: str, path: str, **kwargs):
        return self.session.request(
            method,
            f"{self.url}{path}",
            timeout=kwargs.pop("timeout", (CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT)),
            **kwargs,
        )

    def _apply_tokens(self, data: Mapping[str, object]) -> None:
        token = str(data.get("accessToken", "") or "").strip()
        if not token:
            raise _ClientError("Grok2API 登录响应缺少访问令牌")
        self.access_token = token
        self.access_expires_at = _parse_expiry(data.get("accessTokenExpiresAt"))
        self.has_refresh_session = True

    def login(self) -> None:
        response = self._request(
            "POST",
            "/api/admin/v1/auth/login",
            json={"username": self.username, "password": self.password},
        )
        if response.status_code != 200:
            raise _http_error(response.status_code, login=True)
        data = _response_data(response)
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            raise _ClientError("Grok2API 登录响应缺少令牌数据", response.status_code)
        self._apply_tokens(tokens)

    def refresh(self) -> bool:
        response = self._request("POST", "/api/admin/v1/auth/refresh", json={})
        if response.status_code == 401:
            self.access_token = ""
            self.access_expires_at = 0.0
            self.has_refresh_session = False
            return False
        if response.status_code != 200:
            raise _http_error(response.status_code)
        self._apply_tokens(_response_data(response))
        return True

    def ensure_access(self, *, force_recover: bool = False) -> None:
        now = time.time()
        if not force_recover and self.access_token and self.access_expires_at > now + ACCESS_REFRESH_SKEW:
            return
        if self.has_refresh_session and self.refresh():
            return
        self.login()

    def _auth_headers(self, **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", **headers}

    def verify(self) -> dict:
        self.ensure_access()
        response = self._request("GET", "/api/admin/v1/me", headers=self._auth_headers(Accept="application/json"))
        if response.status_code == 401:
            self.ensure_access(force_recover=True)
            response = self._request("GET", "/api/admin/v1/me", headers=self._auth_headers(Accept="application/json"))
        if response.status_code != 200:
            raise _http_error(response.status_code)
        admin = _response_data(response)
        if not str(admin.get("username", "") or "").strip():
            raise _ClientError("Grok2API 管理员信息响应无效", response.status_code)
        return {"ok": True, "status_code": 200, "detail": "Grok2API 连接和管理员鉴权正常"}

    def logout(self) -> None:
        if self.has_refresh_session:
            try:
                self._request("POST", "/api/admin/v1/auth/logout", json={})
            except requests.RequestException:
                pass
        self.access_token = ""
        self.access_expires_at = 0.0
        self.has_refresh_session = False

    def close(self, *, logout: bool = False) -> None:
        if logout:
            self.logout()
        self.session.close()

    def import_web_account(self, sso: str, email: str = "") -> dict:
        self.ensure_access()
        for attempt in range(2):
            response = self._import_request(sso, email)
            if response.status_code != 401 or attempt > 0:
                break
            response.close()
            self.ensure_access(force_recover=True)
        if response.status_code != 200:
            try:
                raise _http_error(response.status_code)
            finally:
                response.close()
        try:
            content_type = str(response.headers.get("Content-Type", "") or "").lower()
            if not content_type.startswith("text/event-stream"):
                raise _ClientError("Grok2API 导入接口返回了非 SSE 响应", response.status_code)
            return _parse_import_stream(response, started_at=time.monotonic())
        finally:
            response.close()

    def _import_request(self, sso: str, email: str):
        account = {"sso_token": sso}
        if email:
            account["email"] = email
        document = json.dumps(
            {"provider": "grok_web", "accounts": [account]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._request(
            "POST",
            "/api/admin/v1/accounts/web/import",
            headers=self._auth_headers(Accept="text/event-stream"),
            files={"files": ("grok-web-account.json", document, "application/json")},
            stream=True,
        )


def _parse_import_stream(response, *, started_at: float) -> dict:
    event = "message"
    data_lines: list[str] = []

    def dispatch() -> dict | None:
        nonlocal event, data_lines
        current_event, current_data = event, data_lines
        event, data_lines = "message", []
        if not current_data or current_event not in {"complete", "error"}:
            return None
        try:
            payload = json.loads("\n".join(current_data))
        except json.JSONDecodeError as exc:
            raise _ClientError("Grok2API SSE 返回了无效 JSON", response.status_code) from exc
        if not isinstance(payload, dict):
            raise _ClientError("Grok2API SSE 返回了无效结果", response.status_code)
        if current_event == "error":
            code = str(payload.get("code", "") or "").strip()
            detail = f"Grok2API 导入失败（{code}）" if code else "Grok2API 导入失败"
            raise _ClientError(detail, response.status_code)
        created = int(payload.get("created", 0) or 0)
        updated = int(payload.get("updated", 0) or 0)
        skipped = int(payload.get("skipped", 0) or 0)
        failed = int(payload.get("failed", 0) or 0)
        synced = int(payload.get("synced", 0) or 0)
        sync_failed = int(payload.get("syncFailed", 0) or 0)
        accepted = created + updated + skipped
        if failed > 0 or accepted < 1:
            raise _ClientError("Grok2API 未接受当前账号", response.status_code)
        if sync_failed > 0:
            detail = "账号已入库，但初始化同步失败"
        elif skipped > 0 and created == 0 and updated == 0:
            detail = "账号已存在，已跳过"
        else:
            detail = "同步成功"
        return {
            "ok": True,
            "status_code": response.status_code,
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "synced": synced,
            "sync_failed": sync_failed,
            "detail": detail,
        }

    try:
        for raw_line in response.iter_lines(chunk_size=1, decode_unicode=True):
            if time.monotonic() - started_at > IMPORT_TOTAL_TIMEOUT:
                raise _ClientError("Grok2API 导入超过总时限", response.status_code)
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
            line = line.rstrip("\r")
            if not line:
                result = dispatch()
                if result is not None:
                    return result
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        result = dispatch()
        if result is not None:
            return result
    except requests.Timeout as exc:
        raise _ClientError("Grok2API SSE 长时间没有新数据", response.status_code) from exc
    raise _ClientError("Grok2API SSE 在完成事件前结束", response.status_code)


def verify_connection(url: object, username: object, password: object) -> dict:
    normalized_url = _normalize_url(url)
    normalized_username = str(username or "").strip() or "admin"
    normalized_password = str(password or "")
    if not normalized_url:
        raise Grok2APISyncConfigError("请填写 Grok2API 服务地址")
    if not normalized_password:
        raise Grok2APISyncConfigError("请填写 Grok2API 管理员密码")
    client = Grok2APIAdminClient(normalized_url, normalized_username, normalized_password)
    try:
        return client.verify()
    except Exception as exc:
        return _result_from_error(exc)
    finally:
        client.close(logout=True)


def test_sync_config(
    url: object = "",
    username: object = "",
    password: object = "",
    *,
    clear_password: bool = False,
) -> dict:
    with exclusive_file_lock(LOCK_PATH):
        raw = _read_unlocked()
    candidate_url = str(url or "").strip() or str(raw.get("grok2api_sync_url", "") or "").strip()
    candidate_username = str(username or "").strip() or str(raw.get("grok2api_sync_username", "admin") or "admin").strip() or "admin"
    stored_password = str(raw.get("grok2api_sync_password", "") or "")
    candidate_password = "" if clear_password else (str(password or "") or stored_password)
    return verify_connection(candidate_url, candidate_username, candidate_password)


def _get_runtime_client(url: str, username: str, password: str) -> Grok2APIAdminClient:
    global _runtime_client, _runtime_client_signature
    signature = (url, username, password)
    with _runtime_client_lock:
        if _runtime_client is None or _runtime_client_signature != signature:
            if _runtime_client is not None:
                _runtime_client.close()
            _runtime_client = Grok2APIAdminClient(url, username, password)
            _runtime_client_signature = signature
        return _runtime_client


def sync_sso(
    url: object,
    username: object,
    password: object,
    raw_sso: object,
    *,
    email: str = "",
) -> dict:
    try:
        normalized_url = _normalize_url(url)
        normalized_username = str(username or "").strip() or "admin"
        normalized_password = str(password or "")
        sso = _normalize_sso(raw_sso)
        if not normalized_url or not normalized_password or not sso:
            return {"ok": False, "status_code": None, "detail": "云同步配置或 SSO 不完整"}
        client = _get_runtime_client(normalized_url, normalized_username, normalized_password)
        return client.import_web_account(sso, str(email or "").strip())
    except Exception as exc:
        return _result_from_error(exc)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grok2api-sync")
        return _executor


def enqueue_sync(
    config: Mapping[str, object],
    raw_sso: object,
    *,
    email: str = "",
    log_callback: Callable[[str], None] | None = None,
) -> Future | None:
    if not bool(config.get("grok2api_sync_enabled", False)):
        return None
    url = str(config.get("grok2api_sync_url", "") or "").strip()
    username = str(config.get("grok2api_sync_username", "admin") or "admin").strip() or "admin"
    password = str(config.get("grok2api_sync_password", "") or "")
    sso = _normalize_sso(raw_sso)
    if not url or not password or not sso:
        if log_callback:
            log_callback("[Grok2API] 云同步已启用，但 URL、管理员密码或 SSO 不完整")
        return None

    def _job() -> dict:
        result = sync_sso(url, username, password, sso, email=email)
        if log_callback:
            target = f": {email}" if email else ""
            marker = "+" if result.get("ok") else "!"
            try:
                log_callback(f"[Grok2API] [{marker}] {result.get('detail', '同步失败')}{target}")
            except Exception:
                pass
        return result

    return _get_executor().submit(_job)


__all__ = [
    "Grok2APIAdminClient",
    "Grok2APISyncConfigError",
    "enqueue_sync",
    "read_sync_config",
    "save_sync_config",
    "sync_sso",
    "test_sync_config",
    "verify_connection",
]