from __future__ import annotations

import json
import re
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.data.candle_store import fetch_and_upsert_closed_klines
from plugins.crypto_guard.data.binance_rest import MarketDataError, fetch_mark_price, normalize_symbol
from plugins.crypto_guard.backtest.historical_replay import run_historical_replay
from plugins.crypto_guard.data.symbol_registry import add_symbol, list_symbols, pause_symbol, remove_symbol, resume_symbol
from plugins.crypto_guard.ga_master import GAAnalysisRequest, GAMasterController
from plugins.crypto_guard.notify.feishu_cards import build_analysis_card_json, render_text
from plugins.crypto_guard.notify.intent_parser import parse_intent
from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_signal
from plugins.crypto_guard.reasoning.market_state_builder import DEFAULT_TIMEFRAMES, build_market_state_snapshot
from plugins.crypto_guard.review.trade_reviewer import review_trade
from plugins.crypto_guard.review.daily_reviewer import run_daily_review
from plugins.crypto_guard.scheduler.opportunity_watcher import update_opportunity_watches
from plugins.crypto_guard.strategy.version_manager import list_strategy_versions
from plugins.crypto_guard.strategy.shadow_testing import run_shadow_test
from plugins.crypto_guard.strategy.self_evolution import run_self_evolution_cycle
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.tools.status_tools import crypto_list_recent_errors as _crypto_list_recent_errors, crypto_system_status as _crypto_system_status, render_system_status_text
from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms


def _repo() -> tuple[Any, CryptoGuardRepository]:
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    return conn, CryptoGuardRepository(conn)


def crypto_init() -> dict[str, Any]:
    return initialize_database(load_config())


def crypto_symbol_add(symbol: str, category: str = "custom", timeframes: list[str] | None = None, enabled: bool = True) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        result = add_symbol(repo, symbol, category=category, timeframes=timeframes, validate=True)
        if result.get("ok") and not enabled:
            pause_symbol(repo, result["symbol"])
        return result
    finally:
        conn.close()


def crypto_symbol_remove(symbol: str) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return remove_symbol(repo, symbol)
    finally:
        conn.close()


def crypto_symbol_pause(symbol: str) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return pause_symbol(repo, symbol)
    finally:
        conn.close()


def crypto_symbol_resume(symbol: str) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return resume_symbol(repo, symbol)
    finally:
        conn.close()


def crypto_symbol_list() -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return list_symbols(repo)
    finally:
        conn.close()


def crypto_analyze_symbol_once(symbol: str, timeframes: list[str] | None = None, requested_by: str | None = None, request_text: str = "") -> dict[str, Any]:
    conn, repo = _repo()
    try:
        symbol = normalize_symbol(symbol)
        tfs = timeframes or DEFAULT_TIMEFRAMES
        analysis_time = latest_closed_close_time_ms("15m", utc_ms())
        # 临时分析只补齐数据，不写入长期 watchlist。
        fetch_errors: list[str] = []
        for tf in tfs:
            try:
                fetch_and_upsert_closed_klines(repo, symbol, tf, analysis_time_utc=analysis_time, lookback=160)
            except MarketDataError as exc:
                fetch_errors.append(f"{tf}: {exc}")
        if fetch_errors:
            available = sum(len(repo.get_candles(symbol, tf, analysis_time_utc=analysis_time, limit=1)) for tf in tfs)
            if available == 0:
                return {
                    "ok": False,
                    "symbol": symbol,
                    "analysis_time_utc": analysis_time,
                    "error": "market_data_unavailable",
                    "errors": fetch_errors,
                    "text": (
                        f"{symbol} 临时分析失败：当前无法获取 Binance public 行情数据。\n\n"
                        "可能原因：网络/代理连接被重置、Binance 接口暂时不可达，或本机代理未正确转发 HTTPS。\n"
                        "系统未使用任何实盘权限，也不会下单。\n\n"
                        "可以稍后重试，或先发送“系统状态”查看队列与日志。"
                    ),
                }
        snapshot = build_market_state_snapshot(repo, symbol=symbol, analysis_time_utc=analysis_time, mode="ad_hoc", timeframes=tfs)
        snapshot_id = repo.save_market_snapshot(snapshot)
        decision = GAMasterController(repo).analyze_symbol(
            GAAnalysisRequest(
                symbol=symbol,
                decision_type="ad_hoc_analysis",
                analysis_time_utc=analysis_time,
                mode="ad_hoc",
                timeframes=tfs,
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                requested_by=requested_by,
                request_text=request_text,
            )
        )
        _attach_display_context(repo, decision, snapshot, tfs)
        if fetch_errors:
            decision["risk_notes"] = decision.get("risk_notes", []) + ["部分周期行情刷新失败，已使用本地已缓存 K 线；请注意数据可能不是最新。"]
        state_id = int(decision["analysis_state_id"])
        signal_id = int(decision["signal_id"])
        analysis_id = repo.save_ad_hoc_analysis(symbol, requested_by, request_text, decision, signal_id)
        return {
            "ok": True,
            "symbol": symbol,
            "analysis_time_utc": analysis_time,
            "snapshot_id": snapshot_id,
            "signal_id": signal_id,
            "ga_decision_id": decision.get("ga_decision_id"),
            "analysis_state_id": state_id,
            "analysis_id": analysis_id,
            "decision": decision,
            "card_json": build_analysis_card_json(decision, signal_id=signal_id),
            "text": render_text(decision, signal_id=signal_id),
        }
    finally:
        conn.close()


def crypto_create_opportunity_watch(symbol: str, watch_condition: dict[str, Any], expire_minutes: int = 240, signal_id: int | None = None) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return {
            "ok": False,
            "error": "机会监控必须由 GA decision 的飞书按钮确认创建，工具层不能直接创建。",
            "symbol": normalize_symbol(symbol),
            "signal_id": signal_id,
        }
    finally:
        conn.close()


def crypto_update_opportunity_watches(analysis_time_utc: int | None = None) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return update_opportunity_watches(repo, analysis_time_utc=analysis_time_utc)
    finally:
        conn.close()


def crypto_create_paper_order_from_signal(signal_id: int) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return create_paper_order_from_signal(repo, int(signal_id))
    finally:
        conn.close()


def crypto_get_market_state(symbol: str, timeframes: list[str] | None = None) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        symbol = normalize_symbol(symbol)
        analysis_time = latest_closed_close_time_ms("15m", utc_ms())
        snapshot = build_market_state_snapshot(repo, symbol=symbol, analysis_time_utc=analysis_time, mode="ad_hoc", timeframes=timeframes or DEFAULT_TIMEFRAMES)
        return {"ok": True, "snapshot": snapshot}
    finally:
        conn.close()


def crypto_get_closed_candles(symbol: str, interval: str, analysis_time_utc: int, limit: int = 200) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        symbol = normalize_symbol(symbol)
        return repo.no_lookahead_candles(symbol, interval, analysis_time_utc=int(analysis_time_utc), limit=limit)
    finally:
        conn.close()


def crypto_get_open_paper_positions() -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return {"ok": True, "orders": repo.list_open_paper_orders()}
    finally:
        conn.close()


def crypto_review_trade(trade_id: int) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return review_trade(repo, int(trade_id))
    finally:
        conn.close()


def crypto_daily_review(day_utc: str | None = None) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return run_daily_review(repo, day_utc=day_utc)
    finally:
        conn.close()


def crypto_paper_positions(limit: int = 20, symbol: str | None = None, status: str | None = None) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        conditions = []
        params: list[Any] = []
        if symbol:
            conditions.append("t.symbol=?")
            params.append(symbol)
        if status == "open":
            conditions.append("t.exit_price IS NULL")
        elif status == "closed":
            conditions.append("t.exit_price IS NOT NULL")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
            SELECT t.id, t.symbol, t.side, t.entry_price, t.exit_price, t.pnl, t.pnl_percent, t.pnl_r,
                   t.close_reason, t.created_at, t.closed_at, t.max_favorable_excursion, t.max_adverse_excursion,
                   o.stop_loss, o.take_profit_json
            FROM paper_trades t
            LEFT JOIN paper_orders o ON t.order_id = o.id
            {where}
            ORDER BY t.id DESC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        trades = [dict(r) for r in rows]

        total = len(trades)
        open_trades = [t for t in trades if t.get("exit_price") is None]
        closed_trades = [t for t in trades if t.get("exit_price") is not None]
        wins = sum(1 for t in closed_trades if float(t.get("pnl_r") or 0) > 0.05)
        losses = sum(1 for t in closed_trades if float(t.get("pnl_r") or 0) < -0.05)
        breakeven = len(closed_trades) - wins - losses
        total_pnl = sum(float(t.get("pnl") or 0) for t in closed_trades)
        total_pnl_r = sum(float(t.get("pnl_r") or 0) for t in closed_trades)
        win_rate = wins / len(closed_trades) * 100 if closed_trades else 0

        # Win rate bar
        bar_len = 10
        filled = round(win_rate / 100 * bar_len)
        bar = "+" * filled + "-" * (bar_len - filled)

        # Profit factor
        gross_profit = sum(float(t.get("pnl") or 0) for t in closed_trades if float(t.get("pnl_r") or 0) > 0.05)
        gross_loss = abs(sum(float(t.get("pnl") or 0) for t in closed_trades if float(t.get("pnl_r") or 0) < -0.05))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

        # Max consecutive losses
        max_consec_loss = 0
        consec = 0
        for t in closed_trades:
            if float(t.get("pnl_r") or 0) < -0.05:
                consec += 1
                max_consec_loss = max(max_consec_loss, consec)
            else:
                consec = 0

        def _pnl_indicator(pnl_r: float) -> str:
            if pnl_r > 2:
                return "++"
            elif pnl_r > 0.05:
                return "+ "
            elif pnl_r < -0.05:
                return "- "
            else:
                return "= "

        def _reason_short(t: dict[str, Any]) -> str:
            is_open = t.get("exit_price") is None
            if is_open:
                return "持仓"
            return {
                "take_profit": "TP",
                "stop_loss": "SL",
                "timeout": "超时",
                "manual": "手动",
            }.get(t.get("close_reason"), "-")

        def _holding(t: dict[str, Any]) -> str:
            created = t.get("created_at", "")
            closed = t.get("closed_at") or ""
            if not created:
                return "-"
            if not closed:
                return "持仓中"
            try:
                from datetime import datetime
                fmt = "%Y-%m-%d %H:%M:%S"
                start = datetime.strptime(created[:19], fmt)
                end = datetime.strptime(closed[:19], fmt)
                mins = int((end - start).total_seconds() // 60)
                if mins < 60:
                    return f"{mins}m"
                hours = mins // 60
                return f"{hours}h{mins % 60}m"
            except Exception:
                return "-"

        lines = [
            "**CryptoGuard 模拟盘持仓记录**",
            "",
        ]

        # Summary card
        avg_win_r = sum(float(t.get("pnl_r") or 0) for t in closed_trades if float(t.get("pnl_r") or 0) > 0.05) / wins if wins else 0
        avg_loss_r = sum(float(t.get("pnl_r") or 0) for t in closed_trades if float(t.get("pnl_r") or 0) < -0.05) / losses if losses else 0

        lines.append("```")
        lines.append(f"总览  {total}笔  持仓{len(open_trades)}  已平{len(closed_trades)}")
        lines.append(f"胜率  [{bar}]  {win_rate:.0f}%  ({wins}W/{losses}L/{breakeven}BE)")
        lines.append(f"累计  {total_pnl_r:+.2f}R  ({total_pnl:+.2f}U)")
        lines.append(f"盈亏  平均盈{avg_win_r:+.2f}R  平均亏{avg_loss_r:+.2f}R  PF={profit_factor:.2f}")
        if max_consec_loss >= 2:
            lines.append(f"连亏  最多连续{max_consec_loss}笔")
        lines.append("```")
        lines.append("")

        # Trade table
        def _fmt_row(t: dict[str, Any]) -> str:
            side_cn = "多" if str(t.get("side") or "").upper() == "LONG" else "空"
            pnl_r = float(t.get("pnl_r") or 0)
            pnl_u = float(t.get("pnl") or 0)
            symbol_short = t.get("symbol", "").replace("USDT", "")
            indicator = _pnl_indicator(pnl_r)
            reason = _reason_short(t)
            hold = _holding(t)
            entry = t.get("entry_price", "-")
            exit_p = t.get("exit_price") or "-"
            ts = (t.get("created_at") or "-")[5:16]  # MM-DD HH:MM

            if t.get("exit_price") is None:
                # Open position
                return f"#{t['id']:>2}  {symbol_short:<6} {side_cn}  {entry:<10}  {'---':<10}  {'持仓中':>6}  {reason}  {hold}"
            else:
                return f"#{t['id']:>2}  {symbol_short:<6} {side_cn}  {entry:<10}  {exit_p:<10}  {indicator}{pnl_r:+.2f}R  {reason}  {hold}"

        # Header
        lines.append("```")
        lines.append(f"{'#':>3}  {'币种':<6} {'向':<2}  {'入场':<10}  {'出场':<10}  {'盈亏R':>7}  {'原因':<4}  {'时长':<5}")
        lines.append("-" * 62)

        # Open positions first
        for t in open_trades:
            lines.append(_fmt_row(t))

        # Then closed
        for t in closed_trades[:15]:
            lines.append(_fmt_row(t))

        lines.append("```")
        lines.append("")
        lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")
        return {"ok": True, "trades": trades, "text": "\n".join(lines), "total": total, "wins": wins, "losses": losses, "total_pnl": total_pnl, "total_pnl_r": total_pnl_r}
    finally:
        conn.close()


def crypto_list_strategy_versions(strategy_name: str | None = None) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return list_strategy_versions(repo, strategy_name)
    finally:
        conn.close()


def crypto_run_shadow_test(strategy_name: str, candidate_version: str, min_samples: int = 30) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return run_shadow_test(repo, strategy_name=strategy_name, candidate_version=candidate_version, min_samples=min_samples)
    finally:
        conn.close()


def crypto_run_historical_replay(
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    parquet_path: str | None = None,
    strategy_versions: list[str] | None = None,
    export_path: str | None = None,
) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        symbol = normalize_symbol(symbol)
        return run_historical_replay(
            repo,
            symbol=symbol,
            interval=interval,
            start_time=int(start_time),
            end_time=int(end_time),
            parquet_path=parquet_path,
            strategy_versions=strategy_versions,
            export_path=export_path,
        )
    finally:
        conn.close()


def crypto_run_self_evolution(strategy_name: str = "smc_pullback_long", min_reviews: int = 5, min_symbols: int = 2, min_shadow_samples: int = 30) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        return run_self_evolution_cycle(
            repo,
            strategy_name=strategy_name,
            min_reviews=min_reviews,
            min_symbols=min_symbols,
            min_shadow_samples=min_shadow_samples,
            allow_auto_promote=False,
        )
    finally:
        conn.close()


def crypto_request_config_update(config_key: str, new_value: Any, requested_by: str | None = None, request_text: str = "") -> dict[str, Any]:
    conn, repo = _repo()
    try:
        change_id = repo.request_config_hot_reload(
            config_key=config_key,
            new_value=new_value,
            requested_by=requested_by,
            request_text=request_text,
            confirmation_required=True,
        )
        return {
            "ok": True,
            "change_id": change_id,
            "confirmation_required": True,
            "text": f"这是关键参数修改，请回复“确认 {change_id}”以执行：{config_key} -> {new_value}",
        }
    finally:
        conn.close()


def crypto_confirm_config_update(change_id: int) -> dict[str, Any]:
    conn, repo = _repo()
    try:
        result = repo.confirm_config_hot_reload(int(change_id))
        if result.get("ok"):
            result["text"] = "配置热更新已执行，并已写入审计表。\n" + result.get("audit_summary", "")
        return result
    finally:
        conn.close()


def crypto_list_recent_errors(limit: int = 20) -> dict[str, Any]:
    return _crypto_list_recent_errors(limit=limit)


def crypto_system_status() -> dict[str, Any]:
    status = _crypto_system_status()
    status["text"] = render_system_status_text(status)
    return status


def crypto_handle_text_command(text: str, user_id: str | None = None) -> dict[str, Any]:
    confirm_match = re.search(r"确认\s*(\d+)", text)
    if confirm_match:
        return crypto_confirm_config_update(int(confirm_match.group(1)))
    threshold_match = re.search(r"(?:置信度阈值|confidence\s*阈值).{0,12}?([01](?:\.\d+)?)", text, flags=re.IGNORECASE)
    if threshold_match:
        return crypto_request_config_update(
            "risk.min_confidence_for_paper_order",
            float(threshold_match.group(1)),
            requested_by=user_id,
            request_text=text,
        )
    intent = parse_intent(text)
    if intent["intent"] == "system_status":
        return crypto_system_status()
    if intent["intent"] == "list_errors":
        return crypto_list_recent_errors()
    if intent["intent"] == "daily_review":
        return crypto_daily_review()
    if intent["intent"] == "list_strategy_versions":
        return crypto_list_strategy_versions()
    if intent["intent"] == "paper_positions":
        return crypto_paper_positions(symbol=intent.get("symbol"))
    if intent["intent"] == "list_symbols":
        return crypto_symbol_list()
    symbol = intent.get("symbol")
    if not symbol:
        return {"ok": False, "error": "未识别到 symbol", "intent": intent}
    if intent["intent"] == "add_symbol":
        return crypto_symbol_add(symbol, timeframes=intent.get("timeframes"))
    if intent["intent"] == "pause_symbol":
        return crypto_symbol_pause(symbol)
    if intent["intent"] == "resume_symbol":
        return crypto_symbol_resume(symbol)
    if intent["intent"] == "remove_symbol":
        return crypto_symbol_remove(symbol)
    if intent["intent"] == "analyze_once":
        return crypto_analyze_symbol_once(symbol, intent.get("timeframes"), requested_by=user_id, request_text=text)
    if intent["intent"] == "create_paper_order":
        analysis = crypto_analyze_symbol_once(symbol, intent.get("timeframes"), requested_by=user_id, request_text=text)
        if not analysis.get("ok"):
            return analysis
        result = crypto_create_paper_order_from_signal(int(analysis["signal_id"]))
        if not result.get("ok"):
            result["text"] = (
                f"{symbol} 当前不允许加入模拟盘。\n\n"
                "原因：" + "；".join(result.get("risk_reasons") or [result.get("error", "风控未通过")]) + "\n\n"
                "可以点击或发送“加入机会监控”，等待 4H/1H/15M 与 5M 触发重新满足。"
            )
        return result
    return {"ok": False, "error": "暂未支持该意图", "intent": intent}


def json_result(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _attach_display_context(repo: CryptoGuardRepository, decision: dict[str, Any], snapshot: dict[str, Any], timeframes: list[str]) -> None:
    symbol = snapshot["symbol"]
    analysis_time = int(snapshot["analysis_time_utc"])
    latest_price = None
    price_source = "latest_closed_15m"
    try:
        latest_price = float(fetch_mark_price(symbol)["markPrice"])
        price_source = "binance_mark_price"
    except Exception:
        candles = repo.get_candles(symbol, "15m", analysis_time_utc=analysis_time, limit=1)
        if candles:
            latest_price = float(candles[-1]["close"])

    decision["analysis_time_utc"] = analysis_time
    decision["latest_price"] = latest_price
    decision["price_source"] = price_source
    decision["timeframes"] = timeframes
    decision["profiles"] = snapshot.get("profiles", {})
    decision["modules"] = snapshot.get("modules", {})
    decision["data_quality"] = {
        "closed_candles_only": True,
        "analysis_time_utc": analysis_time,
        "timeframes": timeframes,
        "note": "所有 K 线查询限制 close_time <= analysis_time_utc；低周期实时订单流当前为 MVP 占位。",
    }
