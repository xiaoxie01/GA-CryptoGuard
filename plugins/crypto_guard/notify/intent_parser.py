from __future__ import annotations

import re
from typing import Any

from plugins.crypto_guard.data.binance_rest import normalize_symbol


SYMBOL_RE = re.compile(r"\b([A-Za-z]{2,12}(?:USDT)?)\b")


def parse_intent(text: str) -> dict[str, Any]:
    raw = text.strip()
    symbol = _first_symbol(raw)
    lowered = raw.lower()
    if lowered in {"/status", "status"}:
        intent = "system_status"
    elif lowered in {"/errors", "/error-log", "errors", "error-log"} or any(k in raw for k in ("错误日志", "最近错误", "失败任务", "错误查询")):
        intent = "list_errors"
    elif lowered in {"/watchlist", "watchlist"}:
        intent = "list_symbols"
    elif lowered in {"/strategies", "strategies"} or any(k in raw for k in ("策略版本", "策略列表", "查看策略")):
        intent = "list_strategy_versions"
    elif lowered.startswith("/analyze"):
        intent = "analyze_once"
    elif any(k in raw for k in ("每日复盘", "今日复盘", "昨日复盘", "复盘日报", "执行复盘")):
        intent = "daily_review"
    elif any(k in raw for k in ("系统状态", "运行状态", "服务状态", "定时任务状态", "队列状态", "任务状态", "健康检查")):
        intent = "system_status"
    elif any(k in raw for k in ("列出", "当前监控", "监控币种", "列表")):
        intent = "list_symbols"
    elif any(k in raw for k in ("暂停", "停止分析", "先别分析")):
        intent = "pause_symbol"
    elif any(k in raw for k in ("恢复", "继续分析", "重新分析")):
        intent = "resume_symbol"
    elif any(k in raw for k in ("移除", "删除", "取消监控")):
        intent = "remove_symbol"
    elif any(k in raw for k in ("临时", "不要加入", "不加入长期", "不加入监控")) and any(k in raw for k in ("分析", "看一下", "看看")):
        intent = "analyze_once"
    elif any(k in raw for k in ("加入监控", "长期", "以后也分析", "重点分析")) and not any(k in raw for k in ("不要加入", "不加入")):
        intent = "add_symbol"
    elif any(k in raw for k in ("历史持仓", "历史交易", "交易记录", "持仓记录", "平仓记录", "历史订单", "模拟盘记录", "模拟盘历史")):
        intent = "paper_positions"
    elif any(k in raw for k in ("模拟盘", "加入模拟", "开模拟")):
        intent = "create_paper_order"
    elif any(k in raw for k in ("盯着", "提醒", "机会监控")):
        intent = "create_opportunity_watch"
    elif any(k in raw for k in ("分析", "看一下", "看看", "能不能做", "有没有机会")):
        intent = "analyze_once"
    else:
        intent = "unknown"
    scope = "temporary" if any(k in raw for k in ("临时", "不要加入", "不加入长期")) else "long_term_watchlist"
    return {"intent": intent, "symbol": symbol, "timeframes": _timeframes(raw), "scope": scope, "raw_text": raw}


def is_crypto_intent(text: str) -> bool:
    intent = parse_intent(text)
    return intent["intent"] != "unknown" and (intent["intent"] in {"list_symbols", "system_status", "list_errors", "daily_review", "list_strategy_versions", "paper_positions"} or intent.get("symbol"))


def _first_symbol(text: str) -> str | None:
    skip = {"ANALYZE", "STATUS", "ERRORS", "ERROR", "LOG", "WATCHLIST", "REVIEW", "LATEST", "SYSTEM", "STRATEGIES"}
    for match in SYMBOL_RE.finditer(text.upper()):
        token = match.group(1)
        if token in skip:
            continue
        if token in {"LONG", "SHORT", "ENTRY", "TP", "SL"}:
            continue
        try:
            return normalize_symbol(token)
        except ValueError:
            continue
    return None


def _timeframes(text: str) -> list[str]:
    found: list[str] = []
    for tf in ("1d", "4h", "1h", "15m", "5m", "3m"):
        if tf.lower() in text.lower() and tf not in found:
            found.append(tf)
    return found or ["4h", "1h", "15m", "5m"]
