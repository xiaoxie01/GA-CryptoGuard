# -*- coding: utf-8 -*-
"""08-08 P1-3 (PRD): entry confirmation extraction reads ``timeframe_modules``.

``_extract_structured_entry_confirmation`` must read the 15m/5m entry periods
from ``snapshot.timeframe_modules`` in addition to the primary ``modules``, so
a legal closed-candle confirmation that lives only in a lower-timeframe entry
period is found. It picks the LATEST legal closed event with
``close_time <= analysis_time`` and preserves provenance + future-function
protection. Standards are not lowered (S/A grade, LLM confirmed, risk ok,
account gate all remain).

RED-first: the current code reads only ``snapshot.get("modules")``, so a
confirmation present only in ``timeframe_modules`` returns None (RED).

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.ga_judge import _extract_structured_entry_confirmation

_ANALYSIS = 1_700_000_100_000


def _event(**overrides: dict) -> dict:
    event = {
        "event": "bullish_bos",
        "timeframe": "15m",
        "direction": "bullish",
        "candle_close_time": 1_700_000_000_000,
        "price": 60000.0,
        "closed": True,
    }
    event.update(overrides)
    return event


def _empty_tf_modules() -> dict:
    return {
        "15m": {"price_action": {"structure_events": []}, "smc": {}},
        "5m": {"price_action": {"structure_events": []}, "smc": {}},
    }


class TestTimeframeModulesExtraction:
    def test_confirmation_only_in_timeframe_modules_15m(self) -> None:
        """P1-3: a legal closed-candle confirmation present ONLY in
        ``timeframe_modules['15m']`` (not in primary modules) is found."""
        snapshot = {
            "symbol": "BTCUSDT",
            "analysis_time_utc": _ANALYSIS,
            "modules": {"price_action": {"structure_events": []}, "smc": {}},
            "timeframe_modules": {
                **_empty_tf_modules(),
                "15m": {"price_action": {"structure_events": [_event()]}, "smc": {}},
            },
        }
        result = _extract_structured_entry_confirmation(snapshot, "LONG", 60100.0)
        assert result is not None, "P1-3 RED: confirmation in timeframe_modules['15m'] must be found"
        assert result["event_type"] == "BOS"
        assert result["direction"] == "bullish"
        assert result["candle_close_time"] == 1_700_000_000_000
        assert result["source"] == "15m:price_action"

    def test_confirmation_only_in_timeframe_modules_5m(self) -> None:
        snapshot = {
            "symbol": "BTCUSDT",
            "analysis_time_utc": _ANALYSIS,
            "modules": {"price_action": {"structure_events": []}, "smc": {}},
            "timeframe_modules": {
                **_empty_tf_modules(),
                "5m": {"price_action": {"structure_events": [_event(timeframe="5m")]}, "smc": {}},
            },
        }
        result = _extract_structured_entry_confirmation(snapshot, "LONG", 60100.0)
        assert result is not None, "P1-3 RED: confirmation in timeframe_modules['5m'] must be found"
        assert result["source"] == "5m:price_action"

    def test_picks_latest_legal_closed_event_across_primary_and_timeframe(self) -> None:
        """P1-3: the LATEST legal closed event (close_time <= analysis_time) is
        chosen across primary modules and timeframe_modules."""
        snapshot = {
            "symbol": "BTCUSDT",
            "analysis_time_utc": _ANALYSIS,
            "modules": {
                "price_action": {"structure_events": [_event(candle_close_time=1_700_000_000_000)]},
                "smc": {},
            },
            "timeframe_modules": {
                **_empty_tf_modules(),
                "15m": {"price_action": {"structure_events": [_event(candle_close_time=1_700_000_050_000)]}, "smc": {}},
            },
        }
        result = _extract_structured_entry_confirmation(snapshot, "LONG", 60100.0)
        assert result is not None
        assert result["candle_close_time"] == 1_700_000_050_000
        assert result["source"] == "15m:price_action"

    def test_future_event_in_timeframe_modules_rejected(self) -> None:
        """P1-3: a future-leak event in timeframe_modules is rejected (no
        future-function protection lowering)."""
        snapshot = {
            "symbol": "BTCUSDT",
            "analysis_time_utc": _ANALYSIS,
            "modules": {"price_action": {"structure_events": []}, "smc": {}},
            "timeframe_modules": {
                **_empty_tf_modules(),
                "15m": {"price_action": {"structure_events": [_event(candle_close_time=_ANALYSIS + 1)]}, "smc": {}},
            },
        }
        result = _extract_structured_entry_confirmation(snapshot, "LONG", 60100.0)
        assert result is None, "future-leak event must be rejected"

    def test_unclosed_event_in_timeframe_modules_rejected(self) -> None:
        snapshot = {
            "symbol": "BTCUSDT",
            "analysis_time_utc": _ANALYSIS,
            "modules": {"price_action": {"structure_events": []}, "smc": {}},
            "timeframe_modules": {
                **_empty_tf_modules(),
                "15m": {"price_action": {"structure_events": [_event(closed=False)]}, "smc": {}},
            },
        }
        result = _extract_structured_entry_confirmation(snapshot, "LONG", 60100.0)
        assert result is None, "unclosed event must be rejected"
