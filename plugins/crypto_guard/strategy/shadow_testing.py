from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.shadow_testing")


def record_shadow_evaluation(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    strategy_name: str,
    strategy_version: str,
    score: float,
    decision: str,
    evidence: dict[str, Any] | None = None,
    counter_evidence: dict[str, Any] | None = None,
    pnl_r: float | None = None,
    snapshot_id: int | None = None,
) -> dict[str, Any]:
    """候选策略只做影子记录，不推送飞书、不创建模拟盘。"""

    repo.conn.execute(
        """
        INSERT INTO strategy_evaluations(
            symbol, timeframe, analysis_time, strategy_name, strategy_version, score,
            decision, evidence_json, counter_evidence_json, is_shadow, snapshot_id, pnl_r
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            symbol,
            timeframe,
            int(analysis_time_utc),
            strategy_name,
            strategy_version,
            float(score),
            decision,
            "{}" if evidence is None else __import__("json").dumps(evidence, ensure_ascii=False),
            "{}" if counter_evidence is None else __import__("json").dumps(counter_evidence, ensure_ascii=False),
            snapshot_id,
            pnl_r,
        ),
    )
    evaluation_id = int(repo.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    return {"ok": True, "evaluation_id": evaluation_id, "is_shadow": True}


def run_shadow_test(
    repo: CryptoGuardRepository,
    *,
    strategy_name: str,
    candidate_version: str,
    min_samples: int = 30,
    allow_auto_promote: bool = False,
) -> dict[str, Any]:
    # Check if candidate passed backtest gate - if so, use reduced sample requirement
    cfg = load_config().trading_mode
    online_shadow_cfg = cfg.get("evolution", {}).get("online_shadow", {})
    min_samples_after_backtest = online_shadow_cfg.get("min_samples_after_backtest", 5)
    min_samples_without_backtest = online_shadow_cfg.get("min_samples_without_backtest", 30)

    backtest_status = check_candidate_backtest_status(repo, strategy_name, candidate_version)
    backtest = backtest_status.get("backtest", {})
    backtest_passed = backtest_status.get("has_backtest") and backtest_status.get("passed")
    backtest_skipped = backtest.get("skipped", False)
    gate_disabled = backtest.get("gate_disabled", False)

    if gate_disabled:
        # Gate disabled - use reduced samples (system configured to skip gate)
        effective_min_samples = min_samples_after_backtest
    elif backtest_passed and not backtest_skipped:
        # Candidate truly passed backtest gate - use reduced online sample requirement
        effective_min_samples = min_samples_after_backtest
    else:
        # No backtest, skipped (no scoring changes), or failed - use conservative sample requirement
        effective_min_samples = min_samples_without_backtest

    active = repo.active_strategy_version(strategy_name)
    active_version = active.get("version") if active else None
    active_rows = _strategy_eval_rows(repo, strategy_name, active_version, is_shadow=False)
    candidate_rows = _strategy_eval_rows(repo, strategy_name, candidate_version, is_shadow=True)
    sample_count = min(len(active_rows), len(candidate_rows)) if active_rows else len(candidate_rows)
    active_stats = _stats(active_rows)
    candidate_stats = _stats(candidate_rows)

    # P0: Block promotion when candidate has only pseudo-R data (no real pnl_r)
    candidate_data_source = candidate_stats.get("data_source", "real_pnl")
    pseudo_only = candidate_data_source == "pseudo_r_from_score"
    shadow_quality_alert = False

    if sample_count < effective_min_samples:
        recommendation = "insufficient_samples"
        status = "running"
    elif pseudo_only:
        # Cannot promote based on pseudo-R data alone — block verdict
        recommendation = "data_quality_insufficient"
        status = "running"
        shadow_quality_alert = sample_count >= 20
        if shadow_quality_alert:
            LOGGER.warning(
                "shadow_quality_alert: %s/%s has %d samples but all pseudo_r_from_score — real pnl_r required for promotion",
                strategy_name, candidate_version, sample_count,
            )
    elif candidate_stats["avg_r"] > active_stats["avg_r"] and candidate_stats["win_rate"] >= active_stats["win_rate"] and candidate_stats["drawdown"] >= active_stats["drawdown"]:
        recommendation = "candidate_can_be_promoted_with_manual_confirmation"
        status = "passed"
    else:
        recommendation = "reject_candidate"
        status = "rejected"

    fallback_result = {
        "ok": True,
        "strategy_name": strategy_name,
        "candidate_version": candidate_version,
        "active_version": active_version,
        "sample_count": sample_count,
        "min_samples": effective_min_samples,
        "backtest_passed": backtest_status.get("passed", False),
        "active_stats": active_stats,
        "candidate_stats": candidate_stats,
        "recommendation": recommendation,
        "status": status,
        "data_quality": candidate_data_source,
        "shadow_quality_alert": shadow_quality_alert,
        "auto_promoted": False,
        "promotion_allowed": bool(allow_auto_promote),
    }
    result = run_agent_json_task(
        task_name="shadow_test_strategy_verdict",
        payload={
            "strategy_name": strategy_name,
            "candidate_version": candidate_version,
            "active_version": active_version,
            "sample_count": sample_count,
            "min_samples": effective_min_samples,
            "backtest_passed": backtest_status.get("passed", False),
            "active_stats": active_stats,
            "candidate_stats": candidate_stats,
            "fallback_verdict": fallback_result,
        },
        fallback=fallback_result,
        instructions=[
            "复核影子测试结果，判断候选策略是否样本不足、拒绝、或可进入人工确认升级。",
            "必须保守处理过拟合风险；不能绕过人工确认或配置门禁。",
        ],
    )

    # P0: Hard gate — enforce pseudo-only block AFTER LLM verdict.
    # LLM may return "candidate_can_be_promoted..." but we must not allow
    # promotion based on pseudo-R data. Only apply when samples are sufficient
    # (insufficient_samples takes priority — we haven't accumulated enough data yet).
    if pseudo_only and sample_count >= effective_min_samples:
        result["recommendation"] = "data_quality_insufficient"
        result["status"] = "running"
        result["data_quality"] = candidate_data_source
        result["shadow_quality_alert"] = shadow_quality_alert
        result["hard_gate_applied"] = "pseudo_only_block"
        LOGGER.info(
            "hard_gate: forced data_quality_insufficient for %s/%s (pseudo_only, LLM returned %s)",
            strategy_name, candidate_version, result.get("recommendation"),
        )

    result_id = repo.save_shadow_test_result(result)
    result["shadow_test_result_id"] = result_id
    return result


def run_shadow_verdict_runner(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Scan all shadow_testing candidates and promote/reject based on verdict."""
    from datetime import datetime, timezone, timedelta

    # Throttle: only run every 30 minutes
    last_run = repo.conn.execute(
        "SELECT MAX(created_at) as last_at FROM shadow_test_results WHERE verdict_runner_run=1"
    ).fetchone()
    if last_run and last_run["last_at"]:
        try:
            last_time = datetime.fromisoformat(last_run["last_at"])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_time < timedelta(minutes=30):
                return {"ok": True, "processed": 0, "reason": "throttled"}
        except Exception:
            pass

    # Find all candidates in shadow_testing status
    candidates = repo.conn.execute(
        """
        SELECT DISTINCT sv.strategy_name, sv.version
        FROM strategy_versions sv
        WHERE sv.status = 'shadow_testing'
        """
    ).fetchall()

    results = []
    for row in candidates:
        strategy_name = row["strategy_name"]
        candidate_version = row["version"]

        # Check if sample count increased since last verdict
        last_verdict = repo.conn.execute(
            "SELECT sample_count FROM shadow_test_results WHERE strategy_name=? AND candidate_version=? ORDER BY id DESC LIMIT 1",
            (strategy_name, candidate_version),
        ).fetchone()
        current_sample_count = len(_strategy_eval_rows(repo, strategy_name, candidate_version, is_shadow=True))
        if last_verdict and last_verdict["sample_count"] and current_sample_count <= int(last_verdict["sample_count"]):
            # No new samples, skip
            results.append({"strategy_name": strategy_name, "version": candidate_version, "verdict": "skipped_no_new_samples"})
            continue

        shadow = run_shadow_test(
            repo,
            strategy_name=strategy_name,
            candidate_version=candidate_version,
        )

        verdict = shadow.get("recommendation")
        status = shadow.get("status")

        if verdict == "candidate_can_be_promoted_with_manual_confirmation":
            # Promote to review_required
            repo.conn.execute(
                "UPDATE strategy_versions SET status='review_required' WHERE strategy_name=? AND version=?",
                (strategy_name, candidate_version),
            )
            repo.conn.execute(
                "UPDATE strategy_patches SET status='review_required' WHERE strategy_name=? AND candidate_version=?",
                (strategy_name, candidate_version),
            )
            repo.conn.execute(
                "UPDATE evolution_triggers SET status='review_required' WHERE id IN (SELECT trigger_id FROM strategy_patches WHERE candidate_version=? AND trigger_id IS NOT NULL)",
                (candidate_version,),
            )
            # Send notification for review_required promotion
            try:
                repo.enqueue_job(
                    "evolution_trigger_alert",
                    4,
                    "paper_worker",
                    f"system:verdict:review_required:{candidate_version}",
                    {
                        "trigger_type": "verdict_promotion",
                        "trigger_id": None,
                        "patch_id": None,
                        "reason": f"影子测试通过，候选 {candidate_version} 进入人工 review（{shadow.get('sample_count', 0)} 个样本）",
                        "candidate_version": candidate_version,
                        "sample_count": shadow.get("sample_count", 0),
                        "verdict": verdict,
                    },
                )
            except Exception:
                pass
            results.append({"strategy_name": strategy_name, "version": candidate_version, "verdict": "promoted_to_review", "shadow": shadow})

        elif verdict == "reject_candidate":
            # Reject
            repo.conn.execute(
                "UPDATE strategy_versions SET status='rejected', change_reason=? WHERE strategy_name=? AND version=?",
                ("shadow_test_verdict_rejected", strategy_name, candidate_version),
            )
            repo.conn.execute(
                "UPDATE strategy_patches SET status='rejected' WHERE strategy_name=? AND candidate_version=?",
                (strategy_name, candidate_version),
            )
            repo.conn.execute(
                "UPDATE evolution_triggers SET status='rejected' WHERE id IN (SELECT trigger_id FROM strategy_patches WHERE candidate_version=? AND trigger_id IS NOT NULL)",
                (candidate_version,),
            )

            # P1-A: Shadow failure reflection
            _write_failure_reflection(repo, strategy_name, candidate_version, shadow)

            results.append({"strategy_name": strategy_name, "version": candidate_version, "verdict": "rejected", "shadow": shadow})

        else:
            # insufficient_samples - keep running
            results.append({"strategy_name": strategy_name, "version": candidate_version, "verdict": "still_running", "shadow": shadow})

    # Mark this as a verdict runner run for throttling
    if results:
        try:
            repo.conn.execute(
                "INSERT INTO shadow_test_results(strategy_name, candidate_version, sample_count, recommendation, status, verdict_runner_run) VALUES (?, ?, ?, ?, ?, 1)",
                ("verdict_runner", "system", 0, "verdict_run", "completed"),
            )
        except Exception:
            pass
    repo.conn.commit()
    return {"ok": True, "processed": len(results), "results": results}


def run_backtest_gate(
    repo: CryptoGuardRepository,
    *,
    strategy_name: str,
    candidate_version: str,
    symbols: list[str] | None = None,
    lookback_days: int | None = None,
    min_simulated_trades: int | None = None,
    min_decision_samples: int | None = None,
    min_avg_r_improvement: float | None = None,
    max_win_rate_degradation: float | None = None,
    max_drawdown_increase: float | None = None,
    candidate_score_adjustment: float | None = None,
) -> dict[str, Any]:
    """Run historical backtest as fast admission gate before online shadow testing.

    Uses paired comparison: same historical data, same timepoints,
    comparing active vs candidate strategy performance.

    Returns gate result. Caller is responsible for saving to strategy_patches.backtest_result_json.

    Note: If candidate patch doesn't contain scoring-related changes,
    the gate is skipped (returns skipped_or_needs_online_shadow).
    """
    from plugins.crypto_guard.backtest.historical_replay import run_paired_backtest

    cfg = load_config().trading_mode
    gate_cfg = cfg.get("evolution", {}).get("backtest_gate", {})
    if not gate_cfg.get("enabled", True):
        return {"ok": True, "passed": True, "reason": "backtest_gate_disabled", "gate_disabled": True}

    # Read config with defaults
    lookback_days = lookback_days or gate_cfg.get("lookback_days", 60)
    min_simulated_trades = min_simulated_trades or gate_cfg.get("min_simulated_trades", 30)
    min_decision_samples = min_decision_samples or gate_cfg.get("min_decision_samples", 80)
    min_avg_r_improvement = min_avg_r_improvement or gate_cfg.get("min_avg_r_improvement", 0.05)
    max_win_rate_degradation = max_win_rate_degradation or gate_cfg.get("max_win_rate_degradation", 0.10)
    max_drawdown_increase = max_drawdown_increase or gate_cfg.get("max_drawdown_increase", 0.20)

    # Resolve active version
    active = repo.active_strategy_version(strategy_name)
    if not active:
        return {"ok": False, "passed": False, "reason": "no_active_version", "error": "no_active_version"}
    active_version = active.get("version")

    # Check if candidate patch has scoring-related changes
    # If not, skip backtest gate (it won't measure anything meaningful)
    candidate_patch = _get_candidate_patch(repo, strategy_name, candidate_version)
    if candidate_score_adjustment is None:
        # Try to extract from patch
        candidate_score_adjustment = _extract_score_adjustment(candidate_patch)

    if candidate_score_adjustment == 0.0 and not _has_scoring_changes(candidate_patch):
        return {
            "ok": True,
            "passed": False,
            "reason": "skipped_or_needs_online_shadow",
            "skipped": True,
            "strategy_name": strategy_name,
            "candidate_version": candidate_version,
            "note": "候选策略未包含评分相关改动，跳过回测门禁，直接进入在线影子测试",
        }

    # Resolve symbols
    if not symbols:
        symbols_cfg = load_config().symbols
        symbols = list((symbols_cfg.get("default_universe", {}).get("symbols", []))[:3])
    if not symbols:
        return {"ok": False, "passed": False, "reason": "no_symbols", "error": "no_symbols_configured"}

    # Time window
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp() * 1000)

    # Run paired backtest on each symbol (1h + 15m)
    all_active_r_values: list[float] = []
    all_candidate_r_values: list[float] = []
    all_active_outcomes: list[str] = []
    all_candidate_outcomes: list[str] = []
    active_simulated_trades = 0
    candidate_simulated_trades = 0
    active_decision_samples = 0
    candidate_decision_samples = 0
    no_lookahead_ok = True
    symbol_results: list[dict[str, Any]] = []

    for symbol in symbols:
        for interval in ("1h", "15m"):
            result = run_paired_backtest(
                repo,
                symbol=symbol,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
                candidate_score_adjustment=candidate_score_adjustment,
            )
            if not result.get("ok"):
                no_lookahead_ok = False
                continue

            active_stats = result.get("active_stats", {})
            candidate_stats = result.get("candidate_stats", {})

            # Track separate sample counts
            active_simulated_trades += active_stats.get("simulated_trades", 0)
            candidate_simulated_trades += candidate_stats.get("simulated_trades", 0)
            active_decision_samples += active_stats.get("signal_count", 0)
            candidate_decision_samples += candidate_stats.get("signal_count", 0)

            # Collect real R sequences (not avg_r replication)
            all_active_r_values.extend(result.get("active_r_values", []))
            all_candidate_r_values.extend(result.get("candidate_r_values", []))
            all_active_outcomes.extend(result.get("active_trade_outcomes", []))
            all_candidate_outcomes.extend(result.get("candidate_trade_outcomes", []))

            symbol_results.append({
                "symbol": symbol,
                "interval": interval,
                "active_stats": active_stats,
                "candidate_stats": candidate_stats,
                "paired_count": result.get("paired_count", 0),
                "active_r_values": result.get("active_r_values", []),
                "candidate_r_values": result.get("candidate_r_values", []),
            })

    # Aggregate stats using real R sequences
    if not symbol_results:
        return {"ok": False, "passed": False, "reason": "no_valid_backtest_results", "error": "all_backtests_failed"}

    active_stats_agg = _aggregate_stats(all_active_r_values, all_active_outcomes, active_decision_samples, active_simulated_trades)
    candidate_stats_agg = _aggregate_stats(all_candidate_r_values, all_candidate_outcomes, candidate_decision_samples, candidate_simulated_trades)

    # Gate checks
    reasons: list[str] = []
    passed = True

    # Check minimum samples for active (need baseline)
    if active_simulated_trades < min_simulated_trades and active_decision_samples < min_decision_samples:
        passed = False
        reasons.append(f"active 样本不足：模拟交易 {active_simulated_trades} < {min_simulated_trades}，决策样本 {active_decision_samples} < {min_decision_samples}")

    # Check minimum samples for candidate (must have own data)
    min_r_count = gate_cfg.get("min_r_count_for_performance_gate", 5)
    candidate_has_sufficient_data = (
        candidate_simulated_trades >= min_simulated_trades
        or (candidate_decision_samples >= min_decision_samples and len(all_candidate_r_values) >= min_r_count)
    )
    if not candidate_has_sufficient_data:
        passed = False
        reasons.append(f"candidate 样本不足：模拟交易 {candidate_simulated_trades}，R 值数量 {len(all_candidate_r_values)}")

    # Check no-lookahead
    if not no_lookahead_ok:
        passed = False
        reasons.append("no_lookahead 验证失败")

    # Check avg_r improvement
    delta_avg_r = candidate_stats_agg["avg_r"] - active_stats_agg["avg_r"]
    if delta_avg_r < min_avg_r_improvement:
        passed = False
        reasons.append(f"avg_r 改进不足：{delta_avg_r:.4f} < {min_avg_r_improvement}")

    # Check win_rate degradation
    delta_win_rate = candidate_stats_agg["win_rate"] - active_stats_agg["win_rate"]
    if delta_win_rate < -max_win_rate_degradation:
        passed = False
        reasons.append(f"胜率降级过大：{delta_win_rate:.4f} < -{max_win_rate_degradation}")

    # Check drawdown increase (drawdown is negative, so more negative = worse)
    active_dd = active_stats_agg["drawdown"]
    candidate_dd = candidate_stats_agg["drawdown"]
    # Drawdown increase = candidate_dd - active_dd (should be < max_drawdown_increase)
    # If active_dd = -0.10, candidate_dd = -0.15, then increase = -0.05 (worse)
    drawdown_increase = candidate_dd - active_dd
    if drawdown_increase < -max_drawdown_increase:
        passed = False
        reasons.append(f"回撤恶化过大：{drawdown_increase:.4f} < -{max_drawdown_increase}")

    result = {
        "ok": True,
        "passed": passed,
        "reason": "; ".join(reasons) if reasons else "backtest_gate_passed",
        "strategy_name": strategy_name,
        "candidate_version": candidate_version,
        "active_version": active_version,
        "lookback_days": lookback_days,
        "symbols_tested": symbols,
        "active_simulated_trades": active_simulated_trades,
        "candidate_simulated_trades": candidate_simulated_trades,
        "active_decision_samples": active_decision_samples,
        "candidate_decision_samples": candidate_decision_samples,
        "active_stats_aggregate": active_stats_agg,
        "candidate_stats_aggregate": candidate_stats_agg,
        "delta_avg_r": delta_avg_r,
        "delta_win_rate": delta_win_rate,
        "drawdown_increase": drawdown_increase,
        "no_lookahead_ok": no_lookahead_ok,
        "symbol_results": symbol_results,
        "gate_checks": {
            "min_simulated_trades": min_simulated_trades,
            "min_decision_samples": min_decision_samples,
            "min_avg_r_improvement": min_avg_r_improvement,
            "max_win_rate_degradation": max_win_rate_degradation,
            "max_drawdown_increase": max_drawdown_increase,
            "min_r_count_for_performance_gate": min_r_count,
        },
    }

    return result


def _calculate_drawdown(r_values: list[float]) -> float:
    """Calculate max drawdown from a series of R values."""
    if not r_values:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        dd = equity - peak
        max_dd = min(max_dd, dd)
    return max_dd


def _aggregate_stats(
    r_values: list[float],
    outcomes: list[str],
    sample_count: int,
    simulated_trades: int,
) -> dict[str, Any]:
    """Aggregate stats from real R sequences and trade outcomes."""
    wins = outcomes.count("win")
    losses = outcomes.count("loss")
    total_trades = len(outcomes)

    return {
        "sample_count": sample_count,
        "simulated_trades": simulated_trades,
        "avg_r": sum(r_values) / len(r_values) if r_values else 0.0,
        "win_rate": wins / total_trades if total_trades > 0 else 0.0,
        "drawdown": _calculate_drawdown(r_values),
        "r_count": len(r_values),
        "trade_count": total_trades,
        "wins": wins,
        "losses": losses,
    }


def _get_candidate_patch(repo: CryptoGuardRepository, strategy_name: str, candidate_version: str) -> dict[str, Any]:
    """Get candidate patch from strategy_patches table."""
    row = repo.conn.execute(
        "SELECT patch_json FROM strategy_patches WHERE strategy_name=? AND candidate_version=? ORDER BY id DESC LIMIT 1",
        (strategy_name, candidate_version),
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["patch_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_score_adjustment(patch: dict[str, Any]) -> float:
    """Extract score adjustment from candidate patch if present.

    Supports both:
    - score_adjustment: float (single value)
    - score_adjustments: dict (multiple adjustments, summed)
    """
    patch_data = patch.get("patch", patch)

    # Single value
    if "score_adjustment" in patch_data:
        return float(patch_data["score_adjustment"])

    # Multiple adjustments (sum values)
    adjustments = patch_data.get("score_adjustments")
    if isinstance(adjustments, dict) and adjustments:
        return sum(float(v) for v in adjustments.values())

    return 0.0


def _has_scoring_changes(patch: dict[str, Any]) -> bool:
    """Check if patch contains scoring-related changes.

    Currently supported:
    - score_adjustment: float
    - score_adjustments: dict of adjustments
    """
    patch_data = patch.get("patch", patch)
    scoring_keys = {"score_adjustment", "score_adjustments"}
    return bool(scoring_keys & set(patch_data.keys()))


def check_candidate_backtest_status(repo: CryptoGuardRepository, strategy_name: str, candidate_version: str) -> dict[str, Any]:
    """Check if a candidate has passed the backtest gate."""
    row = repo.conn.execute(
        "SELECT backtest_result_json FROM strategy_patches WHERE strategy_name=? AND candidate_version=? AND backtest_result_json IS NOT NULL",
        (strategy_name, candidate_version),
    ).fetchone()
    if not row:
        return {"has_backtest": False}
    try:
        backtest = json.loads(row["backtest_result_json"])
        return {"has_backtest": True, "passed": backtest.get("passed", False), "backtest": backtest}
    except (json.JSONDecodeError, TypeError):
        return {"has_backtest": False}


def promote_shadow_candidate(
    repo: CryptoGuardRepository,
    *,
    strategy_name: str,
    candidate_version: str,
    confirm: bool = False,
    config_allow_auto: bool = False,
    change_reason: str = "",
) -> dict[str, Any]:
    if not (confirm or config_allow_auto):
        return {"ok": False, "error": "promotion requires manual confirmation or config_allow_auto"}
    if not change_reason:
        return {"ok": False, "error": "change_reason required"}
    sample_count = len(_strategy_eval_rows(repo, strategy_name, candidate_version, is_shadow=True))
    if sample_count < 3:
        return {"ok": False, "error": "candidate requires at least 3 observation signals before promotion can be considered", "sample_count": sample_count}
    candidate = repo.get_strategy_version(strategy_name, candidate_version)
    if not candidate or candidate.get("status") not in {"candidate", "shadow_testing", "review_required"}:
        return {"ok": False, "error": "candidate version not found or invalid status"}
    repo.conn.execute(
        "UPDATE strategy_versions SET status='deprecated' WHERE strategy_name=? AND status='active'",
        (strategy_name,),
    )
    repo.conn.execute(
        "UPDATE strategy_versions SET status='active', change_reason=? WHERE strategy_name=? AND version=?",
        (change_reason, strategy_name, candidate_version),
    )
    return {"ok": True, "strategy_name": strategy_name, "active_version": candidate_version}


def _strategy_eval_rows(repo: CryptoGuardRepository, strategy_name: str, version: str | None, *, is_shadow: bool) -> list[dict[str, Any]]:
    if not version:
        return []
    rows = repo.conn.execute(
        """
        SELECT * FROM strategy_evaluations
        WHERE strategy_name=? AND strategy_version=? AND is_shadow=?
        ORDER BY analysis_time ASC, id ASC
        """,
        (strategy_name, version, 1 if is_shadow else 0),
    ).fetchall()
    return [dict(r) for r in rows]


def _write_failure_reflection(
    repo: CryptoGuardRepository,
    strategy_name: str,
    candidate_version: str,
    shadow_result: dict[str, Any],
) -> None:
    """Write structured feedback when shadow candidate fails.

    Implements P1-A: Shadow failure reflection.
    """
    from plugins.crypto_guard.review.loss_classifier import classify_trade

    candidate_stats = shadow_result.get("candidate_stats") or {}
    avg_r = candidate_stats.get("avg_r", 0)
    win_rate = candidate_stats.get("win_rate", 0)
    drawdown = candidate_stats.get("drawdown", 0)
    sample_count = shadow_result.get("sample_count", 0)

    # Determine failure pattern
    if avg_r < 0 and win_rate < 0.45:
        pattern_type = "low_win_rate_negative_r"
    elif avg_r < 0:
        pattern_type = "negative_avg_r"
    elif win_rate < 0.45:
        pattern_type = "low_win_rate"
    elif drawdown < -0.20:
        pattern_type = "high_drawdown"
    else:
        pattern_type = "underperformed_active"

    # Get affected symbols from strategy evaluations
    rows = repo.conn.execute(
        "SELECT DISTINCT symbol FROM strategy_evaluations WHERE strategy_name=? AND strategy_version=? AND is_shadow=1",
        (strategy_name, candidate_version),
    ).fetchall()
    affected_symbols = [r["symbol"] for r in rows if r.get("symbol")]

    # Get affected sides from trade plans
    sides = repo.conn.execute(
        """
        SELECT DISTINCT json_extract(ga.decision_json, '$.trade_plan.side') as side
        FROM strategy_evaluations se
        JOIN ga_decisions ga ON se.ga_decision_id = ga.id
        WHERE se.strategy_name=? AND se.strategy_version=? AND se.is_shadow=1
        """,
        (strategy_name, candidate_version),
    ).fetchall()
    affected_sides = [r["side"] for r in sides if r.get("side")]

    # Build failure report
    finding = (
        f"影子测试失败：{strategy_name}/{candidate_version} "
        f"avg_r={avg_r:.3f}, win_rate={win_rate:.1%}, drawdown={drawdown:.1%}, "
        f"samples={sample_count}"
    )

    suggested_adjustment = {
        "candidate_version": candidate_version,
        "avg_r": avg_r,
        "win_rate": win_rate,
        "drawdown": drawdown,
        "sample_count": sample_count,
        "failure_pattern": pattern_type,
        "symbols": affected_symbols,
        "sides": affected_sides,
    }

    # Write feedback to primary skill (trend_stage is often responsible for shadow failures)
    repo.save_skill_feedback_memory(
        skill_name="trend_stage",
        feedback_type="shadow_failure",
        source_type="shadow_test",
        finding=finding,
        pattern_type=pattern_type,
        affected_symbols=affected_symbols,
        affected_sides=affected_sides,
        suggested_adjustment=suggested_adjustment,
    )

    # Also write to other relevant skills
    for skill in ("momentum", "smc_orderflow"):
        repo.save_skill_feedback_memory(
            skill_name=skill,
            feedback_type="shadow_failure",
            source_type="shadow_test",
            finding=finding,
            pattern_type=pattern_type,
            affected_symbols=affected_symbols,
            affected_sides=affected_sides,
            suggested_adjustment=suggested_adjustment,
        )

    LOGGER.info(
        "shadow_failure_reflection: %s/%s pattern=%s avg_r=%.3f win_rate=%.1f%%",
        strategy_name, candidate_version, pattern_type, avg_r, win_rate * 100,
    )

    # Check rate-limiting for draft patch generation
    _maybe_generate_draft_patch(repo, strategy_name, candidate_version, pattern_type, shadow_result)


def _maybe_generate_draft_patch(
    repo: CryptoGuardRepository,
    strategy_name: str,
    candidate_version: str,
    pattern_type: str,
    shadow_result: dict[str, Any],
) -> None:
    """Generate draft candidate patch if rate-limit allows.

    Max 2 drafts per original trigger, 24h cooldown between attempts.
    """
    from datetime import datetime, timezone, timedelta

    # Find original trigger
    patch = repo.conn.execute(
        "SELECT trigger_id FROM strategy_patches WHERE candidate_version=? AND strategy_name=?",
        (candidate_version, strategy_name),
    ).fetchone()

    if not patch or not patch.get("trigger_id"):
        return

    trigger_id = patch["trigger_id"]

    # Count existing drafts for this trigger
    draft_count = repo.conn.execute(
        "SELECT COUNT(*) as cnt FROM strategy_patches WHERE trigger_id=? AND status='draft'",
        (trigger_id,),
    ).fetchone()

    if draft_count and draft_count["cnt"] >= 2:
        LOGGER.info("shadow_failure_reflection: max drafts reached for trigger %s", trigger_id)
        return

    # Check cooldown (24h since last draft)
    last_draft = repo.conn.execute(
        "SELECT created_at FROM strategy_patches WHERE trigger_id=? AND status='draft' ORDER BY created_at DESC LIMIT 1",
        (trigger_id,),
    ).fetchone()

    if last_draft and last_draft.get("created_at"):
        try:
            last_time = datetime.fromisoformat(last_draft["created_at"])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_time < timedelta(hours=24):
                LOGGER.info("shadow_failure_reflection: cooldown active for trigger %s", trigger_id)
                return
        except Exception:
            pass

    # Generate draft patch with suggested improvements
    candidate_stats = shadow_result.get("candidate_stats") or {}
    draft_patch = {
        "base_version": candidate_version,
        "failure_pattern": pattern_type,
        "suggested_changes": _suggest_changes_for_pattern(pattern_type, candidate_stats),
        "requires_human_approval": True,
    }

    # Create new patch entry with status='draft'
    new_version = f"{candidate_version}.draft.{int(datetime.now(timezone.utc).timestamp())}"
    repo.conn.execute(
        """
        INSERT INTO strategy_patches(strategy_name, candidate_version, patch_json, status, trigger_id, change_reason)
        VALUES (?, ?, ?, 'draft', ?, ?)
        """,
        (
            strategy_name,
            new_version,
            json.dumps(draft_patch, ensure_ascii=False),
            trigger_id,
            f"auto_draft_from_failure_{pattern_type}",
        ),
    )
    repo.save_strategy_version(
        strategy_name=strategy_name,
        version=new_version,
        status="shadow_testing",
        config=draft_patch,
        change_reason=f"auto_draft_from_failure_{pattern_type}",
    )
    repo.conn.commit()

    LOGGER.info(
        "shadow_failure_reflection: generated draft patch %s for trigger %s",
        new_version, trigger_id,
    )


def _suggest_changes_for_pattern(pattern_type: str, stats: dict[str, Any]) -> dict[str, Any]:
    """Suggest parameter changes based on failure pattern."""
    suggestions: dict[str, Any] = {"adjustments": [], "notes": []}

    if pattern_type == "low_win_rate_negative_r":
        suggestions["adjustments"].append({"param": "min_confidence", "delta": 0.05, "reason": "低胜率需要更高置信度门槛"})
        suggestions["adjustments"].append({"param": "require_structure_momentum_alignment", "value": True, "reason": "强制结构动能共振"})
        suggestions["notes"].append("考虑增加订单流确认要求")

    elif pattern_type == "negative_avg_r":
        suggestions["adjustments"].append({"param": "min_rr", "delta": 0.5, "reason": "负平均R需要更高盈亏比"})
        suggestions["adjustments"].append({"param": "min_sl_distance_pct", "delta": 0.2, "reason": "增加止损距离避免噪音打掉"})
        suggestions["notes"].append("检查入场时机是否过早")

    elif pattern_type == "low_win_rate":
        suggestions["adjustments"].append({"param": "min_confidence", "delta": 0.03, "reason": "低胜率需要更严格入场条件"})
        suggestions["notes"].append("考虑增加缠论买点确认")

    elif pattern_type == "high_drawdown":
        suggestions["adjustments"].append({"param": "risk_percent", "delta": -0.1, "reason": "高回撤降低单笔风险"})
        suggestions["adjustments"].append({"param": "max_consecutive_losses", "value": 3, "reason": "限制连续亏损次数"})
        suggestions["notes"].append("检查是否在趋势末期追单")

    else:
        suggestions["notes"].append("需要人工分析具体失败原因")

    return suggestions


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute stats from strategy evaluations.

    If real paper trade outcomes are available (via pnl_r column), use actual PnL.
    Otherwise fall back to score-based pseudo-R.
    """
    # Try to get real trade outcomes from pnl_r column
    real_pnls = []
    for r in rows:
        pnl_r = r.get("pnl_r")
        if pnl_r is not None:
            real_pnls.append(float(pnl_r))

    if real_pnls:
        # Use real trade outcomes
        avg_r = sum(real_pnls) / len(real_pnls)
        wins = len([x for x in real_pnls if x > 0])
        win_rate = wins / len(real_pnls)
        equity = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in real_pnls:
            equity += value
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        return {
            "sample_count": len(real_pnls),
            "avg_r": avg_r,
            "win_rate": win_rate,
            "drawdown": drawdown,
            "data_source": "real_pnl",
        }

    # Fallback: score-based pseudo-R (less reliable)
    scores = [float(r.get("score") or 0) for r in rows]
    pseudo_rs = [(score - 0.5) * 2 for score in scores]
    avg_r = sum(pseudo_rs) / len(pseudo_rs) if pseudo_rs else 0.0
    wins = len([x for x in pseudo_rs if x > 0.1])
    win_rate = wins / len(pseudo_rs) if pseudo_rs else 0.0
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in pseudo_rs:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return {
        "sample_count": len(rows),
        "avg_r": avg_r,
        "win_rate": win_rate,
        "drawdown": drawdown,
        "data_source": "pseudo_r_from_score",
    }
