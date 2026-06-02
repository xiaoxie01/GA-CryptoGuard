"""Performance gate for context-based signal degradation and cooldown.

This module implements two gates:
1. symbol_side_cooldown: Pause trading for symbol+side combinations with consecutive losses
2. context_performance_gate: Downgrade signals based on historical performance

Usage:
    gate = PerformanceGate(repo)
    result = gate.check(symbol="BTCUSDT", side="LONG", signal_grade="S", trend_stage="early")
    # result may modify signal_grade or convert paper_order to opportunity_watch
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class PerformanceGate:
    """Checks historical performance and applies cooldown/downgrade rules."""

    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = 300.0  # 5 minutes cache TTL
        self._config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        """Load performance gate configuration from trading_mode.yaml."""
        try:
            config = load_config()
            return config.trading_mode.get("performance_gate", {})
        except Exception:
            # Return default config if loading fails
            return {
                "enabled": True,
                "cooldown": {
                    "loss_count_threshold": 2,
                    "loss_window": 3,
                    "cooldown_hours": 24,
                },
                "confidence_degradation": {
                    "avg_r_threshold": -0.2,
                    "sample_window": 5,
                    "confidence_penalty": 0.10,
                },
                "context_performance": {
                    "min_samples": 3,
                    "avg_r_threshold": 0.0,
                    "downgrade_rules": {"S": "A", "A": "B", "B": "C", "C": "D", "D": "D"},
                },
            }

    def check(
        self,
        *,
        symbol: str,
        side: str,
        signal_grade: str,
        trend_stage: str,
        confidence: float,
    ) -> dict[str, Any]:
        """Check performance gate and return degradation info.

        Returns:
            {
                "ok": True,
                "cooldown_active": bool,
                "cooldown_reason": str | None,
                "cooldown_until": str | None,
                "performance_degraded": bool,
                "original_grade": str,
                "effective_grade": str,
                "confidence_adjustment": float,
                "effective_confidence": float,
                "should_watch_only": bool,
                "reasons": list[str],
            }
        """
        # If gate is disabled, return passthrough
        if not self._config.get("enabled", True):
            return {
                "ok": True,
                "cooldown_active": False,
                "cooldown_reason": None,
                "cooldown_until": None,
                "performance_degraded": False,
                "original_grade": signal_grade,
                "effective_grade": signal_grade,
                "confidence_adjustment": 0.0,
                "effective_confidence": confidence,
                "should_watch_only": False,
                "reasons": [],
            }

        result = {
            "ok": True,
            "cooldown_active": False,
            "cooldown_reason": None,
            "cooldown_until": None,
            "performance_degraded": False,
            "original_grade": signal_grade,
            "effective_grade": signal_grade,
            "confidence_adjustment": 0.0,
            "effective_confidence": confidence,
            "should_watch_only": False,
            "reasons": [],
        }

        # Check symbol+side cooldown
        cooldown = self._check_cooldown(symbol, side)
        if cooldown["active"]:
            result["cooldown_active"] = True
            result["cooldown_reason"] = cooldown["reason"]
            result["cooldown_until"] = cooldown["until"]
            result["should_watch_only"] = True
            result["reasons"].append(f"symbol_side_cooldown: {cooldown['reason']}")
            return result

        # Check confidence degradation
        conf_adj = self._check_confidence_degradation(symbol, side)
        if conf_adj < 0:
            result["confidence_adjustment"] = conf_adj
            result["effective_confidence"] = max(0.0, confidence + conf_adj)
            result["reasons"].append(f"confidence_degradation: {conf_adj:+.2f}")

        # Check context performance for grade downgrade
        perf = self._check_context_performance(symbol, side, trend_stage, signal_grade)
        if perf["should_downgrade"]:
            result["performance_degraded"] = True
            result["effective_grade"] = perf["effective_grade"]
            result["reasons"].append(f"context_performance: {signal_grade}→{perf['effective_grade']}")

            # For S/A grade signals with poor historical performance,
            # force watch-only regardless of effective grade.
            # This prevents S->A from still entering paper orders.
            if signal_grade in {"S", "A"}:
                result["should_watch_only"] = True
                result["reasons"].append("high_grade_performance_watch_only")
            elif perf["effective_grade"] not in {"S", "A"}:
                # For B/C/D grades, only watch if downgraded below paper order threshold
                result["should_watch_only"] = True
                result["reasons"].append("grade_below_paper_order_threshold")

        return result

    def _check_cooldown(self, symbol: str, side: str) -> dict[str, Any]:
        """Check if symbol+side is in cooldown period."""
        cooldown_cfg = self._config.get("cooldown", {})
        loss_count_threshold = cooldown_cfg.get("loss_count_threshold", 2)
        loss_window = cooldown_cfg.get("loss_window", 3)
        cooldown_hours = cooldown_cfg.get("cooldown_hours", 24)

        recent_trades = self._get_recent_trades(symbol, side, limit=loss_window)

        if len(recent_trades) < loss_window:
            return {"active": False, "reason": None, "until": None}

        # Count losses in recent window
        loss_count = sum(1 for t in recent_trades if t.get("pnl_r", 0) < 0)
        if loss_count < loss_count_threshold:
            return {"active": False, "reason": None, "until": None}

        # Check if cooldown is still active
        last_trade_time = recent_trades[0].get("closed_at")
        if not last_trade_time:
            return {"active": False, "reason": None, "until": None}

        # Parse timestamp and check if cooldown period has passed
        try:
            last_dt = datetime.fromisoformat(last_trade_time.replace("Z", "+00:00"))
            cooldown_until = last_dt + timedelta(hours=cooldown_hours)
            now = datetime.now(cooldown_until.tzinfo) if cooldown_until.tzinfo else datetime.now()

            if now < cooldown_until:
                return {
                    "active": True,
                    "reason": f"最近{loss_window}笔亏损{loss_count}笔，冷却至{cooldown_until.isoformat()}",
                    "until": cooldown_until.isoformat(),
                }
        except (ValueError, TypeError):
            pass

        return {"active": False, "reason": None, "until": None}

    def _check_confidence_degradation(self, symbol: str, side: str) -> float:
        """Check if confidence should be degraded based on recent performance."""
        deg_cfg = self._config.get("confidence_degradation", {})
        avg_r_threshold = deg_cfg.get("avg_r_threshold", -0.2)
        sample_window = deg_cfg.get("sample_window", 5)
        confidence_penalty = deg_cfg.get("confidence_penalty", 0.10)

        recent_trades = self._get_recent_trades(symbol, side, limit=sample_window)

        if len(recent_trades) < sample_window:
            return 0.0

        # Calculate average R for recent window
        pnl_rs = [t.get("pnl_r", 0) for t in recent_trades]
        avg_r = sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0

        # If avg_r below threshold, degrade confidence
        if avg_r < avg_r_threshold:
            return -confidence_penalty

        return 0.0

    def _check_context_performance(
        self, symbol: str, side: str, trend_stage: str, signal_grade: str
    ) -> dict[str, Any]:
        """Check historical performance for symbol+side+trend_stage combination."""
        perf_cfg = self._config.get("context_performance", {})
        min_samples = perf_cfg.get("min_samples", 3)
        avg_r_threshold = perf_cfg.get("avg_r_threshold", 0.0)
        downgrade_rules = perf_cfg.get("downgrade_rules", {"S": "A", "A": "B", "B": "C", "C": "D", "D": "D"})

        # Query historical performance
        perf = self._get_historical_performance(symbol, side, trend_stage)

        if perf["sample_count"] < min_samples:
            return {"should_downgrade": False, "effective_grade": None}

        # If avg_r below threshold, downgrade grade
        if perf["avg_r"] < avg_r_threshold:
            # Apply downgrade rules
            effective_grade = downgrade_rules.get(signal_grade, "D")
            return {"should_downgrade": True, "effective_grade": effective_grade}

        return {"should_downgrade": False, "effective_grade": None}

    def _get_recent_trades(
        self, symbol: str, side: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get recent closed trades for symbol+side."""
        key = f"recent_trades:{symbol}:{side}:{limit}"
        if key in self._cache:
            return self._cache[key]

        try:
            rows = self.repo.conn.execute(
                """
                SELECT
                    t.symbol,
                    t.side,
                    t.pnl_r,
                    t.closed_at
                FROM paper_trades t
                WHERE t.symbol = ? AND t.side = ? AND t.closed_at IS NOT NULL
                ORDER BY t.closed_at DESC
                LIMIT ?
                """,
                (symbol, side, limit),
            ).fetchall()
            result = [dict(r) for r in rows]
        except Exception:
            result = []

        self._cache[key] = result
        return result

    def _get_historical_performance(
        self, symbol: str, side: str, trend_stage: str
    ) -> dict[str, Any]:
        """Get historical performance for symbol+side+trend_stage."""
        key = f"perf:{symbol}:{side}:{trend_stage}"
        if key in self._cache:
            return self._cache[key]

        try:
            row = self.repo.conn.execute(
                """
                SELECT
                    COUNT(*) AS sample_count,
                    AVG(t.pnl_r) AS avg_r,
                    SUM(CASE WHEN t.pnl_r > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS win_rate
                FROM ga_decisions gd
                JOIN signals s ON s.ga_decision_id = gd.id
                JOIN paper_orders po ON po.signal_id = s.id
                JOIN paper_trades t ON t.order_id = po.id AND t.closed_at IS NOT NULL
                WHERE gd.symbol = ?
                  AND UPPER(s.direction) = UPPER(?)
                  AND gd.trend_stage = ?
                """,
                (symbol, side, trend_stage),
            ).fetchone()

            if row:
                result = {
                    "sample_count": row[0] or 0,
                    "avg_r": row[1] or 0.0,
                    "win_rate": row[2] or 0.0,
                }
            else:
                result = {"sample_count": 0, "avg_r": 0.0, "win_rate": 0.0}
        except Exception:
            result = {"sample_count": 0, "avg_r": 0.0, "win_rate": 0.0}

        self._cache[key] = result
        return result

    def update_cache_on_trade_close(self, symbol: str, side: str) -> None:
        """Update cache when a trade is closed (event-driven update)."""
        # Clear relevant cache entries
        keys_to_remove = [
            k for k in self._cache
            if k.startswith(f"recent_trades:{symbol}:{side}")
            or k.startswith(f"perf:{symbol}:{side}")
        ]
        for key in keys_to_remove:
            del self._cache[key]
