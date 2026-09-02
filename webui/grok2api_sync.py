"""Grok2API cloud-sync config and best-effort SSO delivery."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
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
REQUEST_TIMEOUT = 10

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


class Grok2APISyncConfigError(ValueError):
    """Raised when cloud-sync configuration is incomplete or invalid."""


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
    if token.startswith("sso="):
        token = token[4:]
    return token.strip()


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
    url = str(raw.get("grok2api_sync_url", "") or "").strip()
    return {
        "ok": True,
        "enabled": bool(raw.get("grok2api_sync_enabled", False)),
        "url": url,
        "app_key_configured": bool(
            str(raw.get("grok2api_sync_app_key", "") or "").strip()
        ),
        "config_exists": CONFIG_PATH.exists(),
    }


def read_sync_config() -> dict:
    with exclusive_file_lock(LOCK_PATH):
        return _public_state(_read_unlocked())


def save_sync_config(
    enabled: object,
    url: object,
    app_key: object = "",
    *,
    clear_app_key: bool = False,
) -> dict:
    normalized_url = _normalize_url(url)
    with exclusive_file_lock(LOCK_PATH):
        raw = _read_unlocked()
        current_key = str(raw.get("grok2api_sync_app_key", "") or "").strip()
        supplied_key = str(app_key or "").strip()
        effective_key = "" if clear_app_key else (supplied_key or current_key)
        is_enabled = bool(enabled)
        if is_enabled and not normalized_url:
            raise Grok2APISyncConfigError("启用云同步时必须填写 Grok2API 服务地址")
        if is_enabled and not effective_key:
            raise Grok2APISyncConfigError("启用云同步时必须填写 Grok2API app_key")
        updated = dict(raw)
        updated.update(
            {
                "grok2api_sync_enabled": is_enabled,
                "grok2api_sync_url": normalized_url,
                "grok2api_sync_app_key": effective_key,
            }
        )
        atomic_write_json(CONFIG_PATH, updated)
        return _public_state(updated)


def _direct_request(method: str, url: str, app_key: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {app_key}"
    with requests.Session() as session:
        session.trust_env = False
        return session.request(
            method,
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )


def _response_payload(response) -> dict | None:
    try:
        value = response.json()
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _failure(status_code: int | None) -> dict:
    if status_code == 200:
        detail = "服务返回无法识别的响应"
    elif status_code == 401:
        detail = "鉴权失败，请检查 app_key"
    elif status_code == 404:
        detail = "接口不存在，请检查服务根地址和 Grok2API 版本"
    elif status_code is not None and status_code >= 500:
        detail = f"服务暂时不可用（HTTP {status_code}）"
    elif status_code is not None:
        detail = f"同步请求失败（HTTP {status_code}）"
    else:
        detail = "网络请求失败或超时"
    return {"ok": False, "status_code": status_code, "detail": detail}


def verify_connection(url: object, app_key: object) -> dict:
    normalized_url = _normalize_url(url)
    key = str(app_key or "").strip()
    if not normalized_url:
        raise Grok2APISyncConfigError("请填写 Grok2API 服务地址")
    if not key:
        raise Grok2APISyncConfigError("请填写 Grok2API app_key")
    try:
        response = _direct_request(
            "GET", f"{normalized_url}/admin/api/verify", key
        )
    except requests.RequestException:
        return _failure(None)
    payload = _response_payload(response)
    if response.status_code == 200 and payload and payload.get("status") == "success":
        return {"ok": True, "status_code": 200, "detail": "Grok2API 连接和鉴权正常"}
    return _failure(response.status_code)


def test_sync_config(
    url: object = "",
    app_key: object = "",
    *,
    clear_app_key: bool = False,
) -> dict:
    with exclusive_file_lock(LOCK_PATH):
        raw = _read_unlocked()
    candidate_url = str(url or "").strip() or str(
        raw.get("grok2api_sync_url", "") or ""
    ).strip()
    stored_key = str(raw.get("grok2api_sync_app_key", "") or "").strip()
    candidate_key = "" if clear_app_key else (str(app_key or "").strip() or stored_key)
    return verify_connection(candidate_url, candidate_key)


def sync_sso(url: object, app_key: object, raw_sso: object) -> dict:
    normalized_url = _normalize_url(url)
    key = str(app_key or "").strip()
    sso = _normalize_sso(raw_sso)
    if not normalized_url or not key or not sso:
        return {"ok": False, "status_code": None, "detail": "云同步配置或 SSO 不完整"}
    try:
        response = _direct_request(
            "POST",
            f"{normalized_url}/admin/api/tokens/add",
            key,
            json={"tokens": [sso]},
        )
    except requests.RequestException:
        return _failure(None)
    payload = _response_payload(response)
    if response.status_code == 200 and payload and payload.get("status") == "success":
        count = int(payload.get("count", 0) or 0)
        skipped = int(payload.get("skipped", 0) or 0)
        detail = "同步成功" if count > 0 else ("账号已存在，已跳过" if skipped > 0 else "同步请求已接受")
        return {
            "ok": True,
            "status_code": 200,
            "count": count,
            "skipped": skipped,
            "detail": detail,
        }
    return _failure(response.status_code)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="grok2api-sync",
            )
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
    app_key = str(config.get("grok2api_sync_app_key", "") or "").strip()
    sso = _normalize_sso(raw_sso)
    if not url or not app_key or not sso:
        if log_callback:
            log_callback("[Grok2API] 云同步已启用，但 URL、app_key 或 SSO 不完整")
        return None

    def _job() -> dict:
        result = sync_sso(url, app_key, sso)
        if log_callback:
            target = f": {email}" if email else ""
            marker = "+" if result.get("ok") else "!"
            try:
                log_callback(
                    f"[Grok2API] [{marker}] {result.get('detail', '同步失败')}{target}"
                )
            except Exception:
                pass
        return result

    return _get_executor().submit(_job)


__all__ = [
    "Grok2APISyncConfigError",
    "enqueue_sync",
    "read_sync_config",
    "save_sync_config",
    "sync_sso",
    "test_sync_config",
    "verify_connection",
]
