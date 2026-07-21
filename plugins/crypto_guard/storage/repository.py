from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import psycopg

from plugins.crypto_guard.storage import pg_db


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _decode_json(value: Any, default: Any) -> Any:
    """Decode a JSON column value into a Python object, defensively.

    psycopg returns JSONB columns already decoded to Python ``dict``/``list``
    (NOT a ``str``); TEXT columns holding a JSON string come back as ``str``;
    NULL comes back as ``None``. The legacy readers (written for SQLite, where
    every JSON column round-trips as a ``str``) did ``json.loads(col)`` which
    raises ``TypeError: the JSON object must be str, ... not dict`` on the
    psycopg JSONB shape - the broad ``except`` then silently fell back to the
    default, LOSING the real nested data (e.g. ``raw_decision_json`` audit
    fields, ``state_json`` continuity). This helper accepts any of the three
    shapes and returns the decoded object (or ``default`` on NULL/garbage).
    """
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value  # already decoded by psycopg (JSONB) or a scalar JSON value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return default


def _json_dumps_value(value: Any) -> str:
    """Normalize a JSON-bound column value into a JSON ``str`` for a ``%s``
    placeholder targeting a ``jsonb`` column.

    psycopg DECODES ``jsonb`` columns to a Python ``dict``/``list`` on read, so
    a row read back via ``SELECT *`` (e.g. ``order["take_profit_json"]``) is a
    ``list``, NOT a ``str``. Passing that bare ``list`` through ``%s`` makes
    psycopg adapt it as a PostgreSQL ``double precision[]`` / array, which fails
    ``DatatypeMismatch: 字段 ... 的类型为 jsonb, 但表达式的类型为 double precision[]``
    on INSERT/UPDATE into a ``jsonb`` column. A fresh in-memory value is already
    a ``str`` (from ``json.dumps``) and passes through unchanged. ``None`` stays
    ``None``. Anything else is best-effort ``json.dumps``-ed.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> str:
    """``json.dumps(default=...)`` hook for PG row values that round-trip as
    non-JSON-native Python objects.

    07-16 cutover: under SQLite every timestamp column came back as an ISO
    ``str`` (stored as TEXT), so ``json.dumps(payload)`` on a dict assembled from
    fetched rows (e.g. the ``paper_event_alert`` payload in
    ``position_conflict_revalidator._execute_early_exit`` carrying
    ``order_row["filled_at"]`` / ``trade["created_at"]``) serialized cleanly.
    Under PostgreSQL a ``TIMESTAMPTZ``/``TIMESTAMP`` column is decoded by psycopg
    to a ``datetime.datetime`` (and ``DATE`` to ``datetime.date``); ``json.dumps``
    then raises ``TypeError: Object of type datetime is not JSON serializable``
    at the ``agent_jobs.payload_json`` / event-log JSONB write boundary. This hook
    ISO-8601-encodes datetimes/dates so the payload serializes to the same shape
    SQLite produced, without forcing every payload-construction site to coerce
    timestamps to strings.
    """
    from datetime import date as _date, datetime as _dt

    if isinstance(o, _dt):
        if o.tzinfo is None:
            return o.replace(tzinfo=timezone.utc).isoformat()
        return o.isoformat()
    if isinstance(o, _date):
        return o.isoformat()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _json_dumps_payload(payload: Any) -> str:
    """Serialize a job/event/alert payload to a JSON ``str`` for a ``jsonb``
    column, tolerating PG-decoded ``datetime``/``date`` values.

    The single JSONB-write boundary for all ``payload``/``event`` dicts. See
    ``_json_default`` for why datetime handling is required post-cutover.
    """
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _compute_initial_risk_usdt(order: dict[str, Any], entry_price: float) -> float | None:
    """Compute initial_risk_usdt = |entry_price - stop_loss| * quantity.

    Returns None if any required field is missing or invalid.
    """
    try:
        stop = float(order.get("initial_stop_loss") or order.get("stop_loss") or 0)
        quantity = float(order.get("quantity") or 0)
        if stop <= 0 or entry_price <= 0 or quantity <= 0:
            return None
        return abs(entry_price - stop) * quantity
    except (TypeError, ValueError):
        return None


def validate_job_identity(jp: dict[str, Any]) -> str | None:
    """R8-A (P0-2): the SHARED authoritative-symbol identity contract.

    Returns the authoritative ``payload.symbol`` ONLY when the payload passes
    the full identity contract; otherwise returns ``None`` (the caller
    fail-closes). The contract is:

      * ``jp`` is a mapping.
      * ``jp["symbol"]`` is a non-empty string (the authoritative symbol).
      * ``jp["snapshot"]`` is a mapping (a missing / non-dict snapshot is an
        identity failure -- the job cannot prove which symbol its snapshot
        describes -> fail closed).
      * ``jp["snapshot"]["symbol"]`` is a non-empty string STRICTLY EQUAL to
        ``jp["symbol"]``. A swapped / cross-symbol snapshot is corruption ->
        fail closed.

    Pre-R8 the seal only cross-checked ``payload.symbol == payload.snapshot.
    symbol`` WHEN ``snapshot.symbol`` was present, so a MISSING snapshot (or a
    snapshot with no symbol) SKIPPED the check and sealed successfully
    (fail-open -- the user's in-memory ``seal_missing_snapshot=True`` repro).
    The worker preferred ``snapshot.symbol`` over the authoritative
    ``payload.symbol`` (cross-symbol corruption). This single helper is the
    one source of truth wired into seal / claim / worker so the contract cannot
    drift between them.

    The helper returns the authoritative symbol (not just True/False) so the
    caller (seal/claim) can build the enabled-set membership / cardinality
    check from the SAME validated value the worker will use -- eliminating any
    window where the worker could diverge from the seal/claim view.
    """
    if not isinstance(jp, dict):
        return None
    sym = jp.get("symbol")
    if not isinstance(sym, str) or not sym:
        return None
    snap = jp.get("snapshot")
    if not isinstance(snap, dict):
        # A missing / non-dict snapshot cannot prove identity -> fail closed.
        # Pre-R8 the seal skipped this check (the fail-open gap).
        return None
    snap_sym = snap.get("symbol")
    if not isinstance(snap_sym, str) or not snap_sym:
        # A snapshot with no symbol cannot prove identity -> fail closed.
        return None
    if snap_sym != sym:
        # Swapped / cross-symbol snapshot -> corruption -> fail closed.
        return None
    return sym


class CryptoGuardRepository:
    """Repository 层隔离所有 SQL，业务模块不直接拼 SQL。

    PostgreSQL (psycopg3) backend. ``self.conn`` is a pooled psycopg
    connection (dict rows, autocommit=False). Every WRITE method self-wraps its
    body in ``with self.conn.transaction():`` so the write is durable whether or
    not the caller wrapped (no outer txn -> BEGIN+COMMIT, like SQLite autocommit;
    outer txn -> SAVEPOINT, atomic sub-group). Read-only methods issue plain
    SELECTs (the implicit read txn rolls back harmlessly on return).

    Parameter style is ``%s`` (psycopg). JSONB columns receive ``json.dumps(...)``
    strings (psycopg does NOT auto-adapt raw dict/list). BOOLEAN columns receive
    raw Python ``bool``. TIMESTAMPTZ columns use ``NOW()`` or are omitted (let
    ``DEFAULT NOW()`` apply). Identity ids come from ``RETURNING id``.
    """

    def __init__(self, conn: psycopg.Connection):
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
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO symbols(symbol, base_asset, quote_asset, category, enabled, source, default_timeframes, notes)
                    VALUES (%s, %s, 'USDT', %s, %s, %s, %s, %s)
                    ON CONFLICT(symbol) DO UPDATE SET
                        category=excluded.category,
                        enabled=excluded.enabled,
                        source=excluded.source,
                        default_timeframes=COALESCE(excluded.default_timeframes, symbols.default_timeframes),
                        notes=COALESCE(excluded.notes, symbols.notes),
                        updated_at=NOW()
                    """,
                    (symbol, base_asset, category, bool(enabled), source, json.dumps(timeframes or [], ensure_ascii=False), notes),
                )
        return self.get_symbol(symbol) or {"symbol": symbol}

    def get_symbol(self, symbol: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM symbols WHERE symbol=%s", (symbol,))
            row = cur.fetchone()
        return dict(row) if row else None

    def remove_symbol(self, symbol: str) -> bool:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM symbols WHERE symbol=%s", (symbol,))
                rc = cur.rowcount
        return rc > 0

    def set_symbol_enabled(self, symbol: str, enabled: bool) -> bool:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE symbols SET enabled=%s, updated_at=NOW() WHERE symbol=%s",
                    (bool(enabled), symbol),
                )
                rc = cur.rowcount
        return rc > 0

    def list_symbols(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM symbols"
        if not include_disabled:
            sql += " WHERE enabled=TRUE"
        sql += " ORDER BY enabled DESC, category, symbol"
        with self.conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def active_analysis_symbols(self) -> list[str]:
        rows: list[dict[str, Any]] = []
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol FROM symbols WHERE enabled=TRUE
                UNION
                SELECT symbol FROM opportunity_watches WHERE status='active'
                UNION
                SELECT symbol FROM paper_orders WHERE status IN ('pending','open')
                ORDER BY symbol
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        return [str(r["symbol"]) for r in rows]

    def upsert_candles(self, candles: Iterable[dict[str, Any]]) -> int:
        count = 0
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for c in candles:
                    cur.execute(
                        """
                        INSERT INTO candles(
                            symbol, interval, open_time, close_time, open, high, low, close, volume,
                            quote_volume, taker_buy_volume, taker_buy_quote_volume, trade_count, is_closed, source, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
                            updated_at=NOW()
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
                            bool(c.get("is_closed", True)),
                            c.get("source", "binance"),
                        ),
                    )
                    count += 1
        return count

    def get_candles(self, symbol: str, interval: str, *, analysis_time_utc: int, limit: int = 200) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM candles
                WHERE symbol=%s AND interval=%s AND is_closed=TRUE AND close_time <= %s
                ORDER BY open_time DESC
                LIMIT %s
                """,
                (symbol, interval, int(analysis_time_utc), int(limit)),
            )
            rows = [dict(r) for r in cur.fetchall()]
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
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO module_analysis_results(symbol, timeframe, analysis_time, module, result_json, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(symbol, timeframe, analysis_time, module) DO UPDATE SET
                        result_json=excluded.result_json,
                        confidence=excluded.confidence
                    """,
                    (symbol, timeframe, int(analysis_time_utc), module, json.dumps(result, ensure_ascii=False), confidence),
                )

    def save_market_snapshot(self, snapshot: dict[str, Any]) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO market_snapshots(symbol, analysis_time, mode, snapshot_json)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(symbol, analysis_time, mode) DO UPDATE SET snapshot_json=excluded.snapshot_json
                    RETURNING id
                    """,
                    (
                        snapshot["symbol"],
                        int(snapshot["analysis_time_utc"]),
                        snapshot["mode"],
                        json.dumps(snapshot, ensure_ascii=False),
                    ),
                )
                snapshot_id = int(cur.fetchone()["id"])
                cur.execute(
                    "UPDATE market_snapshots SET data_quality_json=%s WHERE id=%s",
                    (json.dumps(snapshot.get("data_quality", _build_data_quality(snapshot)), ensure_ascii=False), snapshot_id),
                )
            self.link_module_results_to_snapshot(snapshot_id, snapshot["symbol"], int(snapshot["analysis_time_utc"]))
        return snapshot_id

    def link_module_results_to_snapshot(self, snapshot_id: int, symbol: str, analysis_time_utc: int) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE module_analysis_results SET snapshot_id=%s WHERE symbol=%s AND analysis_time=%s",
                    (int(snapshot_id), symbol, int(analysis_time_utc)),
                )

    def save_analysis_state(self, state: dict[str, Any]) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analysis_states(
                        symbol, analysis_time, analysis_time_utc, analysis_mode, timeframes,
                        market_structure_json, trend_clarity_json, no_trade_reason_json, key_levels_json,
                        next_triggers_json, next_analysis_json, breakout_watch_json, trade_permission_json,
                        trade_plan_json, opportunity_watch_recommended, paper_trade_allowed, state_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        bool(state.get("opportunity_watch_recommended")),
                        bool((state.get("trade_permission") or {}).get("paper_trade_allowed")),
                        json.dumps(state, ensure_ascii=False),
                    ),
                )
                analysis_state_id = int(cur.fetchone()["id"])
        return analysis_state_id

    def attach_ga_decision_to_analysis_state(self, analysis_state_id: int, ga_decision_id: int) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE analysis_states SET ga_decision_id=%s WHERE id=%s",
                    (int(ga_decision_id), int(analysis_state_id)),
                )

    def create_ga_decision(self, decision: dict[str, Any]) -> int:
        """Persist a GA decision and return its row id.

        R1-8 (07-03 final review) audit-path contract:
        - ``raw_decision_json`` is the top-level JSON column that stores the
          full decision dict (via ``json.dumps(decision)``). All structured
          audit fields (``timeframe_context``, ``alignment``,
          ``htf_conflict``, ``market_reason_codes``, ``risk_check``,
          ``trade_plan``, ``has_trade_plan``, ``opportunity_watch``) live
          at the top level of this JSON.
        - ``raw_llm_summary`` (the original LLM-produced summary text, NOT
          the canonical deterministic summary) is stored at
          ``raw_decision_json["raw_llm_summary"]`` when the controller sets
          it on the legacy decision dict before persistence. Readers (e.g.
          the GA decision adapter) MUST read it from this single path
          rather than guessing nested locations.
        - ``final_summary`` / ``rendered_summary`` are persisted as their
          own columns AND inside ``raw_decision_json``. Per R1-5 both must
          equal the canonical deterministic summary produced by
          ``build_canonical_market_summary``; the original LLM text lives
          only in ``raw_llm_summary``.
        """
        trade_plan = decision.get("trade_plan")
        opportunity_watch = decision.get("opportunity_watch")
        # Hourly Report Accuracy: optional batch linkage + previous grade +
        # deterministic rendered summary (may be None when upstream does
        # not set them; the columns are nullable).
        batch_id = decision.get("batch_id")
        previous_grade = decision.get("previous_grade")
        rendered_summary = decision.get("rendered_summary")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ga_decisions(
                        symbol, analysis_time, analysis_time_utc, decision_type, signal_grade,
                        confidence, market_bias, trend_stage, decision, skill_result_refs_json,
                        evidence_json, counter_evidence_json, risk_check_json, trade_plan_json,
                        opportunity_watch_json, feishu_actions_json, final_summary, raw_decision_json,
                        analysis_state_id, snapshot_id, created_by,
                        batch_id, previous_grade, rendered_summary
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        batch_id,
                        previous_grade,
                        rendered_summary,
                    ),
                )
                ga_decision_id = int(cur.fetchone()["id"])
        return ga_decision_id

    def get_ga_decision(self, ga_decision_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM ga_decisions WHERE id=%s", (int(ga_decision_id),))
            row = cur.fetchone()
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
            item[key] = _decode_json(item.get(column), default)
        return item

    def latest_ga_decisions_by_symbol(self, limit: int = 80, *, min_analysis_time: int | None = None, batch_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        conds: list[str] = []
        if min_analysis_time is not None:
            conds.append("gd.analysis_time >= %s")
            params.append(int(min_analysis_time))
        if batch_id is not None:
            conds.append("gd.batch_id = %s")
            params.append(batch_id)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT gd.*, ROW_NUMBER() OVER (
                        PARTITION BY gd.symbol ORDER BY gd.analysis_time DESC, gd.id DESC
                    ) AS rn
                    FROM ga_decisions gd
                    {where}
                ) sub WHERE rn = 1
                ORDER BY analysis_time DESC, id DESC
                LIMIT %s
                """,
                params + [int(limit)],
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows

    # ── Analysis batch lifecycle helpers (Hourly Report Accuracy) ──────────
    def start_analysis_batch(
        self, *, batch_id: str, primary_interval: str, analysis_time: int, enabled_symbols: list[str],
    ) -> int:
        """Create (or upsert) an analysis_batches row marking the batch running.

        Idempotent on batch_id via UNIQUE constraint; enabling symbols are
        preserved on re-entry. Returns the row id.
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, enabled_symbols_json FROM analysis_batches WHERE batch_id=%s",
                    (batch_id,),
                )
                existing = cur.fetchone()
                if existing:
                    row_id = int(existing["id"])
                    e_syms = _decode_json(existing["enabled_symbols_json"], [])
                    if not e_syms:
                        cur.execute(
                            "UPDATE analysis_batches SET enabled_symbols_json=%s WHERE id=%s",
                            (json.dumps(enabled_symbols, ensure_ascii=False), row_id),
                        )
                    return row_id
                cur.execute(
                    """
                    INSERT INTO analysis_batches(batch_id, primary_interval, analysis_time,
                                                  status, enabled_symbols_json)
                    VALUES (%s, %s, %s, 'running', %s)
                    RETURNING id
                    """,
                    (batch_id, primary_interval, int(analysis_time), json.dumps(enabled_symbols, ensure_ascii=False)),
                )
                return int(cur.fetchone()["id"])

    def seal_analysis_batch(self, batch_id: str) -> bool:
        """07-13 R6-B (P0-1): seal a batch so it becomes claimable.

        The producer calls this AFTER inserting every enabled-symbol job +
        batch_symbol_status row. This method validates EXACT set equality:

            job_symbols(batch_id) == batch_symbol_status(batch_id) == enabled_symbols

        and only on success stamps ``claim_ready_at`` + ``sealed_at``. A batch
        that is missing a symbol, has a duplicate, has a foreign (cross-batch)
        symbol, or is otherwise malformed stays UNSEALED (claim_ready_at IS
        NULL) so ``claim_next_batch`` skips it -- fail closed without claiming
        a prefix.

        Returns True if sealed; False if validation failed (batch stays
        non-claimable). Idempotent: re-sealing an already-sealed batch whose
        set still validates is a no-op (keeps the original sealed_at).

        Plan ref: production-incident-repair-plan-07-13.md §4 P0-1.1-1.6.
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled_symbols_json FROM analysis_batches WHERE batch_id=%s FOR UPDATE",
                    (batch_id,),
                )
                row = cur.fetchone()
                if not row:
                    # No batch row -- nothing to seal. The producer must call
                    # start_analysis_batch first. Fail closed.
                    return False
                enabled = _decode_json(row["enabled_symbols_json"], [])
                enabled_set = set(enabled)
                if not enabled_set:
                    # No enabled symbols declared -- nothing to seal. Fail closed.
                    return False
                # 07-13 R7 (P0-1): job symbols are derived from the AUTHORITATIVE
                # ``payload.symbol`` field, NOT from the ``<batch_id>:<symbol>`` prefix
                # of ``session_id``. The production session_id format is
                # ``system:scheduled:{interval}:{symbol}:{time}`` (cron_scheduler.py),
                # which the legacy prefix-strip parser mangled into the full
                # ``system:scheduled:...`` string and never matched the enabled set ->
                # production batches never sealed. ``payload.symbol`` is format-
                # independent and the single source of truth. We ALSO cross-check
                # identity consistency: each job's ``payload.symbol`` MUST equal
                # ``payload.snapshot.symbol`` (the snapshot it carries), and MUST be
                # a member of ``enabled_set`` (a foreign/cross-batch symbol fails). A job
                # whose ``payload.symbol`` is missing/mismatched fails the WHOLE batch
                # closed -- no prefix is claimed.
                cur.execute(
                    """
                    SELECT session_id, payload_json, batch_id, symbol
                    FROM agent_jobs
                    WHERE job_type='scheduled_market_analysis'
                      AND batch_id=%s
                    """,
                    (str(batch_id),),
                )
                job_rows = cur.fetchall()
                job_symbols_list: list[str] = []
                for r in job_rows:
                    jp = _decode_json(r["payload_json"], None)
                    if jp is None:
                        # Malformed payload_json -> cannot prove identity -> fail closed.
                        return False
                    # 07-15 R8-A (P0-2): delegate to the SHARED identity contract. The
                    # helper requires ``payload.symbol`` be a non-empty string, the
                    # ``snapshot`` be a dict whose ``symbol`` is a non-empty string
                    # STRICTLY equal to ``payload.symbol``. Pre-R8 the seal only
                    # cross-checked snapshot consistency WHEN ``snapshot.symbol`` was
                    # present, so a MISSING snapshot / no-symbol snapshot SKIPPED the
                    # check and sealed (fail-open -- the user's in-memory
                    # ``seal_missing_snapshot=True`` repro). The shared helper makes a
                    # missing/no-symbol/swapped snapshot fail the WHOLE batch closed.
                    sym = validate_job_identity(jp)
                    if sym is None:
                        return False
                    if str(jp.get("batch_id") or "") != str(batch_id):
                        return False
                    if r["batch_id"] != batch_id or r["symbol"] != sym:
                        return False
                    # Identity consistency: the symbol MUST belong to this batch's
                    # enabled set (a foreign/cross-batch symbol pointing at this
                    # batch_id is corruption -> fail closed).
                    if sym not in enabled_set:
                        return False
                    job_symbols_list.append(sym)
                job_symbols = set(job_symbols_list)
                # batch_symbol_status symbols for this batch (list, to catch dups).
                cur.execute(
                    "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s",
                    (batch_id,),
                )
                bss_rows = cur.fetchall()
                bss_symbols_list = [r["symbol"] for r in bss_rows]
                bss_symbols = set(bss_symbols_list)
                # EXACT set equality + cardinality (no duplicates, no missing, no
                # foreign/cross-symbol). A duplicate job inflates len(job_symbols_list)
                # above len(enabled) while the set still matches -- the cardinality
                # check rejects it. A missing symbol fails the set check. A foreign
                # symbol fails the set check (not in enabled).
                if len(job_symbols_list) != len(enabled_set):
                    return False
                if job_symbols != enabled_set:
                    return False
                if len(bss_symbols_list) != len(enabled_set):
                    return False
                if bss_symbols != enabled_set:
                    return False
                # Idempotent: if already sealed, do not refresh the timestamp.
                cur.execute(
                    "SELECT sealed_at FROM analysis_batches WHERE batch_id=%s",
                    (batch_id,),
                )
                already = cur.fetchone()
                if already and already["sealed_at"]:
                    return True
                cur.execute(
                    """
                    UPDATE analysis_batches
                    SET sealed_at=NOW(), claim_ready_at=NOW()
                    WHERE batch_id=%s
                    """,
                    (batch_id,),
                )
                return True

    def mark_batch_symbol_completed(self, *, batch_id: str, symbol: str, failed: bool = False, status: str | None = None) -> None:
        """Mark a symbol as completed/failed/pending for a batch.

        Uses atomic ON CONFLICT upsert on batch_symbol_status (P0-2: concurrent safety).
        If *status* is given it takes precedence; otherwise derived from *failed*.
        """
        final_status = status if status is not None else ("failed" if failed else "completed")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO batch_symbol_status(batch_id, symbol, status, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (batch_id, symbol) DO UPDATE SET
                        status=EXCLUDED.status,
                        updated_at=NOW()
                    """,
                    (batch_id, symbol, final_status),
                )

    def finish_analysis_batch(self, *, batch_id: str, status: str = "success", summary: dict[str, Any] | None = None) -> None:
        """Mark an analysis batch finished and materialize completed/failed lists.

        Phase E (07-07) per design §10.1 P0: the previous implementation wrote
        only ``status`` + ``summary_json`` and never touched the
        ``completed_symbols_json`` / ``failed_symbols_json`` columns. The
        read-side ``get_analysis_batch`` compensated by querying
        ``batch_symbol_status`` at read time, but the raw columns stayed
        empty — which masked the write-link gap and broke diagnostics that
        read the raw column (``SUCCESS_BATCH_MISSING_COMPLETED_SYMBOLS``).

        The fix materializes both columns from ``batch_symbol_status`` INSIDE
        this repo method so callers (``run_ga_workers.py:128,156``) need no
        change. ``get_analysis_batch`` continues to query
        ``batch_symbol_status`` for real-time in-flight counts; post-finish
        the two sources agree (column is authoritative at finish time).
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='completed' ORDER BY symbol",
                    (batch_id,),
                )
                completed_rows = cur.fetchall()
                cur.execute(
                    "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='failed' ORDER BY symbol",
                    (batch_id,),
                )
                failed_rows = cur.fetchall()
                completed = [r["symbol"] for r in completed_rows]
                failed = [r["symbol"] for r in failed_rows]
                cur.execute(
                    """
                    UPDATE analysis_batches
                    SET finished_at=NOW(), status=%s, summary_json=%s,
                        completed_symbols_json=%s, failed_symbols_json=%s
                    WHERE batch_id=%s
                    """,
                    (
                        status,
                        json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                        json.dumps(completed, ensure_ascii=False),
                        json.dumps(failed, ensure_ascii=False),
                        batch_id,
                    ),
                )

    def is_batch_complete(self, batch_id: str) -> bool:
        """Return True if all enabled symbols have been processed (no pending
        and no missing symbols) for the given batch.

        Checks both that there are no pending rows AND that the total count
        in batch_symbol_status matches the enabled_symbols count. A symbol
        that was never registered in batch_symbol_status is still missing.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM batch_symbol_status WHERE batch_id=%s AND status='pending'",
                (batch_id,),
            )
            pending_row = cur.fetchone()
            if int(pending_row["cnt"]) > 0:
                return False
            # Also verify all enabled symbols are accounted for
            batch = self.get_analysis_batch(batch_id)
            if not batch:
                return False
            enabled = set(batch.get("enabled_symbols") or [])
            if not enabled:
                return True
            # Get all symbols registered in batch_symbol_status for this batch
            cur.execute(
                "SELECT DISTINCT symbol FROM batch_symbol_status WHERE batch_id=%s",
                (batch_id,),
            )
            registered_rows = cur.fetchall()
            registered = {r["symbol"] for r in registered_rows}
            return enabled.issubset(registered)

    def batch_symbol_counts(self, batch_id: str) -> dict[str, int]:
        """Return {completed, failed, pending} counts for a batch from batch_symbol_status."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) AS cnt FROM batch_symbol_status WHERE batch_id=%s GROUP BY status",
                (batch_id,),
            )
            rows = cur.fetchall()
        counts = {"completed": 0, "failed": 0, "pending": 0}
        for r in rows:
            key = str(r["status"])
            if key in counts:
                counts[key] = int(r["cnt"])
        return counts

    def batch_has_failures(self, batch_id: str) -> bool:
        """Return True if any symbol in batch_symbol_status has status='failed'."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM batch_symbol_status WHERE batch_id=%s AND status='failed'",
                (batch_id,),
            )
            row = cur.fetchone()
        return int(row["cnt"]) > 0

    def batch_all_failed(self, batch_id: str) -> bool:
        """Return True if ALL symbols in batch_symbol_status have status='failed'."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_cnt FROM batch_symbol_status WHERE batch_id=%s",
                (batch_id,),
            )
            row = cur.fetchone()
        total = int(row["total"]) if row else 0
        failed_cnt = int(row["failed_cnt"]) if row else 0
        return total > 0 and failed_cnt == total

    def get_analysis_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM analysis_batches WHERE batch_id=%s", (batch_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            for col in ("enabled_symbols_json", "completed_symbols_json", "failed_symbols_json", "summary_json"):
                key = col.removesuffix("_json")
                item[key] = _decode_json(item.get(col), [])
            # P0-2: also populate from batch_symbol_status for accurate counts
            cur.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='completed'",
                (batch_id,),
            )
            completed_rows = cur.fetchall()
            cur.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='failed'",
                (batch_id,),
            )
            failed_rows = cur.fetchall()
            cur.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=%s AND status='pending'",
                (batch_id,),
            )
            pending_rows = cur.fetchall()
            item["completed_symbols"] = [r["symbol"] for r in completed_rows]
            item["failed_symbols"] = [r["symbol"] for r in failed_rows]
            item["pending_symbols"] = [r["symbol"] for r in pending_rows]
            return item

    def latest_analysis_batch_id(self, primary_interval: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT batch_id FROM analysis_batches WHERE primary_interval=%s ORDER BY analysis_time DESC, id DESC LIMIT 1",
                (primary_interval,),
            )
            row = cur.fetchone()
        return row["batch_id"] if row else None

    def list_recent_analysis_batches(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return the most recent ``analysis_batches`` rows (newest first).

        Phase E (07-07) per design §9.4: used by
        ``_select_latest_complete_batch`` to find the latest batch with
        ``status='success'`` AND ``completed_count == enabled_count`` AND
        matching GA decision count. Each row's ``summary_json`` /
        ``completed_symbols_json`` / ``failed_symbols_json`` /
        ``enabled_symbols_json`` columns are parsed into dict/list form
        mirroring ``get_analysis_batch`` (without the real-time
        ``batch_symbol_status`` re-query — the materialized columns are
        authoritative post-finish per design §10.1).
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM analysis_batches
                ORDER BY started_at DESC, id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            for col, default in (
                ("enabled_symbols_json", []),
                ("completed_symbols_json", []),
                ("failed_symbols_json", []),
                ("summary_json", {}),
            ):
                key = col.removesuffix("_json")
                item[key] = _decode_json(item.get(col), default)
            items.append(item)
        return items

    def list_ga_decisions_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """Return all ``ga_decisions`` rows for a given batch (newest first).

        Phase E (07-07) per design §9.4: used by
        ``_select_latest_complete_batch`` to verify the GA decision count
        matches the batch's enabled symbol count (guards against a batch
        marked success with completed_symbols materialized but decisions
        missing/stale).
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ga_decisions WHERE batch_id=%s ORDER BY id DESC",
                (batch_id,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def list_recent_ga_decisions(self, limit: int = 200, *, since_ms: int | None = None) -> list[dict[str, Any]]:
        """Return recent ``ga_decisions`` rows (newest first).

        Phase E (07-07) per design §11.1: used by the
        ``DETERMINISTIC_CANDIDATE_REPORTED_AS_TRADE_PLAN`` diagnostic to
        inspect ``raw_decision_json.plan_execution_state`` +
        ``candidate_trade_plan`` + ``has_trade_plan`` over the latest 24h.
        """
        params: list[Any] = []
        where = ""
        if since_ms is not None:
            where = "WHERE analysis_time >= %s"
            params.append(int(since_ms))
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM ga_decisions
                {where}
                ORDER BY analysis_time DESC, id DESC
                LIMIT %s
                """,
                params + [int(limit)],
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def previous_ga_decision_grade(self, symbol: str, *, exclude_batch_id: str | None = None) -> str | None:
        """Return the signal_grade of the most recent ga_decision for ``symbol``.

        If ``exclude_batch_id`` is given, skip decisions from that batch
        so the result comes from a genuinely earlier batch.
        """
        with self.conn.cursor() as cur:
            if exclude_batch_id:
                cur.execute(
                    "SELECT signal_grade FROM ga_decisions WHERE symbol=%s AND (batch_id IS NULL OR batch_id!=%s) ORDER BY analysis_time DESC, id DESC LIMIT 1",
                    (symbol, exclude_batch_id),
                )
            else:
                cur.execute(
                    "SELECT signal_grade FROM ga_decisions WHERE symbol=%s ORDER BY analysis_time DESC, id DESC LIMIT 1",
                    (symbol,),
                )
            row = cur.fetchone()
        return row["signal_grade"] if row else None

    def latest_skill_result_refs(self, symbol: str, analysis_time_utc: int) -> dict[str, int]:
        # 07-14 R8 P2-NEW-1 (point 5 & 7): only 'committed'/'legacy_committed'
        # skill logs are handed to the live SkillOrchestrator. A 'prepared'
        # (in-flight Phase 1), 'aborted_unsealed' (seal failure), or 'aborted'
        # (crash-recovered) audit row must NEVER point the orchestrator at a
        # dead/failed tick. Legacy rows (NULL commit_state) read as
        # legacy_committed (COALESCE) -- no production-history backfill.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT skill_name, MAX(id) AS id
                FROM skill_execution_logs
                WHERE symbol=%s AND analysis_time=%s
                  AND COALESCE(commit_state, 'legacy_committed') IN ('committed', 'legacy_committed')
                GROUP BY skill_name
                """,
                (symbol, int(analysis_time_utc)),
            )
            rows = cur.fetchall()
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
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO parquet_archive_runs(symbol, interval, year_month, path, rows_written, status, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (symbol, interval, year_month, path, int(rows_written), status, error_message),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def latest_parquet_archive_run(self) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM parquet_archive_runs ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        return dict(row) if row else None

    def latest_analysis_state(self, symbol: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM analysis_states
                WHERE symbol=%s
                ORDER BY analysis_time DESC, id DESC
                LIMIT 1
                """,
                (symbol,),
            )
            row = cur.fetchone()
        if not row:
            return None
        item = dict(row)
        item["state"] = _decode_json(item.get("state_json"), {})
        return item

    def latest_analysis_state_for_continuity(
        self,
        symbol: str,
        *,
        analysis_time_utc: int,
        exclude_batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Strict previous-state lookup for analysis continuity (Phase D, 07-05).

        Returns the latest analysis_states row whose ``analysis_time`` is
        strictly less than ``analysis_time_utc`` and whose corresponding
        ``ga_decisions.batch_id`` is not the current batch. Enforces:
          - same symbol (cross-symbol rejected);
          - strict past (future/same-time rejected);
          - not the same batch (same-batch rejected via LEFT JOIN to
            ``ga_decisions`` on ``ga_decision_id``).

        Returns ``None`` if no row qualifies. Caller is responsible for
        stale-age checks (max age in bars/time).
        """
        if not symbol or analysis_time_utc is None or int(analysis_time_utc) <= 0:
            return None
        # P1-3 fix: JOIN ga_decisions.signal_grade so _compact_previous_state
        # reads the actual signal_grade (S/A/B/C/D) from the prior decision,
        # not a heuristic. The JOIN is LEFT so legacy rows without
        # ga_decision_id still qualify.
        with self.conn.cursor() as cur:
            if exclude_batch_id:
                cur.execute(
                    """
                    SELECT s.*, g.signal_grade FROM analysis_states s
                    LEFT JOIN ga_decisions g ON s.ga_decision_id = g.id
                    WHERE s.symbol=%s AND s.analysis_time < %s
                      AND (g.batch_id IS NULL OR g.batch_id != %s)
                    ORDER BY s.analysis_time DESC, s.id DESC
                    LIMIT 1
                    """,
                    (symbol, int(analysis_time_utc), exclude_batch_id),
                )
            else:
                cur.execute(
                    """
                    SELECT s.*, g.signal_grade FROM analysis_states s
                    LEFT JOIN ga_decisions g ON s.ga_decision_id = g.id
                    WHERE s.symbol=%s AND s.analysis_time < %s
                    ORDER BY s.analysis_time DESC, s.id DESC
                    LIMIT 1
                    """,
                    (symbol, int(analysis_time_utc)),
                )
            row = cur.fetchone()
        if not row:
            return None
        item = dict(row)
        item["state"] = _decode_json(item.get("state_json"), {})
        return item

    def latest_analysis_states(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM analysis_states ORDER BY analysis_time DESC, id DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["state"] = _decode_json(item.get("state_json"), {})
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
        commit_state: str = "committed",
        batch_id: str | None = None,
        attempt_id: int | None = None,
    ) -> int:
        # 07-14 R8 P2-NEW-1: the layered lifecycle stamps commit_state at write
        # time. Phase 1 writes 'prepared' (immediate immutable audit); Phase 2
        # flips it to 'committed' (success) or 'aborted_unsealed' (seal failure).
        # Legacy callers (default 'committed') preserve prior behavior so a NULL
        # commit_state never appears from this path; old historical rows keep
        # NULL and read as legacy_committed.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO skill_execution_logs(
                        skill_name, skill_version, symbol, timeframe, analysis_time,
                        input_summary_json, tool_result_json, ga_interpretation_json, final_result_json, confidence,
                        commit_state, batch_id, attempt_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        commit_state,
                        batch_id,
                        attempt_id,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

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
        # Dedup: skip auto_analysis if same (skill_name, feedback_type, finding) written in last 24h.
        # 07-14 R8 P2-NEW-1 prerequisite: the SELECT has THREE placeholders
        # (skill_name, feedback_type, finding) but historically bound only two
        # -> sqlite3.ProgrammingError (swallowed by _maybe_write_skill_feedback's
        # broad except) -> auto_analysis feedback NEVER persisted. Fixed to bind
        # all three so the deferred-feedback-write contract is observable.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                if feedback_type == "auto_analysis":
                    cur.execute(
                        """
                        SELECT id FROM skill_feedback_memory
                        WHERE skill_name=%s AND feedback_type=%s AND finding=%s AND status='candidate'
                          AND created_at > NOW() - INTERVAL '1 day'
                        LIMIT 1
                        """,
                        (skill_name, feedback_type, finding),
                    )
                    existing = cur.fetchone()
                    if existing:
                        return int(existing["id"])

                cur.execute(
                    """
                    INSERT INTO skill_feedback_memory(
                        skill_name, skill_version, feedback_type, source_type, source_id,
                        pattern_type, affected_symbols, affected_sides,
                        finding, suggested_adjustment_json, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                new_id = int(cur.fetchone()["id"])
        return new_id

    def create_signal(self, decision: dict[str, Any], snapshot_id: int | None = None, *, ga_decision_id: int | None = None) -> int:
        trade_plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
        watch = decision.get("opportunity_watch")
        from plugins.crypto_guard.notify.signal_policy import alert_level_for_grade

        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signals(
                        symbol, timeframe, direction, trend_stage, confidence, score, signal_grade, alert_level,
                        decision, market_snapshot_id, trade_plan_json, opportunity_watch_json, ga_reason, risk_notes,
                        ga_decision_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                signal_id = int(cur.fetchone()["id"])
                cur.execute(
                    "UPDATE signals SET ga_decision_json=%s WHERE id=%s",
                    (json.dumps(decision, ensure_ascii=False), signal_id),
                )
            if snapshot_id:
                self.save_strategy_evaluation(decision, snapshot_id)
        return signal_id

    def save_strategy_evaluation(self, decision: dict[str, Any], snapshot_id: int | None = None, *, is_shadow: bool = False) -> int:
        # Active evaluations start as pending_outcome — only backfilled to real_pnl
        # when the corresponding paper_trade closes.
        outcome_source = None
        if is_shadow:
            outcome_source = decision.get("outcome_source")
        elif decision.get("ga_decision_id") is not None:
            outcome_source = "pending_outcome"
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO strategy_evaluations(
                        snapshot_id, symbol, timeframe, analysis_time, strategy_name, strategy_version,
                        score, decision, evidence_json, counter_evidence_json, is_shadow, ga_decision_id,
                        outcome_source, paper_trade_id, shadow_virtual_trade_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        bool(is_shadow),
                        decision.get("ga_decision_id"),
                        outcome_source,
                        decision.get("paper_trade_id"),
                        decision.get("shadow_virtual_trade_id"),
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM signals WHERE id=%s", (int(signal_id),))
            row = cur.fetchone()
        return dict(row) if row else None

    def save_ad_hoc_analysis(self, symbol: str, requested_by: str | None, request_text: str, result: dict[str, Any], signal_id: int | None) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ad_hoc_analyses(symbol, requested_by, request_text, timeframes, analysis_result_json, ga_summary, has_trade_plan, signal_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        symbol,
                        requested_by,
                        request_text,
                        json.dumps(result.get("timeframes", []), ensure_ascii=False),
                        json.dumps(result, ensure_ascii=False),
                        result.get("summary"),
                        bool(result.get("has_trade_plan")),
                        signal_id,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def mark_ad_hoc_analysis_status_by_signal(self, signal_id: int, status: str) -> bool:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE ad_hoc_analyses SET status=%s WHERE signal_id=%s",
                    (status, int(signal_id)),
                )
                affected = cur.rowcount
        return affected > 0

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
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO opportunity_watches(
                        symbol, direction, watch_reason, watch_condition_json, invalid_condition_json,
                        source_signal_id, expires_at, ga_decision_id, created_by_user_action, source_button_action
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        bool(created_by_user_action),
                        source_button_action,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def get_opportunity_watch(self, watch_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM opportunity_watches WHERE id=%s", (int(watch_id),))
            row = cur.fetchone()
        return dict(row) if row else None

    def list_active_opportunity_watches(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunity_watches WHERE status='active' ORDER BY created_at ASC, id ASC"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def list_active_opportunity_watches_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunity_watches WHERE status='active' AND symbol=%s ORDER BY created_at ASC, id ASC",
                (symbol,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def update_opportunity_watch_status(
        self,
        watch_id: int,
        status: str,
        *,
        triggered_at: str | None = None,
        invalidated_reason: str | None = None,
    ) -> bool:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE opportunity_watches
                    SET status=%s,
                        triggered_at=COALESCE(%s, triggered_at),
                        invalidated_reason=COALESCE(%s, invalidated_reason),
                        last_checked_at=NOW(),
                        updated_at=NOW()
                    WHERE id=%s AND status='active'
                    """,
                    (status, triggered_at, invalidated_reason, int(watch_id)),
                )
                affected = cur.rowcount
        return affected == 1

    def touch_opportunity_watch(self, watch_id: int) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE opportunity_watches SET last_checked_at=NOW(), updated_at=NOW() WHERE id=%s",
                    (int(watch_id),),
                )

    def enqueue_job(self, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any], scheduled_at: str | None = None) -> int:
        batch_id = payload.get("batch_id") if job_type == "scheduled_market_analysis" else None
        symbol = validate_job_identity(payload) if job_type == "scheduled_market_analysis" else None
        if job_type == "scheduled_market_analysis" and (not batch_id or not symbol):
            raise ValueError("scheduled_market_analysis requires valid batch_id/symbol identity")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_jobs(
                        job_type, priority, source, session_id, payload_json,
                        scheduled_at, batch_id, symbol
                    )
                    VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s)
                    RETURNING id
                    """,
                    (
                        job_type, int(priority), source, session_id,
                        _json_dumps_payload(payload), scheduled_at, batch_id, symbol,
                    ),
                )
                job_id = int(cur.fetchone()["id"])
            self._enqueue_job_redis(job_id, job_type, priority, source, session_id, payload)
        return job_id

    def enqueue_job_once(self, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any], scheduled_at: str | None = None) -> int:
        """Enqueue a job with idempotency: if a job with the same (job_type, session_id)
        already exists and is pending/running/success, return the existing id.
        If it's failed/cancelled/duplicate, reset to pending and return the existing id.
        Otherwise insert a new job.

        PG note (07-16 cutover): the SELECT-then-INSERT/UPDATE runs inside one
        ``conn.transaction()``. There is no UNIQUE constraint on
        ``(job_type, session_id)`` (the SQLite schema had none either), so the
        legacy ``except sqlite3.IntegrityError`` race-recovery branch was DEAD
        CODE — the INSERT could never raise a UNIQUE violation. We do NOT add a
        ``conn.rollback()``-then-SELECT recovery pattern (user's hard rule:
        ``conn.rollback()`` may break the caller's outer transaction; and there
        is no violation to recover from). The atomic transaction makes the
        SELECT+UPDATE/INSERT a single unit; a genuine concurrent double-insert
        (two connections each seeing no existing row) would create two rows,
        matching the pre-existing SQLite TOCTOU behavior — not a regression.
        """
        batch_id = payload.get("batch_id") if job_type == "scheduled_market_analysis" else None
        symbol = validate_job_identity(payload) if job_type == "scheduled_market_analysis" else None
        if job_type == "scheduled_market_analysis" and (not batch_id or not symbol):
            raise ValueError("scheduled_market_analysis requires valid batch_id/symbol identity")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status FROM agent_jobs WHERE job_type=%s AND session_id=%s",
                    (job_type, session_id),
                )
                existing = cur.fetchone()
                if existing:
                    existing_id = int(existing["id"])
                    status = existing["status"]
                    if status in ("pending", "running", "success"):
                        return existing_id
                    # Reset failed/cancelled/duplicate to pending. This is a FRESH
                    # lifecycle for a previously-TERMINATED job (the prior attempt ran
                    # to a terminal failed/cancelled state), so the single-flight defer
                    # history MUST be cleared — unlike ``claim_next_batch`` (which
                    # reclaims a still-deferred PENDING row and MUST preserve defer
                    # history so a perpetually-deferred job can reach exhaustion under
                    # the R4-P0-1 absolute window). See R1 final-seal recommendation.
                    cur.execute(
                        "UPDATE agent_jobs SET status='pending', priority=%s, source=%s, payload_json=%s, batch_id=%s, symbol=%s, started_at=NULL, error_message=NULL, finished_at=NULL, defer_count=0, deferred_at=NULL, scheduled_at=COALESCE(%s, NOW()) WHERE id=%s",
                        (int(priority), source, _json_dumps_payload(payload), batch_id, symbol, scheduled_at, existing_id),
                    )
                    self._enqueue_job_redis(existing_id, job_type, priority, source, session_id, payload)
                    return existing_id
                # No existing job — insert new
                cur.execute(
                    """
                    INSERT INTO agent_jobs(
                        job_type, priority, source, session_id, payload_json,
                        scheduled_at, batch_id, symbol
                    )
                    VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s)
                    RETURNING id
                    """,
                    (
                        job_type, int(priority), source, session_id,
                        _json_dumps_payload(payload), scheduled_at, batch_id, symbol,
                    ),
                )
                job_id = int(cur.fetchone()["id"])
            self._enqueue_job_redis(job_id, job_type, priority, source, session_id, payload)
        return job_id

    def _enqueue_job_redis(self, job_id: int, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any]) -> None:
        try:
            # 07-10 S2 (P0 #2): ``scheduled_market_analysis`` must NOT enter the
            # Redis single-job queue. The fair-pool dispatch path
            # (``run_once`` -> ``claim_next_batch`` -> ``process_fair_batch``)
            # is the ONLY authority for scheduled market analysis: it atomically
            # claims an entire batch's rows and runs them through the fair
            # coordinator together. If a ``scheduled_market_analysis`` job were
            # RPUSH'd to the Redis background queue, ``run_once``'s
            # ``redis.pop_background_job()`` branch would pop and execute it as a
            # SINGLE job via the legacy serial ``process_job`` path -- bypassing
            # ``claim_next_batch`` and the fair batch entirely (the known LLM
            # starvation path). Redis stays an (optional) wake-up / user-job
            # channel; the DB ``claim_next_batch`` is the sole ownership
            # authority for scheduled analysis. ``enqueue_job`` already wrote the
            # DB row, so simply returning here leaves the job claimable by the
            # batch coordinator.
            #
            # Redis is an acceleration channel only; PostgreSQL remains the
            # ownership authority for every durable job.
            if job_type == "scheduled_market_analysis":
                return
            from plugins.crypto_guard.storage.redis_adapter import RedisAdapter, should_use_redis_for_path

            if not should_use_redis_for_path(None):
                return
            redis = RedisAdapter()
            redis_payload = {
                "db_job_id": job_id,
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
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agent_jobs WHERE status='pending' AND priority <= 2 LIMIT 1"
            )
            row = cur.fetchone()
        return bool(row)

    def claim_next_job(self, *, max_priority: int | None = None, background: bool = False) -> dict[str, Any] | None:
        # 07-16 cutover: PG-native single-statement atomic claim via FOR UPDATE
        # SKIP LOCKED (design §6). One statement: the subselect locks one due
        # pending row (skipping rows another worker already locked), the outer
        # UPDATE flips it to running. Two connections never block on the same
        # row and never double-claim. Replaces the SQLite two-step
        # (SELECT-then-UPDATE-WHERE-status=pending) which relied on SQLite's
        # serialized writes.
        if background and self.has_pending_user_jobs():
            return None
        params: list[Any] = []
        prio_clause = ""
        if max_priority is not None:
            prio_clause = "AND priority <= %s"
            params.append(int(max_priority))
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE agent_jobs
                    SET status='running', started_at=NOW()
                    WHERE id = (
                        SELECT id FROM agent_jobs
                        WHERE status='pending'
                          AND scheduled_at <= NOW()
                          {prio_clause}
                        ORDER BY priority ASC, scheduled_at ASC, id ASC
                        FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    RETURNING *
                    """,
                    params,
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def claim_next_batch(self) -> list[dict[str, Any]] | None:
        """07-10 R5-1 / S3 (P0 #4): atomically claim ALL pending
        ``scheduled_market_analysis`` jobs of ONE batch as a group, AND stamp
        a unique ``claim_token`` + ``lease_until`` on every claimed row so the
        caller can prove ownership.

        The fair-pool dispatch mode (``run_once`` -> ``process_fair_batch``)
        must run an entire batch's symbols through ``run_fair_batch`` together
        so the two-pass barrier + fair rotation can operate on the whole
        enabled-symbol set at once. Claiming one job at a time (the legacy
        ``claim_next_job`` path) would force serial execution and defeat the
        fairness fix.

        CAS semantics across concurrent workers (P0 #4): only ONE worker's
        ``UPDATE...WHERE status='pending'`` can flip a given batch's rows to
        ``running`` AND stamp its own ``claim_token``. A second worker that
        races on the same batch sees those rows already ``running`` (with the
        first worker's token), so its head SELECT finds no pending row for
        that batch -> it either picks a DIFFERENT batch or returns None (idle).
        The atomic ``UPDATE ... RETURNING *`` yields only THIS worker's rows,
        each stamped with its own ``claim_token`` - so the returned set is
        PROVABLY this worker's claim, never a row another worker flipped
        concurrently.

        ``lease_until`` is set to ``NOW() + INTERVAL '30 minutes'``. A worker
        that crashes mid-batch leaves ``status='running'`` with an expired
        lease; ``recover_stale_running_jobs`` resets it to ``pending`` so the
        batch can be reclaimed. Pre-S3 running rows (``lease_until IS NULL``,
        e.g. legacy ``claim_next_job`` rows) are still recovered by age.

        07-10 R4-P0-1: this UPDATE does NOT reset ``defer_count`` /
        ``deferred_at``. A job that was ``single_flight_skipped`` (deferred)
        and re-claimed MUST retain its defer history so the ABSOLUTE exhaustion
        window (``now - deferred_at >= per_symbol_timeout + buffer``) and the
        ``max_defers`` backstop can ACCUMULATE across reclaims -- otherwise a
        job perpetually stuck behind a long-lived lease would never reach
        exhaustion (each reclaim would wipe the clock). New jobs start at
        ``defer_count=0 / deferred_at=NULL`` (schema default); only
        ``defer_claimed_job`` increments the counter + stamps the anchor, and
        only ``finish_job`` (terminal success/failed) retires the row. So a
        re-claimed deferred job keeps its accumulated defer state, while a
        never-deferred job keeps its fresh zeros.

        Strategy (07-16 PG-native, design §6):
        1. Select and lock the batch owning the oldest ready pending job using
           ``FOR UPDATE OF b SKIP LOCKED``. A concurrent worker skips a batch
           already owned by another claimant and can fairly claim the next one.
        2. Generate ``token = secrets.token_hex(16)`` (unique per claim).
        3. Re-validate seal + enabled set + exact cardinality against the
           batch's CURRENT job rows (authoritative ``payload.symbol`` via
           ``validate_job_identity``); any inconsistency fails closed.
        4. Atomic ``UPDATE ... WHERE (payload_json->>'batch_id')=%s AND
           status='pending' RETURNING *`` flips the batch's pending rows to
           running, stamping ``started_at=NOW()``, ``claim_token=token``,
           ``lease_until=NOW() + INTERVAL '30 minutes'``. RETURNING rows ARE
           the provably-owned claimed set - no separate re-SELECT needed.

        Returns ``None`` when no pending ``scheduled_market_analysis`` job is
        ready (idle tick), or when the re-validation fails closed, or when a
        race left zero rows after the batch_id was selected - the caller treats
        all as idle.
        """
        token = secrets.token_hex(16)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                # 1. Lock the batch owning the oldest ready pending job. The
                #    lateral subquery supplies that batch's queue ordering key;
                #    SKIP LOCKED lets another worker move to the next batch
                #    instead of blocking behind this one.
                cur.execute(
                    """
                    SELECT b.batch_id, b.claim_ready_at, b.enabled_symbols_json
                    FROM analysis_batches b
                    JOIN LATERAL (
                        SELECT j.priority, j.scheduled_at, j.id
                        FROM agent_jobs j
                        WHERE j.job_type='scheduled_market_analysis'
                          AND j.status='pending'
                          AND j.scheduled_at <= NOW()
                          AND j.batch_id=b.batch_id
                        ORDER BY j.priority ASC, j.scheduled_at ASC, j.id ASC
                        LIMIT 1
                    ) head ON TRUE
                    WHERE b.claim_ready_at IS NOT NULL
                    ORDER BY head.priority ASC, head.scheduled_at ASC, head.id ASC
                    FOR UPDATE OF b SKIP LOCKED
                    LIMIT 1
                    """
                )
                sealed_row = cur.fetchone()
                if not sealed_row:
                    return None
                batch_id = str(sealed_row["batch_id"] or "")
                # 07-13 R6-B (P0-1): belt-and-suspenders sealing gate. Even though
                # the head-SELECT above only returns jobs whose batch is sealed,
                # re-check claim_ready_at HERE (inside the write transaction, under
                # the batch-row lock) so a batch un-sealed between the head SELECT
                # and this point cannot be claimed. An unsealed batch fails closed.
                if not batch_id or not sealed_row["claim_ready_at"]:
                    return None
                enabled = _decode_json(sealed_row["enabled_symbols_json"], None)
                if enabled is None:
                    return None
                enabled_set = set(enabled)
                if not enabled_set:
                    return None

                # 07-13 R7 (P0-2): re-validate the EXACT enabled set against the
                # batch's CURRENT job rows BEFORE flipping anything. The seal
                # validated the set at seal time, but a job inserted AFTER the seal
                # (post-seal pollution: a duplicate symbol row, or a foreign symbol
                # not in enabled) would be flipped by a naive
                # ``UPDATE ... WHERE batch_id=? AND status='pending'`` and claimed
                # together with the legitimate rows. Re-deriving symbols from the
                # AUTHORITATIVE ``payload.symbol`` field (same source as the seal)
                # and enforcing exact cardinality + set equality against
                # ``enabled_set`` makes the claim fail closed on pollution -- no
                # prefix is claimed.
                #
                # The check counts ALL jobs of this batch (any status), so a batch
                # mid-flight after a crash+recover (some rows terminal success, some
                # reset to pending) still validates: total job symbols == enabled.
                # A duplicate (2 rows for one symbol) inflates the row count above
                # ``len(enabled_set)`` -> cardinality mismatch -> fail closed. A
                # foreign symbol fails the set-equality check. A missing symbol
                # (row deleted) also fails set equality.
                cur.execute(
                    """
                    SELECT payload_json, batch_id, symbol, status
                    FROM agent_jobs
                    WHERE job_type='scheduled_market_analysis'
                      AND batch_id=%s
                    """,
                    (str(batch_id),),
                )
                all_job_rows = cur.fetchall()
                all_symbols: list[str] = []
                pending_symbols: list[str] = []
                for r in all_job_rows:
                    jp = _decode_json(r["payload_json"], None)
                    if jp is None:
                        # Malformed payload -> cannot prove identity -> fail closed.
                        return None
                    # 07-15 R8-A (P0-2): delegate to the SHARED identity contract so
                    # the claim re-validates ``payload.symbol == payload.snapshot.
                    # symbol`` (the consistency the seal enforced). Pre-R8 the claim
                    # only checked ``payload.symbol`` was a non-empty string and
                    # derived the set from it -- a swapped-snapshot job whose
                    # ``payload.symbol`` matched the enabled set was claimed, and
                    # the poison pill reached the worker. The helper makes a
                    # missing/no-symbol/swapped snapshot fail the claim closed
                    # (return None) before any row is flipped to running.
                    sym = validate_job_identity(jp)
                    if sym is None:
                        return None
                    if str(jp.get("batch_id") or "") != batch_id:
                        return None
                    if r["batch_id"] != batch_id or r["symbol"] != sym:
                        return None
                    all_symbols.append(sym)
                    if r["status"] == "pending":
                        pending_symbols.append(sym)
                all_symbols_set = set(all_symbols)
                if len(all_symbols) != len(enabled_set):
                    # Cardinality mismatch -> duplicate job (or extra/missing row).
                    return None
                if all_symbols_set != enabled_set:
                    # Set mismatch -> foreign or missing symbol.
                    return None

                # 3. Atomic flip of the whole batch's pending rows to running,
                #    stamping this worker's ownership token + lease. RETURNING *
                #    yields the provably-owned claimed set (each row carries this
                #    worker's ``claim_token``) - no separate re-SELECT needed.
                #    ``started_at=NOW()`` records the claim time (used by
                #    recover_stale_running_jobs' age fallback for pre-S3 rows).
                #    ``lease_until=NOW() + INTERVAL '30 minutes'``; a crashed worker
                #    leaves running rows with an expired lease for recovery.
                #
                #    R4-P0-1: this UPDATE does NOT reset ``defer_count`` /
                #    ``deferred_at`` - a re-claimed deferred job keeps its defer
                #    history (only defer_claimed_job increments, only finish_job
                #    retires). New jobs keep their fresh schema defaults.
                cur.execute(
                    """
                    UPDATE agent_jobs
                    SET status='running',
                        started_at=NOW(),
                        claim_token=%s,
                        lease_until=NOW() + INTERVAL '30 minutes'
                    WHERE job_type='scheduled_market_analysis'
                      AND status='pending'
                      AND scheduled_at <= NOW()
                      AND batch_id=%s
                    RETURNING *
                    """,
                    (token, str(batch_id)),
                )
                rows = cur.fetchall()
                if not rows:
                    # Lost the race - another worker claimed this batch between the
                    # head SELECT and the UPDATE. Signal idle so the caller loops.
                    return None
                returned_symbols: list[str] = []
                for row in rows:
                    payload = _decode_json(row["payload_json"], None)
                    symbol = validate_job_identity(payload) if payload is not None else None
                    if (
                        symbol is None
                        or row["batch_id"] != batch_id
                        or row["symbol"] != symbol
                        or str(payload.get("batch_id") or "") != batch_id
                    ):
                        raise RuntimeError("claimed batch identity changed during atomic claim")
                    returned_symbols.append(symbol)
                if (
                    len(returned_symbols) != len(pending_symbols)
                    or set(returned_symbols) != set(pending_symbols)
                ):
                    raise RuntimeError("claimed batch exact-set changed during atomic claim")
                return [dict(r) for r in rows]

    def claim_job_by_id_cas(self, *, job_id: int, expected_status: str = "pending") -> bool:
        """07-13 R6-C (P0-2): Redis-acceleration consumer CAS.

        The Redis queue is an acceleration channel; the database is the sole
        ownership authority. Before a Redis-popped payload can flip its row to
        ``running``, this single CAS statement verifies ALL of:

            id = <job_id>                      (identity / expected row)
            status = <expected_status>         (still claimable, not racing)
            scheduled_at <= NOW()              (due-time gate)

        If ANY predicate fails, the UPDATE hits 0 rows and this returns False
        -- the consumer must NOT execute the job (evidence §3.3: a future
        ``scheduled_at`` Redis payload flipped a row to running early, exhausting
        a 300s report wait in ~20s). Future jobs remain deferred WITHOUT
        consuming retry budget; a stale/duplicate Redis payload that lost the
        CAS race also fails closed.

        Stamps ``claim_token`` + ``lease_until`` so the row is provably owned by
        this consumer (mirrors ``claim_next_batch``'s ownership model), and
        ``recover_stale_running_jobs`` can reclaim it on a crash.

        07-16 cutover: wrapped in ``conn.transaction()`` (single UPDATE; commit
        makes the CAS durable, exception rolls it back atomically).

        Plan ref: production-incident-repair-plan-07-13.md §4 P0-2, §7.5.
        """
        token = secrets.token_hex(16)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_jobs
                    SET status='running',
                        started_at=NOW(),
                        claim_token=%s,
                        lease_until=NOW() + INTERVAL '30 minutes'
                    WHERE id=%s
                      AND status=%s
                      AND scheduled_at <= NOW()
                    """,
                    (token, int(job_id), expected_status),
                )
                affected = int(cur.rowcount)
        return affected == 1

    def defer_claimed_job(
        self, job_id: int, claim_token: str, *,
        reason: str = "single_flight_deferred",
        defer_seconds: int = 30,
    ) -> bool:
        """07-10 R3-P0-1 (terminal-review-repair-plan-r3 §3.3) + R4-P1-4:
        CAS-defer a claimed ``agent_jobs`` row from ``running`` back to
        ``pending`` WITHOUT releasing or modifying another worker's ownership.

        A ``single_flight_skipped`` symbol's lease is held by ANOTHER in-flight
        tick (the cross-batch single-flight mutex). This worker MUST NOT call
        ``controller.analyze_symbol``, write ``ga_decisions``/``analysis_states``
        /``signals``, run ``_post_decision_effects``, or release the other
        tick's lease (design §3.2 / historical contract #10). Instead it defers
        ITS OWN claim: CAS the row back to ``pending`` (clearing this tick's
        ``claim_token`` / ``lease_until`` / ``started_at``) and moves
        ``scheduled_at`` forward by ``defer_seconds`` so a later
        ``run_once(background=True)`` reclaims it once the owning tick releases
        the symbol lease.

        CAS semantics (§3.3): the UPDATE affects EXACTLY one row WHERE
        ``id=job_id AND status='running' AND claim_token=<this worker's
        token>``. A zero-row result is a CLAIM-LOSS failure (the row was
        reclaimed by ``recover_stale_running_jobs`` or never owned by this
        worker) and MUST be reported as ``False`` -- it MUST NOT release or
        modify another worker's row. The conditional UPDATE is atomic.

        R4-P1-4 (defer accounting): the defer count is persisted in a DEDICATED
        ``defer_count`` column via an ATOMIC SQL increment
        (``defer_count = defer_count + 1``) inside the same CAS UPDATE, NOT
        parsed out of ``error_message`` (which couples machine state to display
        text and would be reset by any later error-message update).
        ``deferred_at`` anchors the first-defer timestamp
        (``COALESCE(deferred_at, NOW())``) so the caller can apply the
        R4-P0-1 ABSOLUTE defer window (terminate once
        ``now - deferred_at >= per_symbol_timeout + cleanup_buffer``) instead
        of the pre-R4 fixed ``defer_seconds * max_defers`` product that would
        exhaust a 20-min lease at 2 min. ``error_message`` carries only the
        defer ``reason`` string for operators (NOT ``<reason>:<n>``); the
        authoritative count lives in the dedicated ``defer_count`` column
        (R4-P1-4 decoupling -- embedding the count in ``error_message`` would
        re-couple machine state to display text and be clobbered by any later
        error-message update). ``result_json`` is left untouched (no GA
        decision / batch result is written for a deferred symbol).

        Returns ``True`` iff exactly one row was deferred (this worker still
        owned it). Returns ``False`` on claim-loss (zero rows) -- the caller
        treats this as "another worker owns it now; do nothing further".

        07-16 cutover: ``datetime('now', '+N seconds')`` -> PG
        ``NOW() + make_interval(secs => %s)`` (parameterized dynamic interval,
        no string-built modifier). Wrapped in ``conn.transaction()``.
        """
        # Atomic CAS UPDATE: increment defer_count + stamp deferred_at (only if
        # not already set, so the FIRST defer anchors the absolute window) +
        # reset this worker's ownership + push scheduled_at forward.
        # ``error_message`` carries only the defer ``reason`` for operators;
        # ``defer_count`` is the authoritative counter (R4-P1-4).
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_jobs
                    SET status='pending',
                        started_at=NULL,
                        claim_token=NULL,
                        lease_until=NULL,
                        finished_at=NULL,
                        scheduled_at=NOW() + make_interval(secs => %s),
                        defer_count=defer_count + 1,
                        deferred_at=COALESCE(deferred_at, NOW()),
                        error_message=%s
                    WHERE id=%s
                      AND status='running'
                      AND claim_token=%s
                    """,
                    (
                        int(defer_seconds),
                        f"{reason}",
                        int(job_id),
                        str(claim_token),
                    ),
                )
                affected = int(cur.rowcount)
        return affected == 1

    def renew_batch_claim(self, claim_token: str, *, lease_seconds: int = 1800) -> int:
        """Extend every running row owned by one fair-batch claimant.

        The update is a short, independently committed heartbeat. It is safe to
        call immediately before and after the transaction-free provider phase;
        a zero count means ownership was lost and the caller must fail closed.
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_jobs
                    SET lease_until=NOW() + make_interval(secs => %s)
                    WHERE job_type='scheduled_market_analysis'
                      AND status='running'
                      AND claim_token=%s
                    """,
                    (int(lease_seconds), str(claim_token)),
                )
                return int(cur.rowcount)

    def get_job_defer_state(self, job_id: int) -> tuple[int, str | None]:
        """07-10 R4-P1-4 + R4-P0-1: read the single-flight defer state for a
        job from the DEDICATED columns (NOT ``error_message``). Returns
        ``(defer_count, deferred_at)``; ``(0, None)`` for a fresh / non-deferred
        job. ``defer_count`` is the authoritative counter incremented atomically
        by ``defer_claimed_job``; ``deferred_at`` anchors the first-defer
        timestamp so ``process_fair_batch`` can apply an ABSOLUTE defer window
        (terminate once ``now - deferred_at >= per_symbol_timeout +
        cleanup_buffer``) instead of the pre-R4 fixed ``defer_seconds *
        max_defers`` product. Used by ``process_fair_batch`` to decide
        defer-vs-terminate BEFORE calling ``defer_claimed_job``.

        07-16 cutover: ``deferred_at`` is a TIMESTAMPTZ column; psycopg returns
        a timezone-aware ``datetime``. The consumer (``run_ga_workers.
        _parse_db_ts_ms``) expects a STRING, so this method normalizes the
        returned timestamp to an ISO-8601 string at the repository boundary
        (preserving the ``str | None`` return contract callers depend on).
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT defer_count, deferred_at FROM agent_jobs WHERE id=%s",
                (int(job_id),),
            )
            row = cur.fetchone()
        if row is None:
            return (0, None)
        try:
            dc = int(row["defer_count"]) if row["defer_count"] is not None else 0
        except (TypeError, ValueError):
            dc = 0
        da_raw = row["deferred_at"]
        da: str | None = None
        if da_raw is not None:
            # Normalize psycopg's tz-aware datetime to an ISO-8601 string so the
            # consumer's string-based timestamp parser keeps working unchanged.
            try:
                da = da_raw.isoformat() if hasattr(da_raw, "isoformat") else str(da_raw)
            except Exception:
                da = str(da_raw)
        return (dc, da)

    def get_job_defer_count(self, job_id: int) -> int:
        """07-10 R3-P0-1 + R4-P1-4: read the single-flight defer count for a
        job. R4-P1-4: the count now lives in the DEDICATED ``defer_count`` column
        (NOT parsed out of ``error_message``); this helper delegates to
        ``get_job_defer_state``. Kept for backward-compat with callers/tests
        that only need the count."""
        return self.get_job_defer_state(job_id)[0]

    def recover_stale_running_jobs(self, *, older_than_minutes: int = 30) -> int:
        """07-10 S3 (P0 #4): reset running jobs whose lease has expired (or
        pre-S3 legacy rows with no lease, by age). A worker that crashes
        mid-batch leaves ``status='running'`` with an expired ``lease_until``;
        this resets those rows to ``pending`` so the batch can be reclaimed.

        A row is stale if EITHER its ``lease_until`` has passed (S3 path) OR
        it has no ``lease_until`` (pre-S3 legacy ``claim_next_job`` rows, whose
        ownership was never tokenized) AND its ``started_at`` is older than
        ``older_than_minutes``. Rows with a valid, unexpired lease are LEFT
        running (the owning worker may still be processing).

        07-16 cutover: TIMESTAMPTZ columns compare directly (no ``datetime()``
        wrapping). ``datetime('now', '-N minutes')`` ->
        ``NOW() - make_interval(mins => %s)``. Wrapped in ``conn.transaction()``.
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_jobs
                    SET status='pending',
                        started_at=NULL,
                        claim_token=NULL,
                        lease_until=NULL,
                        error_message=COALESCE(error_message, 'recovered stale running job after process restart')
                    WHERE status='running'
                      AND (
                        lease_until IS NOT NULL AND lease_until <= NOW()
                        OR
                        (lease_until IS NULL AND started_at <= NOW() - make_interval(mins => %s))
                      )
                    """,
                    (int(older_than_minutes),),
                )
                affected = int(cur.rowcount)
        return affected

    def finish_job(
        self,
        job_id: int,
        *,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        claim_token: str | None = None,
    ) -> bool:
        status = "failed" if error_message else "success"
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE agent_jobs
                    SET status=%s, finished_at=NOW(), error_message=%s,
                        result_json=%s, lease_until=NULL
                    WHERE id=%s
                    {"AND status='running' AND claim_token=%s" if claim_token is not None else ""}
                    """,
                    (
                        status, error_message,
                        json.dumps(result or {}, ensure_ascii=False), int(job_id),
                        *([str(claim_token)] if claim_token is not None else []),
                    ),
                )
                return int(cur.rowcount) == 1

    def finish_claimed_batch_symbol(
        self,
        *,
        batch_id: str,
        symbol: str,
        job_id: int,
        claim_token: str,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> bool:
        """CAS-finish one fair-batch job and its symbol status atomically."""
        job_status = "failed" if error_message else "success"
        symbol_status = "failed" if error_message else "completed"
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_jobs
                    SET status=%s, finished_at=NOW(), error_message=%s,
                        result_json=%s, lease_until=NULL
                    WHERE id=%s AND batch_id=%s AND symbol=%s
                      AND status='running' AND claim_token=%s
                    """,
                    (
                        job_status, error_message,
                        json.dumps(result or {}, ensure_ascii=False), int(job_id),
                        str(batch_id), str(symbol), str(claim_token),
                    ),
                )
                if int(cur.rowcount) != 1:
                    return False
                cur.execute(
                    """
                    INSERT INTO batch_symbol_status(batch_id, symbol, status, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT(batch_id, symbol) DO UPDATE
                    SET status=EXCLUDED.status, updated_at=NOW()
                    """,
                    (str(batch_id), str(symbol), symbol_status),
                )
                return True

    def claim_feishu_event(self, event_id: str, event_type: str, payload: dict[str, Any] | None = None) -> bool:
        if not event_id:
            return True
        # 07-16 cutover: ``feishu_events.event_id`` is the PK. The SQLite
        # ``try INSERT / except IntegrityError -> return False`` pattern relied
        # on autocommit (a failed INSERT left no residue and did not abort the
        # caller's outer transaction). On psycopg ``autocommit=False``, a raw
        # INSERT that violates the PK would ABORT the caller's outer transaction
        # (the user's hard rule: never conn.rollback()-then-SELECT). So use
        # ``ON CONFLICT DO NOTHING`` (design §6): the INSERT either succeeds
        # (new event -> True) or no-ops on a duplicate (-> False) WITHOUT
        # raising and WITHOUT aborting the outer transaction. Wrapped in a
        # ``conn.transaction()`` (savepoint if the caller already opened one).
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feishu_events(event_id, event_type, payload_json)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (event_id, event_type, _json_dumps_payload(payload or {})),
                )
                inserted = int(cur.rowcount)
        return inserted == 1

    def list_recent_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 'agent_job' AS source, id, job_type AS name, session_id, error_message, finished_at AS ts
                FROM agent_jobs
                WHERE status='failed' OR error_message IS NOT NULL
                UNION ALL
                SELECT 'scheduler_run' AS source, id, job_name AS name, CAST(scheduled_time AS TEXT) AS session_id, error_message, finished_at AS ts
                FROM scheduler_runs
                WHERE status='failed' OR error_message IS NOT NULL
                ORDER BY ts DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def latest_feishu_target(self) -> dict[str, Any] | None:
        # Primary: look in agent_jobs with source='feishu'
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_json FROM agent_jobs
                WHERE source='feishu'
                ORDER BY id DESC
                LIMIT 50
                """
            )
            rows = cur.fetchall()
            for row in rows:
                payload = _decode_json(row["payload_json"], None)
                if not isinstance(payload, dict):
                    continue
                receive_id = payload.get("receive_id")
                if receive_id:
                    return {
                        "receive_id": receive_id,
                        "receive_id_type": payload.get("receive_id_type", "open_id"),
                        "open_id": payload.get("open_id"),
                    }
            # Fallback: look in feishu_events table (user messages via Feishu webhook).
            # PG greenfield schema: feishu_events PK is ``event_id`` (TEXT), not an
            # integer ``id``; order by the chronological ``received_at`` column
            # (SQLite-era code ordered by ``id`` which does not exist here).
            #
            # Wrap the fallback SELECT in a nested transaction (savepoint). On PG a
            # mid-statement error leaves the connection aborted (INERROR) and poisons
            # every later query on it; a bare ``except: pass`` swallows the error but
            # keeps the connection poisoned. The savepoint rolls back ONLY this
            # statement, restoring the connection to a usable state without touching
            # the caller's outer transaction - so a future schema/column mismatch can
            # never silently poison the pooled connection.
            try:
                with self.conn.transaction():
                    cur.execute(
                        """
                        SELECT payload_json FROM feishu_events
                        WHERE event_type='message'
                        ORDER BY received_at DESC
                        LIMIT 10
                        """
                    )
                    feishu_rows = cur.fetchall()
                for row in feishu_rows:
                    payload = _decode_json(row["payload_json"], None)
                    if not isinstance(payload, dict):
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
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*
                FROM signals s
                INNER JOIN (
                    SELECT symbol, MAX(id) AS max_id
                    FROM signals
                    GROUP BY symbol
                ) latest ON latest.max_id = s.id
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def recent_failed_jobs(self, limit: int = 5, *, days: int = 7) -> list[dict[str, Any]]:
        """Return recent failed agent_jobs within the given day window.

        P1-9 (07-05 final review): previously this query had NO time
        window, so the hourly report kept surfacing failures from weeks
        ago forever. The hourly report (``hourly_report.py:_agent_hourly_brief``)
        reads this list to render "最近失败 N 个". Without a window, old
        failures kept appearing in every hourly report, creating noise
        instead of actionable alerts. Now bound to ``days`` (default 7)
        so only recent failures show up. The diagnostic
        ``_check_failed_jobs_outside_window`` separately classifies
        pre-window failures as ``legacy_info`` for audit.

        P2-NEW-2 (R2 reviewer): ``finished_at IS NULL`` bypassed the
        7-day window for crashed/orphaned jobs that never set
        ``finished_at``. Such a job would permanently appear in the
        recent-failures list even months after the crash. The fix uses
        ``COALESCE(finished_at, started_at)`` as the time reference, so
        a NULL-finished_at job falls back to ``started_at`` for the
        7-day window check. Jobs with both ``finished_at`` and
        ``started_at`` older than the window are excluded.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_type, priority, session_id, error_message, finished_at
                FROM agent_jobs
                WHERE status='failed'
                  AND COALESCE(finished_at, started_at) >= NOW() - make_interval(days => %s)
                ORDER BY id DESC
                LIMIT %s
                """,
                (int(days), int(limit)),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def scheduler_success_exists(self, job_name: str, scheduled_time: int) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scheduler_runs WHERE job_name=%s AND scheduled_time=%s AND status='success'",
                (job_name, int(scheduled_time)),
            )
            row = cur.fetchone()
        return bool(row)

    def create_scheduler_run(self, job_name: str, scheduled_time: int) -> int:
        # 07-16 cutover: ON CONFLICT ... DO UPDATE ... RETURNING id collapses the
        # old INSERT + separate ``SELECT id`` into one statement (design §6:
        # prefer ON CONFLICT RETURNING; removes the re-SELECT + the
        # last_insert_rowid() reliance). Wrapped in ``conn.transaction()``.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scheduler_runs(job_name, scheduled_time, started_at, status)
                    VALUES (%s, %s, NOW(), 'running')
                    ON CONFLICT(job_name, scheduled_time) DO UPDATE SET
                        started_at=NOW(),
                        status='running',
                        error_message=NULL
                    RETURNING id
                    """,
                    (job_name, int(scheduled_time)),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def finish_scheduler_run(self, run_id: int, *, status: str, result: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE scheduler_runs
                    SET status=%s, finished_at=NOW(), result_json=%s, error_message=%s
                    WHERE id=%s
                    """,
                    (status, json.dumps(result or {}, ensure_ascii=False), error_message, int(run_id)),
                )

    def acquire_lock(self, lock_name: str, owner: str, ttl_seconds: int) -> bool:
        # 07-16 cutover: ``task_locks.lock_name`` is the PK. The SQLite pattern
        # (DELETE expired, then ``try INSERT / except IntegrityError -> False``)
        # relied on autocommit. On psycopg a PK-violating INSERT would ABORT the
        # caller's outer transaction, so use ``ON CONFLICT DO NOTHING`` and key
        # the result on ``rowcount`` (1 = newly acquired, 0 = still held by
        # another owner -> False) -- no exception, no outer-txn abort. The whole
        # acquire (DELETE expired + INSERT) is one ``conn.transaction()`` so the
        # two statements are atomic (a concurrent acquirer cannot slip between
        # the DELETE and the INSERT on PG's READ COMMITTED + the PK conflict).
        locked_until = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + ttl_seconds, timezone.utc
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_locks WHERE lock_name=%s AND locked_until <= NOW()",
                    (lock_name,),
                )
                cur.execute(
                    """
                    INSERT INTO task_locks(lock_name, owner, locked_until, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (lock_name) DO NOTHING
                    """,
                    (lock_name, owner, locked_until),
                )
                acquired = int(cur.rowcount)
        return acquired == 1

    def release_lock(self, lock_name: str, owner: str | None = None) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                if owner:
                    cur.execute(
                        "DELETE FROM task_locks WHERE lock_name=%s AND owner=%s",
                        (lock_name, owner),
                    )
                else:
                    cur.execute(
                        "DELETE FROM task_locks WHERE lock_name=%s",
                        (lock_name,),
                    )

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
        # 07-16 cutover: ``paper_orders`` has ``UNIQUE(signal_id)`` (no unique on
        # ga_decision_id). The SQLite ``try INSERT / except IntegrityError ->
        # SELECT existing`` pattern relied on autocommit; on psycopg a
        # UNIQUE-violating INSERT would ABORT the caller's outer transaction
        # (user hard rule: no catch-UniqueViolation-then-rollback). So use
        # ``ON CONFLICT (signal_id) DO NOTHING ... RETURNING id`` (design §6):
        # RETURNING yields the new id on success; 0 rows means signal_id already
        # had an order (the conflict target) -> SELECT the existing id then. A
        # NULL signal_id never conflicts (PG UNIQUE treats NULLs as distinct),
        # matching SQLite. ``risk_check_passed`` is a raw bool (PG BOOLEAN).
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_orders(
                        signal_id, ga_decision_id, symbol, side, order_type, entry_price, trigger_price,
                        stop_loss, initial_stop_loss, take_profit_json, quantity, risk_percent, reason, fill_method, source, risk_check_passed,
                        expires_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (signal_id) DO NOTHING
                    RETURNING id
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
                        trade_plan["stop_loss"],  # initial_stop_loss = stop_loss at creation
                        json.dumps(trade_plan.get("take_profits", []), ensure_ascii=False),
                        trade_plan.get("quantity"),
                        trade_plan.get("risk_percent"),
                        trade_plan.get("reason"),
                        trade_plan.get("fill_method"),
                        source,
                        bool(risk_check_passed),
                        expires_at,
                    ),
                )
                row = cur.fetchone()
                if row is not None:
                    return int(row["id"]), True
                # Conflict: signal_id already had an order. Prefer the more
                # specific ga_decision_id match if provided, else the signal_id
                # match (the actual conflict target, guaranteed to exist).
                if ga_decision_id is not None:
                    cur.execute(
                        "SELECT id FROM paper_orders WHERE ga_decision_id=%s",
                        (int(ga_decision_id),),
                    )
                    existing = cur.fetchone()
                    if existing:
                        return int(existing["id"]), False
                cur.execute(
                    "SELECT id FROM paper_orders WHERE signal_id=%s",
                    (int(signal_id),),
                )
                existing = cur.fetchone()
                return int(existing["id"]), False

    def list_open_paper_orders(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM paper_orders WHERE status IN ('pending','open','needs_recheck') ORDER BY id"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def list_open_paper_orders_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM paper_orders WHERE status IN ('pending','open','needs_recheck') AND symbol=%s ORDER BY id",
                (symbol,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def update_paper_order_status(self, order_id: int, status: str, *, filled_at: str | None = None, closed_at: str | None = None) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_orders
                    SET status=%s, filled_at=COALESCE(%s, filled_at), closed_at=COALESCE(%s, closed_at)
                    WHERE id=%s
                    """,
                    (status, filled_at, closed_at, int(order_id)),
                )

    def update_paper_order_stop_loss(self, order_id: int, stop_loss: float, *, reason: str, price_meta: dict | None = None) -> bool:
        """Atomically update the stop_loss on a paper_order and sync to trade/position.

        All steps (CAS UPDATE, audit log, trade/position sync) are wrapped in a
        single SAVEPOINT so they succeed or roll back together.

        Returns True if the row was actually changed (and a log was emitted),
        False if the order does not exist, the new stop equals the current
        one, or a concurrent writer already changed the row (rowcount == 0).
        """
        new_stop = float(stop_loss)
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_orders WHERE id=%s", (int(order_id),))
            row = cur.fetchone()
        if not row:
            return False
        old_stop = row["stop_loss"]
        if old_stop is not None and abs(float(old_stop) - new_stop) < 1e-8:
            return False
        side = str(row["side"]).lower()

        # Controlled-failure sentinel: a ``return False`` inside
        # ``with pg_db.savepoint()`` is a CLEAN exit -> the savepoint RELEASEs,
        # keeping partial writes. To roll back ONLY the local statements on a
        # controlled failure (rowcount==0 / unknown side), raise this sentinel
        # inside the savepoint and catch it right outside. Unexpected
        # exceptions propagate (savepoint rolls back) and are caught by the
        # outer except.
        class _StopLossAbort(Exception):
            pass

        try:
            with pg_db.savepoint(self.conn):
                with self.conn.cursor() as cur:
                    if old_stop is None:
                        cur.execute(
                            "UPDATE paper_orders SET stop_loss=%s WHERE id=%s AND stop_loss IS NULL AND status='open'",
                            (new_stop, int(order_id)),
                        )
                    elif side == "long":
                        cur.execute(
                            "UPDATE paper_orders SET stop_loss=%s WHERE id=%s AND stop_loss=%s "
                            "AND status='open' AND %s >= stop_loss",
                            (new_stop, int(order_id), float(old_stop), new_stop),
                        )
                    elif side == "short":
                        cur.execute(
                            "UPDATE paper_orders SET stop_loss=%s WHERE id=%s AND stop_loss=%s "
                            "AND status='open' AND %s <= stop_loss",
                            (new_stop, int(order_id), float(old_stop), new_stop),
                        )
                    else:
                        raise _StopLossAbort()
                    if int(cur.rowcount) == 0:
                        raise _StopLossAbort()

                    event = {"order_id": int(order_id), "old_stop_loss": old_stop, "new_stop_loss": new_stop}
                    if price_meta:
                        event.update(price_meta)
                    self.log_paper_trade_event(
                        event_type="stop_loss_adjustment",
                        symbol=row["symbol"],
                        side=row["side"],
                        price=new_stop,
                        quantity=row["quantity"],
                        reason=reason,
                        event=event,
                    )

                    cur.execute(
                        "UPDATE paper_trades SET stop_loss=%s WHERE order_id=%s AND closed_at IS NULL",
                        (new_stop, int(order_id)),
                    )
                    if int(cur.rowcount) == 0:
                        raise _StopLossAbort()
                    cur.execute(
                        "SELECT id FROM paper_trades WHERE order_id=%s AND closed_at IS NULL LIMIT 1",
                        (int(order_id),),
                    )
                    trade_row = cur.fetchone()
                    if trade_row:
                        cur.execute(
                            "UPDATE paper_positions SET stop_loss=%s WHERE id=%s AND status='open'",
                            (new_stop, int(trade_row["id"])),
                        )
                        if int(cur.rowcount) == 0:
                            raise _StopLossAbort()
        except _StopLossAbort:
            return False
        except Exception:
            return False
        return True

    def update_stop_loss_across_tables(
        self,
        trade_id: int,
        order_id: int,
        new_stop: float,
        *,
        old_stop: float,
        reason: str,
        price_meta: dict | None = None,
    ) -> bool:
        """Atomic conditional UPDATE of stop_loss across paper_trades, paper_positions,
        and paper_orders. Uses CAS on paper_trades (compare-and-swap with old_stop).
        All three table updates are wrapped in a SAVEPOINT so they succeed or roll
        back together. Requires each table to update exactly the expected number of
        rows; otherwise rolls back the entire operation.

        Returns True if the paper_trades row was updated (winner of concurrent write),
        False if the old_stop no longer matches (concurrent writer changed it first),
        a related row is missing, or any step fails.
        """
        class _StopAcrossAbort(Exception):
            pass

        try:
            with pg_db.savepoint(self.conn):
                with self.conn.cursor() as cur:
                    # 1. CAS on paper_trades — only the winner proceeds
                    cur.execute(
                        "UPDATE paper_trades SET stop_loss=%s WHERE id=%s AND stop_loss=%s",
                        (float(new_stop), int(trade_id), float(old_stop)),
                    )
                    if int(cur.rowcount) == 0:
                        raise _StopAcrossAbort()

                    # 2. Exact update on paper_positions by trade_id (convention: position.id == trade.id)
                    cur.execute(
                        "UPDATE paper_positions SET stop_loss=%s WHERE id=%s AND status='open'",
                        (float(new_stop), int(trade_id)),
                    )
                    if int(cur.rowcount) == 0:
                        raise _StopAcrossAbort()

                    # 3. CAS on paper_orders (status guard)
                    if order_id:
                        cur.execute(
                            "UPDATE paper_orders SET stop_loss=%s WHERE id=%s AND status='open'",
                            (float(new_stop), int(order_id)),
                        )
                        if int(cur.rowcount) == 0:
                            raise _StopAcrossAbort()

                    # 4. Log the event
                    event = {
                        "trade_id": int(trade_id),
                        "order_id": int(order_id),
                        "old_stop_loss": float(old_stop),
                        "new_stop_loss": float(new_stop),
                        "reason": reason,
                    }
                    if price_meta:
                        event.update(price_meta)
                    cur.execute(
                        "SELECT symbol, side FROM paper_trades WHERE id=%s",
                        (int(trade_id),),
                    )
                    trade_row = cur.fetchone()
                    if trade_row:
                        self.log_paper_trade_event(
                            event_type="stop_loss_adjustment",
                            symbol=trade_row["symbol"],
                            side=trade_row["side"],
                            price=float(new_stop),
                            reason=reason,
                            event=event,
                        )
            return True
        except _StopAcrossAbort:
            return False
        except Exception:
            return False

    def create_paper_trade(self, order: dict[str, Any], entry_price: float, *, fill_method: str | None = None, event_time: int | None = None, allow_wall_clock: bool = False) -> int:
        # Guard: one order can only have one open trade
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM paper_trades WHERE order_id=%s AND closed_at IS NULL LIMIT 1",
                (int(order["id"]),),
            )
            existing = cur.fetchone()
        if existing:
            return int(existing["id"])
        # BTC#9 R3-B fix: use event_time (candle.close_time) for all timestamps
        # to eliminate historical backfill time-travel. Fail-closed unless
        # allow_wall_clock=True is explicitly passed.
        if event_time is not None and int(event_time) > 0:
            from plugins.crypto_guard.utils import iso_utc_from_ms
            ts_iso = iso_utc_from_ms(int(event_time))
        elif allow_wall_clock:
            ts_iso = utc_iso()
        else:
            # R3-B: truly fail-closed - no trade, no side effects
            raise ValueError(
                "create_paper_trade requires event_time (candle close_time ms) "
                "for replay fills; pass allow_wall_clock=True for explicit live mode"
            )
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_trades(
                        order_id, symbol, side, entry_price, stop_loss,
                        initial_stop_loss, initial_risk_usdt,
                        take_profit_json, quantity, max_favorable_excursion, max_adverse_excursion,
                        entry_efficiency, exit_efficiency, signal_decay_score, stop_take_path_json, fill_method,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, NULL, NULL, 0, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        order["id"],
                        order["symbol"],
                        order["side"],
                        entry_price,
                        order.get("stop_loss"),
                        order.get("initial_stop_loss") or order.get("stop_loss"),
                        _compute_initial_risk_usdt(order, entry_price),
                        _json_dumps_value(order.get("take_profit_json")),
                        order.get("quantity"),
                        json.dumps([{"event": "filled", "entry_price": entry_price, "ts": ts_iso}], ensure_ascii=False),
                        fill_method or order.get("fill_method"),
                        ts_iso,
                    ),
                )
                trade_id = int(cur.fetchone()["id"])
        account = self.ensure_paper_account()
        position_id = self.upsert_paper_position_from_trade(
            account_id=int(account["id"]),
            trade={**order, "id": trade_id, "entry_price": entry_price, "current_price": entry_price},
            status="open",
            current_price=float(entry_price),
            event_time=event_time,
            allow_wall_clock=allow_wall_clock,
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
            event_time=event_time,
        )
        return trade_id

    def get_open_trade_for_order(self, order_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM paper_trades WHERE order_id=%s AND closed_at IS NULL",
                (int(order_id),),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def update_paper_trade_quality(self, trade_id: int, *, mfe: float, mae: float, stop_take_path: list[dict[str, Any]]) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_trades
                    SET max_favorable_excursion=%s,
                        max_adverse_excursion=%s,
                        stop_take_path_json=%s
                    WHERE id=%s AND closed_at IS NULL
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
        event_time: int | None = None,
        allow_wall_clock: bool = False,
    ) -> bool:
        """Atomically close a paper trade.

        Returns True if the row was updated (winner of any concurrent close),
        False if the trade was already closed (WHERE closed_at IS NULL matched
        no row). Callers MUST check the return value before executing side
        effects (logs, enqueues, position upserts) so that only the winner
        performs them.

        BTC#9 R3-B: event_time (candle close_time ms) is used for closed_at.
        When allow_wall_clock=True, falls back to utc_iso() for live mode.
        When neither event_time nor allow_wall_clock is provided, raises
        ValueError (fail-closed).
        """
        # R3-B: determine closed_at timestamp
        if event_time is not None and int(event_time) > 0:
            from plugins.crypto_guard.utils import iso_utc_from_ms
            closed_at_iso = iso_utc_from_ms(int(event_time))
        elif allow_wall_clock:
            closed_at_iso = utc_iso()
        else:
            raise ValueError(
                "close_paper_trade requires event_time (candle close_time ms) "
                "for replay closes; pass allow_wall_clock=True for explicit live mode"
            )
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_trades
                    SET exit_price=%s, close_reason=%s, pnl=%s, pnl_percent=%s, pnl_r=%s,
                        max_favorable_excursion=%s, max_adverse_excursion=%s,
                        entry_efficiency=%s, exit_efficiency=%s, signal_decay_score=%s,
                        stop_take_path_json=COALESCE(%s, stop_take_path_json),
                        closed_at=%s
                    WHERE id=%s AND closed_at IS NULL
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
                        closed_at_iso,
                        int(trade_id),
                    ),
                )
                affected = int(cur.rowcount)
        return affected > 0

    def backfill_active_evaluation_pnl_r(self, trade: dict[str, Any], pnl_r: float) -> int:
        """Backfill real pnl_r to active strategy_evaluations (is_shadow=0) using exact ga_decision_id.
        One trade updates at most one active evaluation (LIMIT 1 via exact ID match).
        Only updates rows where outcome_source IS NULL.

        Returns number of evaluation rows updated.
        """
        order_id = trade.get("order_id")
        if not order_id:
            return 0

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT ga_decision_id, symbol FROM paper_orders WHERE id=%s",
                (int(order_id),),
            )
            order = cur.fetchone()
        if not order or not order["ga_decision_id"]:
            return 0

        gd_id = int(order["ga_decision_id"])

        # Exact ga_decision_id match - no +/-1h fuzzy matching, LIMIT 1
        # Guard: only rows where outcome_source='pending_outcome' (not already classified)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE strategy_evaluations
                    SET pnl_r=%s, ga_decision_id=%s, paper_trade_id=%s, outcome_source='real_pnl'
                    WHERE id IN (SELECT id FROM strategy_evaluations WHERE ga_decision_id=%s AND is_shadow=FALSE AND pnl_r IS NULL AND outcome_source='pending_outcome' LIMIT 1)
                    """,
                    (float(pnl_r), gd_id, int(trade.get("id") or 0), gd_id),
                )
                updated = int(cur.rowcount)
        return updated

    def backfill_historical_active_pnl_r(self) -> dict[str, int]:
        """One-shot: backfill pnl_r from all closed paper_trades to active evaluations.

        Iterates closed trades with real pnl_r, traces to ga_decision for
        strategy_name + analysis_time, and backfills matching active evals (is_shadow=0).

        Returns {trades_processed, evaluations_updated}.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT pt.id, pt.order_id, pt.pnl_r
                FROM paper_trades pt
                WHERE pt.closed_at IS NOT NULL
                  AND pt.pnl_r IS NOT NULL
                  AND (pt.close_reason IS NULL OR pt.close_reason != 'duplicate_cleanup')
                """
            )
            closed_trades = cur.fetchall()

        trades_processed = 0
        total_updated = 0

        for trade_row in closed_trades:
            updated = self.backfill_active_evaluation_pnl_r(
                {"order_id": trade_row["order_id"]},
                float(trade_row["pnl_r"]),
            )
            if updated > 0:
                trades_processed += 1
                total_updated += updated

        return {"trades_processed": trades_processed, "evaluations_updated": total_updated}

    # ── shadow_virtual_trades ──────────────────────────────────────────

    def _insert_shadow_virtual_trade(self, candidate_version: str, ga_decision_id: int,
                                     symbol: str, side: str, entry_price: float,
                                     stop_loss: float, initial_stop_loss: float,
                                     take_profit_json: str, quantity: float,
                                     initial_risk_usdt: float, *,
                                     strategy_name: str = "smc_pullback_long",
                                     entry_type: str = "market",
                                     max_pending_minutes: int = 120) -> int:
        """Insert a shadow_virtual_trade row WITHOUT self-committing.

        The caller wraps this in ``conn.transaction()`` (either its own outer
        txn -> this INSERT runs as a savepoint-free statement inside it, or a
        standalone caller like ``create_shadow_virtual_trade`` wraps the call).
        Returns the new id via RETURNING.
        """
        entry_type_lower = str(entry_type).lower()
        status = "pending_entry"
        now = datetime.now(timezone.utc)
        opened_at = None
        expires_at = (now + timedelta(minutes=max_pending_minutes)).isoformat()

        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO shadow_virtual_trades(
                    strategy_name, candidate_version, ga_decision_id, symbol, side,
                    entry_type, entry_price, stop_loss, initial_stop_loss, take_profit_json,
                    quantity, initial_risk_usdt, status, opened_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id""",
                (strategy_name, candidate_version, ga_decision_id, symbol, side,
                 entry_type_lower, entry_price, stop_loss, initial_stop_loss, take_profit_json,
                 quantity, initial_risk_usdt, status, opened_at, expires_at),
            )
            row = cur.fetchone()
        return int(row["id"])

    def create_shadow_virtual_trade(self, candidate_version: str, ga_decision_id: int,
                                    symbol: str, side: str, entry_price: float,
                                    stop_loss: float, initial_stop_loss: float,
                                    take_profit_json: str, quantity: float,
                                    initial_risk_usdt: float, *,
                                    strategy_name: str = "smc_pullback_long",
                                    entry_type: str = "market",
                                    max_pending_minutes: int = 120) -> int:
        """Create a shadow virtual trade for a candidate version.

        Idempotent: checks for existing row by (strategy_name, candidate_version, ga_decision_id)
        first. If it exists, returns the existing ID without overwriting.
        Otherwise inserts a new row and commits.

        Status logic:
          - ALL entry types start as 'pending_entry' — the updater activates
            them to 'open' when the first closed candle arrives.
          - entry_type='market' activates immediately on first candle (always true).
          - entry_type='limit'/'trigger'/'stop' activates when price condition is met.

        max_pending_minutes controls expires_at for pending_entry trades (default 120 min).
        """
        # Check for existing row first (idempotent, avoids INSERT OR REPLACE which resets timestamps)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM shadow_virtual_trades"
                " WHERE strategy_name=%s AND candidate_version=%s AND ga_decision_id=%s",
                (strategy_name, candidate_version, ga_decision_id),
            )
            existing = cur.fetchone()
        if existing:
            return int(existing["id"])

        with self.conn.transaction():
            vt_id = self._insert_shadow_virtual_trade(
                candidate_version=candidate_version,
                ga_decision_id=ga_decision_id,
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_loss=stop_loss,
                initial_stop_loss=initial_stop_loss,
                take_profit_json=take_profit_json,
                quantity=quantity,
                initial_risk_usdt=initial_risk_usdt,
                strategy_name=strategy_name,
                entry_type=entry_type,
                max_pending_minutes=max_pending_minutes,
            )
        return vt_id

    def create_shadow_evaluation_with_vt(self, strategy_name: str, strategy_version: str,
                                          ga_decision_id: int, symbol: str, analysis_time: int,
                                          outcome_source: str, *,
                                          vt_kwargs: dict | None = None,
                                          timeframe: str = "15m",
                                          score: float = 0.0,
                                          decision: str = "",
                                          evidence: dict | None = None,
                                          counter_evidence: dict | None = None,
                                          snapshot_id: int | None = None) -> dict:
        """Atomically create shadow evaluation + optional virtual trade.

        Creates both the strategy_evaluation and optional shadow_virtual_trade in
        a single transaction. If either fails, both roll back. When vt_kwargs is
        provided, the created VT is linked to the evaluation via
        shadow_virtual_trade_id.

        Uses _insert_shadow_virtual_trade (no internal commit) so VT insertion
        participates in the outer BEGIN IMMEDIATE transaction.

        Returns {"eval_id": int, "vt_id": int | None}
        """
        try:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    # Insert evaluation
                    cur.execute(
                        """INSERT INTO strategy_evaluations(symbol, analysis_time, strategy_name,
                           strategy_version, ga_decision_id, is_shadow, outcome_source, timeframe,
                           score, decision, evidence_json, counter_evidence_json, snapshot_id,
                           created_at)
                           VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, NOW())
                           RETURNING id""",
                        (symbol, analysis_time, strategy_name, strategy_version,
                         ga_decision_id, outcome_source, timeframe, score, decision,
                         "{}" if evidence is None else json.dumps(evidence, ensure_ascii=False),
                         "{}" if counter_evidence is None else json.dumps(counter_evidence, ensure_ascii=False),
                         snapshot_id))
                    eval_id = int(cur.fetchone()["id"])

                    vt_id = None
                    if vt_kwargs:
                        vt_id = self._insert_shadow_virtual_trade(
                            candidate_version=strategy_version,
                            ga_decision_id=ga_decision_id,
                            strategy_name=strategy_name,
                            **vt_kwargs
                        )
                        # Link evaluation to VT
                        cur.execute(
                            "UPDATE strategy_evaluations SET shadow_virtual_trade_id=%s WHERE id=%s",
                            (vt_id, eval_id))
            return {"eval_id": eval_id, "vt_id": vt_id}
        except Exception:
            raise

    def update_shadow_virtual_trade_prices(self, virtual_trade_id: int,
                                           current_price: float) -> None:
        """Update unrealized PnL for an open shadow virtual trade.

        current_r = (current_price - entry_price) * quantity / initial_risk_usdt
        Tracks max_favorable_excursion and max_adverse_excursion.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT entry_price, quantity, initial_risk_usdt, side,"
                " max_favorable_excursion, max_adverse_excursion"
                " FROM shadow_virtual_trades WHERE id=%s AND status='open'",
                (virtual_trade_id,),
            )
            trade = cur.fetchone()
        if not trade:
            return
        entry = float(trade["entry_price"])
        qty = float(trade["quantity"])
        risk = float(trade["initial_risk_usdt"])
        side = str(trade["side"])
        if risk <= 0:
            return
        multiplier = 1.0 if side == "LONG" else -1.0
        current_r = (current_price - entry) * multiplier * qty / risk
        mfe = max(float(trade["max_favorable_excursion"] or 0), max(current_r, 0.0))
        mae = min(float(trade["max_adverse_excursion"] or 0), min(current_r, 0.0))
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE shadow_virtual_trades SET current_price=%s, unrealized_pnl_r=%s,"
                    " max_favorable_excursion=%s, max_adverse_excursion=%s, updated_at=NOW()"
                    " WHERE id=%s",
                    (current_price, current_r, mfe, mae, virtual_trade_id),
                )

    def close_shadow_virtual_trade(self, virtual_trade_id: int,
                                   close_price: float, close_reason: str) -> dict | None:
        """Close a shadow virtual trade and return the closed trade dict with pnl_r.

        After closing, backfills the corresponding strategy_evaluations row with
        pnl_r, outcome_source='real_pnl', and shadow_virtual_trade_id.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM shadow_virtual_trades WHERE id=%s AND status IN ('open', 'pending_entry')",
                (virtual_trade_id,),
            )
            trade = cur.fetchone()
        if not trade:
            return None
        entry = float(trade["entry_price"])
        qty = float(trade["quantity"])
        risk = float(trade["initial_risk_usdt"])
        side = str(trade["side"])
        if risk <= 0:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shadow_virtual_trades SET status='closed', close_reason=%s,"
                        " closed_at=NOW(), pnl_r=0, updated_at=NOW() WHERE id=%s",
                        (close_reason, virtual_trade_id),
                    )
            return dict(trade)
        multiplier = 1.0 if side == "LONG" else -1.0
        pnl_r = (close_price - entry) * multiplier * qty / risk

        # Backfill candidate evaluation with real PnL (or ambiguous path)
        strategy_name = str(trade["strategy_name"])
        candidate_version = str(trade["candidate_version"])
        ga_decision_id = int(trade["ga_decision_id"])
        # activation_ambiguous_path and ambiguous_path must NOT be counted as real_pnl
        if close_reason in ("activation_ambiguous_path", "ambiguous_path"):
            eval_outcome = "ambiguous_path"
        else:
            eval_outcome = "real_pnl"
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE shadow_virtual_trades SET status='closed', close_reason=%s,"
                    " closed_at=NOW(), pnl_r=%s, updated_at=NOW()"
                    " WHERE id=%s",
                    (close_reason, pnl_r, virtual_trade_id),
                )
                cur.execute(
                    """
                    UPDATE strategy_evaluations
                    SET pnl_r=%s, outcome_source=%s, shadow_virtual_trade_id=%s
                    WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE
                      AND ga_decision_id=%s AND pnl_r IS NULL
                    """,
                    (pnl_r, eval_outcome, virtual_trade_id, strategy_name, candidate_version, ga_decision_id),
                )

        trade_dict = dict(trade)
        trade_dict["pnl_r"] = pnl_r
        trade_dict["status"] = "closed"
        trade_dict["close_reason"] = close_reason
        return trade_dict

    def list_open_shadow_virtual_trades(self) -> list[dict]:
        """Return all currently open or pending-entry shadow virtual trades."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM shadow_virtual_trades WHERE status IN ('open', 'pending_entry') ORDER BY created_at"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def update_shadow_virtual_trade_status(self, virtual_trade_id: int, status: str,
                                           *, event_time: str | None = None) -> None:
        """Transition a shadow virtual trade to a new status (e.g. pending_entry -> open).

        When transitioning to 'open', sets opened_at and expires_at.
        Uses event_time (ISO string) if provided, otherwise falls back to wall clock.
        """
        from datetime import datetime, timezone, timedelta

        if status == "open":
            if event_time is not None:
                now = datetime.fromisoformat(event_time)
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
            else:
                now = datetime.now(timezone.utc)
            expires_at = (now + timedelta(minutes=4320)).isoformat()
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shadow_virtual_trades SET status=%s, opened_at=%s, expires_at=%s, updated_at=NOW() WHERE id=%s",
                        (status, now.isoformat(), expires_at, virtual_trade_id),
                    )
        else:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shadow_virtual_trades SET status=%s, updated_at=NOW() WHERE id=%s",
                        (status, virtual_trade_id),
                    )

    def list_shadow_virtual_trades_for_candidate(self, candidate_version: str) -> list[dict]:
        """Return all shadow virtual trades for a specific candidate version."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM shadow_virtual_trades WHERE candidate_version=%s ORDER BY created_at",
                (candidate_version,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def save_equity_snapshot(self, snapshot: dict[str, Any]) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_equity_snapshots(ts, account_equity, unrealized_pnl, realized_pnl, margin_used, open_position_count, snapshot_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                new_id = int(cur.fetchone()["id"])
        return new_id

    def ensure_paper_account(self, account_name: str = "default", initial_balance: float = 10000.0) -> dict[str, Any]:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_accounts(account_name, initial_balance, current_balance, equity)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(account_name) DO NOTHING
                    """,
                    (account_name, float(initial_balance), float(initial_balance), float(initial_balance)),
                )
                cur.execute(
                    "SELECT * FROM paper_accounts WHERE account_name=%s",
                    (account_name,),
                )
                row = cur.fetchone()
        return dict(row)

    def update_paper_account_from_snapshot(self, snapshot: dict[str, Any], account_name: str = "default") -> dict[str, Any]:
        account = self.ensure_paper_account(account_name)
        equity = float(snapshot.get("account_equity") or account["equity"])
        initial = float(account["initial_balance"] or 10000.0)
        drawdown = min(float(account.get("max_drawdown") or 0), (equity - initial) / initial if initial else 0)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_accounts
                    SET current_balance=%s, equity=%s, realized_pnl=%s, unrealized_pnl=%s, max_drawdown=%s, updated_at=NOW()
                    WHERE id=%s
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
                cur.execute(
                    "SELECT * FROM paper_accounts WHERE id=%s",
                    (int(account["id"]),),
                )
                row = cur.fetchone()
        return dict(row)

    def upsert_paper_position_from_trade(
        self,
        *,
        account_id: int,
        trade: dict[str, Any],
        status: str = "open",
        current_price: float | None = None,
        unrealized_pnl: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        event_time: int | None = None,
        allow_wall_clock: bool = False,
    ) -> int:
        # R3-B: determine timestamp for opened_at/updated_at/closed_at
        if event_time is not None and int(event_time) > 0:
            from plugins.crypto_guard.utils import iso_utc_from_ms
            ts_iso = iso_utc_from_ms(int(event_time))
        elif allow_wall_clock:
            ts_iso = utc_iso()
        else:
            # Fail-closed for replay; live callers must pass allow_wall_clock=True
            # For position upserts that happen during fill/close, event_time is mandatory
            raise ValueError(
                "upsert_paper_position_from_trade requires event_time "
                "for replay; pass allow_wall_clock=True for live mode"
            )
        position_id = int(trade.get("id") or 0)
        with self.conn.cursor() as cur:
            if position_id:
                cur.execute("SELECT id FROM paper_positions WHERE id=%s", (position_id,))
                row = cur.fetchone()
            else:
                row = None
        if row:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE paper_positions
                        SET current_price=%s, stop_loss=%s, take_profit_json=%s, unrealized_pnl=%s, unrealized_pnl_pct=%s,
                            max_favorable_excursion=%s, max_adverse_excursion=%s, status=%s,
                            closed_at=CASE WHEN %s!='open' THEN %s ELSE closed_at END,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (
                            current_price,
                            trade.get("stop_loss"),
                            _json_dumps_value(trade.get("take_profit_json")),
                            float(unrealized_pnl),
                            float(unrealized_pnl_pct),
                            float(trade.get("max_favorable_excursion") or 0),
                            float(trade.get("max_adverse_excursion") or 0),
                            status,
                            status,
                            ts_iso,
                            ts_iso,
                            position_id,
                        ),
                    )
            return position_id
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_positions(
                        id, account_id, symbol, side, entry_price, current_price, quantity, stop_loss, take_profit_json,
                        unrealized_pnl, unrealized_pnl_pct, max_favorable_excursion, max_adverse_excursion, status,
                        opened_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        _json_dumps_value(trade.get("take_profit_json")),
                        float(unrealized_pnl),
                        float(unrealized_pnl_pct),
                        float(trade.get("max_favorable_excursion") or 0),
                        float(trade.get("max_adverse_excursion") or 0),
                        status,
                        ts_iso,
                        ts_iso,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

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
        event_time: int | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        # BTC#9 fix: use event_time (ms) for the event log timestamp when provided;
        # fall back to utc_iso() for live non-replay paths.
        if event_time is not None and int(event_time) > 0:
            from plugins.crypto_guard.utils import iso_utc_from_ms
            ts_iso = iso_utc_from_ms(int(event_time))
        else:
            ts_iso = utc_iso()
        event_payload = dict(event or {})
        event_payload.setdefault("ts", ts_iso)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_trade_logs(position_id, event_type, symbol, side, price, quantity, pnl, pnl_pct, reason, event_json, dedupe_key, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                        json.dumps(event_payload, ensure_ascii=False),
                        dedupe_key,
                        ts_iso,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def sum_closed_realized_pnl(self) -> float:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(pnl), 0) AS total FROM paper_trades WHERE closed_at IS NOT NULL")
            row = cur.fetchone()
        return float(row["total"] or 0)

    def list_open_paper_trades(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trades WHERE closed_at IS NULL ORDER BY id")
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def latest_equity_snapshot(self) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_equity_snapshots ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        return dict(row) if row else None

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trades WHERE id=%s", (int(trade_id),))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_market_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM market_snapshots WHERE id=%s", (int(snapshot_id),))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_trade_review_by_trade(self, trade_id: int) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM trade_reviews WHERE trade_id=%s ORDER BY id DESC LIMIT 1", (int(trade_id),))
            row = cur.fetchone()
        return dict(row) if row else None

    def list_closed_trades_for_review(self, *, start_utc: str | None = None, end_utc: str | None = None, only_unreviewed: bool = True) -> list[dict[str, Any]]:
        where = ["t.closed_at IS NOT NULL"]
        params: list[Any] = []
        if start_utc:
            where.append("t.closed_at >= %s")
            params.append(start_utc)
        if end_utc:
            where.append("t.closed_at < %s")
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
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def save_trade_review(self, trade_id: int, review: dict[str, Any]) -> int:
        # Ensure market_regime_at_loss is serialized as JSON string if it's a dict
        regime_at_loss = review.get("market_regime_at_loss")
        if isinstance(regime_at_loss, dict):
            regime_at_loss = json.dumps(regime_at_loss, ensure_ascii=False)
        elif regime_at_loss is None:
            regime_at_loss = "unknown"
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trade_reviews(
                        trade_id, result, primary_reason, secondary_reasons_json, market_context,
                        improvement_suggestion, ga_review_json, market_regime_at_loss, evolution_trigger_allowed
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        int(trade_id),
                        review["result"],
                        review["primary_reason"],
                        json.dumps(review.get("secondary_reasons", []), ensure_ascii=False),
                        review.get("summary"),
                        json.dumps(review.get("improvement_suggestion", {}), ensure_ascii=False),
                        json.dumps(review, ensure_ascii=False),
                        regime_at_loss,
                        bool(review.get("evolution_trigger_allowed", True)),
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

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
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO daily_review_reports(review_date, summary_json, ga_report, skill_updates_json, evolution_actions_json, pushed_to_feishu)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(review_date) DO UPDATE SET
                        summary_json=excluded.summary_json,
                        ga_report=excluded.ga_report,
                        skill_updates_json=excluded.skill_updates_json,
                        evolution_actions_json=excluded.evolution_actions_json,
                        pushed_to_feishu=excluded.pushed_to_feishu
                    RETURNING id
                    """,
                    (
                        review_date,
                        _json_dumps_payload(summary),
                        ga_report,
                        json.dumps(skill_updates or [], ensure_ascii=False),
                        json.dumps(evolution_actions or {}, ensure_ascii=False),
                        bool(pushed_to_feishu),
                    ),
                )
                row = cur.fetchone()
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
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM evolution_triggers
                WHERE trigger_type=%s AND status IN ('pending','shadow_testing') AND COALESCE(symbol,'')=COALESCE(%s, '')
                ORDER BY id DESC LIMIT 1
                """,
                (trigger_type, symbol),
            )
            existing = cur.fetchone()
        if existing:
            return int(existing["id"])
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO evolution_triggers(
                        trigger_type, strategy_name, symbol, trigger_value, threshold_value, related_trade_ids,
                        original_related_trade_ids, latest_related_trade_ids,
                        market_regime, evolution_allowed, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        trigger_type,
                        strategy_name,
                        symbol,
                        float(trigger_value),
                        float(threshold_value),
                        json.dumps(related_trade_ids or [], ensure_ascii=False),
                        json.dumps(related_trade_ids or [], ensure_ascii=False),
                        json.dumps(related_trade_ids or [], ensure_ascii=False),
                        market_regime,
                        bool(evolution_allowed),
                        status,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def recent_closed_trades(self, limit: int = 10, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "WHERE closed_at IS NOT NULL"
        if symbol:
            where += " AND symbol=%s"
            params.append(symbol)
        params.append(int(limit))
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM paper_trades {where} ORDER BY closed_at DESC, id DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

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

        # Dedup: only dedupe against pending alerts. Sent rows keep their
        # history, so a new enqueue with a fresh payload can reuse the same
        # dedupe_key after a previous send (e.g. periodic reports, retries).
        if dedupe_key:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM alert_outbox WHERE dedupe_key=%s AND status='pending' LIMIT 1",
                    (dedupe_key,),
                )
                existing = cur.fetchone()
            if existing:
                return int(existing["id"])

        # ON CONFLICT DO NOTHING ... RETURNING handles the race where another
        # connection inserts the same dedupe_key between our SELECT and INSERT
        # (the unique index is on (dedupe_key) for pending rows). RETURNING
        # yields the new id on insert; on conflict we re-read the winner.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alert_outbox(alert_type, symbol, priority, payload_json, next_retry_at, dedupe_key)
                    VALUES (%s, %s, %s, %s, NOW(), %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (alert_type, symbol, int(priority), _json_dumps_payload(payload), dedupe_key),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    return int(inserted["id"])
                if dedupe_key:
                    cur.execute(
                        "SELECT id FROM alert_outbox WHERE dedupe_key=%s AND status='pending' LIMIT 1",
                        (dedupe_key,),
                    )
                    winner = cur.fetchone()
                    if winner:
                        return int(winner["id"])
        # Should not reach here: INSERT either succeeds (RETURNING id) or
        # conflicts on a unique constraint whose row we then re-read.
        raise RuntimeError("enqueue_alert: insert returned no row and no dedupe winner found")

    def should_silence_alert(self, *, alert_type: str, symbol: str | None, quiet_minutes: int, never_silence: set[str]) -> bool:
        if alert_type in never_silence:
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM alert_outbox
                WHERE alert_type=%s AND COALESCE(symbol, '')=COALESCE(%s, '')
                  AND status IN ('pending', 'sent')
                  AND created_at >= NOW() - make_interval(mins => %s)
                LIMIT 1
                """,
                (alert_type, symbol, int(quiet_minutes)),
            )
            row = cur.fetchone()
        return bool(row)

    def mark_alert_sent(self, alert_id: int) -> None:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE alert_outbox SET status='sent', updated_at=NOW() WHERE id=%s",
                    (int(alert_id),),
                )

    def mark_alert_failed(self, alert_id: int, error: str, *, max_attempts: int = 3) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM alert_outbox WHERE id=%s", (int(alert_id),))
            row = cur.fetchone()
        if not row:
            return
        retry_count = int(row["retry_count"] or 0) + 1
        if retry_count >= max_attempts:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE alert_outbox
                        SET status='failed', retry_count=%s, last_error=%s, updated_at=NOW()
                        WHERE id=%s
                        """,
                        (retry_count, error[:500], int(alert_id)),
                    )
                    cur.execute(
                        """
                        INSERT INTO alert_failure_log(alert_outbox_id, alert_type, symbol, error_message, retry_count)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (int(alert_id), row["alert_type"], row["symbol"], error[:500], retry_count),
                    )
            return
        delay_seconds = 60 * (2 ** (retry_count - 1))
        next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE alert_outbox
                    SET status='pending', retry_count=%s, next_retry_at=%s, last_error=%s, updated_at=NOW()
                    WHERE id=%s
                    """,
                    (retry_count, next_retry_at, error[:500], int(alert_id)),
                )

    def claim_pending_alerts(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM alert_outbox
                WHERE status='pending' AND COALESCE(next_retry_at, created_at) <= NOW()
                ORDER BY priority ASC, created_at ASC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
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
        with self.conn.cursor() as cur:
            cur.execute("SELECT value_json FROM runtime_config WHERE config_key=%s", (config_key,))
            old = cur.fetchone()
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO config_hot_reload(config_key, old_value, new_value, requested_by, request_text, confirmation_required, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        config_key,
                        old["value_json"] if old else None,
                        json.dumps(new_value, ensure_ascii=False),
                        requested_by,
                        request_text,
                        bool(confirmation_required),
                        "pending" if confirmation_required else "confirmed",
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def apply_config_hot_reload(self, change_id: int) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM config_hot_reload WHERE id=%s", (int(change_id),))
            row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "config change not found"}
        item = dict(row)
        if bool(item.get("confirmation_required")) and not bool(item.get("confirmed")):
            return {"ok": False, "error": "confirmation required", "change_id": change_id}
        summary = f"配置 {item['config_key']} 已热更新：{item.get('old_value') or '-'} -> {item['new_value']}"
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runtime_config(config_key, value_json, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT(config_key) DO UPDATE SET value_json=excluded.value_json, updated_at=NOW()
                    """,
                    (item["config_key"], item["new_value"]),
                )
                cur.execute(
                    """
                    UPDATE config_hot_reload
                    SET confirmed=TRUE, confirmed_at=COALESCE(confirmed_at, NOW()),
                        status='applied', applied_at=NOW(), audit_summary=%s
                    WHERE id=%s
                    """,
                    (summary, int(change_id)),
                )
        return {"ok": True, "change_id": change_id, "audit_summary": summary}

    def confirm_config_hot_reload(self, change_id: int) -> dict[str, Any]:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE config_hot_reload SET confirmed=TRUE, confirmed_at=NOW(), status='confirmed' WHERE id=%s AND status='pending'",
                    (int(change_id),),
                )
        return self.apply_config_hot_reload(change_id)

    def update_strategy_memory_from_review(self, *, strategy_name: str, condition_hash: str, result: str, pnl_r: float, notes: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM strategy_memory WHERE strategy_name=%s AND condition_hash=%s",
                (strategy_name, condition_hash),
            )
            existing = cur.fetchone()
        if not existing:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO strategy_memory(strategy_name, condition_hash, sample_count, win_count, loss_count, avg_rr, avg_pnl_percent, notes)
                        VALUES (%s, %s, 1, %s, %s, %s, %s, %s)
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
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE strategy_memory
                    SET sample_count=%s,
                        win_count=win_count + %s,
                        loss_count=loss_count + %s,
                        avg_rr=%s,
                        avg_pnl_percent=%s,
                        notes=%s,
                        updated_at=NOW()
                    WHERE id=%s
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
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM strategy_memory
                ORDER BY sample_count DESC, updated_at DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def save_strategy_patch_candidate(self, patch: dict[str, Any], evidence: dict[str, Any] | None = None, trigger_id: int | None = None, *, status: str = "draft") -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO strategy_patches(strategy_name, from_version, candidate_version, patch_json, reason, evidence_json, trigger_id, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        patch["strategy_name"],
                        patch["from_version"],
                        patch["candidate_version"],
                        _json_dumps_payload(patch.get("patch", {})),
                        patch.get("change_reason"),
                        _json_dumps_payload(evidence or {}),
                        trigger_id,
                        status,
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def mark_duplicate_patches_rejected(self) -> dict[str, int]:
        """Mark duplicate patches (same trigger_id + candidate_version) as rejected, keeping only the latest."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT trigger_id, candidate_version, COUNT(*) as cnt
                FROM strategy_patches
                WHERE trigger_id IS NOT NULL AND status NOT IN ('rejected', 'duplicate')
                GROUP BY trigger_id, candidate_version
                HAVING COUNT(*) > 1
                """
            )
            duplicates = cur.fetchall()

        rejected = 0
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for dup in duplicates:
                    trigger_id = int(dup["trigger_id"])
                    candidate_version = dup["candidate_version"]
                    # Keep the latest (highest id), reject the rest
                    cur.execute(
                        """
                        UPDATE strategy_patches SET status='duplicate'
                        WHERE trigger_id=%s AND candidate_version=%s AND status NOT IN ('rejected', 'duplicate')
                        AND id NOT IN (
                            SELECT MAX(id) FROM strategy_patches WHERE trigger_id=%s AND candidate_version=%s
                        )
                        """,
                        (trigger_id, candidate_version, trigger_id, candidate_version),
                    )
                    rejected += int(cur.rowcount)

        return {"rejected_duplicates": rejected}

    def cleanup_orphan_patches(self) -> dict[str, int]:
        """Mark strategy_patches as rejected when they have no matching strategy_version.
        Returns counts of {orphans_marked, versions_backfilled}."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT sp.id, sp.strategy_name, sp.candidate_version, sp.status
                FROM strategy_patches sp
                LEFT JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
                WHERE sv.id IS NULL AND sp.status NOT IN ('duplicate', 'rejected')
                """
            )
            orphans = cur.fetchall()

        cleaned = 0
        if orphans:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    for row in orphans:
                        cur.execute(
                            "UPDATE strategy_patches SET status='rejected' WHERE id=%s",
                            (row["id"],),
                        )
                        cleaned += 1

        return {"orphans_cleaned": cleaned}

    def list_strategy_versions(self, strategy_name: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if strategy_name:
            where = "WHERE strategy_name=%s"
            params.append(strategy_name)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM strategy_versions
                {where}
                ORDER BY strategy_name, status='active' DESC, created_at DESC, version DESC
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_strategy_version(self, strategy_name: str, version: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM strategy_versions WHERE strategy_name=%s AND version=%s",
                (strategy_name, version),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def active_strategy_version(self, strategy_name: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM strategy_versions WHERE strategy_name=%s AND status='active' ORDER BY created_at DESC LIMIT 1",
                (strategy_name,),
            )
            row = cur.fetchone()
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
        if status not in {"active", "candidate", "shadow_testing", "deprecated", "review_required", "rejected", "draft"}:
            raise ValueError(f"invalid strategy status: {status}")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO strategy_versions(strategy_name, version, status, config_json, change_reason, created_from_review_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(strategy_name, version) DO UPDATE SET
                        status=excluded.status,
                        config_json=excluded.config_json,
                        change_reason=excluded.change_reason
                    RETURNING id
                    """,
                    (strategy_name, version, status, json.dumps(config, ensure_ascii=False), change_reason, created_from_review_id),
                )
                row = cur.fetchone()
        return int(row["id"])

    def rollback_active_strategy(self, strategy_name: str, target_version: str, change_reason: str) -> dict[str, Any]:
        target = self.get_strategy_version(strategy_name, target_version)
        if not target:
            return {"ok": False, "error": "target strategy version not found"}
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE strategy_versions SET status='deprecated' WHERE strategy_name=%s AND status='active'",
                    (strategy_name,),
                )
                cur.execute(
                    "UPDATE strategy_versions SET status='active', change_reason=%s WHERE strategy_name=%s AND version=%s",
                    (change_reason, strategy_name, target_version),
                )
        return {"ok": True, "strategy_name": strategy_name, "active_version": target_version}

    def save_shadow_test_result(self, result: dict[str, Any]) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO shadow_test_results(
                        strategy_name, candidate_version, active_version, sample_count,
                        active_stats_json, candidate_stats_json, recommendation, status, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
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
                new_id = int(cur.fetchone()["id"])
        return new_id

    def save_historical_replay_result(self, result: dict[str, Any]) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO historical_replay_results(
                        symbol, interval, start_time, end_time, strategy_versions_json, result_json, export_path
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        result["symbol"],
                        result["interval"],
                        int(result["start_time"]),
                        int(result["end_time"]),
                        json.dumps(result.get("strategy_versions", []), ensure_ascii=False),
                        _json_dumps_payload(result),
                        result.get("export_path"),
                    ),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def save_self_evolution_run(self, result: dict[str, Any]) -> int:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO self_evolution_runs(status, result_json)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (result.get("status", "unknown"), _json_dumps_payload(result)),
                )
                new_id = int(cur.fetchone()["id"])
        return new_id

    def list_trade_reviews_with_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
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
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]


def _build_data_quality(snapshot: dict[str, Any]) -> dict[str, Any]:
    profiles = snapshot.get("profiles", {})
    missing = [tf for tf, profile in profiles.items() if int(profile.get("candles_count") or 0) == 0]
    return {
        "closed_candles_only": True,
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
        "missing_timeframes": missing,
        "status": "complete" if not missing else "partial",
    }
