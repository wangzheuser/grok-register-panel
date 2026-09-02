"""MailPoolHub unified temporary-mail client."""

from __future__ import annotations

import time
from typing import Callable, Optional
from urllib.parse import quote


def normalize_base(base_url: str) -> str:
    return str(base_url or "http://127.0.0.1:8080/api/v1").strip().rstrip("/")


def _headers(api_key: str, *, content_type: bool = False) -> dict:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def create_mailbox(
    http_post: Callable,
    base_url: str,
    api_key: str,
    provider: str = "",
    ttl_seconds: int = 900,
) -> tuple[str, str]:
    if not str(api_key or "").strip():
        raise RuntimeError("MailPoolHub API Key 未配置")
    payload = {
        "ttlSeconds": int(ttl_seconds),
        "tags": {"scene": "xai-signup"},
    }
    if str(provider or "").strip():
        payload["provider"] = str(provider).strip()
    response = http_post(
        f"{normalize_base(base_url)}/mailboxes",
        json=payload,
        headers=_headers(api_key, content_type=True),
        timeout=45,
        _force_direct=True,
    )
    response.raise_for_status()
    data = response.json() or {}
    address = str(data.get("address") or "").strip()
    mailbox_id = str(data.get("id") or "").strip()
    if not address or not mailbox_id:
        raise RuntimeError("MailPoolHub 创建邮箱响应缺少 address 或 id")
    return address, mailbox_id


def wait_for_code(
    http_get: Callable,
    http_delete: Callable,
    base_url: str,
    api_key: str,
    mailbox_id: str,
    email: str,
    *,
    timeout: int,
    poll_interval: int,
    extract_code: Callable[[str, str], Optional[str]],
    raise_if_cancelled: Callable,
    sleep_with_cancel: Callable,
    log_callback=None,
    cancel_callback=None,
) -> str:
    base = normalize_base(base_url)
    headers = _headers(api_key)
    deadline = time.time() + timeout
    seen = set()
    try:
        while time.time() < deadline:
            raise_if_cancelled(cancel_callback)
            try:
                response = http_get(
                    f"{base}/mailboxes/{quote(mailbox_id, safe='')}/messages?refresh=true",
                    headers=headers,
                    timeout=45,
                    _force_direct=True,
                )
                response.raise_for_status()
                messages = (response.json() or {}).get("messages") or []
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    detail_response = http_get(
                        f"{base}/mailboxes/{quote(mailbox_id, safe='')}/messages/{quote(message_id, safe='')}",
                        headers=headers,
                        timeout=45,
                        _force_direct=True,
                    )
                    detail_response.raise_for_status()
                    detail = detail_response.json() or {}
                    seen.add(message_id)
                    subject = str(detail.get("subject") or message.get("subject") or "")
                    content = "\n".join(
                        str(detail.get(name) or "") for name in ("text", "html", "rawJson")
                    )
                    code = extract_code(content, subject)
                    if code:
                        if log_callback:
                            log_callback(f"[*] MailPoolHub 已收到验证码邮件: {email}")
                        return code
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] MailPoolHub 刷新邮件失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
        raise TimeoutError(f"MailPoolHub 在 {timeout}s 内未收到验证码邮件")
    finally:
        try:
            response = http_delete(
                f"{base}/mailboxes/{quote(mailbox_id, safe='')}",
                headers=headers,
                timeout=20,
                _force_direct=True,
            )
            if response.status_code not in (200, 404):
                response.raise_for_status()
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] MailPoolHub 清理邮箱失败: {exc}")
