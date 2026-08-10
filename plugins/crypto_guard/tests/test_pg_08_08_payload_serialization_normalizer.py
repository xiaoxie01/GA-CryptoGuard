# -*- coding: utf-8 -*-
"""08-08 P1-1 (PRD): JSON-safe payload normalizer for ``run_agent_json_task``.

``build_agent_json_task_prompt`` must never raise on datetime/date (production
55/55 failure: ``_agent_review_watch_result`` passes a real
``opportunity_watches`` dict_row with TIMESTAMPTZ ``created_at``/``expires_at``/
``updated_at`` datetimes). A ``_json_safe_payload`` normalizer converts
datetime/date to timezone-aware UTC ISO-8601 and FAILS CLOSED (raises
``_PayloadSerializationError``) on unknown object types, non-finite floats,
non-string dict keys, and cyclic structures — it never silently stringifies
(no ``default=str``). ``run_agent_json_task`` records a STRUCTURED category
``llm_failure_category="payload_serialization_failed"`` when the normalizer
throws; diagnostics read that field, never the free-text ``llm_error``.

RED-first + revert-fail: the un-normalized ``json.dumps(payload, ...)`` on the
real watch payload raises ``TypeError: Object of type datetime is not JSON
serializable`` (reproduced inline) while ``build_agent_json_task_prompt``
serializes it.

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.tests.pg_fixtures import make_repo


class _UnknownObject:
    pass


def _materialize_watch(repo) -> dict:
    """Create a real opportunity_watches row and return the dict_row with real
    TIMESTAMPTZ datetimes (the production 55/55 serialization path)."""
    watch_id = repo.create_opportunity_watch(
        "BTCUSDT",
        {
            "direction": "LONG",
            "reason": "测试机会监控",
            "conditions": [{"type": "breakout", "side": "LONG", "level": 101.0, "timeframe": "15m"}],
            "invalid_condition": {"type": "close_below", "side": "LONG", "level": 95.0},
            "expires_minutes": 60,
        },
    )
    return repo.get_opportunity_watch(watch_id)


def _rule_result() -> dict:
    return {
        "status": "triggered",
        "reason": "15m 突破 101.0",
        "condition_results": [
            {"condition": "breakout", "hit": True, "trigger_value": 101.0, "latest_value": 102.0},
        ],
    }


class TestProductionWatchPayloadSerializes:
    def test_real_watch_payload_serializes(self) -> None:
        """P1-1: the exact production payload ``{"watch": <real row>,
        "rule_result": <result>}`` serializes via ``build_agent_json_task_prompt``
        (datetimes → UTC ISO-8601). RED: the un-normalized dumps raises
        TypeError."""
        from plugins.crypto_guard.reasoning.llm_agent_judge import build_agent_json_task_prompt

        handle = make_repo()
        try:
            watch = _materialize_watch(handle.repo)
            dt_fields = [k for k, v in watch.items() if isinstance(v, datetime)]
            assert dt_fields, "the real watch row must carry datetime fields"

            payload = {"watch": watch, "rule_result": _rule_result()}

            # Revert-fail: the un-normalized dump on the EXACT production
            # payload raises TypeError (the old behavior).
            with pytest.raises(TypeError):
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

            # GREEN: the normalizer makes the same payload serializable.
            prompt = build_agent_json_task_prompt(
                task_name="opportunity_watch_review",
                payload=payload,
                fallback={"summary": "测试", "status": "triggered", "action": "notify", "risk_notes": []},
            )
            assert isinstance(prompt, str) and prompt
            assert "triggered" in prompt
            # Datetimes were normalized to timezone-aware UTC ISO-8601.
            assert "+00:00" in prompt or "Z" in prompt
        finally:
            handle.close()


class TestNormalizerFailClosed:
    def _payload(self) -> tuple:
        from plugins.crypto_guard.reasoning.llm_agent_judge import (
            _PayloadSerializationError,
            _json_safe_payload,
        )
        return _PayloadSerializationError, _json_safe_payload

    @pytest.mark.parametrize(
        "label,obj",
        [
            ("unknown_object", _UnknownObject()),
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("neg_inf", float("-inf")),
            ("non_string_dict_key", {1: "x"}),
        ],
    )
    def test_fail_closed_raises_payload_serialization_error(self, label: str, obj) -> None:
        """P1-1: unknown object types / non-finite floats / non-string dict
        keys fail closed (never silently stringified)."""
        _Err, _normalize = self._payload()
        with pytest.raises(_Err):
            _normalize(obj)

    def test_cyclic_dict_raises(self) -> None:
        _Err, _normalize = self._payload()
        a: dict = {}
        a["self"] = a
        with pytest.raises(_Err):
            _normalize(a)

    def test_cyclic_list_raises(self) -> None:
        _Err, _normalize = self._payload()
        a: list = []
        a.append(a)
        with pytest.raises(_Err):
            _normalize(a)

    def test_shared_reference_is_not_cycle(self) -> None:
        """A shared (non-cyclic) reference must serialize fine — the visited-id
        set is path-scoped (added on enter, discarded on exit)."""
        _Err, _normalize = self._payload()
        shared = {"x": 1}
        obj = {"a": shared, "b": shared}
        assert _normalize(obj) == {"a": {"x": 1}, "b": {"x": 1}}

    def test_datetime_to_utc_iso(self) -> None:
        _Err, _normalize = self._payload()
        assert _normalize(datetime(2026, 8, 8, 12, 30, 0)) == "2026-08-08T12:30:00+00:00"
        aware = datetime(2026, 8, 8, 12, 30, 0, tzinfo=timezone.utc)
        assert _normalize(aware) == "2026-08-08T12:30:00+00:00"
        # Non-UTC aware datetime is converted to UTC.
        from datetime import timedelta
        aware_plus2 = datetime(2026, 8, 8, 14, 30, 0, tzinfo=timezone(timedelta(hours=2)))
        assert _normalize(aware_plus2) == "2026-08-08T12:30:00+00:00"

    def test_date_to_iso(self) -> None:
        _Err, _normalize = self._payload()
        assert _normalize(date(2026, 8, 8)) == "2026-08-08"


class TestRunAgentJsonTaskStructuredCategory:
    def test_non_serializable_payload_records_structured_category(self) -> None:
        """P1-1: when the normalizer throws during prompt building,
        ``run_agent_json_task`` records the STRUCTURED category
        ``llm_failure_category="payload_serialization_failed"`` (never a
        string-match on ``llm_error``)."""
        from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task

        result = run_agent_json_task(
            task_name="opportunity_watch_review",
            payload={"watch": {"created_at": _UnknownObject()}},
            fallback={"summary": "s", "status": "waiting", "action": "keep_waiting", "risk_notes": []},
            use_llm=True,
        )
        assert result["llm_status"] == "failed"
        assert result["llm_failure_category"] == "payload_serialization_failed"
