from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_strategy_name(raw: dict[str, Any]) -> str | None:
    """Extract strategy_name from ga_decision raw_decision_json.

    Real data has strategy_name at raw_decision_json.raw_legacy_decision.strategy_name.
    Also supports top-level strategy_name for forward compatibility.
    """
    name = raw.get("strategy_name")
    if name:
        return name
    legacy = raw.get("raw_legacy_decision")
    if isinstance(legacy, dict):
        name = legacy.get("strategy_name")
        if name:
            return name
    return None


class CryptoGuardRepository:
    """Repository 层隔离所有 SQL，业务模块不直接拼 SQL。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert_symbol(
        self,
        symbol: str,
        *,
        category: str = "custom",
        enabled: bool = True,
        source: str = "user",
        timeframes: list[str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        base_asset = symbol.removesuffix("USDT")
        self.conn.execute(
            """
            INSERT INTO symbols(symbol, base_asset, quote_asset, category, enabled, source, default_timeframes, notes)
            VALUES (?, ?, 'USDT', ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                category=excluded.category,
                enabled=excluded.enabled,
                source=excluded.source,
                default_timeframes=COALESCE(excluded.default_timeframes, symbols.default_timeframes),
                notes=COALESCE(excluded.notes, symbols.notes),
                updated_at=CURRENT_TIMESTAMP
            """,
            (symbol, base_asset, category, 1 if enabled else 0, source, json.dumps(timeframes or [], ensure_ascii=False), notes),
        )
        return self.get_symbol(symbol) or {"symbol": symbol}

    def get_symbol(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM symbols WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def remove_symbol(self, symbol: str) -> bool:
        cur = self.conn.execute("DELETE FROM symbols WHERE symbol=?", (symbol,))
        return cur.rowcount > 0

    def set_symbol_enabled(self, symbol: str, enabled: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE symbols SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE symbol=?",
            (1 if enabled else 0, symbol),
        )
        return cur.rowcount > 0

    def list_symbols(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM symbols"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY enabled DESC, category, symbol"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def active_analysis_symbols(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT symbol FROM symbols WHERE enabled=1
            UNION
            SELECT symbol FROM opportunity_watches WHERE status='active'
            UNION
            SELECT symbol FROM paper_orders WHERE status IN ('pending','open')
            ORDER BY symbol
            """
        ).fetchall()
        return [str(r["symbol"]) for r in rows]

    def upsert_candles(self, candles: Iterable[dict[str, Any]]) -> int:
        count = 0
        for c in candles:
            self.conn.execute(
                """
                INSERT INTO candles(
                    symbol, interval, open_time, close_time, open, high, low, close, volume,
                    quote_volume, taker_buy_volume, taker_buy_quote_volume, trade_count, is_closed, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    quote_volume=excluded.quote_volume,
                    taker_buy_volume=excluded.taker_buy_volume,
                    taker_buy_quote_volume=excluded.taker_buy_quote_volume,
                    trade_count=excluded.trade_count,
                    is_closed=excluded.is_closed,
                    source=excluded.source,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    c["symbol"],
                    c["interval"],
                    int(c["open_time"]),
                    int(c["close_time"]),
                    float(c["open"]),
                    float(c["high"]),
                    float(c["low"]),
                    float(c["close"]),
                    float(c["volume"]),
                    c.get("quote_volume"),
                    c.get("taker_buy_volume"),
                    c.get("taker_buy_quote_volume"),
                    c.get("trade_count"),
                    1 if c.get("is_closed", True) else 0,
                    c.get("source", "binance"),
                ),
            )
            count += 1
        return count

    def get_candles(self, symbol: str, interval: str, *, analysis_time_utc: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM candles
            WHERE symbol=? AND interval=? AND is_closed=1 AND close_time <= ?
            ORDER BY open_time DESC
            LIMIT ?
            """,
            (symbol, interval, int(analysis_time_utc), int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def no_lookahead_candles(self, symbol: str, interval: str, *, analysis_time_utc: int, limit: int = 200) -> dict[str, Any]:
        candles = self.get_candles(symbol, interval, analysis_time_utc=analysis_time_utc, limit=limit)
        violation = [c for c in candles if int(c["close_time"]) > int(analysis_time_utc) or int(c.get("is_closed", 1)) != 1]
        return {
            "ok": len(violation) == 0,
            "symbol": symbol,
            "interval": interval,
            "analysis_time_utc": int(analysis_time_utc),
            "count": len(candles),
            "candles": candles,
            "violation_count": len(violation),
        }

    def save_module_result(self, symbol: str, timeframe: str, analysis_time_utc: int, module: str, result: dict[str, Any], confidence: float | None) -> None:
        self.conn.execute(
            """
            INSERT INTO module_analysis_results(symbol, timeframe, analysis_time, module, result_json, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, analysis_time, module) DO UPDATE SET
                result_json=excluded.result_json,
                confidence=excluded.confidence
            """,
            (symbol, timeframe, int(analysis_time_utc), module, json.dumps(result, ensure_ascii=False), confidence),
        )

    def save_market_snapshot(self, snapshot: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO market_snapshots(symbol, analysis_time, mode, snapshot_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, analysis_time, mode) DO UPDATE SET snapshot_json=excluded.snapshot_json
            """,
            (
                snapshot["symbol"],
                int(snapshot["analysis_time_utc"]),
                snapshot["mode"],
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )
        row = self.conn.execute(
            "SELECT id FROM market_snapshots WHERE symbol=? AND analysis_time=? AND mode=?",
            (snapshot["symbol"], int(snapshot["analysis_time_utc"]), snapshot["mode"]),
        ).fetchone()
        snapshot_id = int(row["id"])
        self.conn.execute(
            "UPDATE market_snapshots SET data_quality_json=? WHERE id=?",
            (json.dumps(snapshot.get("data_quality", _build_data_quality(snapshot)), ensure_ascii=False), snapshot_id),
        )
        self.link_module_results_to_snapshot(snapshot_id, snapshot["symbol"], int(snapshot["analysis_time_utc"]))
        return snapshot_id

    def link_module_results_to_snapshot(self, snapshot_id: int, symbol: str, analysis_time_utc: int) -> None:
        self.conn.execute(
            "UPDATE module_analysis_results SET snapshot_id=? WHERE symbol=? AND analysis_time=?",
            (int(snapshot_id), symbol, int(analysis_time_utc)),
        )

    def save_analysis_state(self, state: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO analysis_states(
                symbol, analysis_time, analysis_time_utc, analysis_mode, timeframes,
                market_structure_json, trend_clarity_json, no_trade_reason_json, key_levels_json,
                next_triggers_json, next_analysis_json, breakout_watch_json, trade_permission_json,
                trade_plan_json, opportunity_watch_recommended, paper_trade_allowed, state_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["symbol"],
                int(state["analysis_time"]),
                state["analysis_time_utc"],
                state.get("analysis_mode", "unknown"),
                json.dumps(state.get("timeframes", []), ensure_ascii=False),
                json.dumps(state.get("market_structure") or {}, ensure_ascii=False),
                json.dumps(state.get("trend_clarity") or {}, ensure_ascii=False),
                json.dumps(state.get("no_trade_reason") or {}, ensure_ascii=False),
                json.dumps(state.get("key_levels") or {}, ensure_ascii=False),
                json.dumps(state.get("next_triggers") or [], ensure_ascii=False),
                json.dumps(state.get("next_analysis") or {}, ensure_ascii=False),
                json.dumps(state.get("breakout_watch") or {}, ensure_ascii=False),
                json.dumps(state.get("trade_permission") or {}, ensure_ascii=False),
                json.dumps(state.get("trade_plan") or {}, ensure_ascii=False),
                1 if state.get("opportunity_watch_recommended") else 0,
                1 if (state.get("trade_permission") or {}).get("paper_trade_allowed") else 0,
                json.dumps(state, ensure_ascii=False),
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def attach_ga_decision_to_analysis_state(self, analysis_state_id: int, ga_decision_id: int) -> None:
        self.conn.execute(
            "UPDATE analysis_states SET ga_decision_id=? WHERE id=?",
            (int(ga_decision_id), int(analysis_state_id)),
        )

    def create_ga_decision(self, decision: dict[str, Any]) -> int:
        trade_plan = decision.get("trade_plan")
        opportunity_watch = decision.get("opportunity_watch")
        self.conn.execute(
            """
            INSERT INTO ga_decisions(
                symbol, analysis_time, analysis_time_utc, decision_type, signal_grade,
                confidence, market_bias, trend_stage, decision, skill_result_refs_json,
                evidence_json, counter_evidence_json, risk_check_json, trade_plan_json,
                opportunity_watch_json, feishu_actions_json, final_summary, raw_decision_json,
                analysis_state_id, snapshot_id, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision["symbol"],
                int(decision["analysis_time"]),
                decision["analysis_time_utc"],
                decision["decision_type"],
                decision["signal_grade"],
                float(decision.get("confidence") or 0),
                decision.get("market_bias"),
                decision.get("trend_stage"),
                decision["decision"],
                json.dumps(decision.get("skill_result_refs") or {}, ensure_ascii=False),
                json.dumps(decision.get("evidence") or [], ensure_ascii=False),
                json.dumps(decision.get("counter_evidence") or [], ensure_ascii=False),
                json.dumps(decision.get("risk_check") or {}, ensure_ascii=False),
                json.dumps(trade_plan, ensure_ascii=False) if trade_plan else None,
                json.dumps(opportunity_watch, ensure_ascii=False) if opportunity_watch else None,
                json.dumps(decision.get("feishu_actions") or [], ensure_ascii=False),
                decision.get("final_summary") or decision.get("summary") or "",
                json.dumps(decision, ensure_ascii=False),
                decision.get("analysis_state_id"),
                decision.get("snapshot_id"),
                decision.get("created_by", "ga_master_controller"),
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def get_ga_decision(self, ga_decision_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ga_decisions WHERE id=?", (int(ga_decision_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        for column, default in (
            ("skill_result_refs_json", {}),
            ("evidence_json", []),
            ("counter_evidence_json", []),
            ("risk_check_json", {}),
            ("trade_plan_json", None),
            ("opportunity_watch_json", None),
            ("feishu_actions_json", []),
            ("raw_decision_json", {}),
        ):
            key = column.removesuffix("_json")
            try:
                item[key] = json.loads(item.get(column) or ("null" if default is None else json.dumps(default)))
            except Exception:
                item[key] = default
        return item

    def latest_ga_decisions_by_symbol(self, limit: int = 80, *, min_analysis_time: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if min_analysis_time is not None:
            where = "WHERE analysis_time >= ?"
            params.append(int(min_analysis_time))
        rows = self.conn.execute(
            f"""
            SELECT gd.*
            FROM ga_decisions gd
            JOIN (
                SELECT symbol, MAX(analysis_time) AS max_time
                FROM ga_decisions
                {where}
                GROUP BY symbol
            ) latest ON latest.symbol=gd.symbol AND latest.max_time=gd.analysis_time
            ORDER BY gd.analysis_time DESC, gd.id DESC
            LIMIT ?
            """,
            params + [int(limit)],
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_skill_result_refs(self, symbol: str, analysis_time_utc: int) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT skill_name, MAX(id) AS id
            FROM skill_execution_logs
            WHERE symbol=? AND analysis_time=?
            GROUP BY skill_name
            """,
            (symbol, int(analysis_time_utc)),
        ).fetchall()
        return {str(r["skill_name"]): int(r["id"]) for r in rows}

    def record_parquet_archive_run(
        self,
        *,
        symbol: str,
        interval: str,
        year_month: str,
        path: str,
        rows_written: int,
        status: str,
        error_message: str | None = None,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO parquet_archive_runs(symbol, interval, year_month, path, rows_written, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, interval, year_month, path, int(rows_written), status, error_message),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def latest_parquet_archive_run(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM parquet_archive_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def latest_analysis_state(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM analysis_states
            WHERE symbol=?
            ORDER BY analysis_time DESC, id DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["state"] = json.loads(item.get("state_json") or "{}")
        except Exception:
            item["state"] = {}
        return item

    def latest_analysis_states(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM analysis_states ORDER BY analysis_time DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["state"] = json.loads(item.get("state_json") or "{}")
            except Exception:
                item["state"] = {}
            out.append(item)
        return out

    def save_skill_execution_log(
        self,
        *,
        skill_name: str,
        skill_version: str,
        symbol: str,
        timeframe: str,
        analysis_time: int,
        input_summary: dict[str, Any] | None,
        tool_result: dict[str, Any],
        ga_interpretation: dict[str, Any],
        final_result: dict[str, Any],
        confidence: float | None = None,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO skill_execution_logs(
                skill_name, skill_version, symbol, timeframe, analysis_time,
                input_summary_json, tool_result_json, ga_interpretation_json, final_result_json, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_name,
                skill_version,
                symbol,
                timeframe,
                int(analysis_time),
                json.dumps(input_summary or {}, ensure_ascii=False),
                json.dumps(tool_result, ensure_ascii=False),
                json.dumps(ga_interpretation, ensure_ascii=False),
                json.dumps(final_result, ensure_ascii=False),
                confidence,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def save_skill_feedback_memory(
        self,
        *,
        skill_name: str,
        skill_version: str = "1.0",
        feedback_type: str,
        source_type: str,
        finding: str,
        source_id: int | None = None,
        pattern_type: str | None = None,
        affected_symbols: list[str] | None = None,
        affected_sides: list[str] | None = None,
        suggested_adjustment: dict[str, Any] | None = None,
        status: str = "candidate",
    ) -> int:
        # Dedup: skip auto_analysis if same (skill_name, finding) written in last 24h
        if feedback_type == "auto_analysis":
            existing = self.conn.execute(
                """
                SELECT id FROM skill_feedback_memory
                WHERE skill_name=? AND feedback_type=? AND finding=? AND status='candidate'
                  AND created_at > datetime('now', '-1 day')
                LIMIT 1
                """,
                (skill_name, finding),
            ).fetchone()
            if existing:
                return int(existing["id"])

        self.conn.execute(
            """
            INSERT INTO skill_feedback_memory(
                skill_name, skill_version, feedback_type, source_type, source_id,
                pattern_type, affected_symbols, affected_sides,
                finding, suggested_adjustment_json, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_name,
                skill_version,
                feedback_type,
                source_type,
                source_id,
                pattern_type,
                json.dumps(affected_symbols or [], ensure_ascii=False),
                json.dumps(affected_sides or [], ensure_ascii=False),
                finding,
                json.dumps(suggested_adjustment or {}, ensure_ascii=False),
                status,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def create_signal(self, decision: dict[str, Any], snapshot_id: int | None = None, *, ga_decision_id: int | None = None) -> int:
        trade_plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
        watch = decision.get("opportunity_watch")
        from plugins.crypto_guard.notify.signal_policy import alert_level_for_grade

        self.conn.execute(
            """
            INSERT INTO signals(
                symbol, timeframe, direction, trend_stage, confidence, score, signal_grade, alert_level,
                decision, market_snapshot_id, trade_plan_json, opportunity_watch_json, ga_reason, risk_notes,
                ga_decision_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision["symbol"],
                decision.get("timeframe"),
                (trade_plan or {}).get("side") or decision.get("market_bias"),
                decision.get("trend_stage"),
                decision.get("confidence"),
                decision.get("confidence"),
                decision.get("signal_grade"),
                alert_level_for_grade(decision.get("signal_grade")),
                decision.get("decision"),
                snapshot_id,
                json.dumps(trade_plan, ensure_ascii=False) if trade_plan else None,
                json.dumps(watch, ensure_ascii=False) if watch else None,
                decision.get("summary"),
                json.dumps(decision.get("risk_notes", []), ensure_ascii=False),
                ga_decision_id or decision.get("ga_decision_id"),
            ),
        )
        signal_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        self.conn.execute(
            "UPDATE signals SET snapshot_id=?, ga_decision_json=?, ga_decision_id=? WHERE id=?",
            (snapshot_id, json.dumps(decision, ensure_ascii=False), ga_decision_id or decision.get("ga_decision_id"), signal_id),
        )
        if snapshot_id:
            self.save_strategy_evaluation(decision, snapshot_id)
        return signal_id

    def save_strategy_evaluation(self, decision: dict[str, Any], snapshot_id: int | None = None, *, is_shadow: bool = False) -> int:
        self.conn.execute(
            """
            INSERT INTO strategy_evaluations(
                snapshot_id, symbol, timeframe, analysis_time, strategy_name, strategy_version,
                score, decision, evidence_json, counter_evidence_json, is_shadow
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                decision["symbol"],
                decision.get("timeframe") or "15m",
                int(decision.get("analysis_time_utc") or 0),
                decision.get("strategy_name", "deterministic_sop"),
                decision.get("strategy_version", "1.0"),
                float(decision.get("confidence") or 0),
                decision.get("decision"),
                json.dumps(decision.get("evidence", []), ensure_ascii=False),
                json.dumps(decision.get("counter_evidence", []), ensure_ascii=False),
                1 if is_shadow else 0,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM signals WHERE id=?", (int(signal_id),)).fetchone()
        return dict(row) if row else None

    def save_ad_hoc_analysis(self, symbol: str, requested_by: str | None, request_text: str, result: dict[str, Any], signal_id: int | None) -> int:
        self.conn.execute(
            """
            INSERT INTO ad_hoc_analyses(symbol, requested_by, request_text, timeframes, analysis_result_json, ga_summary, has_trade_plan, signal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                requested_by,
                request_text,
                json.dumps(result.get("timeframes", []), ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                result.get("summary"),
                1 if result.get("has_trade_plan") else 0,
                signal_id,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def mark_ad_hoc_analysis_status_by_signal(self, signal_id: int, status: str) -> bool:
        cur = self.conn.execute(
            "UPDATE ad_hoc_analyses SET status=? WHERE signal_id=?",
            (status, int(signal_id)),
        )
        return cur.rowcount > 0

    def create_opportunity_watch(
        self,
        symbol: str,
        watch: dict[str, Any],
        source_signal_id: int | None = None,
        expires_at: str | None = None,
        *,
        ga_decision_id: int | None = None,
        created_by_user_action: bool = False,
        source_button_action: str | None = None,
    ) -> int:
        if expires_at is None and watch.get("expires_minutes"):
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(minutes=int(watch.get("expires_minutes") or 0))
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conditions = watch.get("conditions", [])
        if isinstance(conditions, dict):
            conditions = [conditions]
        self.conn.execute(
            """
            INSERT INTO opportunity_watches(
                symbol, direction, watch_reason, watch_condition_json, invalid_condition_json,
                source_signal_id, expires_at, ga_decision_id, created_by_user_action, source_button_action
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                watch.get("direction"),
                watch.get("reason"),
                json.dumps(conditions, ensure_ascii=False),
                json.dumps(watch.get("invalid_condition"), ensure_ascii=False),
                source_signal_id,
                expires_at,
                ga_decision_id,
                1 if created_by_user_action else 0,
                source_button_action,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def get_opportunity_watch(self, watch_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM opportunity_watches WHERE id=?", (int(watch_id),)).fetchone()
        return dict(row) if row else None

    def list_active_opportunity_watches(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM opportunity_watches WHERE status='active' ORDER BY created_at ASC, id ASC"
            ).fetchall()
        ]

    def list_active_opportunity_watches_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM opportunity_watches WHERE status='active' AND symbol=? ORDER BY created_at ASC, id ASC",
                (symbol,),
            ).fetchall()
        ]

    def update_opportunity_watch_status(
        self,
        watch_id: int,
        status: str,
        *,
        triggered_at: str | None = None,
        invalidated_reason: str | None = None,
    ) -> bool:
        cur = self.conn.execute(
            """
            UPDATE opportunity_watches
            SET status=?,
                triggered_at=COALESCE(?, triggered_at),
                invalidated_reason=COALESCE(?, invalidated_reason),
                last_checked_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='active'
            """,
            (status, triggered_at, invalidated_reason, int(watch_id)),
        )
        return cur.rowcount == 1

    def touch_opportunity_watch(self, watch_id: int) -> None:
        self.conn.execute(
            "UPDATE opportunity_watches SET last_checked_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(watch_id),),
        )

    def enqueue_job(self, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any], scheduled_at: str | None = None) -> int:
        self.conn.execute(
            """
            INSERT INTO agent_jobs(job_type, priority, source, session_id, payload_json, scheduled_at)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (job_type, int(priority), source, session_id, json.dumps(payload, ensure_ascii=False), scheduled_at),
        )
        job_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        self._enqueue_job_redis(job_id, job_type, priority, source, session_id, payload)
        return job_id

    def enqueue_job_once(self, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any], scheduled_at: str | None = None) -> int:
        """Enqueue a job with idempotency: if a job with the same (job_type, session_id)
        already exists and is pending/running/success, return the existing id.
        If it's failed/cancelled/duplicate, reset to pending and return the existing id.
        Otherwise insert a new job.
        """
        existing = self.conn.execute(
            "SELECT id, status FROM agent_jobs WHERE job_type=? AND session_id=?",
            (job_type, session_id),
        ).fetchone()
        if existing:
            existing_id = int(existing["id"])
            status = existing["status"]
            if status in ("pending", "running", "success"):
                return existing_id
            # Reset failed/cancelled/duplicate to pending
            self.conn.execute(
                "UPDATE agent_jobs SET status='pending', priority=?, source=?, payload_json=?, started_at=NULL, error_message=NULL, finished_at=NULL, scheduled_at=COALESCE(?, CURRENT_TIMESTAMP) WHERE id=?",
                (int(priority), source, json.dumps(payload, ensure_ascii=False), scheduled_at, existing_id),
            )
            self._enqueue_job_redis(existing_id, job_type, priority, source, session_id, payload)
            return existing_id
        # No existing job — insert new
        try:
            self.conn.execute(
                """
                INSERT INTO agent_jobs(job_type, priority, source, session_id, payload_json, scheduled_at)
                VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (job_type, int(priority), source, session_id, json.dumps(payload, ensure_ascii=False), scheduled_at),
            )
        except sqlite3.IntegrityError:
            # Race condition: another process inserted between our SELECT and INSERT
            existing = self.conn.execute(
                "SELECT id FROM agent_jobs WHERE job_type=? AND session_id=?",
                (job_type, session_id),
            ).fetchone()
            if existing:
                return int(existing["id"])
            raise
        job_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        self._enqueue_job_redis(job_id, job_type, priority, source, session_id, payload)
        return job_id

    def _enqueue_job_redis(self, job_id: int, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any]) -> None:
        try:
            db_row = self.conn.execute("PRAGMA database_list").fetchone()
            database_path = db_row["file"] if db_row and "file" in db_row.keys() else None
            from plugins.crypto_guard.storage.redis_adapter import RedisAdapter, should_use_redis_for_path

            if not should_use_redis_for_path(database_path):
                return
            redis = RedisAdapter()
            redis_payload = {
                "sqlite_job_id": job_id,
                "database_path": database_path,
                "job_type": job_type,
                "priority": int(priority),
                "source": source,
                "session_id": session_id,
                "payload": payload,
            }
            if int(priority) <= 2:
                redis.enqueue_user_job(redis_payload)
            else:
                redis.enqueue_background_job(redis_payload)
        except Exception:
            pass

    def has_pending_user_jobs(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM agent_jobs WHERE status='pending' AND priority <= 2 LIMIT 1"
        ).fetchone()
        return bool(row)

    def claim_next_job(self, *, max_priority: int | None = None, background: bool = False) -> dict[str, Any] | None:
        if background and self.has_pending_user_jobs():
            return None
        where = "status='pending' AND datetime(scheduled_at) <= datetime('now')"
        params: list[Any] = []
        if max_priority is not None:
            where += " AND priority <= ?"
            params.append(int(max_priority))
        row = self.conn.execute(
            f"SELECT * FROM agent_jobs WHERE {where} ORDER BY priority ASC, scheduled_at ASC, id ASC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        cur = self.conn.execute(
            "UPDATE agent_jobs SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (int(row["id"]),),
        )
        if cur.rowcount != 1:
            return None
        return dict(row)

    def recover_stale_running_jobs(self, *, older_than_minutes: int = 30) -> int:
        cur = self.conn.execute(
            """
            UPDATE agent_jobs
            SET status='pending',
                started_at=NULL,
                error_message=COALESCE(error_message, 'recovered stale running job after process restart')
            WHERE status='running'
              AND datetime(started_at) <= datetime('now', ?)
            """,
            (f"-{int(older_than_minutes)} minutes",),
        )
        return int(cur.rowcount)

    def finish_job(self, job_id: int, *, result: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        status = "failed" if error_message else "success"
        self.conn.execute(
            """
            UPDATE agent_jobs
            SET status=?, finished_at=CURRENT_TIMESTAMP, error_message=?, result_json=?
            WHERE id=?
            """,
            (status, error_message, json.dumps(result or {}, ensure_ascii=False), int(job_id)),
        )

    def claim_feishu_event(self, event_id: str, event_type: str, payload: dict[str, Any] | None = None) -> bool:
        if not event_id:
            return True
        try:
            self.conn.execute(
                """
                INSERT INTO feishu_events(event_id, event_type, payload_json)
                VALUES (?, ?, ?)
                """,
                (event_id, event_type, json.dumps(payload or {}, ensure_ascii=False)),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_recent_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT 'agent_job' AS source, id, job_type AS name, session_id, error_message, finished_at AS ts
            FROM agent_jobs
            WHERE status='failed' OR error_message IS NOT NULL
            UNION ALL
            SELECT 'scheduler_run' AS source, id, job_name AS name, CAST(scheduled_time AS TEXT) AS session_id, error_message, finished_at AS ts
            FROM scheduler_runs
            WHERE status='failed' OR error_message IS NOT NULL
            ORDER BY ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_feishu_target(self) -> dict[str, Any] | None:
        # Primary: look in agent_jobs with source='feishu'
        rows = self.conn.execute(
            """
            SELECT payload_json FROM agent_jobs
            WHERE source='feishu'
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                continue
            receive_id = payload.get("receive_id")
            if receive_id:
                return {
                    "receive_id": receive_id,
                    "receive_id_type": payload.get("receive_id_type", "open_id"),
                    "open_id": payload.get("open_id"),
                }
        # Fallback: look in feishu_events table (user messages via Feishu webhook)
        try:
            rows = self.conn.execute(
                """
                SELECT payload_json FROM feishu_events
                WHERE event_type='message'
                ORDER BY rowid DESC
                LIMIT 10
                """
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    continue
                receive_id = payload.get("receive_id")
                if receive_id:
                    return {
                        "receive_id": receive_id,
                        "receive_id_type": payload.get("receive_id_type", "chat_id"),
                        "open_id": payload.get("open_id"),
                    }
        except Exception:
            pass
        return None

    def latest_signals_by_symbol(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT s.*
                FROM signals s
                INNER JOIN (
                    SELECT symbol, MAX(id) AS max_id
                    FROM signals
                    GROUP BY symbol
                ) latest ON latest.max_id = s.id
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        ]

    def recent_failed_jobs(self, limit: int = 5) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT id, job_type, priority, session_id, error_message, finished_at
                FROM agent_jobs
                WHERE status='failed'
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        ]

    def scheduler_success_exists(self, job_name: str, scheduled_time: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE job_name=? AND scheduled_time=? AND status='success'",
            (job_name, int(scheduled_time)),
        ).fetchone()
        return bool(row)

    def create_scheduler_run(self, job_name: str, scheduled_time: int) -> int:
        self.conn.execute(
            """
            INSERT INTO scheduler_runs(job_name, scheduled_time, started_at, status)
            VALUES (?, ?, CURRENT_TIMESTAMP, 'running')
            ON CONFLICT(job_name, scheduled_time) DO UPDATE SET
                started_at=CURRENT_TIMESTAMP,
                status='running',
                error_message=NULL
            """,
            (job_name, int(scheduled_time)),
        )
        row = self.conn.execute(
            "SELECT id FROM scheduler_runs WHERE job_name=? AND scheduled_time=?",
            (job_name, int(scheduled_time)),
        ).fetchone()
        return int(row["id"])

    def finish_scheduler_run(self, run_id: int, *, status: str, result: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE scheduler_runs
            SET status=?, finished_at=CURRENT_TIMESTAMP, result_json=?, error_message=?
            WHERE id=?
            """,
            (status, json.dumps(result or {}, ensure_ascii=False), error_message, int(run_id)),
        )

    def acquire_lock(self, lock_name: str, owner: str, ttl_seconds: int) -> bool:
        self.conn.execute(
            "DELETE FROM task_locks WHERE lock_name=? AND datetime(locked_until) <= datetime('now')",
            (lock_name,),
        )
        locked_until = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + ttl_seconds, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        try:
            self.conn.execute(
                """
                INSERT INTO task_locks(lock_name, owner, locked_until, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (lock_name, owner, locked_until),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_lock(self, lock_name: str, owner: str | None = None) -> None:
        if owner:
            self.conn.execute("DELETE FROM task_locks WHERE lock_name=? AND owner=?", (lock_name, owner))
        else:
            self.conn.execute("DELETE FROM task_locks WHERE lock_name=?", (lock_name,))

    def create_paper_order(
        self,
        signal_id: int | None,
        signal: dict[str, Any],
        trade_plan: dict[str, Any],
        *,
        ga_decision_id: int | None = None,
        source: str = "signal_compat",
        risk_check_passed: bool = False,
    ) -> tuple[int, bool]:
        from plugins.crypto_guard.paper.pending_order_manager import compute_expires_at

        expires_at = compute_expires_at(trade_plan.get("entry_type"))
        try:
            self.conn.execute(
                """
                INSERT INTO paper_orders(
                    signal_id, ga_decision_id, symbol, side, order_type, entry_price, trigger_price,
                    stop_loss, take_profit_json, quantity, risk_percent, reason, fill_method, source, risk_check_passed,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(signal_id) if signal_id is not None else None,
                    ga_decision_id,
                    signal["symbol"],
                    trade_plan["side"],
                    trade_plan["entry_type"],
                    trade_plan.get("entry_price"),
                    trade_plan.get("trigger_price"),
                    trade_plan["stop_loss"],
                    json.dumps(trade_plan.get("take_profits", []), ensure_ascii=False),
                    trade_plan.get("quantity"),
                    trade_plan.get("risk_percent"),
                    trade_plan.get("reason"),
                    trade_plan.get("fill_method"),
                    source,
                    1 if risk_check_passed else 0,
                    expires_at,
                ),
            )
            return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]), True
        except sqlite3.IntegrityError:
            if ga_decision_id is not None:
                row = self.conn.execute("SELECT id FROM paper_orders WHERE ga_decision_id=?", (int(ga_decision_id),)).fetchone()
                if row:
                    return int(row["id"]), False
            row = self.conn.execute("SELECT id FROM paper_orders WHERE signal_id=?", (int(signal_id),)).fetchone()
            return int(row["id"]), False

    def list_open_paper_orders(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM paper_orders WHERE status IN ('pending','open') ORDER BY id").fetchall()]

    def list_open_paper_orders_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM paper_orders WHERE status IN ('pending','open') AND symbol=? ORDER BY id",
                (symbol,),
            ).fetchall()
        ]

    def update_paper_order_status(self, order_id: int, status: str, *, filled_at: str | None = None, closed_at: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE paper_orders
            SET status=?, filled_at=COALESCE(?, filled_at), closed_at=COALESCE(?, closed_at)
            WHERE id=?
            """,
            (status, filled_at, closed_at, int(order_id)),
        )

    def update_paper_order_stop_loss(self, order_id: int, stop_loss: float, *, reason: str) -> None:
        row = self.conn.execute("SELECT * FROM paper_orders WHERE id=?", (int(order_id),)).fetchone()
        self.conn.execute("UPDATE paper_orders SET stop_loss=? WHERE id=?", (float(stop_loss), int(order_id)))
        if row:
            self.log_paper_trade_event(
                event_type="stop_loss_adjustment",
                symbol=row["symbol"],
                side=row["side"],
                price=float(stop_loss),
                quantity=row["quantity"],
                reason=reason,
                event={"order_id": int(order_id), "old_stop_loss": row["stop_loss"], "new_stop_loss": float(stop_loss)},
            )

    def create_paper_trade(self, order: dict[str, Any], entry_price: float, *, fill_method: str | None = None) -> int:
        # Guard: one order can only have one open trade
        existing = self.conn.execute(
            "SELECT id FROM paper_trades WHERE order_id=? AND closed_at IS NULL LIMIT 1",
            (int(order["id"]),),
        ).fetchone()
        if existing:
            return int(existing["id"])
        signal_id = order.get("signal_id")
        market_snapshot_id = None
        if signal_id:
            signal = self.get_signal(int(signal_id))
            market_snapshot_id = signal.get("market_snapshot_id") or signal.get("snapshot_id") if signal else None
        self.conn.execute(
            """
            INSERT INTO paper_trades(
                order_id, signal_id, market_snapshot_id, symbol, side, entry_price, stop_loss,
                take_profit_json, quantity, max_favorable_excursion, max_adverse_excursion,
                entry_efficiency, exit_efficiency, signal_decay_score, stop_take_path_json, fill_method
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, NULL, 0, ?, ?)
            """,
            (
                order["id"],
                signal_id,
                market_snapshot_id,
                order["symbol"],
                order["side"],
                entry_price,
                order.get("stop_loss"),
                order.get("take_profit_json"),
                order.get("quantity"),
                json.dumps([{"event": "filled", "entry_price": entry_price, "ts": utc_iso()}], ensure_ascii=False),
                fill_method or order.get("fill_method"),
            ),
        )
        trade_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        account = self.ensure_paper_account()
        position_id = self.upsert_paper_position_from_trade(
            account_id=int(account["id"]),
            trade={**order, "id": trade_id, "entry_price": entry_price, "current_price": entry_price},
            status="open",
            current_price=float(entry_price),
        )
        self.log_paper_trade_event(
            position_id=position_id,
            event_type="open_position",
            symbol=order["symbol"],
            side=order["side"],
            price=float(entry_price),
            quantity=order.get("quantity"),
            reason=fill_method or order.get("fill_method") or "filled",
            event={"order_id": order["id"], "trade_id": trade_id, "fill_method": fill_method or order.get("fill_method")},
        )
        return trade_id

    def get_open_trade_for_order(self, order_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM paper_trades WHERE order_id=? AND closed_at IS NULL", (int(order_id),)).fetchone()
        return dict(row) if row else None

    def update_paper_trade_quality(self, trade_id: int, *, mfe: float, mae: float, stop_take_path: list[dict[str, Any]]) -> None:
        self.conn.execute(
            """
            UPDATE paper_trades
            SET max_favorable_excursion=?,
                max_adverse_excursion=?,
                stop_take_path_json=?
            WHERE id=? AND closed_at IS NULL
            """,
            (float(mfe), float(mae), json.dumps(stop_take_path, ensure_ascii=False), int(trade_id)),
        )

    def close_paper_trade(
        self,
        trade_id: int,
        *,
        exit_price: float,
        close_reason: str,
        pnl: float,
        pnl_percent: float,
        pnl_r: float,
        mfe: float,
        mae: float,
        entry_efficiency: float | None = None,
        exit_efficiency: float | None = None,
        signal_decay_score: float | None = None,
        stop_take_path: list[dict[str, Any]] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE paper_trades
            SET exit_price=?, close_reason=?, pnl=?, pnl_percent=?, pnl_r=?,
                max_favorable_excursion=?, max_adverse_excursion=?,
                entry_efficiency=?, exit_efficiency=?, signal_decay_score=?,
                stop_take_path_json=COALESCE(?, stop_take_path_json),
                closed_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                exit_price,
                close_reason,
                pnl,
                pnl_percent,
                pnl_r,
                mfe,
                mae,
                entry_efficiency,
                exit_efficiency,
                signal_decay_score,
                json.dumps(stop_take_path, ensure_ascii=False) if stop_take_path is not None else None,
                int(trade_id),
            ),
        )

    def backfill_shadow_evaluation_pnl_r(self, trade: dict[str, Any], pnl_r: float) -> int:
        """Backfill real pnl_r to shadow strategy_evaluations linked to this trade.

        When a paper trade closes, find all shadow candidate evaluations (is_shadow=1)
        for the same strategy_name, symbol, and nearby analysis_time, and backfill
        the real pnl_r.

        Returns number of evaluation rows updated.
        """
        order_id = trade.get("order_id")
        if not order_id:
            return 0

        order = self.conn.execute(
            "SELECT ga_decision_id, symbol FROM paper_orders WHERE id=?",
            (int(order_id),),
        ).fetchone()
        if not order:
            return 0

        # Get analysis_time and strategy_name from ga_decision
        analysis_time = None
        strategy_name = None
        if order["ga_decision_id"]:
            gd = self.conn.execute(
                "SELECT analysis_time, raw_decision_json FROM ga_decisions WHERE id=?",
                (int(order["ga_decision_id"]),),
            ).fetchone()
            if gd:
                try:
                    analysis_time = int(gd["analysis_time"])
                except (ValueError, TypeError):
                    pass
                # Extract strategy_name from raw_decision_json
                # real data: raw_decision_json.raw_legacy_decision.strategy_name
                try:
                    raw = json.loads(gd["raw_decision_json"] or "{}")
                    strategy_name = _extract_strategy_name(raw)
                except (json.JSONDecodeError, TypeError):
                    pass

        if analysis_time is None:
            return 0

        # Backfill pnl_r to candidate shadow evaluations for the same strategy + symbol + time window
        if strategy_name:
            self.conn.execute(
                """
                UPDATE strategy_evaluations
                SET pnl_r=?
                WHERE symbol=? AND strategy_name=? AND is_shadow=1 AND pnl_r IS NULL
                  AND ABS(analysis_time - ?) < 3600000
                """,
                (float(pnl_r), order["symbol"], strategy_name, analysis_time),
            )
        else:
            # Fallback: match by symbol + time only (no strategy_name in ga_decision)
            self.conn.execute(
                """
                UPDATE strategy_evaluations
                SET pnl_r=?
                WHERE symbol=? AND is_shadow=1 AND pnl_r IS NULL
                  AND ABS(analysis_time - ?) < 3600000
                """,
                (float(pnl_r), order["symbol"], analysis_time),
            )
        updated = int(self.conn.execute("SELECT changes() AS c").fetchone()["c"])
        if updated:
            self.conn.commit()
        return updated

    def backfill_historical_shadow_pnl_r(self) -> dict[str, int]:
        """One-shot: backfill pnl_r from all closed paper_trades to shadow evaluations.

        Iterates closed trades with real pnl_r, traces to ga_decision for
        strategy_name + analysis_time, and backfills matching shadow evals.

        Returns {trades_processed, evaluations_updated}.
        """
        closed_trades = self.conn.execute(
            """
            SELECT pt.id, pt.order_id, pt.pnl_r
            FROM paper_trades pt
            WHERE pt.closed_at IS NOT NULL
              AND pt.pnl_r IS NOT NULL
              AND (pt.close_reason IS NULL OR pt.close_reason != 'duplicate_cleanup')
            """
        ).fetchall()

        trades_processed = 0
        total_updated = 0

        for trade_row in closed_trades:
            updated = self.backfill_shadow_evaluation_pnl_r(
                {"order_id": trade_row["order_id"]},
                float(trade_row["pnl_r"]),
            )
            if updated > 0:
                trades_processed += 1
                total_updated += updated

        return {"trades_processed": trades_processed, "evaluations_updated": total_updated}

    def save_equity_snapshot(self, snapshot: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO paper_equity_snapshots(ts, account_equity, unrealized_pnl, realized_pnl, margin_used, open_position_count, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(snapshot["ts"]),
                float(snapshot["account_equity"]),
                float(snapshot.get("unrealized_pnl", 0)),
                float(snapshot.get("realized_pnl", 0)),
                snapshot.get("margin_used"),
                int(snapshot.get("open_position_count", 0)),
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def ensure_paper_account(self, account_name: str = "default", initial_balance: float = 10000.0) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO paper_accounts(account_name, initial_balance, current_balance, equity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_name) DO NOTHING
            """,
            (account_name, float(initial_balance), float(initial_balance), float(initial_balance)),
        )
        return dict(self.conn.execute("SELECT * FROM paper_accounts WHERE account_name=?", (account_name,)).fetchone())

    def update_paper_account_from_snapshot(self, snapshot: dict[str, Any], account_name: str = "default") -> dict[str, Any]:
        account = self.ensure_paper_account(account_name)
        equity = float(snapshot.get("account_equity") or account["equity"])
        initial = float(account["initial_balance"] or 10000.0)
        drawdown = min(float(account.get("max_drawdown") or 0), (equity - initial) / initial if initial else 0)
        self.conn.execute(
            """
            UPDATE paper_accounts
            SET current_balance=?, equity=?, realized_pnl=?, unrealized_pnl=?, max_drawdown=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                equity,
                equity,
                float(snapshot.get("realized_pnl") or 0),
                float(snapshot.get("unrealized_pnl") or 0),
                drawdown,
                int(account["id"]),
            ),
        )
        return dict(self.conn.execute("SELECT * FROM paper_accounts WHERE id=?", (int(account["id"]),)).fetchone())

    def upsert_paper_position_from_trade(
        self,
        *,
        account_id: int,
        trade: dict[str, Any],
        status: str = "open",
        current_price: float | None = None,
        unrealized_pnl: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
    ) -> int:
        position_id = int(trade.get("id") or 0)
        row = self.conn.execute("SELECT id FROM paper_positions WHERE id=?", (position_id,)).fetchone() if position_id else None
        if row:
            self.conn.execute(
                """
                UPDATE paper_positions
                SET current_price=?, stop_loss=?, take_profit_json=?, unrealized_pnl=?, unrealized_pnl_pct=?,
                    max_favorable_excursion=?, max_adverse_excursion=?, status=?,
                    closed_at=CASE WHEN ?!='open' THEN CURRENT_TIMESTAMP ELSE closed_at END
                WHERE id=?
                """,
                (
                    current_price,
                    trade.get("stop_loss"),
                    trade.get("take_profit_json"),
                    float(unrealized_pnl),
                    float(unrealized_pnl_pct),
                    float(trade.get("max_favorable_excursion") or 0),
                    float(trade.get("max_adverse_excursion") or 0),
                    status,
                    status,
                    position_id,
                ),
            )
            return position_id
        self.conn.execute(
            """
            INSERT INTO paper_positions(
                id, account_id, symbol, side, entry_price, current_price, quantity, stop_loss, take_profit_json,
                unrealized_pnl, unrealized_pnl_pct, max_favorable_excursion, max_adverse_excursion, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id or None,
                int(account_id),
                trade["symbol"],
                trade["side"],
                float(trade["entry_price"]),
                current_price if current_price is not None else trade.get("current_price"),
                float(trade.get("quantity") or 1),
                trade.get("stop_loss"),
                trade.get("take_profit_json"),
                float(unrealized_pnl),
                float(unrealized_pnl_pct),
                float(trade.get("max_favorable_excursion") or 0),
                float(trade.get("max_adverse_excursion") or 0),
                status,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def log_paper_trade_event(
        self,
        *,
        event_type: str,
        symbol: str,
        side: str | None = None,
        price: float | None = None,
        quantity: float | None = None,
        pnl: float | None = None,
        pnl_pct: float | None = None,
        reason: str | None = None,
        event: dict[str, Any] | None = None,
        position_id: int | None = None,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO paper_trade_logs(position_id, event_type, symbol, side, price, quantity, pnl, pnl_pct, reason, event_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id,
                event_type,
                symbol,
                side,
                price,
                quantity,
                pnl,
                pnl_pct,
                reason,
                json.dumps(event or {}, ensure_ascii=False),
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def sum_closed_realized_pnl(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(pnl), 0) AS total FROM paper_trades WHERE closed_at IS NOT NULL").fetchone()
        return float(row["total"] or 0)

    def list_open_paper_trades(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM paper_trades WHERE closed_at IS NULL ORDER BY id").fetchall()]

    def latest_equity_snapshot(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM paper_equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM paper_trades WHERE id=?", (int(trade_id),)).fetchone()
        return dict(row) if row else None

    def get_market_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM market_snapshots WHERE id=?", (int(snapshot_id),)).fetchone()
        return dict(row) if row else None

    def get_trade_review_by_trade(self, trade_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trade_reviews WHERE trade_id=? ORDER BY id DESC LIMIT 1", (int(trade_id),)).fetchone()
        return dict(row) if row else None

    def list_closed_trades_for_review(self, *, start_utc: str | None = None, end_utc: str | None = None, only_unreviewed: bool = True) -> list[dict[str, Any]]:
        where = ["t.closed_at IS NOT NULL"]
        params: list[Any] = []
        if start_utc:
            where.append("datetime(t.closed_at) >= datetime(?)")
            params.append(start_utc)
        if end_utc:
            where.append("datetime(t.closed_at) < datetime(?)")
            params.append(end_utc)
        if only_unreviewed:
            where.append("r.id IS NULL")
        sql = f"""
            SELECT t.*
            FROM paper_trades t
            LEFT JOIN trade_reviews r ON r.trade_id = t.id
            WHERE {' AND '.join(where)}
            ORDER BY t.closed_at ASC, t.id ASC
        """
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def save_trade_review(self, trade_id: int, review: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO trade_reviews(
                trade_id, result, primary_reason, secondary_reasons_json, market_context,
                improvement_suggestion, ga_review_json, market_regime_at_loss, evolution_trigger_allowed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(trade_id),
                review["result"],
                review["primary_reason"],
                json.dumps(review.get("secondary_reasons", []), ensure_ascii=False),
                review.get("summary"),
                json.dumps(review.get("improvement_suggestion", {}), ensure_ascii=False),
                json.dumps(review, ensure_ascii=False),
                review.get("market_regime_at_loss"),
                1 if review.get("evolution_trigger_allowed", True) else 0,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def save_daily_review_report(
        self,
        *,
        review_date: str,
        summary: dict[str, Any],
        ga_report: str,
        skill_updates: list[dict[str, Any]] | None = None,
        evolution_actions: dict[str, Any] | None = None,
        pushed_to_feishu: bool = False,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO daily_review_reports(review_date, summary_json, ga_report, skill_updates_json, evolution_actions_json, pushed_to_feishu)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_date) DO UPDATE SET
                summary_json=excluded.summary_json,
                ga_report=excluded.ga_report,
                skill_updates_json=excluded.skill_updates_json,
                evolution_actions_json=excluded.evolution_actions_json,
                pushed_to_feishu=excluded.pushed_to_feishu
            """,
            (
                review_date,
                json.dumps(summary, ensure_ascii=False),
                ga_report,
                json.dumps(skill_updates or [], ensure_ascii=False),
                json.dumps(evolution_actions or {}, ensure_ascii=False),
                1 if pushed_to_feishu else 0,
            ),
        )
        row = self.conn.execute("SELECT id FROM daily_review_reports WHERE review_date=?", (review_date,)).fetchone()
        return int(row["id"])

    def create_evolution_trigger(
        self,
        *,
        trigger_type: str,
        trigger_value: float,
        threshold_value: float,
        related_trade_ids: list[int] | None = None,
        strategy_name: str | None = None,
        symbol: str | None = None,
        market_regime: str | None = None,
        evolution_allowed: bool = True,
        status: str = "pending",
    ) -> int:
        existing = self.conn.execute(
            """
            SELECT id FROM evolution_triggers
            WHERE trigger_type=? AND status IN ('pending','shadow_testing') AND COALESCE(symbol,'')=COALESCE(?, '')
            ORDER BY id DESC LIMIT 1
            """,
            (trigger_type, symbol),
        ).fetchone()
        if existing:
            return int(existing["id"])
        self.conn.execute(
            """
            INSERT INTO evolution_triggers(
                trigger_type, strategy_name, symbol, trigger_value, threshold_value, related_trade_ids,
                market_regime, evolution_allowed, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger_type,
                strategy_name,
                symbol,
                float(trigger_value),
                float(threshold_value),
                json.dumps(related_trade_ids or [], ensure_ascii=False),
                market_regime,
                1 if evolution_allowed else 0,
                status,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def recent_closed_trades(self, limit: int = 10, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "WHERE closed_at IS NOT NULL"
        if symbol:
            where += " AND symbol=?"
            params.append(symbol)
        params.append(int(limit))
        return [
            dict(r)
            for r in self.conn.execute(
                f"SELECT * FROM paper_trades {where} ORDER BY closed_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        ]

    def enqueue_alert(
        self,
        *,
        alert_type: str,
        payload: dict[str, Any],
        symbol: str | None = None,
        priority: int = 5,
        dedupe_key: str | None = None,
    ) -> int:
        # Validation for evolution_review: must be interactive card with valid JSON
        if alert_type == "evolution_review":
            if payload.get("msg_type") != "interactive":
                raise ValueError(
                    f"evolution_review must use msg_type='interactive', got '{payload.get('msg_type')}'"
                )
            content_str = payload.get("content")
            if not content_str:
                raise ValueError("evolution_review content must not be empty")
            try:
                card = json.loads(content_str)
                if not isinstance(card, dict) or "body" not in card:
                    raise ValueError("evolution_review content must be a valid card JSON with 'body'")
                elements = card.get("body", {}).get("elements")
                if not isinstance(elements, list):
                    raise ValueError("evolution_review content must have body.elements as a list")
                has_button = any(e.get("tag") == "button" for e in elements if isinstance(e, dict))
                if not has_button:
                    raise ValueError("evolution_review content must contain at least one button element")
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError(f"evolution_review content must be valid JSON: {e}") from e

        self.conn.execute(
            """
            INSERT INTO alert_outbox(alert_type, symbol, priority, payload_json, next_retry_at, dedupe_key)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """,
            (alert_type, symbol, int(priority), json.dumps(payload, ensure_ascii=False), dedupe_key),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def should_silence_alert(self, *, alert_type: str, symbol: str | None, quiet_minutes: int, never_silence: set[str]) -> bool:
        if alert_type in never_silence:
            return False
        row = self.conn.execute(
            """
            SELECT 1 FROM alert_outbox
            WHERE alert_type=? AND COALESCE(symbol, '')=COALESCE(?, '')
              AND status IN ('pending', 'sent')
              AND datetime(created_at) >= datetime('now', ?)
            LIMIT 1
            """,
            (alert_type, symbol, f"-{int(quiet_minutes)} minutes"),
        ).fetchone()
        return bool(row)

    def mark_alert_sent(self, alert_id: int) -> None:
        self.conn.execute(
            "UPDATE alert_outbox SET status='sent', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(alert_id),),
        )

    def mark_alert_failed(self, alert_id: int, error: str, *, max_attempts: int = 3) -> None:
        row = self.conn.execute("SELECT * FROM alert_outbox WHERE id=?", (int(alert_id),)).fetchone()
        if not row:
            return
        retry_count = int(row["retry_count"] or 0) + 1
        if retry_count >= max_attempts:
            self.conn.execute(
                """
                UPDATE alert_outbox
                SET status='failed', retry_count=?, last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (retry_count, error[:500], int(alert_id)),
            )
            self.conn.execute(
                """
                INSERT INTO alert_failure_log(alert_outbox_id, alert_type, symbol, error_message, retry_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(alert_id), row["alert_type"], row["symbol"], error[:500], retry_count),
            )
            return
        delay_seconds = 60 * (2 ** (retry_count - 1))
        next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.conn.execute(
            """
            UPDATE alert_outbox
            SET status='pending', retry_count=?, next_retry_at=?, last_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (retry_count, next_retry_at, error[:500], int(alert_id)),
        )

    def claim_pending_alerts(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM alert_outbox
            WHERE status='pending' AND datetime(COALESCE(next_retry_at, created_at)) <= datetime('now')
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def request_config_hot_reload(
        self,
        *,
        config_key: str,
        new_value: Any,
        requested_by: str | None,
        request_text: str,
        confirmation_required: bool = True,
    ) -> int:
        old = self.conn.execute("SELECT value_json FROM runtime_config WHERE config_key=?", (config_key,)).fetchone()
        self.conn.execute(
            """
            INSERT INTO config_hot_reload(config_key, old_value, new_value, requested_by, request_text, confirmation_required, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config_key,
                old["value_json"] if old else None,
                json.dumps(new_value, ensure_ascii=False),
                requested_by,
                request_text,
                1 if confirmation_required else 0,
                "pending" if confirmation_required else "confirmed",
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def apply_config_hot_reload(self, change_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM config_hot_reload WHERE id=?", (int(change_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "config change not found"}
        item = dict(row)
        if int(item.get("confirmation_required") or 0) and not int(item.get("confirmed") or 0):
            return {"ok": False, "error": "confirmation required", "change_id": change_id}
        summary = f"配置 {item['config_key']} 已热更新：{item.get('old_value') or '-'} -> {item['new_value']}"
        self.conn.execute(
            """
            INSERT INTO runtime_config(config_key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(config_key) DO UPDATE SET value_json=excluded.value_json, updated_at=CURRENT_TIMESTAMP
            """,
            (item["config_key"], item["new_value"]),
        )
        self.conn.execute(
            """
            UPDATE config_hot_reload
            SET confirmed=1, confirmed_at=COALESCE(confirmed_at, CURRENT_TIMESTAMP),
                status='applied', applied_at=CURRENT_TIMESTAMP, audit_summary=?
            WHERE id=?
            """,
            (summary, int(change_id)),
        )
        return {"ok": True, "change_id": change_id, "audit_summary": summary}

    def confirm_config_hot_reload(self, change_id: int) -> dict[str, Any]:
        self.conn.execute(
            "UPDATE config_hot_reload SET confirmed=1, confirmed_at=CURRENT_TIMESTAMP, status='confirmed' WHERE id=? AND status='pending'",
            (int(change_id),),
        )
        return self.apply_config_hot_reload(change_id)

    def update_strategy_memory_from_review(self, *, strategy_name: str, condition_hash: str, result: str, pnl_r: float, notes: str) -> None:
        existing = self.conn.execute(
            "SELECT * FROM strategy_memory WHERE strategy_name=? AND condition_hash=?",
            (strategy_name, condition_hash),
        ).fetchone()
        if not existing:
            self.conn.execute(
                """
                INSERT INTO strategy_memory(strategy_name, condition_hash, sample_count, win_count, loss_count, avg_rr, avg_pnl_percent, notes)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_name,
                    condition_hash,
                    1 if result == "win" else 0,
                    1 if result == "loss" else 0,
                    float(pnl_r),
                    float(pnl_r) * 100,
                    notes,
                ),
            )
            return
        sample_count = int(existing["sample_count"] or 0)
        new_count = sample_count + 1
        old_avg_rr = float(existing["avg_rr"] or 0)
        avg_rr = ((old_avg_rr * sample_count) + float(pnl_r)) / new_count
        old_avg_pct = float(existing["avg_pnl_percent"] or 0)
        avg_pct = ((old_avg_pct * sample_count) + float(pnl_r) * 100) / new_count
        self.conn.execute(
            """
            UPDATE strategy_memory
            SET sample_count=?,
                win_count=win_count + ?,
                loss_count=loss_count + ?,
                avg_rr=?,
                avg_pnl_percent=?,
                notes=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                new_count,
                1 if result == "win" else 0,
                1 if result == "loss" else 0,
                avg_rr,
                avg_pct,
                notes,
                int(existing["id"]),
            ),
        )

    def strategy_memory_top(self, limit: int = 10) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT * FROM strategy_memory
                ORDER BY sample_count DESC, updated_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        ]

    def save_strategy_patch_candidate(self, patch: dict[str, Any], evidence: dict[str, Any] | None = None, trigger_id: int | None = None) -> int:
        self.conn.execute(
            """
            INSERT INTO strategy_patches(strategy_name, from_version, candidate_version, patch_json, reason, evidence_json, trigger_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'shadow_testing')
            """,
            (
                patch["strategy_name"],
                patch["from_version"],
                patch["candidate_version"],
                json.dumps(patch.get("patch", {}), ensure_ascii=False),
                patch.get("change_reason"),
                json.dumps(evidence or {}, ensure_ascii=False),
                trigger_id,
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def mark_duplicate_patches_rejected(self) -> dict[str, int]:
        """Mark duplicate patches (same trigger_id + candidate_version) as rejected, keeping only the latest."""
        # Find duplicates
        duplicates = self.conn.execute(
            """
            SELECT trigger_id, candidate_version, COUNT(*) as cnt
            FROM strategy_patches
            WHERE trigger_id IS NOT NULL AND status NOT IN ('rejected', 'duplicate')
            GROUP BY trigger_id, candidate_version
            HAVING cnt > 1
            """
        ).fetchall()

        rejected = 0
        for dup in duplicates:
            trigger_id = int(dup["trigger_id"])
            candidate_version = dup["candidate_version"]
            # Keep the latest (highest id), reject the rest
            self.conn.execute(
                """
                UPDATE strategy_patches SET status='duplicate'
                WHERE trigger_id=? AND candidate_version=? AND status NOT IN ('rejected', 'duplicate')
                AND id NOT IN (
                    SELECT MAX(id) FROM strategy_patches WHERE trigger_id=? AND candidate_version=?
                )
                """,
                (trigger_id, candidate_version, trigger_id, candidate_version),
            )
            rejected += self.conn.execute("SELECT changes() AS c").fetchone()["c"]

        if rejected:
            self.conn.commit()

        return {"rejected_duplicates": rejected}

    def cleanup_orphan_patches(self) -> dict[str, int]:
        """Mark strategy_patches as rejected when they have no matching strategy_version.
        Returns counts of {orphans_marked, versions_backfilled}."""
        orphans = self.conn.execute(
            """
            SELECT sp.id, sp.strategy_name, sp.candidate_version, sp.status
            FROM strategy_patches sp
            LEFT JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
            WHERE sv.id IS NULL AND sp.status NOT IN ('duplicate', 'rejected')
            """
        ).fetchall()

        cleaned = 0
        for row in orphans:
            self.conn.execute(
                "UPDATE strategy_patches SET status='rejected' WHERE id=?",
                (row["id"],),
            )
            cleaned += 1

        if cleaned:
            self.conn.commit()

        return {"orphans_cleaned": cleaned}

    def list_strategy_versions(self, strategy_name: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if strategy_name:
            where = "WHERE strategy_name=?"
            params.append(strategy_name)
        return [
            dict(r)
            for r in self.conn.execute(
                f"""
                SELECT * FROM strategy_versions
                {where}
                ORDER BY strategy_name, status='active' DESC, created_at DESC, version DESC
                """,
                params,
            ).fetchall()
        ]

    def get_strategy_version(self, strategy_name: str, version: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_name=? AND version=?",
            (strategy_name, version),
        ).fetchone()
        return dict(row) if row else None

    def active_strategy_version(self, strategy_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_name=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (strategy_name,),
        ).fetchone()
        return dict(row) if row else None

    def save_strategy_version(
        self,
        *,
        strategy_name: str,
        version: str,
        status: str,
        config: dict[str, Any],
        change_reason: str,
        created_from_review_id: int | None = None,
    ) -> int:
        if status not in {"active", "candidate", "shadow_testing", "deprecated", "review_required", "rejected"}:
            raise ValueError(f"invalid strategy status: {status}")
        self.conn.execute(
            """
            INSERT INTO strategy_versions(strategy_name, version, status, config_json, change_reason, created_from_review_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_name, version) DO UPDATE SET
                status=excluded.status,
                config_json=excluded.config_json,
                change_reason=excluded.change_reason
            """,
            (strategy_name, version, status, json.dumps(config, ensure_ascii=False), change_reason, created_from_review_id),
        )
        row = self.conn.execute(
            "SELECT id FROM strategy_versions WHERE strategy_name=? AND version=?",
            (strategy_name, version),
        ).fetchone()
        return int(row["id"])

    def rollback_active_strategy(self, strategy_name: str, target_version: str, change_reason: str) -> dict[str, Any]:
        target = self.get_strategy_version(strategy_name, target_version)
        if not target:
            return {"ok": False, "error": "target strategy version not found"}
        self.conn.execute(
            "UPDATE strategy_versions SET status='deprecated' WHERE strategy_name=? AND status='active'",
            (strategy_name,),
        )
        self.conn.execute(
            "UPDATE strategy_versions SET status='active', change_reason=? WHERE strategy_name=? AND version=?",
            (change_reason, strategy_name, target_version),
        )
        return {"ok": True, "strategy_name": strategy_name, "active_version": target_version}

    def save_shadow_test_result(self, result: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO shadow_test_results(
                strategy_name, candidate_version, active_version, sample_count,
                active_stats_json, candidate_stats_json, recommendation, status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                result["strategy_name"],
                result["candidate_version"],
                result.get("active_version"),
                int(result.get("sample_count") or 0),
                json.dumps(result.get("active_stats", {}), ensure_ascii=False),
                json.dumps(result.get("candidate_stats", {}), ensure_ascii=False),
                result.get("recommendation"),
                result.get("status", "running"),
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def save_historical_replay_result(self, result: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO historical_replay_results(
                symbol, interval, start_time, end_time, strategy_versions_json, result_json, export_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["symbol"],
                result["interval"],
                int(result["start_time"]),
                int(result["end_time"]),
                json.dumps(result.get("strategy_versions", []), ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                result.get("export_path"),
            ),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def save_self_evolution_run(self, result: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO self_evolution_runs(status, result_json)
            VALUES (?, ?)
            """,
            (result.get("status", "unknown"), json.dumps(result, ensure_ascii=False)),
        )
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def list_trade_reviews_with_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT
                    r.id AS review_id,
                    r.result,
                    r.primary_reason,
                    r.secondary_reasons_json,
                    r.market_regime_at_loss,
                    r.evolution_trigger_allowed,
                    r.ga_review_json,
                    r.created_at AS review_created_at,
                    t.symbol,
                    t.pnl_r,
                    t.close_reason
                FROM trade_reviews r
                LEFT JOIN paper_trades t ON t.id = r.trade_id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        ]


def _build_data_quality(snapshot: dict[str, Any]) -> dict[str, Any]:
    profiles = snapshot.get("profiles", {})
    missing = [tf for tf, profile in profiles.items() if int(profile.get("candles_count") or 0) == 0]
    return {
        "closed_candles_only": True,
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
        "missing_timeframes": missing,
        "status": "complete" if not missing else "partial",
    }
