# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import connectivity


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def check(config, response):
    return connectivity.check_email_api(
        "mailpoolhub",
        config,
        lambda *_args, **_kwargs: response,
        lambda *_args, **_kwargs: response,
    )


def provider(name="mailgw", health="healthy", **capability_overrides):
    capabilities = {
        "createMailbox": True,
        "listMessages": True,
        "getMessage": True,
    }
    capabilities.update(capability_overrides)
    return {"name": name, "healthStatus": health, "capabilities": capabilities}


def test_mailpoolhub_connectivity_requires_key_and_valid_response():
    assert check({}, Response())[1:] == (False, "MailPoolHub 需配置 API Key")
    assert check({"mailpoolhub_api_key": "key"}, Response(401))[1] is False
    assert check({"mailpoolhub_api_key": "key"}, Response(200, {"providers": {}}))[1] is False


def test_mailpoolhub_connectivity_auto_requires_healthy_core_capabilities():
    config = {"mailpoolhub_api_key": "key"}
    result = check(config, Response(200, {"providers": [provider(), provider("mailtm")]}))
    assert result[1] is True
    assert "2 个健康渠道" in result[2]

    result = check(
        config,
        Response(200, {"providers": [provider(health="unhealthy"), provider(getMessage=False)]}),
    )
    assert result[1] is False
    assert "没有具备核心能力" in result[2]


def test_mailpoolhub_connectivity_validates_pinned_provider():
    config = {"mailpoolhub_api_key": "key", "mailpoolhub_provider": "mailgw"}
    assert check(config, Response(200, {"providers": [provider("other")]}))[1] is False
    assert check(config, Response(200, {"providers": [provider(health="unhealthy")]}))[1] is False
    result = check(config, Response(200, {"providers": [provider()]}))
    assert result[1] is True
    assert "固定渠道 mailgw 健康" in result[2]
