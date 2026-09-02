# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from email_providers import mailpoolhub
from email_providers.common import extract_verification_code


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_create_mailbox_auto_and_fixed_provider():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(201, {"id": "mbx_1", "address": "user@example.com"})

    assert mailpoolhub.create_mailbox(post, "http://127.0.0.1:8080/api/v1/", "key") == (
        "user@example.com",
        "mbx_1",
    )
    assert "provider" not in calls[0][1]["json"]
    assert calls[0][1]["_force_direct"] is True

    mailpoolhub.create_mailbox(post, "http://127.0.0.1:8080/api/v1", "key", "mailgw")
    assert calls[1][1]["json"]["provider"] == "mailgw"


def test_create_mailbox_rejects_missing_key_and_malformed_response():
    with pytest.raises(RuntimeError, match="API Key"):
        mailpoolhub.create_mailbox(lambda *_args, **_kwargs: None, "http://mail", "")
    with pytest.raises(RuntimeError, match="缺少"):
        mailpoolhub.create_mailbox(
            lambda *_args, **_kwargs: Response(201, {"id": "mbx_only"}),
            "http://mail",
            "key",
        )


def test_wait_for_code_retries_reads_detail_and_cleans_mailbox():
    list_calls = 0
    detail_urls = []
    deleted = []

    def get(url, **kwargs):
        nonlocal list_calls
        assert kwargs["_force_direct"] is True
        if "?refresh=true" in url:
            list_calls += 1
            if list_calls == 1:
                raise RuntimeError("temporary refresh failure")
            return Response(200, {"messages": [{"id": "provider/id with space"}]})
        detail_urls.append(url)
        return Response(
            200,
            {
                "subject": "SpaceXAI confirmation code: 605-680",
                "text": "Use 605-680 to validate your email.",
            },
        )

    def delete(url, **kwargs):
        deleted.append((url, kwargs))
        return Response(200, {"success": True})

    code = mailpoolhub.wait_for_code(
        get,
        delete,
        "http://127.0.0.1:8080/api/v1",
        "key",
        "mbx/id",
        "user@example.com",
        timeout=10,
        poll_interval=5,
        extract_code=extract_verification_code,
        raise_if_cancelled=lambda _callback: None,
        sleep_with_cancel=lambda _seconds, _callback: None,
    )
    assert code == "605-680"
    assert list_calls == 2
    assert "/provider%2Fid%20with%20space" in detail_urls[0]
    assert deleted[0][0].endswith("/mailboxes/mbx%2Fid")
    assert deleted[0][1]["_force_direct"] is True


def test_wait_timeout_still_cleans_mailbox():
    deleted = []
    with pytest.raises(TimeoutError, match="未收到验证码"):
        mailpoolhub.wait_for_code(
            lambda *_args, **_kwargs: Response(200, {"messages": []}),
            lambda url, **_kwargs: deleted.append(url) or Response(404),
            "http://127.0.0.1:8080/api/v1",
            "key",
            "mbx_timeout",
            "user@example.com",
            timeout=0,
            poll_interval=5,
            extract_code=extract_verification_code,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )
    assert deleted == ["http://127.0.0.1:8080/api/v1/mailboxes/mbx_timeout"]
