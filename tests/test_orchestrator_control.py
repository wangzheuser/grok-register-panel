# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_until_100 as orchestrator


def test_resolve_batch_count_bounds_and_fallback():
    assert orchestrator.resolve_batch_count({"batch_count": 1}) == 1
    assert orchestrator.resolve_batch_count({"batch_count": 30}) == 30
    assert orchestrator.resolve_batch_count({"batch_count": 1000}) == 1000
    assert orchestrator.resolve_batch_count({"batch_count": 0}) == 1
    assert orchestrator.resolve_batch_count({"batch_count": 999}) == 999
    assert orchestrator.resolve_batch_count({"batch_count": 1001}) == 1000
    assert orchestrator.resolve_batch_count({"batch_count": "bad"}) == 40
    assert "batch_n = BATCH_COUNT" in (ROOT / "run_until_100.py").read_text(encoding="utf-8")


def test_apply_control_keeps_batch_size_separate_from_add_target():
    previous = (
        orchestrator.CONTROL_FILE,
        orchestrator.AUTHS,
        orchestrator.WORKERS,
        orchestrator.BATCH_COUNT,
        orchestrator.RISK_PAUSE,
        orchestrator.BASE0,
        orchestrator.TARGET_CPA,
    )
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        auths = root / "cpa_auth"
        auths.mkdir()
        (auths / "xai-one.json").write_text("{}", encoding="utf-8")
        (auths / "xai-two.json").write_text("{}", encoding="utf-8")
        control = root / "monitor_control.json"
        control.write_text(
            json.dumps(
                {
                    "workers": 3,
                    "batch_count": 30,
                    "add_count": 1,
                    "risk_pause": 2,
                }
            ),
            encoding="utf-8",
        )
        try:
            orchestrator.CONTROL_FILE = control
            orchestrator.AUTHS = auths
            orchestrator.apply_control()
            assert orchestrator.BATCH_COUNT == 30
            assert orchestrator.BASE0 == 2
            assert orchestrator.TARGET_CPA == 3
        finally:
            (
                orchestrator.CONTROL_FILE,
                orchestrator.AUTHS,
                orchestrator.WORKERS,
                orchestrator.BATCH_COUNT,
                orchestrator.RISK_PAUSE,
                orchestrator.BASE0,
                orchestrator.TARGET_CPA,
            ) = previous
