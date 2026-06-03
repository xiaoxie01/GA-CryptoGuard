from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any


def build_analysis_card(decision: dict[str, Any], *, signal_id: int | None = None) -> dict[str, Any]:
    content = render_text(decision, signal_id=signal_id)
    actions = _actions(decision, signal_id)
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": content}]
    elements.extend(actions)
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": elements},
    }


def _legacy_summary(decision: dict[str, Any]) -> list[str]:
    lines = [
        f"**{decision['symbol']} 临时分析**",
        "",
        f"当前结论：{_decision_text(decision)}",
        f"趋势阶段：{decision.get('trend_stage', 'unknown')}",
        f"信号等级：{decision.get('signal_grade')}",
        f"置信度：{round(float(decision.get('confidence', 0)) * 100)}%",
        f"研判来源：{_analysis_source_text(decision)}",
        "",
        "**理由：**",
    ]
    lines.extend([f"- {x}" for x in decision.get("evidence", [])] or ["- 暂无明确优势证据"])
    if decision.get("risk_notes"):
        lines.append("")
        lines.append("**风险点：**")
        lines.extend([f"- {x}" for x in decision["risk_notes"]])
    plan = decision.get("trade_plan")
    if plan:
        tps = ", ".join(str(tp.get("price")) for tp in plan.get("take_profits", []))
        lines.extend(["", "**模拟计划：**", f"Entry: {plan.get('entry_price') or plan.get('trigger_price')}", f"SL: {plan.get('stop_loss')}", f"TP: {tps}", f"风险: {plan.get('risk_percent')}%"])
    watch = decision.get("opportunity_watch")
    if watch and not plan:
        lines.append("")
        lines.append("**等待条件：**")
        lines.extend([f"- {x}" for x in watch.get("conditions", [])])
    lines.append("")
    lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")
    return lines


def build_analysis_card_json(decision: dict[str, Any], *, signal_id: int | None = None) -> str:
    return json.dumps(build_analysis_card(decision, signal_id=signal_id), ensure_ascii=False)


def render_text(decision: dict[str, Any], *, signal_id: int | None = None) -> str:
    if not decision.get("modules"):
        return "\n".join(_legacy_summary(decision))
    plan = decision.get("trade_plan")
    watch = decision.get("opportunity_watch")
    lines = [
        f"**{decision['symbol']} 临时分析**",
        "",
        "**一句话结论**",
        f"- {_plain_summary(decision)}",
        "",
        "**当前判断**",
        f"- 结论：{_decision_text(decision)}",
        f"- 市场倾向：{_bias_text(decision.get('market_bias'))}",
        f"- 趋势状态：{_stage_text(decision.get('trend_stage'))}",
        f"- 信号等级：{decision.get('signal_grade')}（{_grade_text(decision.get('signal_grade'))}）",
        f"- 置信度：{round(float(decision.get('confidence', 0)) * 100)}%",
        f"- 研判来源：{_analysis_source_text(decision)}",
        f"- 分析时间：{_fmt_time_utc8(decision.get('analysis_time_utc'))}",
        f"- 最新标记价格：{_fmt_price(decision.get('latest_price'))}（{_price_source_text(decision.get('price_source'))}）",
        "",
    ]
    lines.extend(_opportunity_lines(decision))
    lines.extend(_profile_lines(decision))
    lines.extend(_key_level_lines(decision.get("modules") or {}))
    lines.extend(_module_lines(decision.get("modules") or {}))
    lines.extend(_evidence_lines(decision))
    if plan:
        tps = ", ".join(f"{tp.get('price')}({tp.get('ratio')})" for tp in plan.get("take_profits", []))
        lines.extend(["", "**模拟盘计划**", f"- 方向：{_side_text(plan.get('side'))}", f"- 入场：{plan.get('entry_price') or plan.get('trigger_price')}", f"- 止损：{plan.get('stop_loss')}", f"- 止盈：{tps}", f"- 单笔风险：{plan.get('risk_percent')}%", f"- 失效条件：{plan.get('invalid_condition')}"])
    if watch and not plan:
        lines.extend(["", "**等待触发条件**"])
        lines.extend([f"- {_textify_condition(x)}" for x in watch.get("conditions", [])])
    lines.extend(["", "**数据说明**", f"- 分析周期：{', '.join(decision.get('timeframes') or [])}", "- 只使用已收盘 K 线；系统查询条件为 close_time <= 分析时间，避免未来函数。"])
    lines.append("")
    lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")
    return "\n".join(lines)


def _actions(decision: dict[str, Any], signal_id: int | None) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    allowed_actions = decision.get("feishu_actions") or decision.get("suggested_actions", [])
    ga_decision_id = decision.get("ga_decision_id") or decision.get("id") or (decision.get("ga_decision") or {}).get("ga_decision_id")
    for action, text in (
        ("create_paper_order", "加入模拟盘"),
        ("create_opportunity_watch", "加入机会监控"),
        ("add_to_watchlist", "加入长期产品池"),
        ("ignore", "忽略"),
    ):
        if action not in allowed_actions:
            continue
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": text},
                "type": "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "plugin": "crypto_guard",
                            "action": action,
                            "symbol": decision["symbol"],
                            "signal_id": signal_id,
                            "ga_decision_id": ga_decision_id,
                        },
                    }
                ],
            }
        )
    return buttons


def _decision_text(decision: dict[str, Any]) -> str:
    mapping = {
        "trade_plan_available": "模拟盘候选",
        "wait_for_pullback": "等待回踩确认",
        "wait_for_breakout": "等待突破确认",
        "wait_for_reclaim": "等待重新站回关键位",
        "avoid_chop": "震荡回避",
        "monitor_only": "仅观察",
        "no_edge": "无明显优势",
    }
    return mapping.get(decision.get("decision"), decision.get("decision", "未知"))


def _plain_summary(decision: dict[str, Any]) -> str:
    summary = str(decision.get("summary") or "").strip()
    if summary:
        return _humanize_text(summary)
    if decision.get("decision") == "no_edge":
        return f"{decision.get('symbol')} 当前没有清晰优势，先观察，不生成模拟盘计划。"
    if decision.get("decision") == "monitor_only":
        return f"{decision.get('symbol')} 仅适合观察，等待结构更清晰。"
    return f"{decision.get('symbol')} 已完成多周期研判。"


def _fmt_price(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.8g}"
    except Exception:
        return str(value)


def _analysis_source_text(decision: dict[str, Any]) -> str:
    source = decision.get("analysis_source")
    status = decision.get("llm_status")
    if source == "llm_agent" and status == "ok":
        return "LLM/GA Agent"
    if source == "deterministic_fallback":
        return "LLM/GA 失败后规则降级"
    if source == "deterministic_sop":
        return "规则 SOP"
    return "GA SOP"


def _fmt_time_utc8(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        raw = int(float(value))
        ts = raw / 1000 if raw > 10_000_000_000 else raw
        dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except Exception:
        return str(value)


def _bias_text(value: object) -> str:
    return {
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "mixed": "多空混杂",
    }.get(str(value), str(value or "未知"))


def _stage_text(value: object) -> str:
    return {
        "early": "趋势初期",
        "middle": "趋势中段",
        "late": "趋势末端",
        "range": "震荡区间",
        "transition": "转换期",
        "unknown": "未知",
    }.get(str(value), str(value or "未知"))


def _structure_text(value: object) -> str:
    return {
        "bullish": "偏多结构",
        "bearish": "偏空结构",
        "range": "震荡结构",
        "mixed": "混合结构",
        "unknown": "未知结构",
    }.get(str(value), str(value or "-"))


def _momentum_text(value: object) -> str:
    return {
        "bullish": "动能偏多",
        "bearish": "动能偏空",
        "neutral": "动能中性",
        "mixed": "动能混杂",
    }.get(str(value), str(value or "-"))


def _grade_text(value: object) -> str:
    return {
        "S": "强机会",
        "A": "较强机会",
        "B": "观察机会",
        "C": "仅观察",
        "D": "无优势",
    }.get(str(value), "未分级")


def _side_text(value: object) -> str:
    return {"LONG": "做多", "SHORT": "做空"}.get(str(value), str(value or "-"))


def _price_source_text(value: object) -> str:
    return {
        "binance_mark_price": "Binance 标记价格",
        "latest_closed_15m": "最近已收盘 15m K 线",
    }.get(str(value), str(value or "-"))


def _profile_lines(decision: dict[str, Any]) -> list[str]:
    profiles = decision.get("profiles") or {}
    if not profiles:
        return []
    lines = ["", "**多周期画像**"]
    for tf, profile in profiles.items():
        lines.append(
            f"- {tf}：{_structure_text(profile.get('market_structure'))}，"
            f"{_stage_text(profile.get('trend_stage'))}，"
            f"{_momentum_text(profile.get('momentum'))}，样本 {profile.get('candles_count', 0)} 根"
        )
    return lines


def _opportunity_lines(decision: dict[str, Any]) -> list[str]:
    lines = ["", "**机会判断**"]
    if decision.get("has_trade_plan") and decision.get("trade_plan"):
        lines.append("- 已形成模拟盘计划，但仍需要按止损和失效条件执行。")
        return lines
    if decision.get("opportunity_watch"):
        lines.append("- 暂不直接模拟开仓，可以加入机会监控等待触发条件。")
        return lines
    reasons = _dedupe([str(x) for x in (decision.get("counter_evidence") or []) + (decision.get("risk_notes") or []) if "不构成实盘建议" not in str(x)])
    lines.append("- 当前不生成模拟盘计划。")
    for reason in reasons[:4]:
        lines.append(f"- 原因：{_humanize_text(reason)}")
    return lines


def _key_level_lines(modules: dict) -> list[str]:
    pa = modules.get("price_action") or {}
    levels = pa.get("key_levels") or {}
    return [
        "",
        "**关键价格区**",
        f"- 支撑：{_fmt_levels(levels.get('support'))}",
        f"- 阻力：{_fmt_levels(levels.get('resistance'))}",
        f"- 失效位：{_fmt_price(pa.get('invalid_level'))}",
    ]


def _module_lines(modules: dict) -> list[str]:
    pa = modules.get("price_action") or {}
    smc = modules.get("smc") or {}
    order_flow = modules.get("order_flow") or {}
    momentum = modules.get("momentum") or {}
    trend = modules.get("trend_stage") or {}
    chanlun = modules.get("chanlun") or {}
    lines = ["", "**模块明细**"]
    lines.append(
        f"- 价格行为：{_structure_text(pa.get('market_structure'))}，摆动序列={_event_text(pa.get('swing_sequence'))}，"
        f"最近事件={_event_text(pa.get('last_event'))}，区间状态={_event_text(pa.get('range_status'))}"
    )
    lines.append(
        f"- SMC：流动性={_event_text((smc.get('liquidity') or {}).get('last_event'))}，"
        f"FVG缺口={'存在' if ((smc.get('fvg') or {}).get('exists', False)) else '未发现'}，"
        f"价格位置={_premium_discount_text(smc.get('premium_discount'))}"
    )
    lines.append(
        f"- 订单流：{_flow_text(order_flow.get('flow_confirmation'))}，CVD={_slope_text(order_flow.get('cvd_slope'))}，"
        f"说明={'当前订单流为降级/估算数据，只作辅助参考' if order_flow.get('flow_confirmation') in {'not_available', 'neutral'} else '可作辅助确认'}"
    )
    lines.append(
        f"- 动能：{_momentum_text(momentum.get('direction'))}，分数={momentum.get('momentum_score', '-')}，"
        f"质量={_quality_text(momentum.get('quality'))}，波动={_atr_text(momentum.get('atr_state'))}"
    )
    lines.append(f"- 趋势阶段：{_stage_text(trend.get('trend_stage'))}，主风险={trend.get('main_risk', '-')}")
    lines.append(
        f"- 缠论：信号={_event_text(chanlun.get('signal'))}，结构={_event_text(chanlun.get('current_structure'))}，"
        f"说明={'MVP 占位，完整分型/笔/中枢/背驰尚未接入' if chanlun.get('implemented') is False else '-'}"
    )
    return lines


def _evidence_lines(decision: dict) -> list[str]:
    lines = ["", "**证据与风险**"]
    evidence = _dedupe(decision.get("evidence", []))
    counter = _dedupe(decision.get("counter_evidence", []))
    risks = _dedupe([x for x in decision.get("risk_notes", []) if "不构成实盘建议" not in str(x)])
    title = "暂无机会的主要依据" if decision.get("decision") == "no_edge" else "支持证据"
    lines.append(f"- {title}：" + ("；".join(_humanize_text(x) for x in evidence[:4]) if evidence else "暂无明确支持证据"))
    lines.append("- 反向证据/矛盾点：" + ("；".join(_humanize_text(x) for x in counter[:4]) if counter else "暂无明显反向证据"))
    lines.append("- 风险点：" + ("；".join(_humanize_text(x) for x in risks[:4]) if risks else "暂无额外风险点"))
    return lines


def _fmt_levels(levels: object) -> str:
    if not levels:
        return "-"
    if isinstance(levels, list):
        return ", ".join(_fmt_price(x) for x in levels[-4:])
    return str(levels)


def _dedupe(items: list) -> list[str]:
    seen = set()
    out = []
    for item in items:
        text = str(item)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _event_text(value: object) -> str:
    mapping = {
        None: "-",
        "None": "-",
        "none": "无",
        "mixed": "混杂",
        "range_bound": "区间震荡",
        "inside_range": "仍在区间内",
        "breakout": "突破",
        "breakout_retest": "突破后回踩",
        "bullish_bos": "向上结构突破",
        "bearish_bos": "向下结构突破",
        "bullish_choch": "向上结构转换",
        "bearish_choch": "向下结构转换",
        "bi_up": "向上一笔",
        "bi_down": "向下一笔",
    }
    return mapping.get(value, str(value or "-"))


def _premium_discount_text(value: object) -> str:
    return {"premium": "偏高区域", "discount": "偏低区域", "equilibrium": "均衡区域"}.get(str(value), str(value or "-"))


def _flow_text(value: object) -> str:
    return {
        "supports_long": "订单流支持做多",
        "supports_short": "订单流支持做空",
        "neutral": "订单流中性",
        "not_available": "订单流不可用",
    }.get(str(value), str(value or "-"))


def _slope_text(value: object) -> str:
    return {"up": "上行", "down": "下行", "flat": "走平"}.get(str(value), str(value or "-"))


def _quality_text(value: object) -> str:
    return {
        "healthy": "正常",
        "overheated": "过热",
        "exhausted": "衰竭",
        "range": "震荡",
        "insufficient_data": "样本不足",
    }.get(str(value), str(value or "-"))


def _atr_text(value: object) -> str:
    return {"expanding": "波动放大", "contracting": "波动收缩", "normal": "正常"}.get(str(value), str(value or "-"))


def _textify_condition(value: object) -> str:
    if isinstance(value, dict):
        kind = value.get("type") or value.get("kind") or "条件"
        level = value.get("level") or value.get("price")
        side = _side_text(value.get("side") or value.get("direction"))
        return f"{_event_text(kind)} {side} {level or ''}".strip()
    return str(value)


def _humanize_text(value: object) -> str:
    text = str(value)
    replacements = {
        "range_bound": "区间震荡",
        "inside_range": "区间内运行",
        "transition": "转换期",
        "range": "震荡",
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "mixed": "混杂",
        "premium": "偏高区域",
        "discount": "偏低区域",
        "FVG": "FVG缺口",
        "degraded=true": "数据已降级",
        "confidence": "可信度",
        "aggressive_buy_ratio": "主动买入占比",
        "momentum_score": "动能分数",
        "trend_stage": "趋势阶段",
        "smc_pullback_long": "SMC 回踩做多策略",
        "no_edge": "无明显优势",
        "monitor_only": "仅观察",
        "trade_plan_available": "模拟盘候选",
    }
    for src, dst in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(src, dst)
    return text


def build_evolution_review_card(
    candidate_version: str,
    sample_count: int,
    reason: str,
    backtest_status: dict[str, Any] | None = None,
    active_stats: dict[str, Any] | None = None,
    candidate_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a feishu card for evolution review with approve/reject buttons."""

    backtest_info = ""
    if backtest_status:
        if backtest_status.get("passed"):
            backtest_info = "**回测门禁**: 通过"
        elif backtest_status.get("skipped"):
            backtest_info = "**回测门禁**: 跳过（配置禁用或数据不足）"
        else:
            backtest_info = f"**回测门禁**: 未通过 ({backtest_status.get('reason', 'unknown')})"

    stats_info = ""
    if active_stats and candidate_stats:
        stats_info = f"""
**性能对比**:
- Active: avg_r={active_stats.get('avg_r', '-')}, win_rate={active_stats.get('win_rate', '-')}
- Candidate: avg_r={candidate_stats.get('avg_r', '-')}, win_rate={candidate_stats.get('win_rate', '-')}
"""

    content = f"""**CryptoGuard 自进化 - 人工审核**

**候选版本**: {candidate_version}
**影子样本数**: {sample_count}
**触发原因**: {reason}

{backtest_info}
{stats_info}
候选策略已通过影子测试，等待人工确认升级。

**请审核以下内容后决定是否批准：**
1. 候选策略的改进逻辑是否合理
2. 影子测试的样本量是否足够
3. 是否存在过拟合风险

注意：当前样本基于模拟盘和影子测试，非真实成交 PnL。

不构成实盘建议，所有策略变更仅进入 candidate/shadow 流程。"""

    buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "批准升级"},
            "type": "primary",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "plugin": "crypto_guard",
                        "action": "approve_evolution",
                        "candidate_version": candidate_version,
                    },
                }
            ],
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "拒绝"},
            "type": "danger",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "plugin": "crypto_guard",
                        "action": "reject_evolution",
                        "candidate_version": candidate_version,
                    },
                }
            ],
        },
    ]

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": content}]
    elements.extend(buttons)
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": elements},
    }
