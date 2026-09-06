# -*- coding: utf-8 -*-
"""单任务上限 / 轮次上限 / 验证码等待 配置链路 + 编排收尾（drain）机制。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_until_100 as orchestrator
from webui import monitor


def test_save_control_clamps_timeout_keys():
    previous = monitor.CONTROL_FILE
    with tempfile.TemporaryDirectory() as temp:
        monitor.CONTROL_FILE = Path(temp) / "monitor_control.json"
        try:
            c = monitor.save_control(
                {
                    "attempt_timeout_sec": 0,
                    "round_timeout_min": 0,
                    "mail_code_wait": 120,
                }
            )
            assert c["attempt_timeout_sec"] == 0
            assert c["round_timeout_min"] == 0
            assert c["mail_code_wait"] == 120

            c = monitor.save_control(
                {
                    "attempt_timeout_sec": 99,
                    "round_timeout_min": 3,
                    "mail_code_wait": 5,
                }
            )
            # 单任务/轮次上限无下限校验（0 除外），验证码等待低于下限回落默认 180
            assert c["attempt_timeout_sec"] == 99
            assert c["round_timeout_min"] == 3
            assert c["mail_code_wait"] == 180

            c = monitor.save_control(
                {
                    "attempt_timeout_sec": 99999,
                    "round_timeout_min": 9999,
                    "mail_code_wait": 9999,
                }
            )
            # 单任务/轮次上限不校验上限：任意 >=0 的整数原样保留
            assert c["attempt_timeout_sec"] == 99999
            assert c["round_timeout_min"] == 9999
            assert c["mail_code_wait"] == 600

            c = monitor.save_control(
                {"attempt_timeout_sec": -5, "round_timeout_min": -1}
            )
            # 负数非法，回落默认
            assert c["attempt_timeout_sec"] == 360
            assert c["round_timeout_min"] == 1440

            c = monitor.save_control({})
            # 空更新保留已保存的值，不重置为默认
            assert c["attempt_timeout_sec"] == 360
            assert c["round_timeout_min"] == 1440
            assert c["mail_code_wait"] == 600

            c = monitor.save_control(
                {
                    "attempt_timeout_sec": "",
                    "round_timeout_min": None,
                    "mail_code_wait": "bad",
                }
            )
            assert c["attempt_timeout_sec"] == 360
            assert c["round_timeout_min"] == 1440
            assert c["mail_code_wait"] == 180
        finally:
            monitor.CONTROL_FILE = previous


def test_apply_control_reads_round_and_attempt_timeout():
    previous = (
        orchestrator.CONTROL_FILE,
        orchestrator.ROUND_TIMEOUT_MIN,
        orchestrator.ATTEMPT_TIMEOUT_SEC,
    )
    with tempfile.TemporaryDirectory() as temp:
        control = Path(temp) / "monitor_control.json"
        control.write_text(
            json.dumps({"round_timeout_min": 90, "attempt_timeout_sec": 0}),
            encoding="utf-8",
        )
        orchestrator.CONTROL_FILE = control
        try:
            orchestrator.apply_control()
            assert orchestrator.ROUND_TIMEOUT_MIN == 90
            assert orchestrator.ATTEMPT_TIMEOUT_SEC == 0

            control.write_text(
                json.dumps({"round_timeout_min": 0, "attempt_timeout_sec": "bad"}),
                encoding="utf-8",
            )
            orchestrator.apply_control()
            assert orchestrator.ROUND_TIMEOUT_MIN == 0
            assert orchestrator.ATTEMPT_TIMEOUT_SEC == 360

            control.write_text(
                json.dumps({"round_timeout_min": 1}),
                encoding="utf-8",
            )
            orchestrator.apply_control()
            assert orchestrator.ROUND_TIMEOUT_MIN == 1

            control.write_text(
                json.dumps({"round_timeout_min": 5000, "attempt_timeout_sec": 7200}),
                encoding="utf-8",
            )
            orchestrator.apply_control()
            # 不设上限校验
            assert orchestrator.ROUND_TIMEOUT_MIN == 5000
            assert orchestrator.ATTEMPT_TIMEOUT_SEC == 7200
        finally:
            (
                orchestrator.CONTROL_FILE,
                orchestrator.ROUND_TIMEOUT_MIN,
                orchestrator.ATTEMPT_TIMEOUT_SEC,
            ) = previous


def test_drain_grace_bounds():
    previous = orchestrator.ATTEMPT_TIMEOUT_SEC
    try:
        orchestrator.ATTEMPT_TIMEOUT_SEC = 360
        assert orchestrator.drain_grace_secs() == 600
        orchestrator.ATTEMPT_TIMEOUT_SEC = 0
        assert orchestrator.drain_grace_secs() == 900
        orchestrator.ATTEMPT_TIMEOUT_SEC = 60
        assert orchestrator.drain_grace_secs() == 300
        # 大预算不设上限，宽限跟随预算
        orchestrator.ATTEMPT_TIMEOUT_SEC = 7200
        assert orchestrator.drain_grace_secs() == 7440
    finally:
        orchestrator.ATTEMPT_TIMEOUT_SEC = previous


def test_request_and_clear_drain():
    previous = orchestrator.DRAIN_FILE
    with tempfile.TemporaryDirectory() as temp:
        orchestrator.DRAIN_FILE = Path(temp) / "batch-drain.json"
        try:
            orchestrator.request_drain()
            data = json.loads(orchestrator.DRAIN_FILE.read_text(encoding="utf-8"))
            assert data["expire_at"] > time.time()
            assert data["reason"] == "round_timeout"
            orchestrator.clear_drain()
            assert not orchestrator.DRAIN_FILE.exists()
            orchestrator.clear_drain()  # 幂等
        finally:
            orchestrator.DRAIN_FILE = previous


def test_supervisor_drain_requested():
    from batch_supervisor import DRAIN_FILE, drain_requested

    assert drain_requested() is False
    DRAIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        DRAIN_FILE.write_text(
            json.dumps({"expire_at": time.time() + 600}), encoding="utf-8"
        )
        assert drain_requested() is True
        DRAIN_FILE.write_text(
            json.dumps({"expire_at": time.time() - 10}), encoding="utf-8"
        )
        assert drain_requested() is False
        DRAIN_FILE.write_text("not json", encoding="utf-8")
        assert drain_requested() is True  # 无法解析时按 mtime 兜底
    finally:
        try:
            DRAIN_FILE.unlink()
        except OSError:
            pass


def test_grok_attempt_budget_helpers():
    import grok_register_ttk as app

    previous = app.config.get("attempt_timeout_sec")
    try:
        app.config["attempt_timeout_sec"] = 600
        assert app.resolve_attempt_budget() == 600
        app.config["attempt_timeout_sec"] = 0
        assert app.resolve_attempt_budget() == 0
        app.config["attempt_timeout_sec"] = "bad"
        assert app.resolve_attempt_budget() == 0

        # 未开启预算：check 不抛
        app.check_attempt_deadline(None, 600)
        # 已超期：抛 AttemptBudgetExceeded
        app.config["attempt_timeout_sec"] = 600
        deadline = time.monotonic() - 1
        try:
            app.check_attempt_deadline(deadline, 600)
            raise AssertionError("expected AttemptBudgetExceeded")
        except app.AttemptBudgetExceeded:
            pass

        # classify_failure 归类为 task_timeout
        assert app.classify_failure(app.AttemptBudgetExceeded("单任务超时")) == (
            app.FAIL_TASK_TIMEOUT
        )
    finally:
        if previous is None:
            app.config.pop("attempt_timeout_sec", None)
        else:
            app.config["attempt_timeout_sec"] = previous


def test_attempt_budget_default_360():
    import grok_register_ttk as app

    had = app.config.pop("attempt_timeout_sec", None)
    try:
        assert app.resolve_attempt_budget() == 360
    finally:
        if had is not None:
            app.config["attempt_timeout_sec"] = had


def test_get_oai_code_uses_mail_wait_config():
    import grok_register_ttk as app

    previous = app.config.get("mail_code_wait")
    try:
        app.config["mail_code_wait"] = 120
        import inspect

        src = inspect.getsource(app.get_oai_code)
        assert "mail_code_wait" in src
        # 验证实际解析逻辑
        assert app.config.get("mail_code_wait") == 120
    finally:
        if previous is None:
            app.config.pop("mail_code_wait", None)
        else:
            app.config["mail_code_wait"] = previous


if __name__ == "__main__":
    test_save_control_clamps_timeout_keys()
    test_apply_control_reads_round_and_attempt_timeout()
    test_drain_grace_bounds()
    test_request_and_clear_drain()
    test_supervisor_drain_requested()
    test_grok_attempt_budget_helpers()
    test_attempt_budget_default_360()
    test_get_oai_code_uses_mail_wait_config()
    print("OK timeout config")
