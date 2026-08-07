from __future__ import annotations

import argparse
import json
import math
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.notify.alert_delivery import (
    DEFAULT_NEVER_SILENCE,
    process_alert_outbox,
    send_markdown_alert,
)
from plugins.crypto_guard.notify.hourly_report import build_hourly_report, resolve_report_target
from plugins.crypto_guard.notify.feishu_cards import build_analysis_card_json, render_text
from plugins.crypto_guard.notify.signal_policy import should_push_signal
from plugins.crypto_guard.ga_master import GAAnalysisRequest, GAMasterController
from plugins.crypto_guard.ga_master.decision_schema import controller_decision_from_legacy
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.risk.account_risk_guard import AccountRiskGuard
from plugins.crypto_guard.ga_master.feishu_action_builder import build_feishu_actions
from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions
from plugins.crypto_guard.review.daily_reviewer import run_daily_review
from plugins.crypto_guard.review.trade_reviewer import review_trade
from plugins.crypto_guard.scheduler.opportunity_watcher import update_opportunity_watches
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository, validate_job_identity, _decode_json
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter, should_use_redis_for_path
from plugins.crypto_guard.storage.pg_db import get_conn as _pg_get_conn
from plugins.crypto_guard.tools.ga_crypto_tools import crypto_handle_text_command
from plugins.crypto_guard.utils import utc_ms

LOGGER = get_logger("crypto_guard.worker")

# Phase B (07-07): module-level cache for per-batch circuit breakers.
# The controller is created per-job (line ~50), so the breaker cannot live
# on the controller instance. Instead, we cache breakers here keyed by
# batch_id. The breaker is created on first use and persists for the
# batch lifetime. When the batch finishes, the snapshot is merged into
# the batch summary and the breaker is removed from the cache.
_batch_breakers: dict[str, Any] = {}


class _FairBatchOwnershipLost(RuntimeError):
    """The database lease/CAS no longer belongs to this worker."""

# 07-10 R3-P0-1 + R4-P0-1 + R5-P0 (terminal-review-repair-plan-r5 P0): a
# ``single_flight_skipped`` symbol defers THIS tick's own ``agent_jobs`` claim
# back to ``pending`` (CAS keyed on this worker's ``claim_token``) and moves
# ``scheduled_at`` forward by a defer interval so a later ``run_once`` reclaims
# + processes it once the owning tick releases the symbol lease.
#
# R4-P0-1 (ABSOLUTE defer window): the pre-R4 design used a fixed
# ``_SINGLE_FLIGHT_DEFER_SECONDS * _SINGLE_FLIGHT_MAX_DEFERS`` product
# (15*8 = 120s) to bound the defer. But the legitimate LLM per-symbol lease runs
# 180..1200s (config ``llm.scheduling.per_symbol_timeout_seconds``). A 120s
# defer window would falsely mark a 20-min lease ``single_flight_defer_exhausted``
# at 2 min — the owning tick is still mid-flight. The defer window is now
# ABSOLUTE: terminate only once ``now - deferred_at >= per_symbol_timeout +
# _SINGLE_FLIGHT_DEFER_CLEANUP_BUFFER_SECONDS`` (a cleanup buffer so a call
# finishing right at the deadline still releases before exhaustion). The
# per-scheduled-at defer interval (``_SINGLE_FLIGHT_DEFER_SECONDS``) stays small
# so the owning tick is re-polled promptly; it is NOT the exhaustion bound.
#
# R5-P0 (dynamic backstop): the R4-P0-1 follow-on kept a FIXED
# ``_SINGLE_FLIGHT_MAX_DEFERS = 8`` backstop and OR-ed it into the exhaustion
# condition. Because ``_SINGLE_FLIGHT_DEFER_SECONDS = 15``, that backstop still
# fired at 8*15 = 120s -- EARLIER than the shortest legitimate absolute window
# (180+60 = 240s) and far earlier than the default (300+60 = 360s) / max
# (1200+60 = 1260s). The absolute window was "authoritative" in name only; the
# OR-ed fixed count owned precedence and re-introduced the very premature
# exhaustion R4-P0-1 was meant to kill. The count backstop is now DYNAMIC and
# derives from the absolute window:
# ``max_defers = ceil(defer_window_seconds / defer_seconds) + cleanup_margin``
# (e.g. 240/15 -> 17, 360/15 -> 25, 1260/15 -> 85), so it can NEVER fire inside
# the legitimate absolute window. The absolute window is the SOLE authority when
# ``deferred_at`` is parseable; the dynamic count backstop is a FAIL-CLOSED
# fallback used ONLY when ``deferred_at`` is None / unparseable (an unknown
# elapsed time must not silently defer forever, but must also never fire inside a
# legitimate window).
#
# The defer config (interval + absolute window + dynamic max_defers) is resolved
# from ``llm.scheduling`` in ``_resolve_single_flight_defer_config`` so it scales
# with the operator's configured ``per_symbol_timeout_seconds``; the module
# constants below are the FALLBACK for tests / a missing scheduling block.
_SINGLE_FLIGHT_DEFER_SECONDS = 15
_SINGLE_FLIGHT_DEFER_CLEANUP_BUFFER_SECONDS = 60
# R5-P0: margin added on top of ceil(window/interval) so the dynamic count
# backstop never coincides with the absolute-window boundary (avoids a race where
# the two bounds tie and the count fires a tick early).
_SINGLE_FLIGHT_DEFER_CLEANUP_MARGIN = 1


def _dynamic_max_defers(defer_window_seconds: int, defer_seconds: int) -> int:
    """07-10 R5-P0: compute the dynamic count backstop from the ABSOLUTE defer
    window so the count can NEVER fire inside the legitimate window.

    ``max_defers = ceil(defer_window_seconds / defer_seconds) + cleanup_margin``.
    E.g. window=240 / interval=15 -> ceil(16) + 1 = 17; 360/15 -> 25; 1260/15 ->
    85. Because ``defer_seconds`` is strictly positive and the cleanup margin is
    added AFTER the ceil, the count backstop only becomes operative strictly
    AFTER the absolute window has already elapsed -- so when ``deferred_at`` is
    parseable the absolute window is the sole authority, and the count backstop
    is a FAIL-CLOSED fallback for the (defensive) case where ``deferred_at`` is
    None / unparseable (an unknown elapsed time must not silently defer forever).
    """
    if defer_seconds <= 0:
        # Defensive: a non-positive interval would otherwise divide-by-zero.
        # Fall back to the window itself as a (very large) per-tick count.
        return max(1, int(defer_window_seconds)) + _SINGLE_FLIGHT_DEFER_CLEANUP_MARGIN
    return int(math.ceil(defer_window_seconds / defer_seconds)) + _SINGLE_FLIGHT_DEFER_CLEANUP_MARGIN


@dataclass(frozen=True)
class _SingleFlightDeferConfig:
    """07-10 R4-P0-1 + R5-P0: resolved single-flight defer policy for one batch.

    ``defer_seconds``: the per-iteration ``scheduled_at`` bump (small, so the
    owning tick is re-polled promptly after it releases the lease).
    ``defer_window_seconds``: the ABSOLUTE exhaustion bound = the legitimate
    per-symbol LLM lease (``per_symbol_timeout_seconds``) + a cleanup buffer. A
    symbol is terminated with ``single_flight_defer_exhausted`` only once
    ``now - deferred_at >= defer_window_seconds`` -- this is the SOLE authority
    when ``deferred_at`` is parseable. This guarantees a legitimately long lease
    (up to 1200s) is never falsely exhausted at 120s.
    ``max_defers``: R5-P0 DYNAMIC count backstop =
    ``ceil(defer_window_seconds / defer_seconds) + cleanup_margin`` (e.g. 240/15
    -> 17, 360/15 -> 25, 1260/15 -> 85). It is a FAIL-CLOSED fallback used ONLY
    when ``deferred_at`` is None / unparseable; it can NEVER fire inside the
    legitimate absolute window (the pre-R5 fixed ``=8`` backstop fired at 120s
    and re-introduced the premature exhaustion R4-P0-1 was meant to kill).
    ``per_symbol_timeout_seconds``: the configured per-symbol LLM deadline, kept
    for audit / diagnostics.
    """
    defer_seconds: int = _SINGLE_FLIGHT_DEFER_SECONDS
    defer_window_seconds: int = 300 + _SINGLE_FLIGHT_DEFER_CLEANUP_BUFFER_SECONDS
    max_defers: int = _dynamic_max_defers(
        300 + _SINGLE_FLIGHT_DEFER_CLEANUP_BUFFER_SECONDS, _SINGLE_FLIGHT_DEFER_SECONDS
    )
    per_symbol_timeout_seconds: int = 300


def _resolve_single_flight_defer_config(llm_cfg: dict[str, Any]) -> _SingleFlightDeferConfig:
    """07-10 R4-P0-1 + R5-P0: resolve the single-flight defer policy from
    ``llm.scheduling.per_symbol_timeout_seconds`` so the ABSOLUTE defer window
    scales with the configured LLM lease and never prematurely exhausts a
    legitimately long call.

    The defer window = ``per_symbol_timeout_seconds`` (180..1200) +
    ``_SINGLE_FLIGHT_DEFER_CLEANUP_BUFFER_SECONDS`` (60s), so a call finishing
    right at the per-symbol deadline still releases the lease before exhaustion.
    R5-P0: ``max_defers`` is derived DYNAMICALLY from that same window
    (``ceil(window / defer_seconds) + cleanup_margin``), so the count backstop
    can never fire inside the legitimate window.
    Validation of ``per_symbol_timeout_seconds`` already ran at startup
    (``config/loader.py::_validate_llm_scheduling``), so a value reaching here is
    an int in [180, 1200]; the floor / int coercion below is a defensive
    backstop for tests that pass a raw dict without the full loader validation.
    """
    sched = (llm_cfg.get("scheduling") or {}) if isinstance(llm_cfg, dict) else {}
    try:
        pst = int(sched.get("per_symbol_timeout_seconds", 300))
    except (TypeError, ValueError):
        pst = 300
    if pst < 180:
        pst = 180
    elif pst > 1200:
        pst = 1200
    _defer_window = pst + _SINGLE_FLIGHT_DEFER_CLEANUP_BUFFER_SECONDS
    return _SingleFlightDeferConfig(
        defer_seconds=_SINGLE_FLIGHT_DEFER_SECONDS,
        defer_window_seconds=_defer_window,
        max_defers=_dynamic_max_defers(_defer_window, _SINGLE_FLIGHT_DEFER_SECONDS),
        per_symbol_timeout_seconds=pst,
    )


def _parse_db_ts_ms(ts: str | None) -> int | None:
    """Parse a database timestamp string (``YYYY-MM-DD HH:MM:SS`` or an ISO-8601 variant with optional
    fractional seconds / timezone) into epoch milliseconds. Returns ``None`` on
    any parse failure so the caller can treat an unparseable ``deferred_at`` as
    "elapsed unknown -> do NOT exhaust" (fail-safe: a None/unknown elapsed time
    must NOT prematurely terminate a legitimate lease)."""
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    # Normalize the database "YYYY-MM-DD HH:MM:SS" form to ISO-8601.
    iso = s.replace(" ", "T", 1) if " " in s else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        # Fall back to the common UTC format without timezone.
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _broker_verifier_allows(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int | None,
    deterministic_risk_ok: bool,
) -> tuple[bool, dict[str, Any]]:
    """08-04 contract E8 production wiring (fresh reviewer P1): run the
    read-only ``AnalysisToolBroker`` verifier round for an order candidate.

    The verifier is VETO-ONLY: ``order_allowed`` requires the candidate, an
    approving verifier AND the deterministic risk gate open (E8), so it can
    block an order but can never grant eligibility on its own. It vetoes when
    deterministic risk is blocked, evidence is unavailable (market summary /
    skill evidence / account state), or concentration exceeds 5 open orders.

    FAIL-OPEN by design: if the broker's five read seams are absent (e.g. a
    repository shim), the module is unavailable, or the round raises, this
    returns ``(True, ...)`` so the existing deterministic gates (risk engine,
    account guard, recheck gate) remain the authoritative order decision. The
    broker is an additional advisory veto, never a silent order blocker.
    """
    try:
        from plugins.crypto_guard.tools.analysis_tool_broker import (
            AnalysisToolBroker,
            run_analysis_rounds,
        )
        seams = (
            getattr(repo, "get_candles", None),
            getattr(repo, "latest_skill_result_refs", None),
            getattr(repo, "latest_analysis_states", None),
            getattr(repo, "list_active_opportunity_watches_for_symbol", None),
            getattr(repo, "list_open_paper_orders", None),
        )
        if not all(seams):
            return True, {"reason": "broker_seams_missing_skip", "order_allowed": True}
        broker = AnalysisToolBroker(repo)
        result = run_analysis_rounds(
            broker,
            symbol=str(symbol),
            timeframe=str(timeframe or "15m"),
            analysis_time_utc=int(analysis_time_utc or utc_ms()),
            conflict=False,
            watch_hit=False,
            order_candidate=True,
            deterministic_risk_ok=bool(deterministic_risk_ok),
        )
        order_allowed = bool(result.get("order_allowed"))
        verifier = result.get("verifier") or {}
        return order_allowed, {
            "reason": "broker_verifier",
            "order_allowed": order_allowed,
            "verdict": verifier.get("verdict"),
        }
    except Exception as exc:  # noqa: BLE001 — advisory gate must fail open
        LOGGER.warning("broker verifier skipped (fail-open): %s", exc)
        return True, {"reason": "broker_verifier_exception_skip", "order_allowed": True}


def _post_decision_effects(
    repo: CryptoGuardRepository,
    decision: dict[str, Any],
    payload: dict[str, Any],
    *,
    send_message: Callable[..., Any] | None = None,
    job_id: Any = None,
) -> dict[str, Any]:
    """07-10 R5-3: shared post-decision side-effect pipeline.

    Runs the paper-order auto-create, position-conflict revalidation, and the
    real-time signal alert for a finalized GA decision. Extracted from
    ``process_job``'s ``scheduled_market_analysis`` branch so the fair-batch
    path (``process_fair_batch``) can apply the EXACT same side effects to a
    fair-batch-produced decision as the legacy serial path — no divergence in
    paper orders, position revalidation, or alerts between the two dispatch
    modes.

    ``payload`` carries ``allow_realtime_signal_alert`` + the report target
    metadata (same shape ``enqueue_market_analysis`` writes). ``job_id`` is
    used only for the done-log line.
    """
    signal_id = int(decision["signal_id"])
    sent = False
    target = None

    # Auto-create paper order for S/A grade signals with valid trade plan
    auto_order = None
    grade = str(decision.get("signal_grade") or "D").upper()
    has_plan = bool(decision.get("has_trade_plan") and decision.get("trade_plan"))
    risk_ok = bool((decision.get("risk_check") or {}).get("ok"))
    ga_decision_id = decision.get("ga_decision_id")
    # Don't auto-create if there's already an open order for this symbol
    existing_orders = repo.list_open_paper_orders_for_symbol(decision.get("symbol", ""))
    if grade in {"S", "A"} and has_plan and risk_ok and ga_decision_id and not existing_orders:
        # 08-04 contract E8 production wiring (fresh reviewer P1): the read-only
        # analysis broker runs a VETO-ONLY verifier round before an order may be
        # auto-created. Fail-open (see _broker_verifier_allows) so the
        # deterministic gates above stay authoritative; the broker can veto but
        # never grant eligibility.
        verifier_ok, _verifier = _broker_verifier_allows(
            repo,
            symbol=decision.get("symbol", ""),
            timeframe="15m",
            analysis_time_utc=int(decision.get("analysis_time_utc") or utc_ms()),
            deterministic_risk_ok=bool(risk_ok),
        )
        if verifier_ok:
            try:
                from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_ga_decision
                auto_order = create_paper_order_from_ga_decision(repo, int(ga_decision_id))
                LOGGER.info("auto paper order created ga_decision_id=%s result=%s", ga_decision_id, auto_order)
                # Send notification when order is newly created (not idempotent)
                if auto_order.get("ok") and auto_order.get("created"):
                    _target = resolve_report_target(repo, payload)
                    if _target and send_message:
                        plan = decision.get("trade_plan") or {}
                        tps = ", ".join(str(tp.get("price")) for tp in plan.get("take_profits", []))
                        side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(plan.get("side") or "").upper(), plan.get("side") or "-")
                        entry_type = str(plan.get("entry_type") or "limit")
                        status_cn = "待成交挂单" if entry_type == "limit" else "已成交（市价）"
                        entry_price = plan.get("entry_price") or plan.get("trigger_price") or "-"
                        # 08-04 contract A: the create/pending push must carry the
                        # mandated fields — order_id, side, entry, SL/TP,
                        # quantity-or-risk, expiry, and the source decision id.
                        order_id = auto_order.get("order_id")
                        risk_percent = (decision.get("risk_check") or {}).get("risk_percent") or plan.get("risk_percent")
                        quantity = plan.get("quantity") or plan.get("position_size")
                        quantity_text = f"{quantity} 张" if quantity else f"{risk_percent}% 风险" if risk_percent else "-"
                        from plugins.crypto_guard.notify.time_utils import format_event_time_cst
                        from plugins.crypto_guard.paper.pending_order_manager import compute_expires_at
                        event_time = format_event_time_cst(datetime.now(timezone.utc))
                        order_text = "\n".join([
                            "**CryptoGuard 已自动创建模拟盘订单**",
                            "",
                            f"- 时间：{event_time}",
                            f"- 产品：{decision.get('symbol')}",
                            f"- 订单号：{order_id}",
                            f"- 方向：{side_cn}",
                            f"- 状态：{status_cn}",
                            f"- 入场价：{entry_price}",
                            f"- 止损价：{plan.get('stop_loss')}",
                            f"- 止盈价：{tps}",
                            f"- 数量/风险：{quantity_text}",
                            f"- 有效期：{compute_expires_at(entry_type)}",
                            f"- 信号等级：{grade}，置信度：{round(float(decision.get('confidence', 0)) * 100)}%",
                            f"- 决策ID：{ga_decision_id}",
                            "",
                            "不构成实盘建议，仅用于模拟盘与策略研究。",
                        ])
                        send_markdown_alert(
                            repo, send_message,
                            receive_id=_target["receive_id"],
                            receive_id_type=_target.get("receive_id_type", "chat_id"),
                            text=order_text,
                            alert_type="paper_order_filled",
                            symbol=decision.get("symbol"),
                            priority=3,
                        )
            except Exception as exc:
                LOGGER.warning("auto paper order failed ga_decision_id=%s error=%s", ga_decision_id, exc)
                auto_order = {"ok": False, "error": str(exc)}

    # P0-2 (08-02): auto-materialize opportunity watches from the decision's
    # structured watch + feishu_actions. Runs on the SAME shared pipeline as
    # the paper-order auto-create, so both the fair-batch and the legacy serial
    # dispatch paths materialize a watch when the decision says so -- no user
    # Feishu button click required. Atomic + idempotent via
    # ``repo.upsert_auto_opportunity_watch`` (one active watch per
    # symbol+direction; a later batch refreshes it in place).
    auto_watch = None
    actions = decision.get("suggested_actions") or []
    watch = decision.get("opportunity_watch")
    # P0-3/P0-2 (08-02): "valid structured watch" is the watcher's own
    # contract (reasoning/watch_conditions.is_structured_watch) — a dict with
    # a LONG/SHORT direction, EVERY condition a structured dict (kind + usable
    # level + side), and a structured-or-None invalid_condition. A text
    # conditions blob (pre-P0-3 LLM shape) fails this check and the wire-in
    # does NOT fabricate a watch from it (fail-closed, no manufactured
    # opportunity).
    from plugins.crypto_guard.reasoning.watch_conditions import is_structured_watch

    watch_is_structured = is_structured_watch(watch)
    # Re-check for an open order AFTER the auto-order block above: a S/A
    # decision that just created a paper order needs no watch (the order IS the
    # actionable item), while a B-grade / degraded S/A (no plan / risk not ok)
    # with no open order materializes the watch.
    if (
        "create_opportunity_watch" in actions
        and grade in {"S", "A", "B"}
        and watch_is_structured
        and ga_decision_id
        and not repo.list_open_paper_orders_for_symbol(decision.get("symbol", ""))
    ):
        try:
            watch_id, watch_action = repo.upsert_auto_opportunity_watch(
                decision.get("symbol", ""),
                watch,
                source_signal_id=int(signal_id),
                ga_decision_id=int(ga_decision_id),
            )
            auto_watch = {"ok": True, "watch_id": watch_id, "action": watch_action}
            LOGGER.info(
                "auto opportunity watch %s watch_id=%s symbol=%s direction=%s ga_decision_id=%s",
                watch_action, watch_id, decision.get("symbol"), watch.get("direction"), ga_decision_id,
            )
        except Exception as exc:
            LOGGER.warning("auto opportunity watch failed ga_decision_id=%s error=%s", ga_decision_id, exc)
            auto_watch = {"ok": False, "error": str(exc)}

    # Position-aware analysis: revalidate open positions when GA decision conflicts
    from plugins.crypto_guard.paper.position_conflict_revalidator import run_position_conflict_revalidation
    _pos_result = run_position_conflict_revalidation(
        repo,
        symbol=decision.get("symbol"),
        ga_decision_id=ga_decision_id,
        send_message=send_message,
    )
    if _pos_result.get("conflict_count"):
        LOGGER.info(
            "position_conflict_revalidation: symbol=%s checked=%s conflicts=%s closed=%s stop_adjusted=%s recheck=%s",
            decision.get("symbol"),
            _pos_result.get("checked_count"),
            _pos_result.get("conflict_count"),
            _pos_result.get("closed_count"),
            _pos_result.get("stop_adjusted_count"),
            _pos_result.get("recheck_count"),
        )

    # v2: scheduled analysis is recorded into analysis_states/signals and summarized hourly.
    # Real-time Feishu alerts are reserved for paper/risk/opportunity events.
    if payload.get("allow_realtime_signal_alert") and should_push_signal(decision):
        target = resolve_report_target(repo, payload)
        if target and send_message:
            sent = bool(
                _send_interactive_alert(
                    repo,
                    send_message,
                    target["receive_id"],
                    target.get("receive_id_type", "chat_id"),
                    build_analysis_card_json(decision, signal_id=signal_id),
                    alert_type="signal_alert",
                    symbol=decision.get("symbol"),
                    priority=5,
                ).get("sent")
            )
    result = {"ok": True, "signal_id": signal_id, "decision": decision, "pushed": sent, "target": target, "auto_order": auto_order, "auto_watch": auto_watch}
    LOGGER.info(
        "post_decision_effects done job_id=%s signal_id=%s grade=%s pushed=%s decision=%s",
        job_id,
        signal_id,
        decision.get("signal_grade"),
        sent,
        decision.get("decision"),
    )
    return result


def process_fair_batch(
    repo: CryptoGuardRepository,
    jobs: list[dict[str, Any]],
    *,
    send_message: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """07-10 R5-3: run a whole batch of ``scheduled_market_analysis`` jobs
    through the fair-pool coordinator, then persist each symbol's decision via
    the SAME post-decision pipeline the legacy serial path uses.

    Flow:
    1. Collect ``{symbol: snapshot}`` + the shared ``batch_id`` from the
       claimed jobs (all carry the same batch_id — ``claim_next_batch``
       grouped them).
    2. Build/refresh ``_batch_breakers[batch_id]`` (breaker + retry_budget +
       wall_clock_budget) + the PROCESS-LEVEL ``SingleFlightLease`` singleton
       (S5, P1 #5 — shared across every batch_id so the cross-batch same-symbol
       mutex is real). ``run_fair_batch`` is called with ``release_lease=False``
       so the lease survives the LLM phase; this function releases each symbol's
       lease AFTER its persistence + ``_post_decision_effects`` finish.
    3. Resolve ``FairBatchConfig`` from the loaded llm config and call
       ``run_fair_batch`` with ``llm_call_fn=fair_llm_call_adapter`` (ONE
       provider call per attempt, no inner 3x retry wrapper — directive #2).
    4. For each ``SymbolLLMResult``: feed ``(candidate, attempt_meta)`` into
       ``controller.analyze_symbol`` as the PRESET candidate so the
       controller's risk gate / hysteresis / clamp / persistence run WITHOUT a
       second LLM call. The §8 envelope (attempt_meta) is carried into the
       persisted decision row for the report.
    5. Per symbol: ``mark_batch_symbol_completed``; **finish this symbol's
       ``agent_job``** (S6, P1 #6 — ``finish_job(job_id, result=,
       error_message=)`` with the per-symbol outcome, so a failed symbol's job
       is ``status='failed'`` not mislabeled ``success``); when the batch
       completes, ``finish_analysis_batch`` with the controller's
       ``get_batch_llm_health`` (Phase E aggregate). The breaker cache entry
       is popped on completion (success or partial-failed). ``run_once`` no
       longer uniformly finishes fair-batch jobs (that would double-finish +
       overwrite per-symbol outcomes).
    6. Per symbol: ``_post_decision_effects`` (paper order / position
       revalidation / signal alert) — identical to the serial path.

    A symbol whose ``analyze_symbol`` post-decision pipeline raises is marked
    failed in ``batch_symbol_status`` and counted in the Phase E
    ``llm_symbols_worker_failed`` aggregate (expected denominator =
    ``enabled_symbols`` from the batch row, R1-2). The exception is logged but
    does NOT abort the rest of the batch — each symbol is independently
    finalized.
    """
    if not jobs:
        return {"ok": True, "processed": False, "reason": "empty_batch"}
    # All jobs share one batch_id (claim_next_batch grouped them). Read it
    # from the first job's payload.  07-16 cutover: payload_json is JSONB ->
    # already a dict under PG; _decode_json handles both str and decoded forms.
    first_payload = _decode_json(jobs[0]["payload_json"], {})
    batch_id = first_payload.get("batch_id") or ""
    claim_tokens = {str(job.get("claim_token") or "") for job in jobs}
    if len(claim_tokens) != 1 or not next(iter(claim_tokens), ""):
        raise RuntimeError("fair batch rows do not share one non-empty claim token")
    batch_claim_token = next(iter(claim_tokens))
    # Collect per-symbol snapshot + job metadata keyed by symbol. A symbol
    # appearing twice in the batch (shouldn't happen — enqueue dedupes by
    # session_id) would collapse to its last job.
    snapshots: dict[str, dict[str, Any]] = {}
    job_by_symbol: dict[str, dict[str, Any]] = {}
    # 07-10 P1-3 (terminal review): a malformed scheduled_market_analysis job
    # whose payload carries NO symbol is a poison pill. ``claim_next_batch``
    # already flipped it to ``status='running'`` (S3 ownership token), so the
    # pre-P1-3 code's bare ``continue`` left it ``running`` forever: its lease
    # expires -> ``recover_stale_running_jobs`` resets it to ``pending`` -> it
    # is re-claimed next tick -> re-skipped -> infinite loop, and the job is
    # NEVER surfaced as failed (the operator sees a phantom re-claiming job).
    # P1-3 fix: mark every malformed job ``failed`` IMMEDIATELY with
    # ``invalid_scheduled_payload`` so it exits the queue for good, and record
    # the malformed count so the batch's final status reflects the failure
    # (it must NOT render ``success`` with a job that crashed on ingest).
    malformed_jobs: list[dict[str, Any]] = []
    for job in jobs:
        payload = _decode_json(job["payload_json"], {})
        snap = payload.get("snapshot") or {}
        # 07-15 R8-A (P0-2): the worker uses ONLY ``payload.symbol`` as the
        # authoritative symbol, validated by the SHARED identity contract (the
        # same helper seal/claim use). Pre-R8 the worker derived
        # ``sym = str(snap.get("symbol") or payload.get("symbol") or "")`` which
        # PREFERRED ``snapshot.symbol`` over ``payload.symbol`` -- a swapped
        # snapshot made the worker analyze the WRONG symbol under the running
        # job's identity (cross-symbol corruption). Now: a missing/no-symbol/
        # swapped-snapshot job fails ``validate_job_identity`` (returns None) and
        # is marked ``failed`` (``invalid_scheduled_payload``) WITHOUT analysis,
        # exactly like the pre-existing no-symbol poison pill. This unifies the
        # malformed trigger from "no symbol" to "any identity inconsistency".
        sym = validate_job_identity(payload)
        if sym is None:
            job_id = job.get("id")
            LOGGER.error(
                "process_fair_batch: malformed scheduled_market_analysis job "
                "id=%s batch=%s failed identity contract (payload.symbol missing "
                "or snapshot.symbol missing/swapped) -> marking failed "
                "(invalid_scheduled_payload). It will NOT be retried.",
                job_id, batch_id,
            )
            try:
                repo.finish_job(
                    int(job_id),
                    result={
                        "ok": False,
                        "reason": "invalid_scheduled_payload",
                        "batch_id": batch_id,
                    },
                    error_message="invalid_scheduled_payload: payload failed identity contract (symbol missing or snapshot.symbol missing/swapped)",
                    claim_token=str(job.get("claim_token") or ""),
                )
            except Exception:
                LOGGER.exception(
                    "process_fair_batch: failed to mark malformed job id=%s "
                    "failed (batch=%s); it may re-loop on lease expiry.",
                    job_id, batch_id,
                )
            malformed_jobs.append({"job_id": job_id, "reason": "invalid_scheduled_payload"})
            continue
        snapshots[sym] = snap
        job_by_symbol[sym] = {"job": job, "payload": payload}
    repo.conn.commit()
    symbols = list(snapshots.keys())
    if not symbols:
        # Every job in this batch was malformed (no symbol). Mark the batch
        # failed so it does not render ``success`` with zero symbols processed,
        # and return. ``is_batch_complete`` would otherwise see an enabled-set
        # subset mismatch; ``finish_analysis_batch(status='failed')`` records
        # the terminal state and clears the batch from the active set.
        if malformed_jobs:
            try:
                repo.finish_analysis_batch(
                    batch_id=batch_id, status="failed",
                    summary={"malformed_jobs": malformed_jobs},
                )
            except Exception:
                LOGGER.warning(
                    "process_fair_batch: finish_analysis_batch(failed) for "
                    "all-malformed batch=%s", batch_id, exc_info=True,
                )
            _batch_breakers.pop(batch_id, None)
            return {
                "ok": True, "processed": True, "batch_id": batch_id,
                "symbols": 0, "results": [], "failed_symbols": [],
                "malformed_jobs": malformed_jobs, "queue": "fair_pool",
            }
        return {"ok": True, "processed": False, "reason": "no_symbols"}

    # 07-10 S1 (P0 #1) + terminal-review P0-1: inject the REAL strict
    # cross-batch previous-analysis continuity into each snapshot BEFORE the
    # fair coordinator builds the LLM prompt. ``fair_llm_call_adapter`` runs
    # inside ``run_fair_batch`` (below), which executes BEFORE
    # ``controller.analyze_symbol`` -- so the controller's own
    # ``attach_analysis_continuity_to_snapshot`` call (controller.py:467) runs
    # AFTER the prompt is already built. Without this pre-injection,
    # ``_compact_snapshot`` lazily builds continuity with ``previous_row=None``
    # -> ``continuity_status="missing"`` and the LLM never sees the real prior
    # analysis/time/changes. ``attach_analysis_continuity_to_snapshot`` is
    # idempotent (returns early if the key exists), so the controller's later
    # attach is a no-op. ``snapshots[sym]`` is the SAME dict reference the
    # adapter reads, so mutating it here is visible to the prompt builder.
    #
    # design §11 "Missing continuity: fail closed for LLM confirmation, retain
    # deterministic observation." The PRE-S1 code swallowed any per-symbol
    # injection exception (warn + continue) but STILL let the symbol proceed
    # to ``run_fair_batch`` -> the adapter built the prompt with a
    # ``continuity_status="missing"`` block the LLM could not distinguish from
    # a legitimate first analysis, then the LLM confirmed a plan on top of
    # unverified/unavailable context. That is FAIL-OPEN and violates §11.
    # P0-1 fix: a symbol whose strict-prior lookup OR attach raises is added to
    # ``continuity_unavailable`` and is REMOVED from the symbols handed to
    # ``run_fair_batch`` so NO provider call is made for it (the adapter is
    # never reached). Below, after ``run_fair_batch`` returns, each such symbol
    # is synthesized a terminal ``continuity_unavailable`` envelope carrying a
    # DETERMINISTIC-SOP candidate (``use_llm=False``) so the controller's
    # observation/risk/persistence pipeline still runs for audit, but the
    # envelope's ``llm_terminal_reason="continuity_unavailable"`` +
    # ``plan_execution_state="no_candidate"`` PROHIBIT any executable plan
    # (fail-closed). One symbol's failure does NOT abort the rest of the batch
    # (each is finalized independently).
    try:
        from plugins.crypto_guard.reasoning.decision_context import (
            attach_analysis_continuity_to_snapshot,
        )
    except Exception:
        attach_analysis_continuity_to_snapshot = None  # type: ignore[assignment]
    continuity_unavailable: set[str] = set()
    for sym, snap in snapshots.items():
        try:
            previous_row_strict = None
            if attach_analysis_continuity_to_snapshot is not None:
                previous_row_strict = repo.latest_analysis_state_for_continuity(
                    str(snap.get("symbol") or sym),
                    analysis_time_utc=int(snap.get("analysis_time_utc") or 0),
                    exclude_batch_id=batch_id,
                )
                attach_analysis_continuity_to_snapshot(
                    snap,
                    previous_row=previous_row_strict,
                    current_batch_id=batch_id,
                    current_decision=None,
                )
        except Exception:
            # design §11 fail-closed: this symbol may NOT receive an LLM
            # confirmation. Record it and synthesize a deterministic-only
            # envelope below. Do NOT re-raise (one symbol must not abort the
            # batch).
            continuity_unavailable.add(sym)
            LOGGER.warning(
                "process_fair_batch: pre-inject continuity FAILED batch=%s "
                "symbol=%s -> fail-closed (§11): LLM confirmation DISABLED, "
                "deterministic SOP observation only, no executable plan.",
                batch_id, sym, exc_info=True,
            )
    # Symbols whose continuity could not be verified are EXCLUDED from the
    # fair coordinator's work list so no provider call / lease / prompt is
    # ever made for them (the adapter, which would build a prompt on top of an
    # unverifiable continuity block, is never reached). The expected-symbol
    # denominator (BatchMetrics.expected_symbols) stays the FULL count so the
    # Phase F coverage diagnostic still counts these as "missing Attempt-1"
    # (the correct fail-closed signal), not as silently-attempted.
    attemptable_symbols = [s for s in symbols if s not in continuity_unavailable]
    # Build/refresh the batch breaker cache entry (breaker + retry_budget +
    # wall_clock_budget). Mirrors process_job's scheduled_market_analysis
    # branch so the controller reuses the SAME breaker the fair coordinator
    # records against (R1-1: coordinator owns record_attempt).
    from plugins.crypto_guard.reasoning.llm_breaker import (
        CircuitBreaker, BatchRetryBudget, BatchWallClockBudget, SingleFlightLease,
        global_single_flight_lease,
    )
    from plugins.crypto_guard.config.loader import load_config
    llm_cfg = load_config().trading_mode.get("llm", {})
    breaker_cfg = llm_cfg.get("circuit_breaker", {})
    retry_cfg = llm_cfg.get("retry", {})
    batch_state = _batch_breakers.get(batch_id)
    if not isinstance(batch_state, dict):
        breaker = CircuitBreaker(
            enabled=breaker_cfg.get("enabled", True),
            consecutive_threshold=breaker_cfg.get("consecutive_failures", 3),
            rate_threshold=breaker_cfg.get("rate_threshold", 0.5),
            rate_window=breaker_cfg.get("rate_window", 10),
            min_rate_samples=breaker_cfg.get("min_rate_samples", 5),
        )
        retry_budget = BatchRetryBudget(
            max_batch_retry_calls=retry_cfg.get("max_batch_retry_calls", 9),
        )
        wall_clock_budget = BatchWallClockBudget(
            budget_seconds=retry_cfg.get("batch_wall_clock_budget_seconds", 90),
        )
        breaker._wall_clock_budget = wall_clock_budget
        batch_state = {
            "breaker": breaker,
            "retry_budget": retry_budget,
            "wall_clock_budget": wall_clock_budget,
        }
        _batch_breakers[batch_id] = batch_state
    breaker = batch_state["breaker"]
    retry_budget = batch_state["retry_budget"]
    wall_clock_budget = batch_state["wall_clock_budget"]
    # 07-10 S5 (P1 #5): use the PROCESS-LEVEL ``SingleFlightLease`` singleton,
    # NOT a per-``batch_id`` instance. The prior design cached a fresh
    # ``SingleFlightLease()`` per ``_batch_breakers[batch_id]`` -> every
    # batch_id got its OWN lease registry -> two overlapping ticks for the SAME
    # symbol but DIFFERENT batch_ids each acquired cleanly on their isolated
    # lease -> NO cross-batch mutex (the P1 #5 hole). The singleton is shared
    # across every ``process_fair_batch`` call in this process, so a symbol
    # held by batch A's tick is visible as held to batch B's tick. CryptoGuard
    # runs a single process (4 daemon worker threads; no multi-process
    # fan-out), so module-level scope is correct.
    lease = global_single_flight_lease()

    # Resolve the fair-pool config and run the coordinator. The adapter does
    # ONE provider call per attempt; the coordinator owns retry + breaker +
    # the two-pass barrier. R2-2 bounds the barrier wait so a hung provider
    # call cannot block the coordinator forever.
    from plugins.crypto_guard.reasoning.llm_fair_scheduler import (
        run_fair_batch, resolve_fair_batch_config, BatchMetrics,
        SymbolLLMResult,
    )
    from plugins.crypto_guard.reasoning.llm_agent_judge import fair_llm_call_adapter
    from plugins.crypto_guard.utils import utc_ms
    cfg = resolve_fair_batch_config(llm_cfg)
    metrics = BatchMetrics(expected_symbols=len(symbols))
    # Do not retain a PostgreSQL transaction while provider calls run. The
    # continuity queries above may have opened an implicit read transaction;
    # close it, then publish a short ownership heartbeat before dispatch.
    repo.conn.commit()
    if repo.renew_batch_claim(batch_claim_token) != len(jobs):
        raise RuntimeError("fair batch ownership lost before provider dispatch")
    fair_results = run_fair_batch(
        batch_id=batch_id, symbols=attemptable_symbols, snapshots=snapshots,
        cfg=cfg, breaker=breaker, retry_budget=retry_budget,
        wall_clock_budget=wall_clock_budget, metrics=metrics, lease=lease,
        llm_call_fn=fair_llm_call_adapter, now_ms=utc_ms,
        release_lease=False,
    )
    if repo.renew_batch_claim(batch_claim_token) != len(jobs):
        raise RuntimeError("fair batch ownership lost during provider dispatch")
    # 07-10 P0-1 (design §11 fail-closed): synthesize a terminal
    # ``continuity_unavailable`` envelope for every symbol whose strict-prior
    # continuity could NOT be verified above. The coordinator never attempted
    # them (excluded from ``attemptable_symbols``), so they are absent from
    # ``fair_results``. Each gets a DETERMINISTIC-SOP candidate (``use_llm=
    # False``) so the controller's observation/risk/persistence pipeline runs
    # for audit, but the envelope marks LLM confirmation DISABLED with
    # ``llm_terminal_reason="continuity_unavailable"`` and forces
    # ``plan_execution_state="no_candidate"`` so NO executable plan is
    # produced (the §11 "prohibit execution" half). The per-symbol loop below
    # treats this like any other preset candidate: the controller consumes it
    # (no provider call), persists a decision, and ``finish_job`` records the
    # per-symbol outcome. ``candidate`` carries the deterministic observation;
    # ``attempt_meta`` carries the structured terminal reason for the report.
    if continuity_unavailable:
        from plugins.crypto_guard.reasoning.llm_agent_judge import (
            run_agent_sop_decision,
        )
        for sym in continuity_unavailable:
            snap = snapshots.get(sym) or {}
            det_candidate: dict[str, Any] | None = None
            try:
                det_candidate = run_agent_sop_decision(snap, use_llm=False)
            except Exception:
                LOGGER.exception(
                    "process_fair_batch: deterministic SOP fallback also "
                    "failed for continuity_unavailable symbol=%s batch=%s; "
                    "persisting with no candidate (still fail-closed).",
                    sym, batch_id,
                )
                det_candidate = None
            # Force the deterministic observation to be NON-executable: even
            # if ``run_ga_sop_decision`` produced a candidate trade plan, a
            # symbol whose continuity is unavailable may NOT execute it
            # (design §11: prohibit execution). Strip the trade plan + mark
            # the envelope so the controller's risk gate and the Phase F
            # diagnostics both see a withheld, non-executable outcome.
            #
            # The §8 LLM-metadata envelope (llm_terminal_reason etc.) is
            # carried ON the candidate dict itself: when candidate is not
            # None, the controller's preset path
            # (llm_agent_judge.py:171-174) returns
            # ``apply_risk_to_decision(candidate, snapshot)`` WITHOUT merging
            # ``attempt_meta`` -- so the persisted decision's envelope fields
            # come from the candidate. We overwrite the SOP defaults
            # (``llm_disabled``) with the precise ``continuity_unavailable``
            # reason so the report + Phase F diagnostics classify it
            # correctly (NOT as a generic llm_disabled / llm_parse_failed).
            if isinstance(det_candidate, dict):
                det_candidate = dict(det_candidate)
                det_candidate["trade_plan"] = None
                det_candidate["has_trade_plan"] = False
                det_candidate["candidate_trade_plan"] = None
                det_candidate["plan_status"] = "withheld"
                det_candidate["plan_blockers"] = ["continuity_unavailable"]
                det_candidate["plan_origin"] = "deterministic_sop"
                det_candidate["plan_execution_state"] = "no_candidate"
                # §8 envelope override (see comment above re: candidate path).
                det_candidate["llm_status"] = "disabled"
                det_candidate["llm_attempt_count"] = 0
                det_candidate["llm_provider_call_count"] = 0
                det_candidate["llm_latency_ms"] = 0
                det_candidate["llm_prompt_bytes"] = None
                det_candidate["llm_continuity_included"] = None
                det_candidate["llm_model"] = None
                det_candidate["llm_config_name"] = None
                det_candidate["llm_terminal_reason"] = "continuity_unavailable"
                det_candidate["llm_fallback_reason"] = "continuity_unavailable"
                det_candidate["llm_error_category"] = None
                det_candidate["llm_error_stage"] = None
                det_candidate["llm_error"] = None
                det_candidate["llm_retry_round"] = None
                det_candidate["llm_schedule_round"] = None
                det_candidate["llm_schedule_position"] = -1
                det_candidate["llm_effective_thinking_budget_tokens"] = None
                det_candidate["llm_effective_max_output_tokens"] = None
                det_candidate["llm_effective_temperature"] = None
                det_candidate["llm_provider_timeout_ms"] = None
            fair_results[sym] = SymbolLLMResult(
                symbol=sym,
                schedule_position=-1,
                schedule_round=0,
                candidate=det_candidate,
                attempt_meta={
                    "llm_status": "disabled",
                    "llm_attempt_count": 0,
                    "llm_provider_call_count": 0,
                    "llm_latency_ms": 0,
                    "llm_prompt_bytes": None,
                    "llm_continuity_included": None,
                    "llm_model": None,
                    "llm_config_name": None,
                    "llm_terminal_reason": "continuity_unavailable",
                    "llm_fallback_reason": "continuity_unavailable",
                    "llm_error_category": None,
                    "llm_error_stage": None,
                    "llm_error": None,
                    "llm_retry_round": None,
                    "llm_schedule_round": None,
                    "llm_schedule_position": -1,
                    "llm_effective_thinking_budget_tokens": None,
                    "llm_effective_max_output_tokens": None,
                    "llm_effective_temperature": None,
                    "llm_provider_timeout_ms": None,
                    "continuity_unavailable": True,
                },
                terminal_reason="continuity_unavailable",
            )

    # Persist each symbol's decision via the controller's post-decision
    # pipeline, feeding the fair-batch candidate as the preset so NO second
    # LLM call happens. The controller reuses the shared breaker (above).
    controller = GAMasterController(repo)
    controller._breakers = _batch_breakers
    per_symbol_results: list[dict[str, Any]] = []
    failed_symbols: list[str] = []
    # 07-10 S5 (P1 #5): ``run_fair_batch`` was called with ``release_lease=False``
    # above, so the global single-flight lease for every ACQUIRED symbol is still
    # HELD. The acquired set = keys of ``fair_results`` whose terminal_reason is
    # NOT a policy-skip / continuity-unavailable code. P1-2 (terminal review):
    # policy-skipped symbols (``single_flight_skipped`` / ``missing_snapshot``)
    # are NOW keys in ``fair_results`` (P1-2 synthesized structured envelopes for
    # them), but they were NEVER acquired by THIS tick - their lease is held by
    # ANOTHER tick (single-flight) or was never taken (missing-snapshot).
    # Releasing them here would drop ANOTHER batch's lease and re-open the P1 #5
    # cross-batch mutex hole, so they MUST be excluded from the release set
    # exactly like ``continuity_unavailable``. P0-1 caveat: the synthesized
    # ``continuity_unavailable`` envelopes are ALSO keys in ``fair_results`` but
    # were NEVER leased (the coordinator never attempted them), so they too are
    # excluded. We own the release now and MUST drop each ACQUIRED symbol's
    # lease ONLY after its per-symbol persistence
    # (``mark_batch_symbol_completed``) + side effects (``_post_decision_effects``)
    # finish, so the cross-batch mutex covers the whole decision-write + side-
    # effect window -- not just the LLM-call window (the P1 #5 fix). The outer
    # ``try/finally`` releases any acquired symbol still held if the loop itself
    # raises unexpectedly (no lease leak on any exit path).
    _POLICY_SKIP_TERMINAL = {"single_flight_skipped", "missing_snapshot"}

    def _is_releaseable(sym: str, result: Any) -> bool:
        if sym in continuity_unavailable:
            return False
        tr = getattr(result, "terminal_reason", None)
        if tr in _POLICY_SKIP_TERMINAL:
            return False
        return True

    _acquired_symbols = [
        s for s, r in fair_results.items() if _is_releaseable(s, r)
    ]
    _released_symbols: set[str] = set()
    # 07-10 R3-P0-1 (terminal-review-repair-plan-r3 §3): the single-flight skip
    # path MUST NOT execute any protected side effect. A ``single_flight_skipped``
    # symbol's cross-batch lease is held by ANOTHER in-flight tick; this tick
    # never acquired it and therefore MUST NOT call ``controller.analyze_symbol``
    # (which would write ``ga_decisions`` / ``analysis_states`` / ``signals``),
    # MUST NOT run ``_post_decision_effects`` (position-conflict revalidation /
    # auto paper order / real-time alert), MUST NOT mark
    # ``batch_symbol_status`` completed, and MUST NOT ``finish_job`` as success.
    # Instead this tick defers ITS OWN claim on the symbol's ``agent_jobs`` row:
    # CAS it from ``running`` back to ``pending`` (clearing this tick's
    # ``claim_token`` / ``lease_until`` / ``started_at``) and moves
    # ``scheduled_at`` forward by a small defer interval so a later
    # ``run_once(background=True)`` reclaims it once the owning tick releases
    # the symbol lease (historical contract #10 / R3 §3.2). ``missing_snapshot``
    # is malformed INPUT (not a legitimate defer): it fails TERMINALLY with zero
    # controller / persist / post effects. Both skip types therefore short-circuit
    # BELOW before any controller / persistence / post-effect work runs.
    # (_SINGLE_FLIGHT_DEFER_SECONDS / _SINGLE_FLIGHT_DEFER_CLEANUP_MARGIN are
    # module-level; max_defers is derived dynamically -- see
    # _resolve_single_flight_defer_config / _dynamic_max_defers.)
    deferred_symbols: list[str] = []
    defer_exhausted_symbols: list[str] = []
    try:
        for sym in symbols:
            sym_meta = job_by_symbol.get(sym)
            if sym_meta is None:
                continue
            payload = sym_meta["payload"]
            snap = payload["snapshot"]
            fair_result = fair_results.get(sym)
            preset_candidate = fair_result.candidate if fair_result else None
            preset_attempt_meta = (
                fair_result.attempt_meta if fair_result else {}
            )
            # R3-P0-1 §3.2: short-circuit policy-skip symbols BEFORE any
            # controller / persistence / post-effect work. ``single_flight_skipped``
            # defers this tick's claim (bounded); ``missing_snapshot`` fails
            # terminally. Neither writes a GA decision / analysis state / signal
            # / order / alert for this symbol.
            _skip_reason = getattr(fair_result, "terminal_reason", None) if fair_result else None
            if _skip_reason == "single_flight_skipped":
                job_id = int(sym_meta["job"].get("id"))
                claim_token = sym_meta["job"].get("claim_token")
                # R4-P1-4: defer count + first-defer timestamp live in DEDICATED
                # columns (NOT error_message). R4-P0-1 + R5-P0: the exhaustion
                # bound is ABSOLUTE (now - deferred_at >= defer_window), not a
                # fixed defer_seconds*max_defers product, so a legitimate long LLM
                # lease (up to per_symbol_timeout=1200s) is never falsely
                # exhausted at 120s.
                _defer_cfg = _resolve_single_flight_defer_config(llm_cfg)
                defer_count = 0
                deferred_at: str | None = None
                try:
                    defer_count, deferred_at = repo.get_job_defer_state(job_id)
                except Exception:
                    LOGGER.warning(
                        "process_fair_batch: get_job_defer_state failed "
                        "batch=%s symbol=%s job_id=%s",
                        batch_id, sym, job_id, exc_info=True,
                    )
                # R4-P0-1 + R5-P0: ABSOLUTE defer window is the SOLE authority
                # whenever deferred_at is parseable. A symbol is exhausted ONLY
                # if the owning tick has held the lease past the legitimate
                # per_symbol_timeout + cleanup buffer (the LLM call should have
                # finished or hard-timed-out by then). A deferred_at that is None
                # (first defer this sequence) can NEVER be exhausted yet.
                #
                # R5-P0 (dynamic backstop, FAIL-CLOSED): the pre-R5 design OR-ed
                # a FIXED ``defer_count >= 8`` backstop into the exhaustion
                # condition, which fired at 8*15 = 120s -- EARLIER than every
                # legitimate absolute window (240/360/1260s) and re-introduced the
                # premature exhaustion R4-P0-1 was meant to kill. The count
                # backstop is now (a) DYNAMIC -- ``max_defers =
                # ceil(defer_window_seconds / defer_seconds) + cleanup_margin`` so
                # it can never fire inside the legitimate window -- AND (b) gated
                # so it is consulted ONLY when ``deferred_at`` is None /
                # unparseable (``_deferred_at_known`` is False). When the
                # timestamp IS known, the absolute window alone decides; the
                # count backstop never gets a vote. This is the fail-closed
                # fallback for an unknown elapsed time (must not silently defer
                # forever) without ever firing inside a legitimate window.
                _defer_elapsed_s = 0
                _deferred_at_known = False
                if deferred_at:
                    try:
                        _deferred_ms = _parse_db_ts_ms(deferred_at)
                        _now_ms = utc_ms()
                        if _deferred_ms is not None:
                            _defer_elapsed_s = max(0, (_now_ms - _deferred_ms) // 1000)
                            _deferred_at_known = True
                    except Exception:
                        _defer_elapsed_s = 0
                _absolute_exhausted = (
                    _deferred_at_known
                    and _defer_elapsed_s >= _defer_cfg.defer_window_seconds
                )
                # R5-P0: the count backstop is consulted ONLY when the absolute
                # window CANNOT be evaluated (deferred_at None / unparseable).
                _backstop_exhausted = (
                    (not _deferred_at_known) and defer_count >= _defer_cfg.max_defers
                )
                if _absolute_exhausted or _backstop_exhausted:
                    # R3 §3.2.10 + R4-P0-1: defer policy exhausted -> terminate
                    # the job with an explicit non-executable reason. NO GA
                    # decision, NO post effects. The batch_symbol_status row is
                    # marked failed so the batch can still complete (and the
                    # operator sees the exhaustion, not a phantom pending job).
                    # The reason string records WHICH bound fired (absolute vs
                    # backstop) for diagnostics.
                    _exhaust_reason = (
                        "single_flight_defer_exhausted:absolute_window"
                        if _absolute_exhausted
                        else "single_flight_defer_exhausted:backstop_cap"
                    )
                    terminal_result = {
                        "ok": False,
                        "reason": "single_flight_defer_exhausted",
                        "exhaust_reason": _exhaust_reason,
                        "batch_id": batch_id,
                        "symbol": sym,
                        "defer_count": defer_count,
                        "defer_elapsed_s": _defer_elapsed_s,
                        "defer_window_s": _defer_cfg.defer_window_seconds,
                        "per_symbol_timeout_s": _defer_cfg.per_symbol_timeout_seconds,
                    }
                    try:
                        finished = repo.finish_claimed_batch_symbol(
                            batch_id=batch_id,
                            symbol=sym,
                            job_id=job_id,
                            claim_token=str(claim_token or ""),
                            result={
                                **terminal_result,
                            },
                            error_message=(
                                f"single_flight_defer_exhausted: "
                                f"defer_count={defer_count} "
                                f"defer_elapsed_s={_defer_elapsed_s} "
                                f"defer_window_s={_defer_cfg.defer_window_seconds} "
                                f"max_defers={_defer_cfg.max_defers} "
                                f"reason={_exhaust_reason}"
                            ),
                        )
                        if not finished:
                            raise _FairBatchOwnershipLost(
                                "fair batch ownership lost before defer-exhausted commit"
                            )
                    except _FairBatchOwnershipLost:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "process_fair_batch: finish_claimed_batch_symbol"
                            "(defer_exhausted) "
                            "failed batch=%s symbol=%s job_id=%s",
                            batch_id, sym, job_id,
                        )
                        raise
                    defer_exhausted_symbols.append(sym)
                    failed_symbols.append(sym)
                    per_symbol_results.append(terminal_result)
                    continue
                # Defer this tick's claim: CAS running->pending keyed on THIS
                # worker's claim_token. Zero rows = claim-loss (another worker
                # / recovery owns the row now) -> nothing further to do (do NOT
                # touch another worker's row). On success the row is pending
                # again with a forward ``scheduled_at``; a later ``run_once``
                # reclaims + processes it exactly once (R3 §3.2.9). R4-P1-4: the
                # atomic increment of defer_count + COALESCE(deferred_at, now)
                # happen INSIDE this CAS UPDATE.
                deferred_ok = False
                if claim_token:
                    try:
                        deferred_ok = repo.defer_claimed_job(
                            job_id, str(claim_token),
                            reason="single_flight_deferred",
                            defer_seconds=_defer_cfg.defer_seconds,
                        )
                    except Exception:
                        LOGGER.exception(
                            "process_fair_batch: defer_claimed_job raised "
                            "batch=%s symbol=%s job_id=%s",
                            batch_id, sym, job_id,
                        )
                        deferred_ok = False
                if deferred_ok:
                    deferred_symbols.append(sym)
                    per_symbol_results.append({
                        "symbol": sym, "ok": True,
                        "reason": "single_flight_deferred",
                        "defer_count": defer_count + 1,
                    })
                    LOGGER.info(
                        "process_fair_batch: deferred single-flight-skip "
                        "symbol=%s batch=%s job_id=%s defer_count=%s "
                        "defer_elapsed_s=%s/%s (absolute window)",
                        sym, batch_id, job_id, defer_count + 1,
                        _defer_elapsed_s, _defer_cfg.defer_window_seconds,
                    )
                else:
                    # Claim-loss (lease expired + recovered by another worker,
                    # or this tick never owned it). The row is no longer ours;
                    # leave it for whoever owns it. Record the outcome so the
                    # batch summary is honest (no phantom success).
                    LOGGER.warning(
                        "process_fair_batch: single-flight defer claim-loss "
                        "(row no longer owned by this tick) symbol=%s "
                        "batch=%s job_id=%s",
                        sym, batch_id, job_id,
                    )
                    deferred_symbols.append(sym)
                    per_symbol_results.append({
                        "symbol": sym, "ok": False,
                        "reason": "single_flight_defer_claim_loss",
                    })
                # batch_symbol_status is LEFT PENDING (never registered for a
                # deferred symbol) so ``is_batch_complete`` stays False and the
                # batch is NOT falsely completed while the symbol awaits
                # re-claim (R3 §3.2.7). NO lease release here -- this tick never
                # acquired the symbol's lease (the owning tick holds it).
                repo.conn.commit()
                continue
            if _skip_reason == "missing_snapshot":
                # R3 §3.2 final paragraph: ``missing_snapshot`` is malformed
                # INPUT (the enqueue pipeline failed to attach a snapshot), NOT a
                # legitimate defer. Fail TERMINALLY with zero controller /
                # persist-decision / post effects: no ``analyze_symbol``, no
                # ``ga_decisions`` row, no ``_post_decision_effects``. Mark the
                # batch symbol failed + finish the job failed so the batch can
                # still complete and the operator sees the malformed input.
                job_id = int(sym_meta["job"].get("id"))
                LOGGER.error(
                    "process_fair_batch: missing_snapshot (malformed input) -> "
                    "terminal fail, zero controller/post effects. symbol=%s "
                    "batch=%s job_id=%s",
                    sym, batch_id, job_id,
                )
                try:
                    finished = repo.finish_claimed_batch_symbol(
                        batch_id=batch_id,
                        symbol=sym,
                        job_id=job_id,
                        claim_token=str(sym_meta["job"].get("claim_token") or ""),
                        result={
                            "ok": False,
                            "reason": "missing_snapshot",
                            "batch_id": batch_id,
                            "symbol": sym,
                        },
                        error_message="missing_snapshot: malformed scheduled payload",
                    )
                    if not finished:
                        raise _FairBatchOwnershipLost(
                            "fair batch ownership lost before missing-snapshot commit"
                        )
                except _FairBatchOwnershipLost:
                    raise
                except Exception:
                    LOGGER.exception(
                        "process_fair_batch: finish_claimed_batch_symbol"
                        "(missing_snapshot) "
                        "failed batch=%s symbol=%s job_id=%s",
                        batch_id, sym, job_id,
                    )
                    raise
                failed_symbols.append(sym)
                per_symbol_results.append({
                    "symbol": sym, "ok": False, "reason": "missing_snapshot",
                })
                continue
            try:
                decision = controller.analyze_symbol(
                    GAAnalysisRequest(
                        symbol=snap["symbol"],
                        decision_type="scheduled_analysis",
                        analysis_time_utc=int(snap.get("analysis_time_utc") or 0),
                        mode=snap.get("mode") or "scheduled",
                        snapshot=snap,
                        snapshot_id=payload.get("snapshot_id"),
                        allow_realtime_signal_alert=bool(payload.get("allow_realtime_signal_alert")),
                        batch_id=batch_id,
                    ),
                    preset_llm_candidate=preset_candidate,
                    preset_llm_attempt_meta=preset_attempt_meta,
                )
                _post_decision_effects(
                    repo, decision, payload,
                    send_message=send_message, job_id=sym_meta["job"].get("id"),
                )
                per_symbol_results.append({"symbol": sym, "ok": True, "decision": decision})
                # 07-10 S6 (P1 #6): finish THIS symbol's agent_job as success.
                # The prior design left per-job finishing to ``run_once``'s
                # uniform post-batch loop, which marked EVERY job in the batch
                # ``success`` regardless of whether its ``analyze_symbol`` raised
                # -> a failed symbol's job was mislabeled success (the P1 #6
                # defect, which hid per-symbol failures from the ops/dashboard).
                # ``finish_job(job_id, result=, error_message=)`` already supports
                # per-job success/failed; we call it here per-symbol so the job
                # row's status reflects THIS symbol's outcome. The uniform loop
                # in ``run_once`` is removed (it would double-finish + mislabel).
                completed = repo.finish_claimed_batch_symbol(
                    batch_id=batch_id,
                    symbol=sym,
                    job_id=int(sym_meta["job"].get("id")),
                    claim_token=str(sym_meta["job"].get("claim_token") or ""),
                    result=per_symbol_results[-1],
                )
                if not completed:
                    repo.conn.rollback()
                    raise _FairBatchOwnershipLost(
                        "fair batch ownership lost before symbol commit"
                    )
            except Exception as sym_exc:
                if isinstance(sym_exc, _FairBatchOwnershipLost):
                    raise
                LOGGER.exception(
                    "process_fair_batch: analyze_symbol failed batch=%s symbol=%s",
                    batch_id, sym,
                )
                failed_symbols.append(sym)
                per_symbol_results.append(
                    {"symbol": sym, "ok": False, "error": str(sym_exc)},
                )
                # 07-10 S6 (P1 #6): finish THIS symbol's agent_job as FAILED with
                # the exception text -- NOT success. Revert-fail: the prior
                # uniform loop in ``run_once`` would overwrite this with
                # ``finish_job(result=<batch result>)`` (no error_message ->
                # status='success'), mislabeling the failed symbol.
                try:
                    failed_written = repo.finish_claimed_batch_symbol(
                        batch_id=batch_id,
                        symbol=sym,
                        job_id=int(sym_meta["job"].get("id")),
                        claim_token=str(sym_meta["job"].get("claim_token") or ""),
                        result=per_symbol_results[-1],
                        error_message=str(sym_exc)[:500],
                    )
                    if not failed_written:
                        repo.conn.rollback()
                        raise _FairBatchOwnershipLost(
                            "fair batch ownership lost before failure commit"
                        )
                except Exception as finish_exc:
                    if isinstance(finish_exc, _FairBatchOwnershipLost):
                        raise
                    LOGGER.warning(
                        "process_fair_batch: finish_job(failed) failed "
                        "batch=%s symbol=%s job_id=%s",
                        batch_id, sym, sym_meta["job"].get("id"),
                    )
            finally:
                # S5 (P1 #5): release this symbol's lease now that its
                # persistence + side effects are done (success OR failure).
                # Only release symbols ``run_fair_batch`` actually acquired.
                # P1-2 (terminal review): a policy-skipped symbol IS now in
                # ``fair_results`` (structured envelope) but was NEVER acquired
                # by THIS tick - its lease (if any) belongs to ANOTHER tick that
                # is still mid-flight. Releasing it here would drop that other
                # tick's cross-batch mutex (the P1 #5 hole), so exclude policy-
                # skip terminal reasons exactly as we exclude
                # ``continuity_unavailable``. ``_is_releaseable`` is the single
                # source of truth shared with the safety-net release below.
                fair_result = fair_results.get(sym)
                if (fair_result is not None
                        and sym not in _released_symbols
                        and _is_releaseable(sym, fair_result)):
                    try:
                        lease.release(symbol=sym)
                    except Exception:
                        LOGGER.exception(
                            "process_fair_batch: failed to release lease for "
                            "%s (batch=%s)", sym, batch_id,
                        )
                    else:
                        _released_symbols.add(sym)
                repo.conn.commit()
    finally:
        # Safety net: release any acquired symbol NOT already released (e.g. an
        # unexpected exception skipped its per-symbol finally). Idempotent --
        # ``release`` on an already-released symbol is a no-op.
        for sym in _acquired_symbols:
            if sym not in _released_symbols:
                try:
                    lease.release(symbol=sym)
                except Exception:
                    LOGGER.exception(
                        "process_fair_batch: safety-net release failed for %s "
                        "(batch=%s)", sym, batch_id,
                    )

    # Finish the batch when all enabled symbols are processed. Mirror the
    # serial path's status selection + llm_health merge (including the P2-5
    # exception-path per-decision aggregate).
    # 07-10 P1-3 (terminal review): if any malformed job was marked failed
    # above, the batch must NOT render ``success`` even if every ENABLED
    # symbol completed cleanly — a poison-pill ingest failure is a real
    # defect the operator must see. Force ``partial_failed`` (or ``failed``
    # when every enabled symbol also failed) and carry the malformed list in
    # the summary so the report surfaces it.
    # 07-10 R3-P0-1 (terminal review): a batch with a DEFERRED symbol is NOT
    # complete — ``is_batch_complete`` is False because the deferred symbol's
    # ``batch_symbol_status`` row stays ``pending`` (R3 §3.2.7). So this block
    # is skipped and the batch stays ``running`` until a later ``run_once``
    # re-claims the deferred job, processes it, and registers it. That is the
    # required behavior (the batch must NOT be falsely completed). The
    # ``defer_exhausted`` path marks the batch symbol failed, so a batch where
    # the defer policy is exhausted DOES complete (as ``partial_failed`` /
    # ``failed``) and surfaces the exhausted symbols below.
    batch_summary: dict[str, Any] | None = None
    try:
        if repo.is_batch_complete(batch_id):
            if repo.batch_all_failed(batch_id):
                batch_status = "failed"
            elif repo.batch_has_failures(batch_id) or malformed_jobs:
                batch_status = "partial_failed"
            else:
                batch_status = "success"
            llm_health = controller.get_batch_llm_health(batch_id)
            summary: dict[str, Any] | None = {"llm_health": llm_health} if llm_health else None
            if malformed_jobs:
                if summary is None:
                    summary = {}
                summary["malformed_jobs"] = malformed_jobs
            if defer_exhausted_symbols:
                if summary is None:
                    summary = {}
                summary["defer_exhausted_symbols"] = defer_exhausted_symbols
            repo.finish_analysis_batch(
                batch_id=batch_id, status=batch_status, summary=summary,
            )
            batch_summary = summary
            _batch_breakers.pop(batch_id, None)
    except Exception:
        LOGGER.warning(
            "process_fair_batch: finish_analysis_batch failed batch=%s",
            batch_id, exc_info=True,
        )

    return {
        "ok": True,
        "processed": True,
        "batch_id": batch_id,
        "symbols": len(symbols),
        "results": per_symbol_results,
        "failed_symbols": failed_symbols,
        "malformed_jobs": malformed_jobs,
        "deferred_symbols": deferred_symbols,
        "defer_exhausted_symbols": defer_exhausted_symbols,
        "batch_summary": batch_summary,
        "queue": "fair_pool",
    }


def process_job(repo: CryptoGuardRepository, job: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    payload = _decode_json(job["payload_json"], {})
    job_type = job["job_type"]
    LOGGER.info("process_job start id=%s type=%s priority=%s session=%s", job.get("id"), job_type, job.get("priority"), job.get("session_id"))
    if job_type == "feishu_user_message":
        result = crypto_handle_text_command(payload.get("text", ""), payload.get("open_id"))
        _maybe_send_feishu_result(repo, payload, result, send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "feishu_button_callback":
        result = handle_button_callback(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "scheduled_market_analysis":
        snapshot = payload["snapshot"]
        batch_id = payload.get("batch_id")
        # Phase B (07-07): get or create per-batch circuit breaker
        from plugins.crypto_guard.reasoning.llm_breaker import (
            CircuitBreaker,
            BatchRetryBudget,
            BatchWallClockBudget,
        )
        from plugins.crypto_guard.config.loader import load_config
        llm_cfg = load_config().trading_mode.get("llm", {})
        breaker_cfg = llm_cfg.get("circuit_breaker", {})
        retry_cfg = llm_cfg.get("retry", {})
        # 07-10 S7 (P1 #8): deterministic-only rollback. When the fair-pool
        # scheduling mode is DISABLED (the feature-flag rollback case), the
        # serial ``process_job`` path is the ONLY path a scheduled analysis
        # takes. The prior design let it fall through to
        # ``controller.analyze_symbol`` with NO preset -> ``run_agent_sop_decision``
        # consulted ``CRYPTO_GUARD_LLM_ANALYSIS`` (default on) -> the serial
        # LLM starvation path (the very bug fair scheduling was built to fix:
        # one shared 90s budget, first-few-symbols-drain-it, rest starved).
        # implement.md:64 requires the rollback fallback be DETERMINISTIC-ONLY.
        # Fix: build the deterministic SOP decision with ``use_llm=False`` and
        # feed it to the controller as a preset candidate. ``run_agent_sop_decision``
        # consumes the preset (no provider call) and the controller runs the
        # full risk/hysteresis/clamp/persistence pipeline on it. No LLM, no
        # shared-budget starvation. When mode == ``fair_pool`` this branch is
        # only reached for stragglers not claimed by ``claim_next_batch``;
        # preserve the existing (preset-free) serial behavior there.
        _sched_mode_s7 = (llm_cfg.get("scheduling", {}) or {}).get("mode", "fair_pool")
        _deterministic_only_s7 = _sched_mode_s7 != "fair_pool"
        _preset_candidate_s7: dict[str, Any] | None = None
        _preset_attempt_meta_s7: dict[str, Any] | None = None
        if _deterministic_only_s7:
            from plugins.crypto_guard.reasoning.llm_agent_judge import (
                run_agent_sop_decision,
            )
            _det_s7 = run_agent_sop_decision(snapshot, use_llm=False)
            _preset_candidate_s7 = _det_s7
            _preset_attempt_meta_s7 = {
                "llm_status": "disabled",
                "llm_attempt_count": 0,
                "llm_provider_call_count": 0,
                "llm_terminal_reason": "llm_disabled",
                "llm_fallback_reason": "llm_disabled",
                "llm_config_name": None,
                "llm_model": None,
                "llm_error_category": None,
                "llm_error_stage": None,
                "llm_error": None,
                "llm_retry_round": None,
                "llm_schedule_round": None,
                "llm_schedule_position": None,
                "llm_continuity_included": None,
                "llm_prompt_bytes": None,
                "llm_latency_ms": 0,
                "llm_effective_thinking_budget_tokens": None,
                "llm_effective_max_output_tokens": None,
                "llm_effective_temperature": None,
                "llm_provider_timeout_ms": None,
            }
        breaker = _batch_breakers.get(batch_id or "")
        if breaker is None and batch_id:
            # Create the full batch_state dict (breaker + budgets) so the
            # controller can reuse it. The controller will also create this
            # on first symbol, but creating here ensures the breaker exists
            # before the controller runs.
            from plugins.crypto_guard.reasoning.llm_breaker import (
                BatchRetryBudget,
                BatchWallClockBudget,
            )
            breaker = CircuitBreaker(
                enabled=breaker_cfg.get("enabled", True),
                consecutive_threshold=breaker_cfg.get("consecutive_failures", 3),
                rate_threshold=breaker_cfg.get("rate_threshold", 0.5),
                rate_window=breaker_cfg.get("rate_window", 10),
                # 07-09-overtrigger P0-3: rate-based open only fires when the
                # rate window has at least min_rate_samples observations. The
                # worker is the production entrypoint for
                # scheduled_market_analysis and creates the breaker BEFORE the
                # controller claims it via _batch_breakers. If we do not pass
                # the config here, the controller path cannot recover it -
                # the worker's breaker (default 5) wins and any override in
                # trading_mode.yaml is silently ignored.
                min_rate_samples=breaker_cfg.get("min_rate_samples", 5),
            )
            retry_budget = BatchRetryBudget(
                max_batch_retry_calls=retry_cfg.get("max_batch_retry_calls", 9),
            )
            wall_clock_budget = BatchWallClockBudget(
                budget_seconds=retry_cfg.get("batch_wall_clock_budget_seconds", 90),
            )
            breaker._wall_clock_budget = wall_clock_budget
            _batch_breakers[batch_id] = {
                "breaker": breaker,
                "retry_budget": retry_budget,
                "wall_clock_budget": wall_clock_budget,
            }
        try:
            controller = GAMasterController(repo)
            # Inject the shared breaker cache into the controller so it reuses
            # the same breaker instead of creating a new one per symbol.
            controller._breakers = _batch_breakers
            _analyze_request_s7 = GAAnalysisRequest(
                symbol=snapshot["symbol"],
                decision_type="scheduled_analysis",
                analysis_time_utc=int(snapshot.get("analysis_time_utc") or 0),
                mode=snapshot.get("mode") or "scheduled",
                snapshot=snapshot,
                snapshot_id=payload.get("snapshot_id"),
                allow_realtime_signal_alert=bool(payload.get("allow_realtime_signal_alert")),
                batch_id=batch_id,
            )
            if _deterministic_only_s7:
                # P1 #8: feed the deterministic SOP decision as a preset so the
                # controller runs the full risk/persistence pipeline WITHOUT an
                # LLM call (no shared-budget starvation on the rollback path).
                decision = controller.analyze_symbol(
                    _analyze_request_s7,
                    preset_llm_candidate=_preset_candidate_s7,
                    preset_llm_attempt_meta=_preset_attempt_meta_s7,
                )
            else:
                # fair_pool mode: preserve the prior call signature (no preset
                # kwargs). Some tests stub ``analyze_symbol(request)`` with a
                # narrower signature, so we must not add kwargs here.
                decision = controller.analyze_symbol(_analyze_request_s7)
            # Hourly Report Accuracy: record batch progress for completion gate.
            if batch_id:
                try:
                    repo.mark_batch_symbol_completed(batch_id=batch_id, symbol=snapshot["symbol"])
                    # P0-3: finish the batch when all symbols are done
                    # P1-4 (Round 3): check for failed symbols to set correct batch status
                    if repo.is_batch_complete(batch_id):
                        if repo.batch_all_failed(batch_id):
                            batch_status = "failed"
                        elif repo.batch_has_failures(batch_id):
                            batch_status = "partial_failed"
                        else:
                            batch_status = "success"
                        # Phase B (07-07): merge breaker snapshot into batch summary
                        llm_health = controller.get_batch_llm_health(batch_id)
                        summary = {"llm_health": llm_health} if llm_health else None
                        repo.finish_analysis_batch(batch_id=batch_id, status=batch_status, summary=summary)
                        # Clean up breaker cache for this batch
                        _batch_breakers.pop(batch_id, None)
                except Exception:
                    LOGGER.warning("mark_batch_symbol_completed failed batch=%s symbol=%s", batch_id, snapshot["symbol"])
        except Exception as analysis_exc:
            if batch_id:
                try:
                    repo.mark_batch_symbol_completed(batch_id=batch_id, symbol=snapshot["symbol"], failed=True)
                    # P0-3: finish the batch when all symbols are done
                    # P1-4 (Round 3): check for failed symbols to set correct batch status
                    if repo.is_batch_complete(batch_id):
                        if repo.batch_all_failed(batch_id):
                            batch_status = "failed"
                        elif repo.batch_has_failures(batch_id):
                            batch_status = "partial_failed"
                        else:
                            batch_status = "success"
                        # Phase B (07-07): merge breaker snapshot into batch summary.
                        # Use the breaker cache directly since controller may not
                        # be available in the except block.
                        _bs = _batch_breakers.get(batch_id)
                        _brk = _bs.get("breaker") if isinstance(_bs, dict) else None
                        llm_health = _brk.snapshot() if _brk else {}
                        wcb = _bs.get("wall_clock_budget") if isinstance(_bs, dict) else None
                        if wcb is not None:
                            llm_health["wall_clock_budget_ms_remaining"] = wcb.remaining_ms()
                        # 07-10 Phase E reviewer P2-5: the normal completion
                        # path calls ``controller.get_batch_llm_health`` which
                        # merges the per-decision aggregate (_aggregate_batch_
                        # llm_outcomes) into the snapshot. This exception path
                        # used to build llm_health from the breaker snapshot
                        # alone, dropping the Phase E per-decision fields
                        # (expected_symbols, llm_symbols_attempted, coverage, ...).
                        # Without them _render_llm_coverage_line returns "" and
                        # the report silently falls back to the legacy single-
                        # line stats, losing the pipeline/coverage split and the
                        # skip breakdown exactly when something went wrong. Merge
                        # the aggregate here too so the exception-path batch
                        # reports with the same fidelity as the happy path.
                        try:
                            from plugins.crypto_guard.ga_master.controller import (
                                _aggregate_batch_llm_outcomes,
                            )
                            _per_decision = _aggregate_batch_llm_outcomes(repo, batch_id)
                            if _per_decision:
                                llm_health.update(_per_decision)
                        except Exception:
                            LOGGER.warning(
                                "run_ga_workers: per-decision LLM aggregate failed "
                                "on exception path batch=%s", batch_id, exc_info=True,
                            )
                        summary = {"llm_health": llm_health} if llm_health else None
                        repo.finish_analysis_batch(batch_id=batch_id, status=batch_status, summary=summary)
                        _batch_breakers.pop(batch_id, None)
                except Exception:
                    pass
            raise
        signal_id = int(decision["signal_id"])
        # 07-10 R5-3: post-decision side effects are shared with the fair
        # batch path via ``_post_decision_effects`` so paper orders, position
        # revalidation, and signal alerts cannot diverge between dispatch
        # modes. This block used to inline all three effects.
        result = _post_decision_effects(
            repo, decision, payload,
            send_message=send_message, job_id=job.get("id"),
        )
        return result
    if job_type == "update_opportunity_watches":
        result = update_opportunity_watches(repo, analysis_time_utc=payload.get("analysis_time_utc"))
        LOGGER.info("process_job done id=%s type=%s ok=%s triggered=%s", job.get("id"), job_type, result.get("ok"), result.get("triggered"))
        return result
    if job_type in ("opportunity_watch_alert", "opportunity_watch_recheck"):
        # 08-04 contract A: legacy opportunity_watch_alert jobs (if any still
        # queued) are replayed as a silent internal-only recheck — never a
        # feishu push. Both job types dispatch to the same handler.
        result = handle_opportunity_watch_recheck(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "trade_review":
        result = review_trade(repo, int(payload["trade_id"]))
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "daily_review":
        result = run_daily_review(repo, day_utc=payload.get("day_utc"))
        target = resolve_report_target(repo, payload)
        loss_count = payload.get("loss_count")
        loss_header = f"（今日 {loss_count} 笔止损触发复盘）\n" if loss_count else ""
        # Build detailed evolution status
        evolution_text = _build_evolution_status_text(repo)
        full_text = loss_header + result["text"] + evolution_text

        # Three-layer push defense:
        # L1: run_daily_review(force=False) already returns idempotent if report exists
        # L2: check pushed_to_feishu before sending
        # L3: alert dedupe_key includes review_date
        review_date = result.get("day_start_utc", "")[:10]
        already_pushed = result.get("pushed_to_feishu")
        if target and send_message and not already_pushed:
            sent_result = send_markdown_alert(
                repo, send_message,
                receive_id=target["receive_id"],
                receive_id_type=target.get("receive_id_type", "chat_id"),
                text=full_text,
                alert_type="daily_review",
                priority=5,
                dedupe_key=f"daily_review:{review_date}",
            )
            result["sent"] = bool(sent_result.get("sent"))
            result["target"] = target
            # Mark pushed_to_feishu on successful send
            if sent_result.get("sent") and review_date:
                repo.conn.execute(
                    "UPDATE daily_review_reports SET pushed_to_feishu=1 WHERE review_date=%s",
                    (review_date,),
                )
        else:
            result["sent"] = False
            result["target"] = target
        LOGGER.info("process_job done id=%s type=%s ok=%s reviews=%s sent=%s idempotent=%s", job.get("id"), job_type, result.get("ok"), result.get("new_reviews"), result.get("sent"), result.get("idempotent"))
        return result
    if job_type == "intraday_loss_review":
        result = _handle_intraday_loss_review(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "hourly_feishu_report":
        retry_count = int(payload.get("retry_count") or 0)
        expected_batch_id = payload.get("expected_batch_id")
        report_hour_utc = payload.get("report_hour_utc")
        expected_analysis_time = payload.get("expected_analysis_time")
        receive_id = payload.get("receive_id")
        receive_id_type = payload.get("receive_id_type")
        # 07-13 R6-C (P0-4): carry the immutable first-wait/deadline anchor
        # across retries so the report gates on REAL elapsed time, not retry_count.
        first_wait_utc = payload.get("first_wait_utc")
        # R8-B (P0-1): carry the monotonic ``poll_sequence`` so every requeue has
        # a unique session_id even when ``retry_count`` is clamped at the cap.
        # Default 0 for legacy/first payloads that carry no ``poll_sequence``.
        poll_sequence = int(payload.get("poll_sequence") or 0)
        report = build_hourly_report(
            repo,
            retry_count=retry_count,
            expected_batch_id=expected_batch_id,
            report_hour_utc=report_hour_utc,
            expected_analysis_time=expected_analysis_time if expected_analysis_time is not None else None,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            first_wait_utc=first_wait_utc,
            poll_sequence=poll_sequence,
        )
        if report.get("error") == "batch_incomplete_requeued":
            LOGGER.info("hourly_feishu_report requeued retry=%s batch=%s", report.get("retry_count"), report.get("batch_id"))
            return report
        target = resolve_report_target(repo, payload)
        if target and send_message:
            sent_result = send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=report["text"], alert_type="hourly_summary", priority=3)
            report["sent"] = bool(sent_result.get("sent"))
            report["target"] = target
        else:
            report["sent"] = False
            report["target"] = target
        LOGGER.info("process_job done id=%s type=%s sent=%s", job.get("id"), job_type, report.get("sent"))
        return report
    if job_type == "update_paper_positions":
        result = update_paper_positions(repo)
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "paper_event_alert":
        result = handle_paper_event_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "alert_outbox_retry":
        result = process_alert_outbox(repo, send_message, limit=int(payload.get("limit") or 10))
        LOGGER.info("process_job done id=%s type=%s processed=%s sent=%s", job.get("id"), job_type, result.get("processed"), result.get("sent"))
        return result
    if job_type == "paper_drawdown_alert":
        result = handle_paper_drawdown_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "evolution_trigger_alert":
        result = handle_evolution_trigger_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s queued=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"), result.get("queued"))
        return result
    if job_type == "pending_order_management":
        from plugins.crypto_guard.paper.pending_order_manager import run_pending_order_management
        result = run_pending_order_management(repo, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s expired=%s cancelled=%s", job.get("id"), job_type, result.get("ok"), result.get("expire", {}).get("expired_count"), result.get("conflict", {}).get("cancelled_count"))
        return result
    if job_type == "pending_order_revalidation":
        from plugins.crypto_guard.paper.pending_revalidator import revalidate_pending_orders
        result = revalidate_pending_orders(repo, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s reviewed=%s actions=%s", job.get("id"), job_type, result.get("ok"), result.get("reviewed_count"), result.get("actions_count"))
        return result
    if job_type == "position_conflict_revalidation":
        from plugins.crypto_guard.paper.position_conflict_revalidator import run_position_conflict_revalidation
        result = run_position_conflict_revalidation(repo, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s checked=%s conflicts=%s closed=%s stop_adjusted=%s recheck=%s",
                    job.get("id"), job_type, result.get("ok"),
                    result.get("checked_count"), result.get("conflict_count"),
                    result.get("closed_count"), result.get("stop_adjusted_count"),
                    result.get("recheck_count"))
        return result
    return {"ok": False, "error": f"未知 job_type: {job_type}"}


def _handle_intraday_loss_review(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Handle intraday loss threshold alert — risk warning only, NOT daily review.

    Does NOT write daily_review_reports or skill_feedback_memory.
    Only pushes a risk alert and optionally evaluates evolution triggers.
    """
    from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
    from plugins.crypto_guard.notify.time_utils import format_event_time_cst_for_line

    day_utc = payload.get("day_utc", "")
    loss_count = int(payload.get("loss_count") or 0)
    target = resolve_report_target(repo, payload)

    # Evaluate evolution triggers (creates/updates trigger, does NOT create candidate)
    evolution = evaluate_evolution_triggers(repo)

    event_time = format_event_time_cst_for_line(datetime.now(timezone.utc).isoformat())

    # Build risk alert text
    lines = [
        "**CryptoGuard 盘中风险提醒 · 止损阈值触发**",
        "",
        f"- 日期：{day_utc}",
        f"- 今日止损：{loss_count} 笔",
        f"- {event_time}",
        f"- 进化状态：{'已触发' if evolution.get('triggered') else '未触发'}",
        "",
        "系统将继续监控，不影响现有模拟盘持仓。",
        "",
        "不构成实盘建议，仅用于模拟盘与策略研究。",
    ]
    text = "\n".join(lines)

    sent = False
    if target and send_message:
        loss_bucket = "3_loss" if loss_count <= 3 else "5_loss"
        sent_result = send_markdown_alert(
            repo, send_message,
            receive_id=target["receive_id"],
            receive_id_type=target.get("receive_id_type", "chat_id"),
            text=text,
            alert_type="intraday_loss_review",
            priority=4,
            dedupe_key=f"intraday_loss_review:{day_utc}:{loss_bucket}",
        )
        sent = bool(sent_result.get("sent"))

    return {
        "ok": True,
        "sent": sent,
        "target": target,
        "loss_count": loss_count,
        "day_utc": day_utc,
        "evolution": evolution,
    }


def handle_button_callback(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    from plugins.crypto_guard.data.symbol_registry import add_symbol
    from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_ga_decision, create_paper_order_from_signal

    action = payload.get("action")
    symbol = payload.get("symbol")
    signal_id = payload.get("signal_id")
    ga_decision_id = payload.get("ga_decision_id")
    if action == "create_paper_order":
        result = create_paper_order_from_ga_decision(repo, int(ga_decision_id)) if ga_decision_id else create_paper_order_from_signal(repo, int(signal_id))
    elif action == "add_to_watchlist":
        result = add_symbol(repo, symbol, validate=False)
    elif action == "create_opportunity_watch":
        # 08-02 Codex P1 (terminal-review round 2): the manual button path must
        # re-check the SAME materialization gate as the worker auto path —
        # ``is_structured_watch`` (which now requires ``needed is True``) — so a
        # watch with needed=False / non-positive expires / non-str reason / a
        # non-level condition can never be button-created. Fail-closed.
        from plugins.crypto_guard.reasoning.watch_conditions import is_structured_watch

        ga_decision = repo.get_ga_decision(int(ga_decision_id)) if ga_decision_id else None
        if ga_decision:
            actions = set(ga_decision.get("feishu_actions") or [])
            grade = str(ga_decision.get("signal_grade") or "D").upper()
            watch = ga_decision.get("opportunity_watch") or {}
            if "create_opportunity_watch" not in actions or grade in {"D", "C"}:
                result = {"ok": False, "error": "该 GA decision 不允许加入机会监控"}
            elif not watch:
                result = {"ok": False, "error": "该 GA decision 没有机会监控条件"}
            elif not is_structured_watch(watch):
                result = {"ok": False, "error": "该 GA decision 的机会监控未通过结构化校验（需 needed=True 的 structured watch）"}
            else:
                result = {
                    "ok": True,
                    "watch_id": repo.create_opportunity_watch(
                        symbol or ga_decision["symbol"],
                        watch,
                        source_signal_id=int(signal_id) if signal_id else None,
                        ga_decision_id=int(ga_decision_id),
                        created_by_user_action=True,
                        source_button_action=action,
                    ),
                }
        else:
            signal = repo.get_signal(int(signal_id)) if signal_id else None
            watch = _decode_json(signal.get("opportunity_watch_json"), {}) if signal else {}
            if not signal:
                result = {"ok": False, "error": "该 signal 不存在"}
            elif str(signal.get("signal_grade") or "D").upper() in {"D", "C"}:
                result = {"ok": False, "error": "D/C 级信号不允许加入机会监控"}
            elif not watch:
                result = {"ok": False, "error": "该 signal 没有机会监控条件"}
            elif not is_structured_watch(watch):
                result = {"ok": False, "error": "该 signal 的机会监控未通过结构化校验（需 needed=True 的 structured watch）"}
            else:
                compat_ga_decision_id = signal.get("ga_decision_id") or _ensure_ga_decision_for_watch_signal(repo, signal, watch)
                result = {
                    "ok": True,
                    "watch_id": repo.create_opportunity_watch(
                        symbol or signal["symbol"],
                        watch,
                        source_signal_id=int(signal_id),
                        ga_decision_id=int(compat_ga_decision_id),
                        created_by_user_action=True,
                        source_button_action=action,
                    ),
                }
    elif action == "ignore":
        marked = repo.mark_ad_hoc_analysis_status_by_signal(int(signal_id), "ignored") if signal_id else False
        result = {"ok": True, "ignored": True, "ad_hoc_marked": marked}
    elif action == "approve_evolution":
        candidate_version = payload.get("candidate_version")
        if not candidate_version:
            result = {"ok": False, "error": "missing candidate_version"}
        else:
            # Find strategy name from strategy_versions
            row = repo.conn.execute(
                "SELECT strategy_name FROM strategy_versions WHERE version=%s",
                (candidate_version,)
            ).fetchone()
            strategy_name = row["strategy_name"] if row else "smc_pullback_long"

            from plugins.crypto_guard.strategy.shadow_testing import promote_shadow_candidate
            result = promote_shadow_candidate(
                repo,
                strategy_name=strategy_name,
                candidate_version=candidate_version,
                confirm=True,
                change_reason="manual approve from Feishu evolution review",
            )

            # Only update trigger and patches if promotion succeeded
            if result.get("ok"):
                # Update trigger resolved_at
                # 07-16 cutover: datetime('now')->NOW(), ?->%s, self-wrap transaction.
                with repo.conn.transaction():
                    repo.conn.execute(
                        "UPDATE evolution_triggers SET resolved_at=NOW(), status='active' WHERE id IN "
                        "(SELECT trigger_id FROM strategy_patches WHERE candidate_version=%s AND trigger_id IS NOT NULL)",
                        (candidate_version,)
                    )
                    repo.conn.execute(
                        "UPDATE strategy_patches SET status='active' WHERE candidate_version=%s AND status NOT IN ('rejected', 'duplicate', 'active')",
                        (candidate_version,)
                    )
    elif action == "reject_evolution":
        candidate_version = payload.get("candidate_version")
        if not candidate_version:
            result = {"ok": False, "error": "missing candidate_version"}
        else:
            # Update all 3 tables to rejected
            # 07-16 cutover: ?->%s, self-wrap transaction.
            with repo.conn.transaction():
                repo.conn.execute(
                    "UPDATE strategy_versions SET status='rejected', change_reason='manual reject from Feishu' WHERE version=%s",
                    (candidate_version,)
                )
                repo.conn.execute(
                    "UPDATE strategy_patches SET status='rejected' WHERE candidate_version=%s AND status NOT IN ('rejected', 'duplicate')",
                    (candidate_version,)
                )
                repo.conn.execute(
                    "UPDATE evolution_triggers SET status='rejected', resolved_at=NOW() WHERE id IN "
                    "(SELECT trigger_id FROM strategy_patches WHERE candidate_version=%s AND trigger_id IS NOT NULL)",
                    (candidate_version,)
                )
            result = {"ok": True, "action": "reject_evolution", "candidate_version": candidate_version}
    else:
        result = {"ok": False, "error": f"未知按钮动作: {action}"}
    if send_message and payload.get("receive_id"):
        send_markdown_alert(
            repo,
            send_message,
            receive_id=payload["receive_id"],
            receive_id_type=payload.get("receive_id_type", "open_id"),
            text=_button_result_text(action, result),
            alert_type="button_callback_result",
            symbol=symbol,
            priority=2,
        )
    return result


def _run_recheck_analysis(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    analysis_time_utc: int,
    snapshot_id: int | None,
) -> dict[str, Any]:
    """08-04 contract B: fresh recheck decision from the LATEST closed candle.

    ``build_market_state_snapshot`` always rebuilds from the latest closed
    candle (never a stored/stale snapshot), then the real controller pipeline
    runs and persists the decision. This is the default ``_analyze`` seam.
    """
    snapshot = build_market_state_snapshot(
        repo,
        symbol=symbol,
        analysis_time_utc=analysis_time_utc,
        mode="opportunity_watch",
    )
    controller = GAMasterController(repo)
    request = GAAnalysisRequest(
        symbol=symbol,
        decision_type="opportunity_watch_recheck",
        analysis_time_utc=int(snapshot.get("analysis_time_utc") or analysis_time_utc),
        mode="opportunity_watch",
        snapshot=snapshot,
        snapshot_id=snapshot_id,
        requested_by="opportunity_watch_recheck",
        request_text="opportunity watch recheck",
    )
    return controller.analyze_symbol(request)


def _recheck_order_gate(repo: CryptoGuardRepository, watch: dict[str, Any], decision: dict[str, Any]) -> tuple[bool, str]:
    """08-04 contract B gate: an order is created ONLY when S/A + llm ok +
    LLM confirmed + risk_ok + valid final trade_plan + account not paused +
    direction valid. Anything else rejects the order (B/C/D grades,
    llm-unconfirmed, risk-rejected, continuity-invalidated, candidate-only).

    ``plan_execution_state == "confirmed"`` already encodes the finalizer
    contract (llm confirmed AND has_trade_plan AND non-empty plan AND risk_ok
    AND effective grade S/A) — see ``_finalize_plan_lifecycle``. We re-check
    the individual fields here so a malformed/stale decision cannot slip through
    on that one flag alone.
    """
    sym = str(watch.get("symbol") or "")
    state = decision.get("plan_execution_state")
    if state != "confirmed":
        return False, f"plan_execution_state={state!r} (need confirmed)"
    grade = str(decision.get("effective_signal_grade") or decision.get("signal_grade") or "")
    if grade not in ("S", "A"):
        return False, f"grade={grade!r} (need S/A)"
    if decision.get("llm_status") != "ok":
        return False, f"llm_status={decision.get('llm_status')!r} (need ok)"
    if decision.get("plan_origin") != "llm_confirmed":
        return False, f"plan_origin={decision.get('plan_origin')!r} (need llm_confirmed)"
    risk = decision.get("risk_check") or {}
    if not risk.get("risk_ok"):
        return False, "risk_ok=false"
    tp = decision.get("trade_plan")
    if not isinstance(tp, dict) or not tp:
        return False, "no final trade_plan"
    side = str(tp.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        return False, f"invalid side {side!r}"
    # Direction valid: the order side must match the watch's intended direction.
    wdir = str(watch.get("direction") or "").upper()
    if wdir and wdir != side:
        return False, f"direction mismatch watch={wdir!r} plan={side!r}"
    entry = tp.get("entry_price")
    stop = tp.get("stop_loss")
    if entry is None or stop is None:
        return False, "missing entry_price/stop_loss"
    try:
        if abs(float(entry) - float(stop)) < 1e-9:
            return False, "entry_price==stop_loss invalid"
    except (TypeError, ValueError):
        return False, "non-numeric entry_price/stop_loss"
    acct = AccountRiskGuard(repo).check(symbol=sym, side=side)
    if acct.get("pause_active"):
        return False, "account risk paused"
    return True, "ok"


def handle_opportunity_watch_recheck(
    repo: CryptoGuardRepository,
    payload: dict[str, Any],
    *,
    send_message: Callable[..., Any] | None = None,
    _analyze: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """08-04 contract A/B + 08-06 once-ever: triggered-watch follow-up is
    INTERNAL-ONLY and ONCE-EVER.

    No feishu alert is produced and no alert_outbox row is written. The job
    runs a FRESH re-analysis from the latest closed candle (contract B). When
    the recheck clears the order gate (S/A + llm confirmed + risk_ok + account
    ok + direction valid) it bridges the decision into ONE paper order via
    ``create_paper_order(trigger_watch_id=...)``; the partial unique index
    ``idx_paper_orders_trigger_watch_once`` (once-ever: one order per watch over
    its ENTIRE lifetime, ``WHERE trigger_watch_id IS NOT NULL``) + a task lock
    make the bridge idempotent (single analysis, single order, no duplicate).
    A terminal (filled/expired/cancelled) order STILL holds the link: a delayed
    retry recheck that fires afterwards is judged duplicate and never
    re-analyzes nor mints a second order. Otherwise the watch's ``recheck_status``
    records the rejection and nothing is ordered.

    Legacy ``opportunity_watch_alert`` jobs are replayed through this same
    silent path, so a stale queued job degrades to a harmless no-op instead of
    a user-facing push.
    """
    watch = repo.get_opportunity_watch(int(payload["watch_id"]))
    if not watch:
        return {"ok": False, "error": "opportunity_watch 不存在", "sent": False, "internal_only": True}
    watch_id = int(watch["id"])
    # Task lock: only ONE recheck may run per watch at a time. A duplicate /
    # repeated trigger that slips through dedup is a no-op here.
    lock_name = f"opportunity_watch_recheck:{watch_id}"
    if not repo.acquire_lock(lock_name, "opportunity_watch_recheck", 600):
        return {
            "ok": False, "error": "recheck_already_in_progress",
            "sent": False, "internal_only": True, "watch_id": watch_id,
        }
    try:
        # Idempotency (once-ever): a paper order of ANY status already bridged
        # for this watch must never produce a second order -- a terminal order
        # still holds the link, so a delayed retry is a duplicate, not a new order.
        existing = repo.get_paper_order_by_trigger_watch(watch_id)
        if existing is not None:
            repo.touch_opportunity_watch(watch_id)
            return {
                "ok": True, "watch_id": watch_id, "internal_only": True,
                "sent": False, "duplicate": True,
                "paper_order_id": int(existing["id"]),
                "text": "内部观察上下文已归档（已有订单，不重复下单）",
            }
        repo.touch_opportunity_watch(watch_id)
        sym = str(watch.get("symbol") or "")
        analysis_time_utc = utc_ms()
        analyzer = _analyze or _run_recheck_analysis
        decision = analyzer(
            repo, symbol=sym, analysis_time_utc=analysis_time_utc, snapshot_id=None,
        )
        ok, reason = _recheck_order_gate(repo, watch, decision)
        if not ok:
            repo.set_opportunity_watch_recheck_status(watch_id, "recheck_rejected")
            return {
                "ok": True, "watch_id": watch_id, "internal_only": True,
                "sent": False, "rejected": True, "reason": reason,
                "text": "内部观察上下文已归档（不推送）",
            }
        # 08-04 contract E8 production wiring (fresh reviewer P1): a VETO-ONLY
        # broker verifier round runs before the watch->order bridge. Fail-open
        # (see _broker_verifier_allows); the deterministic recheck gate above
        # stays authoritative — the broker can veto but never grant eligibility.
        verifier_ok, _verifier = _broker_verifier_allows(
            repo,
            symbol=sym,
            timeframe="15m",
            analysis_time_utc=analysis_time_utc,
            deterministic_risk_ok=True,
        )
        if not verifier_ok:
            repo.set_opportunity_watch_recheck_status(watch_id, "recheck_rejected")
            return {
                "ok": True, "watch_id": watch_id, "internal_only": True,
                "sent": False, "rejected": True, "reason": "broker_verifier_veto",
                "text": "内部观察上下文已归档（不推送）",
            }
        tp = decision.get("trade_plan")
        signal = {
            "symbol": sym,
            "side": str(tp.get("side") or "").upper(),
            "grade": decision.get("effective_signal_grade"),
        }
        ga_id = int(decision.get("ga_decision_id") or 0)
        order_id, created = repo.create_paper_order(
            decision.get("signal_id"),
            signal,
            tp,
            ga_decision_id=ga_id if ga_id else None,
            source="watch_recheck",
            risk_check_passed=True,
            trigger_watch_id=watch_id,
        )
        repo.set_opportunity_watch_recheck_status(watch_id, "order_created", order_id=order_id)
        return {
            "ok": True, "watch_id": watch_id, "internal_only": True,
            "sent": False, "paper_order_id": order_id, "created": created,
            "ga_decision_id": ga_id, "text": "内部观察上下文已归档（不推送）",
        }
    finally:
        repo.release_lock(lock_name, "opportunity_watch_recheck")


def handle_paper_event_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    target = resolve_report_target(repo, payload)
    event_type = payload.get("event_type", "paper_event")
    event_cn = {
        "paper_order_filled": "已成交",
        "paper_order_expired": "已过期",
        "take_profit_hit": "止盈触发",
        "stop_loss_hit": "止损触发",
        "stop_loss_adjustment": "止损调整",
        "close_position": "平仓",
        "risk_alert": "风险提醒",
        "opportunity_triggered": "机会触发",
    }.get(event_type, event_type)
    side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(payload.get("side") or "").upper(), payload.get("side") or "-")
    fill_method_cn = {
        "limit_range_touch": "限价触及",
        "trigger_touch": "触发价触及",
        "next_candle_open_with_slippage": "市价成交（含滑点）",
    }.get(payload.get("fill_method"), payload.get("fill_method") or "")
    close_reason_cn = {
        "take_profit": "止盈",
        "stop_loss": "止损",
        "timeout": "超时平仓",
        "manual": "手动平仓",
        "conflict_exit": "方向冲突提前退出",
        "strong_conflict_profit_protection": "利润保护平仓",
    }.get(payload.get("close_reason"), payload.get("close_reason") or "")
    if event_type == "close_position" and payload.get("close_reason") == "conflict_exit":
        event_cn = "提前退出"
    elif event_type == "close_position" and payload.get("close_reason") == "manual":
        event_cn = "手动平仓"
    elif event_type == "close_position" and payload.get("close_reason") == "strong_conflict_profit_protection":
        event_cn = "利润保护平仓"

    # Calculate USDT P&L from R-multiple
    pnl_r = payload.get("pnl_r")
    pnl_usdt_text = ""
    if pnl_r is not None:
        order_id = payload.get("order_id")
        if order_id:
            try:
                order_row = repo.conn.execute("SELECT entry_price, stop_loss FROM paper_orders WHERE id=%s", (int(order_id),)).fetchone()
                if order_row:
                    entry = float(order_row["entry_price"] or 0)
                    stop = float(order_row["stop_loss"] or 0)
                    risk_per_unit = abs(entry - stop)
                    risk_pct = 0.005  # 0.5% default
                    risk_usdt = 10000.0 * risk_pct  # 10000U starting equity * 0.5%
                    pnl_usdt = float(pnl_r) * risk_usdt
                    pnl_usdt_text = f"（{pnl_usdt:+.2f}U）"
            except Exception:
                pass

    # Build event-specific details
    detail_lines = []
    from plugins.crypto_guard.notify.time_utils import format_event_time_cst, format_event_time_cst_compact, format_event_time_cst_for_line

    # Resolve event time: use event's own timestamp first, fall back to current UTC
    event_time = payload.get("event_time") or payload.get("closed_at")
    if event_type == "paper_order_filled":
        event_time = event_time or payload.get("filled_at")
    # 08-04 contract A: fills render an explicit "成交时间" line inside the fill
    # branch, so skip the generic time line here to avoid a duplicated field.
    if event_type != "paper_order_filled":
        if event_time:
            detail_lines.append(f"- {format_event_time_cst_for_line(event_time)}")
        else:
            detail_lines.append(f"- {format_event_time_cst_for_line(datetime.now(timezone.utc).isoformat())}")

    if event_type == "stop_loss_adjustment":
        new_stop = payload.get("new_stop_loss")
        adj_reason = payload.get("reason", "")
        mark_price = payload.get("mark_price")
        if mark_price:
            detail_lines.append(f"- 当前 Mark Price：{float(mark_price):.4f}")
        if new_stop:
            detail_lines.append(f"- 新止损：{new_stop}")
        if adj_reason:
            detail_lines.append(f"- 原因：{adj_reason}")
        current_r = payload.get("current_r")
        mfe_r = payload.get("mfe_r")
        if current_r is not None:
            detail_lines.append(f"- 当前 R：{float(current_r):+.2f}")
        if mfe_r is not None:
            detail_lines.append(f"- MFE/R：{float(mfe_r):+.2f}")
    elif event_type in ("take_profit_hit", "stop_loss_hit", "close_position"):
        reason = close_reason_cn
        detail_lines.append(f"- 原因：{reason}")
        # Entry details
        entry_price = payload.get("entry_price")
        if entry_price:
            detail_lines.append(f"- 入场价：{float(entry_price):.4f}")
        filled_at = payload.get("filled_at")
        if filled_at:
            filled_cn = format_event_time_cst_compact(filled_at)
            if filled_cn != "不可用":
                detail_lines.append(f"- 入场时间：{filled_cn}")
            else:
                detail_lines.append(f"- 入场时间：{filled_at}")
        # TP/SL prices
        stop_loss = payload.get("stop_loss")
        if stop_loss:
            detail_lines.append(f"- 止损价：{float(stop_loss):.4f}")
        take_profits = payload.get("take_profits") or []
        if take_profits:
            tp_prices = [f"{float(tp.get('price', tp)):.4f}" if isinstance(tp, dict) else f"{float(tp):.4f}" for tp in take_profits]
            detail_lines.append(f"- 止盈价：{', '.join(tp_prices)}")
        # Exit price — show as "退出 Mark Price" for conflict/profit-protection, "退出价" for TP/SL
        exit_price = payload.get("exit_price")
        close_reason = payload.get("close_reason", "")
        if exit_price:
            if close_reason in ("conflict_exit", "strong_conflict_profit_protection"):
                detail_lines.append(f"- 退出 Mark Price：{float(exit_price):.4f}")
            else:
                detail_lines.append(f"- 退出价：{float(exit_price):.4f}")
        # Mark price context for close events
        mark_price = payload.get("mark_price")
        if mark_price and not exit_price:
            detail_lines.append(f"- 当前 Mark Price：{float(mark_price):.4f}")
        if pnl_r is not None:
            detail_lines.append(f"- 盈亏：{float(pnl_r):+.2f}R{pnl_usdt_text}")
        # R-multiples for profit protection / conflict
        current_r = payload.get("current_r")
        mfe_r = payload.get("mfe_r")
        retracement_r = payload.get("retracement_r")
        if current_r is not None:
            detail_lines.append(f"- 当前 R：{float(current_r):+.2f}")
        if mfe_r is not None:
            detail_lines.append(f"- MFE/R：{float(mfe_r):+.2f}")
        if retracement_r is not None:
            detail_lines.append(f"- 回撤 R：{float(retracement_r):+.2f}")
    elif event_type == "paper_order_filled":
        if fill_method_cn:
            detail_lines.append(f"- 成交方式：{fill_method_cn}")
        # 08-04 contract A: the fill push must carry fill price, fill time,
        # slippage and the resulting position.
        fill_time = payload.get("filled_at") or payload.get("event_time")
        if fill_time:
            filled_cn = format_event_time_cst_compact(fill_time)
            detail_lines.append(f"- 成交时间：{filled_cn if filled_cn != '不可用' else fill_time}")
        slippage = payload.get("slippage")
        if slippage is not None:
            detail_lines.append(f"- 滑点：{float(slippage):+.4f}")
        position = payload.get("position")
        if isinstance(position, dict) and position.get("quantity"):
            pos_side = {"LONG": "多单", "SHORT": "空单"}.get(str(position.get("side") or "").upper(), position.get("side") or "仓位")
            avg_price = position.get("avg_price") or position.get("avg_entry_price")
            pos_text = f"{pos_side} {position.get('quantity')} 张"
            if avg_price is not None:
                pos_text += f" @ {float(avg_price):.4f}"
            detail_lines.append(f"- 持仓：{pos_text}")
        stop_loss = payload.get("stop_loss")
        if stop_loss:
            detail_lines.append(f"- 止损价：{float(stop_loss):.4f}")
        take_profits = payload.get("take_profits") or []
        if take_profits:
            tp_prices = [f"{float(tp.get('price', tp)):.4f}" if isinstance(tp, dict) else f"{float(tp):.4f}" for tp in take_profits]
            detail_lines.append(f"- 止盈价：{', '.join(tp_prices)}")
    else:
        reason = close_reason_cn or fill_method_cn
        if reason:
            detail_lines.append(f"- 原因：{reason}")
        if pnl_r is not None:
            detail_lines.append(f"- 盈亏：{float(pnl_r):+.2f}R{pnl_usdt_text}")

    # Price: use specific labels — never show a generic "价格" label
    close_reason = payload.get("close_reason", "")
    if event_type in ("take_profit_hit", "stop_loss_hit"):
        price_label = "退出价"
        display_price = payload.get("exit_price") or "不可用"
    elif event_type == "close_position":
        if close_reason in ("conflict_exit", "strong_conflict_profit_protection"):
            price_label = "退出 Mark Price"
        else:
            price_label = "退出价"
        display_price = payload.get("exit_price") or "不可用"
    elif event_type == "paper_order_filled":
        price_label = "成交价"
        entry_price = payload.get("entry_price")
        display_price = f"{float(entry_price):.4f}" if entry_price is not None else "不可用"
    elif event_type == "stop_loss_adjustment":
        price_label = "当前 Mark Price"
        display_price = payload.get("mark_price") or "不可用"
    else:
        # Unknown event_type — skip price line entirely
        price_label = None
        display_price = None

    lines = [
        f"**CryptoGuard 模拟盘 · {event_cn}**",
        "",
        f"- 产品：{payload.get('symbol', '-')}",
        f"- 方向：{side_cn}",
        f"- 订单：#{payload.get('order_id', '-')}",
    ]
    # 08-04 contract A: paper lifecycle pushes carry the source decision id so
    # the push is traceable to the GA decision that created the order.
    source_decision_id = payload.get("source_decision_id") or payload.get("ga_decision_id")
    if source_decision_id is not None:
        lines.append(f"- 决策ID：{source_decision_id}")
    if price_label and display_price:
        lines.append(f"- {price_label}：{display_price}")
    lines = lines + detail_lines + [
        "",
        "不构成实盘建议，仅用于模拟盘与策略研究。",
    ]
    text = "\n".join(lines)
    sent = False
    if target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type=str(event_type), symbol=payload.get("symbol"), priority=3).get("sent"))
    return {"ok": True, "sent": sent, "target": target, "text": text}


def handle_paper_drawdown_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    snapshot = payload.get("snapshot") or {}
    target = resolve_report_target(repo, payload)
    from plugins.crypto_guard.notify.time_utils import format_event_time_cst_for_line
    # Fallback to current UTC time so drawdown always shows a real UTC+8 time
    # instead of "不可用" when event_time/created_at are missing.
    event_time = format_event_time_cst_for_line(
        payload.get("event_time") or payload.get("created_at") or datetime.now(timezone.utc).isoformat()
    )
    text = "\n".join(
        [
            "**CryptoGuard 模拟盘回撤提醒**",
            "",
            f"- {event_time}",
            f"- 账户权益：{float(snapshot.get('account_equity') or 0):.2f}",
            f"- 已实现盈亏：{float(snapshot.get('realized_pnl') or 0):.2f}",
            f"- 未实现盈亏：{float(snapshot.get('unrealized_pnl') or 0):.2f}",
            f"- 回撤：{abs(float(snapshot.get('drawdown_percent') or 0)):.2f}%",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
    )
    sent = False
    if target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type="risk_alert", priority=3).get("sent"))
    return {"ok": True, "sent": sent, "target": target, "text": text}


def _fmt_utc8(ts: str | None) -> str:
    """Format an ISO timestamp to UTC+8 display string.

    Delegates to the shared formatter in notify/time_utils.py.
    Returns the time in 'YYYY-MM-DD HH:MM (UTC+8)' or '-' if None/unparseable.
    """
    from plugins.crypto_guard.notify.time_utils import format_event_time_cst_compact
    if not ts:
        return "-"
    result = format_event_time_cst_compact(ts)
    if result == "不可用":
        return str(ts)[:16]
    return result


def _build_evolution_status_text(repo: CryptoGuardRepository) -> str:
    """Build detailed evolution status text for daily review notification.

    Uses strategy_evaluations (WHERE is_shadow=1) for per-patch shadow stats,
    NOT paper_trades. Shows data quality breakdown: total/real_pnl/pseudo_r samples,
    win_rate, backtest status, effective_min_samples, blocking reason.
    """
    import json
    from plugins.crypto_guard.config.loader import load_config as _load_cfg

    _cfg = _load_cfg().trading_mode
    _online_cfg = _cfg.get("evolution", {}).get("online_shadow", {})
    _min_after_bt = _online_cfg.get("min_samples_after_backtest", 5)
    _min_without_bt = _online_cfg.get("min_samples_without_backtest", 30)
    _backtest_enabled = _cfg.get("evolution", {}).get("backtest_gate", {}).get("enabled", True)

    lines = []

    # ── Triggers ──────────────────────────────────────────────
    all_triggers = repo.conn.execute(
        "SELECT * FROM evolution_triggers ORDER BY latest_triggered_at DESC"
    ).fetchall()

    open_triggers = [dict(t) for t in all_triggers if t["status"] in ("pending", "shadow_testing", "review_required")]
    total_triggers = len(all_triggers)
    open_trigger_count = len(open_triggers)

    if not open_triggers:
        if total_triggers > 0:
            lines.append("")
            lines.append("---")
            lines.append("**自进化状态**")
            lines.append("")
            lines.append(f"共 {total_triggers} 个触发记录，全部已关闭。无活跃候选。")
            lines.append("")
        return "\n".join(lines)

    lines.append("")
    lines.append("---")
    lines.append("**自进化状态**")
    lines.append("")

    lines.append(f"共 {open_trigger_count} 个活跃触发 / 共 {total_triggers} 个历史触发")
    lines.append("")

    trigger_type_cn = {
        "consecutive_stop_losses": "连续止损",
        "daily_loss_threshold": "单日止损",
        "account_drawdown": "账户回撤",
    }
    status_cn = {
        "pending": "待处理",
        "shadow_testing": "影子测试中",
        "active": "已激活",
        "rejected": "已拒绝",
    }

    for t in open_triggers[:8]:
        ttype = trigger_type_cn.get(t.get("trigger_type"), t.get("trigger_type"))
        st = status_cn.get(t.get("status"), t.get("status"))
        trigger_value = t.get("trigger_value", 0)
        threshold = t.get("threshold_value", 0)

        # Parse original and latest trade IDs separately
        original_ids = _parse_json_list(t.get("original_related_trade_ids"))
        latest_ids = _parse_json_list(t.get("latest_related_trade_ids"))

        created = _fmt_utc8(t.get("created_at"))
        latest_at = _fmt_utc8(t.get("latest_triggered_at"))

        lines.append(f"[{st}] {ttype}")
        lines.append(f"  触发值：{trigger_value}（阈值 {threshold}）")
        if original_ids:
            lines.append(f"  原始关联交易：{' / '.join(f'#{tid}' for tid in original_ids[:5])}")
        if latest_ids and latest_ids != original_ids:
            lines.append(f"  最新关联交易：{' / '.join(f'#{tid}' for tid in latest_ids[:5])}")
        elif latest_ids:
            lines.append(f"  关联交易：{' / '.join(f'#{tid}' for tid in latest_ids[:5])}")
        lines.append(f"  首次触发：{created}")
        if latest_at != created:
            lines.append(f"  最近触发：{latest_at}")
        lines.append("")

    # ── Patches with shadow evaluation stats ──────────────────
    all_patches = repo.conn.execute(
        "SELECT * FROM strategy_patches WHERE status IN ('candidate', 'shadow_testing', 'review_required') ORDER BY id DESC"
    ).fetchall()

    open_patches = [dict(p) for p in all_patches]
    total_patch_count = len(open_patches)

    if open_patches:
        lines.append(f"**候选补丁**（共 {total_patch_count} 个）")
        lines.append("")

        for p in open_patches[:10]:
            p = dict(p)
            patch_id = p.get("id")
            strategy_name = p.get("strategy_name", "-")
            candidate_version = p.get("candidate_version", "-")
            reason = p.get("reason", "-")
            created = _fmt_utc8(p.get("created_at"))

            # Shadow evaluation stats from strategy_evaluations
            stats = repo.conn.execute(
                """SELECT COUNT(*) as total,
                          COUNT(CASE WHEN pnl_r IS NOT NULL AND outcome_source='real_pnl' THEN 1 END) as real_count,
                          COUNT(CASE WHEN pnl_r IS NULL OR outcome_source IS NULL OR outcome_source!='real_pnl' THEN 1 END) as pseudo_count,
                          AVG(CASE WHEN pnl_r IS NOT NULL AND outcome_source='real_pnl' THEN pnl_r END) as avg_r,
                          AVG(CASE WHEN pnl_r IS NULL OR outcome_source IS NULL OR outcome_source!='real_pnl' THEN (score - 0.5) * 2 END) as pseudo_avg_r
                   FROM strategy_evaluations
                   WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE AND ga_decision_id IS NOT NULL""",
                (strategy_name, candidate_version),
            ).fetchone()

            total = int(stats["total"]) if stats else 0
            real_count = int(stats["real_count"]) if stats else 0
            pseudo_count = int(stats["pseudo_count"]) if stats else 0
            avg_r = round(float(stats["avg_r"]), 3) if stats and stats["avg_r"] is not None else None

            # Compute win_rate from real PnL evaluations only
            if real_count >= 5:
                win_row = repo.conn.execute(
                    """SELECT COUNT(*) as wins FROM strategy_evaluations
                       WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE AND pnl_r IS NOT NULL AND outcome_source='real_pnl' AND pnl_r > 0.005""",
                    (strategy_name, candidate_version),
                ).fetchone()
                wins = int(win_row["wins"]) if win_row else 0
                wr = wins / real_count * 100
                win_text = f"胜率 {wr:.0f}%（{wins}W/{real_count - wins}L）"
            elif real_count > 0:
                win_text = "胜率不可计算（样本不足，需 ≥5 个真实 PnL 样本）"
            else:
                win_text = "胜率不可计算（无真实 PnL 样本）"

            # Data quality
            if real_count >= 5:
                dq = "good"
            elif real_count >= 1:
                dq = "limited"
            else:
                dq = "no_real_pnl"

            # Backtest status
            bt = _get_backtest_status(repo, candidate_version)

            lines.append(f"Patch #{patch_id}（{candidate_version}）")
            lines.append(f"  策略：{strategy_name}")
            lines.append(f"  原因：{reason}")
            lines.append(f"  影子样本：{total} 个（真实 PnL: {real_count}，伪 R: {pseudo_count}）")
            if avg_r is not None:
                lines.append(f"  平均 R：{avg_r}")
            lines.append(f"  {win_text}")
            lines.append(f"  数据质量：{dq}")

            # Backtest gate status
            if bt.get("gate_disabled"):
                lines.append(f"  回测门禁：已关闭")
            elif bt.get("skipped"):
                lines.append(f"  回测门禁：跳过（{bt.get('reason', '-')}）")
            elif bt.get("passed"):
                lines.append(f"  回测门禁：通过")
            else:
                lines.append(f"  回测门禁：未通过（{bt.get('reason', '-')}）")

            # Effective min samples and blocking reason
            has_backtest_pass = bt.get("passed") and not bt.get("skipped") and not bt.get("gate_disabled")
            gate_disabled = bt.get("gate_disabled", False)
            effective_min = _min_after_bt if (has_backtest_pass or gate_disabled) else _min_without_bt
            gap = max(0, effective_min - real_count) if real_count < effective_min else 0

            if gap > 0:
                lines.append(f"  还需 {gap} 个真实 PnL 样本才能进入判决（需要 {effective_min}，当前 {real_count}）")
            else:
                lines.append(f"  样本充足（{real_count}/{effective_min}），等待 shadow verdict 判决")

            # Last shadow sample time
            last_eval = repo.conn.execute(
                "SELECT created_at FROM strategy_evaluations WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE ORDER BY created_at DESC LIMIT 1",
                (strategy_name, candidate_version),
            ).fetchone()
            if last_eval:
                lines.append(f"  最后影子样本：{_fmt_utc8(last_eval['created_at'])}")

            lines.append(f"  创建时间：{created}")
            lines.append("")

        if total_patch_count > 10:
            lines.append(f"  ... 还有 {total_patch_count - 10} 个候选未显示")
            lines.append("")

    # ── Next steps ────────────────────────────────────────────
    lines.append("**下一步**")
    if _backtest_enabled:
        lines.append(f"- 影子测试需 {_min_after_bt} 个真实 PnL 样本（通过回测门禁后）或 {_min_without_bt} 个（未通过回测）")
    else:
        lines.append(f"- 影子测试需至少 {_min_without_bt} 个真实 PnL 样本确认效果")
    lines.append("- 胜率和盈亏比达标后可进入 review 阶段")
    lines.append("- review 通过后可手动确认进入 active")
    lines.append("")

    return "\n".join(lines)


def _parse_json_list(val: Any) -> list:
    """Parse a JSON-encoded list from DB, returning empty list on failure."""
    if not val:
        return []
    try:
        result = json.loads(val) if isinstance(val, str) else val
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []



def _get_backtest_status(repo: CryptoGuardRepository, candidate_version: str) -> dict[str, Any]:
    """Get backtest result for a candidate version."""
    row = repo.conn.execute(
        "SELECT backtest_result_json FROM strategy_patches WHERE candidate_version=%s AND backtest_result_json IS NOT NULL ORDER BY id DESC LIMIT 1",
        (candidate_version,)
    ).fetchone()
    if row and row["backtest_result_json"]:
        bt = _decode_json(row["backtest_result_json"], None)
        if isinstance(bt, dict):
            return bt
    return {"status": "unknown", "skipped": True, "reason": "no_backtest_data"}


def handle_evolution_trigger_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Send immediate notification when evolution is triggered or verdict promotes."""
    import json
    from plugins.crypto_guard.notify.time_utils import format_event_time_cst_for_line

    # Cleanup old text-type evolution_review alerts (should be interactive).
    # 07-16 cutover: literal % in LIKE -> %%; ``payload_json`` is ``jsonb`` and PG
    # has no ``~~`` (LIKE) operator for jsonb, so cast to text first to preserve
    # the SQLite-era substring match on the JSON text representation; self-wrap
    # transaction.
    with repo.conn.transaction():
        repo.conn.execute(
            "UPDATE alert_outbox SET status='superseded' WHERE alert_type='evolution_review' AND payload_json::text LIKE '%%\"msg_type\": \"text\"%%' AND status IN ('pending', 'sent')"
        )

    target = resolve_report_target(repo, payload)
    trigger_type = payload.get("trigger_type", "unknown")
    loss_count = payload.get("loss_count", 0)
    day = payload.get("day_utc", "-")
    trigger_id = payload.get("trigger_id")
    patch_id = payload.get("patch_id")
    reason = payload.get("reason", "")
    related_ids = payload.get("related_trade_ids") or []
    trigger_value = payload.get("trigger_value")
    threshold = payload.get("threshold_value")
    candidate_version = payload.get("candidate_version")
    sample_count = payload.get("sample_count", 0)

    trigger_type_cn = {
        "consecutive_stop_losses": "连续止损",
        "daily_loss_threshold": "单日止损",
        "account_drawdown": "账户回撤",
        "verdict_promotion": "影子测试通过",
    }.get(trigger_type, trigger_type)

    # Build trigger detail
    detail_lines = [f"**CryptoGuard 自进化触发**", ""]

    event_time = format_event_time_cst_for_line(datetime.now(timezone.utc).isoformat())

    if trigger_type == "verdict_promotion":
        # Special handling for verdict promotion
        detail_lines.append(f"- 触发类型：{trigger_type_cn}")
        detail_lines.append(f"- 候选版本：{candidate_version}")
        detail_lines.append(f"- 影子样本数：{sample_count}")
        detail_lines.append(f"- 原因：{reason}")
        detail_lines.append(f"- {event_time}")
        detail_lines.append("")
        detail_lines.append("候选策略已通过影子测试，等待人工确认升级。")
        detail_lines.append("")
        detail_lines.append("**请审核以下内容后决定是否批准：**")
        detail_lines.append("1. 候选策略的改进逻辑是否合理")
        detail_lines.append("2. 影子测试的样本量是否足够")
        detail_lines.append("3. 是否存在过拟合风险")
    else:
        # Original trigger handling
        detail_lines.append(f"- 触发类型：{trigger_type_cn}")
        if trigger_value and threshold:
            detail_lines.append(f"- 触发值：{trigger_value}（阈值 {threshold}）")
        if loss_count:
            detail_lines.append(f"- 今日止损：{loss_count} 笔")
        if reason:
            detail_lines.append(f"- 原因：{reason}")
        detail_lines.append(f"- {event_time}")
        if related_ids:
            ids_str = "/".join(f"#{tid}" for tid in related_ids[:5])
            detail_lines.append(f"- 关联交易：{ids_str}")
        if trigger_id:
            detail_lines.append(f"- 触发器 ID：#{trigger_id}")
        if patch_id:
            detail_lines.append(f"- 候选补丁 ID：#{patch_id}")
        detail_lines.append("")
        detail_lines.append("系统已自动创建候选补丁并进入影子测试。")

    # Use actual config values for sample requirement
    from plugins.crypto_guard.config.loader import load_config as _load_cfg2
    _cfg2 = _load_cfg2().trading_mode
    _online_cfg2 = _cfg2.get("evolution", {}).get("online_shadow", {})
    _min_after_bt2 = _online_cfg2.get("min_samples_after_backtest", 5)
    _min_without_bt2 = _online_cfg2.get("min_samples_without_backtest", 30)
    _backtest_enabled2 = _cfg2.get("evolution", {}).get("backtest_gate", {}).get("enabled", True)

    if trigger_type != "verdict_promotion":
        if _backtest_enabled2:
            detail_lines.append(f"影子测试需 {_min_after_bt2} 个样本（通过回测门禁）或 {_min_without_bt2} 个样本（未通过回测）后方可进入 review。")
        else:
            detail_lines.append(f"影子测试需至少 {_min_without_bt2} 个样本确认效果后方可进入 review。")

    # Get full evolution status
    evolution_text = _build_evolution_status_text(repo)

    text = "\n".join(detail_lines) + "\n" + evolution_text + "\n不构成实盘建议，所有策略变更仅进入 candidate/shadow 流程。"

    sent = False
    queued = False

    # For verdict_promotion: always enqueue to outbox if target exists (independent of send_message)
    if target and trigger_type == "verdict_promotion" and candidate_version:
        # Send card via outbox for retry capability
        from plugins.crypto_guard.notify.feishu_cards import build_evolution_review_card
        backtest_status = _get_backtest_status(repo, candidate_version)
        card = build_evolution_review_card(candidate_version, sample_count, reason, backtest_status=backtest_status)

        # Use alert_outbox for reliable delivery
        alert_id = repo.enqueue_alert(
            alert_type="evolution_review",
            symbol=None,
            priority=4,
            payload={
                "receive_id": target["receive_id"],
                "receive_id_type": target.get("receive_id_type", "chat_id"),
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
                "fallback_text": f"CryptoGuard 自进化人工审核: {candidate_version}, {sample_count} 样本",
            },
            dedupe_key=f"evolution_review:{candidate_version}",
        )
        queued = bool(alert_id)
        sent = queued  # For backward compatibility

    # For other types: use send_markdown_alert (requires send_message)
    elif target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type="evolution_trigger", priority=4).get("sent"))

    return {"ok": True, "sent": sent, "queued": queued, "target": target, "text": text}


def run_once(*, user_only: bool = False, background: bool = False, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    cfg = load_config()
    initialize_database(cfg)
    # PostgreSQL writes commit in short repository transactions. In
    # particular, the fair-batch path closes any implicit read transaction
    # before the provider call, heartbeats ownership on both sides of that
    # call, and CAS-finalizes each symbol independently.
    with _pg_get_conn() as conn:
        repo = CryptoGuardRepository(conn)
        # 07-16 cutover: Redis eligibility no longer depends on the (removed)
        # SQLite file path. PostgreSQL is a single shared durable DB, so Redis
        # is always eligible unless explicitly disabled via env
        # (``should_use_redis_for_path(None)`` encodes the production case in the
        # adapter -- matches feishu_integration.py / repository.py).
        redis = RedisAdapter() if should_use_redis_for_path(None) else None
        redis_payload = (redis.pop_user_job() if user_only else (redis.pop_background_job() if background else None)) if redis else None
        # 07-16 cutover: Redis is an acceleration channel ONLY. The PostgreSQL
        # ``agent_jobs`` table is the single authoritative job source. The legacy
        # SQLite cross-file mismatch guard (``PRAGMA database_list`` vs
        # ``redis_payload['database_path']``) was removed with the cutover --
        # PostgreSQL is one shared durable DB with no file path, so there is no
        # cross-SQLite-file identity check of any kind on this path. The Redis
        # payload's legacy ``database_path`` field is carried for backward
        # compatibility and does NOT participate in any PostgreSQL identity
        # decision. The consumer-side defenses that remain are: (1) the
        # job-type gate below (``scheduled_market_analysis`` must NOT run via the
        # Redis single-job path) and (2) ``claim_job_by_id_cas``, which is the
        # database-ownership gate for an ordinary Redis job (it rechecks id +
        # status + scheduled_at<=NOW() against PostgreSQL before the row may
        # flip to running).
        # 07-10 P1-4 (terminal review): consumer-side guard. The S2 producer
        # guard (``_enqueue_job_redis``) already keeps
        # ``scheduled_market_analysis`` out of the Redis queue, so a popped
        # payload of this type means the producer guard was bypassed (a stale
        # item enqueued before S2, a future code path that skips
        # ``enqueue_job``/``_enqueue_job_redis``, or a manual RPUSH). Without
        # this guard, ``run_once`` would execute it as a SINGLE serial
        # ``process_job`` here -> bypassing ``claim_next_batch`` and the fair
        # batch entirely (the known LLM starvation path), and the PostgreSQL row
        # (the sole authority) would never be claimed by the batch coordinator.
        # Defense in depth: drop the Redis payload (do NOT claim its
        # ``db_job_id`` here -- the row stays ``status='pending'`` so
        # ``claim_next_batch`` can claim the whole batch together below) and
        # fall through to the fair-pool path. We MUST NOT execute it serially,
        # even if Redis says so. This is a job-type gate only; no
        # ``database_path`` mismatch check exists on the PostgreSQL path (the
        # legacy SQLite file check was removed by the cutover).
        if redis_payload and redis_payload.get("job_type") == "scheduled_market_analysis":
            LOGGER.warning(
                "run_once: Redis popped a scheduled_market_analysis payload "
                "(job_type=%s db_job_id=%s) -- the S2 producer guard should "
                "have kept this out of Redis. Dropping the Redis item and "
                "re-routing to the fair-pool batch path (claim_next_batch); the "
                "PostgreSQL row is the sole authority and stays pending for the "
                "batch coordinator to claim as a group. NOT executing it as a "
                "single serial job (would bypass the fair batch / starve LLM).",
                redis_payload.get("job_type"), redis_payload.get("db_job_id"),
            )
            redis_payload = None
        if redis_payload:
            payload = redis_payload.get("payload") or {}
            db_job_id = redis_payload.get("db_job_id")
            if db_job_id:
                # Redis is an acceleration channel; PostgreSQL is
                # the sole ownership authority. The consumer MUST recheck
                # scheduled_at before flipping a row to running -- evidence §3.3
                # showed a future-scheduled Redis payload (report retry with
                # scheduled_at in the future) was claimed by id alone, exhausting
                # a nominal 300s wait in ~20s. claim_job_by_id_cas verifies
                # id + status='pending' + datetime(scheduled_at)<=datetime('now')
                # in ONE statement; a future or stale/duplicate payload fails
                # closed (0 rows) and the row stays pending for the database path.
                claimed = repo.claim_job_by_id_cas(
                    job_id=int(db_job_id), expected_status="pending",
                )
                if not claimed:
                    redis_payload = None
            if not redis_payload:
                job = repo.claim_next_job(max_priority=2) if user_only else repo.claim_next_job(background=background)
                if not job:
                    return {"ok": True, "processed": False, "reason": "redis_payload_stale"}
                result = process_job(repo, job, send_message=send_message)
                repo.finish_job(job["id"], result=result)
                return {"ok": True, "processed": True, "job_id": job["id"], "result": result, "queue": "postgres_after_stale_redis"}
            job = {
                "id": db_job_id or redis_payload.get("redis_job_id") or "redis",
                "job_type": redis_payload.get("job_type"),
                "priority": redis_payload.get("priority", 1),
                "source": redis_payload.get("source", "redis"),
                "session_id": redis_payload.get("session_id", "redis"),
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
            try:
                result = process_job(repo, job, send_message=send_message)
                if db_job_id:
                    repo.finish_job(int(db_job_id), result=result)
                return {"ok": True, "processed": True, "job_id": job["id"], "result": result, "queue": "redis"}
            except Exception as exc:
                if db_job_id:
                    repo.finish_job(int(db_job_id), error_message=str(exc))
                # Persist the failure-side finish_job before re-raising: the
                # outer ``transaction()`` rolls back on the propagating
                # exception, which would otherwise discard this savepointed
                # write (the same discard bug ``transaction()`` fixes for the
                # clean path). Commit if the txn is still usable; if it was
                # aborted by the underlying error, rollback so the job is
                # retried instead of wedged.
                try:
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise
        # 07-10 R5-2: fair-pool dispatch. When the configured LLM scheduling
        # mode is ``fair_pool`` and this is a background worker (not a user-
        # only worker, which must still handle interactive jobs serially),
        # claim an entire batch of ``scheduled_market_analysis`` jobs at once
        # and run it through ``process_fair_batch`` -> ``run_fair_batch``.
        # This is the production entry point the directive requires: a real
        # batch-level production path that atomically claims same-batch jobs
        # (directive #1) and feeds them to the fair coordinator's single-
        # attempt adapter (directive #2). Legacy serial mode (or any non-
        # scheduled user job) still falls through to the per-job path below.
        if background and not user_only:
            try:
                sched_mode = (
                    cfg.trading_mode.get("llm", {}).get("scheduling", {}).get("mode", "fair_pool")
                )
            except Exception:
                sched_mode = "fair_pool"
            if sched_mode == "fair_pool":
                batch_jobs = repo.claim_next_batch()
                if batch_jobs:
                    try:
                        result = process_fair_batch(repo, batch_jobs, send_message=send_message)
                        # 07-10 S6 (P1 #6): ``process_fair_batch`` now finishes
                        # EACH symbol's agent_job per-symbol (success/failed
                        # reflecting that symbol's ``analyze_symbol`` outcome).
                        # The prior uniform ``finish_job`` loop here would (a)
                        # double-finish every job and (b) overwrite a failed
                        # symbol's ``status='failed'`` with ``status='success'``
                        # (no error_message) -- the P1 #6 defect. Removed.
                        return {
                            "ok": True, "processed": True,
                            "batch_id": result.get("batch_id"),
                            "result": result, "queue": "fair_pool",
                        }
                    except Exception as exc:
                        LOGGER.exception(
                            "process_fair_batch failed batch_id=%s",
                            (_decode_json(batch_jobs[0]["payload_json"], {}) if batch_jobs else {}).get("batch_id"),
                        )
                        # 07-10 S6 (P1 #6): a whole-batch exception means
                        # ``process_fair_batch`` raised BEFORE or AFTER the
                        # per-symbol loop. Per-symbol jobs already finished inside
                        # ``process_fair_batch`` carry ``status`` in
                        # {success, failed}; only mark the STILL-RUNNING ones
                        # failed here so we never overwrite a symbol's real
                        # per-symbol outcome (e.g. a success finished just before
                        # the batch-completion block raised).
                        for j in batch_jobs:
                            try:
                                _jid = int(j["id"])
                                _cur = repo.conn.execute(
                                    "SELECT status FROM agent_jobs WHERE id=%s",
                                    (_jid,),
                                ).fetchone()
                                _cur_status = _cur["status"] if _cur else None
                                if _cur_status in ("success", "failed"):
                                    continue  # already finished per-symbol
                                repo.finish_job(
                                    _jid,
                                    error_message=str(exc),
                                    claim_token=str(j.get("claim_token") or ""),
                                )
                            except Exception:
                                pass
                        # Persist the still-running->failed cleanup marks before
                        # re-raising (see the redis-path sibling above): without
                        # this commit the outer ``transaction()`` rollback would
                        # discard them and the jobs would be wedged in the
                        # (rolled-back) claim state.
                        try:
                            conn.commit()
                        except Exception:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                        raise
                # No fair-pool batch ready -> fall through to the idle / serial
                # claim_next_job path below (handles alert outbox, shadow
                # verdicts, and any non-scheduled user jobs).
        job = repo.claim_next_job(max_priority=2) if user_only else repo.claim_next_job(background=background)
        if not job:
            if background:
                outbox = process_alert_outbox(repo, send_message, limit=10)
                if outbox.get("processed"):
                    return {"ok": True, "processed": True, "job_id": None, "result": outbox}
                # Run shadow verdict runner periodically when idle in background mode
                try:
                    from plugins.crypto_guard.strategy.shadow_testing import run_shadow_verdict_runner
                    verdict_result = run_shadow_verdict_runner(repo)
                    if verdict_result.get("processed"):
                        LOGGER.info("shadow_verdict_runner processed=%s", verdict_result.get("processed"))
                except Exception:
                    LOGGER.exception("shadow_verdict_runner failed")
            return {"ok": True, "processed": False}
        try:
            result = process_job(repo, job, send_message=send_message)
            repo.finish_job(job["id"], result=result)
            return {"ok": True, "processed": True, "job_id": job["id"], "result": result}
        except Exception as exc:
            LOGGER.exception("process_job failed id=%s type=%s", job.get("id"), job.get("job_type"))
            _send_job_error_to_user(repo, job, exc, send_message)
            repo.finish_job(job["id"], error_message=str(exc))
            # Persist the failure-side finish_job before re-raising (same
            # reason as the redis and fair-batch sibling blocks above).
            try:
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise


def run_loop(*, user_only: bool = False, background: bool = False, sleep_seconds: float = 1.0) -> None:
    while True:
        try:
            run_once(user_only=user_only, background=background)
        except KeyboardInterrupt:
            raise
        except Exception:
            LOGGER.exception("run_loop iteration failed")
            traceback.print_exc()
        time.sleep(sleep_seconds)


def _maybe_send_feishu_result(
    repo: CryptoGuardRepository,
    payload: dict[str, Any],
    result: dict[str, Any],
    send_message: Callable[..., Any] | None = None,
) -> None:
    if not send_message or not payload.get("receive_id"):
        return
    message_id = str(payload.get("message_id") or "").strip()
    if message_id:
        lock_name = f"feishu_result_sent:{message_id}"
        if not repo.acquire_lock(lock_name, "feishu_result_sender", 24 * 60 * 60):
            LOGGER.info("skip duplicate feishu result send message_id=%s", message_id)
            return
    receive_id = payload["receive_id"]
    receive_id_type = payload.get("receive_id_type", "open_id")
    if result.get("card_json"):
        sent_result = _send_interactive_alert(
            repo,
            send_message,
            receive_id,
            receive_id_type,
            result["card_json"],
            alert_type="ad_hoc_analysis",
            symbol=result.get("symbol"),
            priority=1,
        )
        if sent_result.get("silenced"):
            LOGGER.info("ad hoc analysis card silenced receive_id=%s signal_id=%s", receive_id, result.get("signal_id"))
        elif not sent_result.get("sent"):
            LOGGER.warning(
                "send interactive card failed or queued for retry receive_id=%s signal_id=%s alert_id=%s error=%s",
                receive_id,
                result.get("signal_id"),
                sent_result.get("alert_id"),
                sent_result.get("error"),
            )
    elif result.get("decision"):
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=render_text(result["decision"], signal_id=result.get("signal_id")), alert_type="ad_hoc_analysis_text", symbol=(result.get("decision") or {}).get("symbol"), priority=1)
    elif result.get("text"):
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=result["text"], alert_type="user_command_result", priority=1)
    elif isinstance(result.get("symbols"), list):
        rows = result.get("symbols", [])
        text = _render_symbol_list(rows)
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=text, alert_type="symbol_list", priority=1)
    else:
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=f"**CryptoGuard 返回结果**\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```", alert_type="user_command_result", priority=1)


def _send_job_error_to_user(repo: CryptoGuardRepository, job: dict[str, Any], exc: Exception, send_message: Callable[..., Any] | None) -> None:
    if not send_message:
        return
    try:
        payload = _decode_json(job.get("payload_json"), {})
        receive_id = payload.get("receive_id")
        if not receive_id:
            return
        text = (
            "CryptoGuard 处理这条消息时遇到异常，已写入日志和 agent_jobs.error_message。\n\n"
            f"任务：{job.get('job_type')} #{job.get('id')}\n"
            f"错误：{exc}\n\n"
            "如果是行情接口网络错误，可以稍后重试，或检查代理/网络后再发送分析请求。"
        )
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=payload.get("receive_id_type", "open_id"), text=text, alert_type="job_error", priority=1)
    except Exception:
        LOGGER.exception("failed to send job error to user id=%s", job.get("id"))


def _send_interactive_alert(
    repo: CryptoGuardRepository,
    send_message: Callable[..., Any] | None,
    receive_id: str,
    receive_id_type: str,
    content: str,
    *,
    alert_type: str,
    symbol: str | None = None,
    priority: int = 5,
) -> dict[str, Any]:
    quiet_cfg = ((load_config().trading_mode.get("feishu") or {}).get("quiet_period") or {})
    quiet_minutes = int(quiet_cfg.get("normal_duplicate_alert_minutes", 5))
    never_silence = set(quiet_cfg.get("never_silence") or DEFAULT_NEVER_SILENCE)
    redis = RedisAdapter() if should_use_redis_for_path(None) else None
    redis_quiet_symbol = symbol or "-"
    if alert_type not in never_silence and redis and redis.is_quiet(redis_quiet_symbol, alert_type):
        return {"ok": True, "sent": False, "silenced": True, "source": "redis_quiet"}
    if repo.should_silence_alert(alert_type=alert_type, symbol=symbol, quiet_minutes=quiet_minutes, never_silence=never_silence):
        return {"ok": True, "sent": False, "silenced": True}
    if alert_type not in never_silence:
        lock_name = f"alert_dedupe:{symbol or '-'}:{alert_type}"
        redis_locked = bool(redis and redis.acquire_lock(lock_name, max(quiet_minutes * 60, 1), owner="interactive_alert"))
        if not redis_locked and not repo.acquire_lock(lock_name, "interactive_alert", max(quiet_minutes * 60, 1)):
            return {"ok": True, "sent": False, "silenced": True}
        if redis:
            redis.set_quiet(redis_quiet_symbol, alert_type, max(quiet_minutes * 60, 1))
    alert_id = repo.enqueue_alert(
        alert_type=alert_type,
        symbol=symbol,
        priority=priority,
        payload={"receive_id": receive_id, "receive_id_type": receive_id_type, "msg_type": "interactive", "content": content},
        dedupe_key=f"{symbol or '-'}:{alert_type}",
    )
    if not send_message:
        return {"ok": True, "sent": False, "queued": True, "alert_id": alert_id}
    try:
        sent = send_message(receive_id, content, msg_type="interactive", receive_id_type=receive_id_type)
        if sent:
            repo.mark_alert_sent(alert_id)
            return {"ok": True, "sent": True, "alert_id": alert_id}
        raise RuntimeError("send_message returned falsy")
    except Exception as exc:
        max_attempts = int((load_config().trading_mode.get("alerts") or {}).get("retry_max_attempts", 3))
        repo.mark_alert_failed(alert_id, str(exc), max_attempts=max_attempts)
        return {"ok": True, "sent": False, "alert_id": alert_id, "error": str(exc)}


def _render_symbol_list(rows: list[dict[str, Any]]) -> str:
    lines = ["**当前监控品种**", ""]
    if not rows:
        lines.append("- 暂无监控品种")
        return "\n".join(lines)
    for r in rows:
        enabled = "启用" if r.get("enabled") else "暂停"
        category = r.get("category") or "-"
        source = r.get("source") or "-"
        timeframes = r.get("default_timeframes") or "[]"
        lines.append(f"- **{r['symbol']}**：{enabled}，{category}，source={source}，周期={timeframes}")
    return "\n".join(lines)


def _button_result_text(action: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"操作失败：{result.get('error', '未知错误')}"
    if action == "create_paper_order":
        return "已加入模拟盘。" if result.get("created") else "这条信号已经加入过模拟盘，不会重复创建订单。"
    if action == "create_opportunity_watch":
        return "已加入机会监控。"
    if action == "add_to_watchlist":
        return "已加入长期产品池。"
    if action == "approve_evolution":
        return "已批准候选策略升级。"
    if action == "reject_evolution":
        return "已拒绝候选策略。"
    return "已忽略。"


def _ensure_ga_decision_for_watch_signal(repo: CryptoGuardRepository, signal: dict[str, Any], watch: dict[str, Any]) -> int:
    legacy = {
        "symbol": signal["symbol"],
        "decision": signal.get("decision") or "wait_for_pullback",
        "signal_grade": signal.get("signal_grade") or "B",
        "confidence": float(signal.get("confidence") or 0),
        "summary": signal.get("ga_reason") or "兼容旧 signal 创建的 GA decision。",
        "market_bias": signal.get("direction") or "neutral",
        "trend_stage": signal.get("trend_stage") or "unknown",
        "has_trade_plan": False,
        "trade_plan": None,
        "opportunity_watch": watch,
        "risk_check": {"ok": False, "reasons": ["未提供完整 trade_plan，仅允许机会监控"]},
        "evidence": [],
        "counter_evidence": [],
        "risk_notes": _safe_json_list(signal.get("risk_notes")),
    }
    actions = build_feishu_actions(legacy, legacy["risk_check"])
    ga_decision = controller_decision_from_legacy(
        legacy=legacy,
        decision_type="legacy_signal_compat",
        analysis_time=utc_ms(),
        skill_result_refs={},
        feishu_actions=actions,
        snapshot_id=signal.get("market_snapshot_id"),
        analysis_state_id=None,
    )
    ga_decision_id = repo.create_ga_decision(ga_decision)
    legacy["ga_decision_id"] = ga_decision_id
    # 07-16 cutover: UPDATE self-wraps in a transaction (writes self-commit).
    with repo.conn.transaction():
        repo.conn.execute(
            "UPDATE signals SET ga_decision_id=%s, ga_decision_json=%s WHERE id=%s",
            (ga_decision_id, json.dumps(legacy, ensure_ascii=False), int(signal["id"])),
        )
    return int(ga_decision_id)


def _safe_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except Exception:
        return [raw]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-only", action="store_true")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(run_once(user_only=args.user_only, background=args.background), ensure_ascii=False, indent=2))
    else:
        run_loop(user_only=args.user_only, background=args.background)


if __name__ == "__main__":
    main()
