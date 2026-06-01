from __future__ import annotations

import json
from typing import Any


CRYPTO_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "crypto_symbol_add", "description": "添加 Binance U 本位合约到 CryptoGuard 长期监控池。只做监控，不做实盘。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "category": {"type": "string"}, "timeframes": {"type": "array", "items": {"type": "string"}}, "enabled": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "crypto_symbol_remove", "description": "从 CryptoGuard 产品池移除 symbol。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "crypto_symbol_pause", "description": "暂停某个 symbol 的定时分析。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "crypto_symbol_resume", "description": "恢复某个 symbol 的定时分析。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "crypto_symbol_list", "description": "列出 CryptoGuard 当前监控产品池。", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "crypto_analyze_symbol_once", "description": "临时分析一个 Binance U 本位合约，返回标准 GA decision JSON 和飞书卡片 JSON；不会加入长期监控。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "timeframes": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {"name": "crypto_create_opportunity_watch", "description": "从 GA decision 的等待条件创建机会监控。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "watch_condition": {"type": "object"}, "expire_minutes": {"type": "integer"}, "signal_id": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "crypto_create_paper_order_from_signal", "description": "从 signal 创建模拟盘订单；同一个 signal_id 幂等，不会重复创建。", "parameters": {"type": "object", "properties": {"signal_id": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "crypto_get_market_state", "description": "读取指定 symbol 的 MarketStateSnapshot。所有查询只使用 analysis_time_utc 前已收盘 K 线。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "timeframes": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {"name": "crypto_get_closed_candles", "description": "No-lookahead K 线查询工具，只返回 close_time <= analysis_time_utc 的已收盘 K 线。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "interval": {"type": "string"}, "analysis_time_utc": {"type": "integer"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "crypto_get_open_paper_positions", "description": "列出当前模拟盘 pending/open 订单。", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "crypto_review_trade", "description": "对已平仓模拟盘交易进行结构化复盘，补丁只进入 candidate。", "parameters": {"type": "object", "properties": {"trade_id": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "crypto_daily_review", "description": "执行每日模拟盘复盘：批量复盘已平仓交易、更新策略记忆、生成日报。", "parameters": {"type": "object", "properties": {"day_utc": {"type": "string", "description": "YYYY-MM-DD，可选，默认昨日 UTC"}}}}},
    {"type": "function", "function": {"name": "crypto_list_strategy_versions", "description": "列出策略版本，并由 GA/LLM 总结 active/candidate/shadow 状态。", "parameters": {"type": "object", "properties": {"strategy_name": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "crypto_update_opportunity_watches", "description": "手动更新机会监控，并由 GA/LLM 复核触发/失效/等待原因。", "parameters": {"type": "object", "properties": {"analysis_time_utc": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "crypto_run_shadow_test", "description": "运行候选策略影子测试，由 GA/LLM 复核是否样本不足、拒绝或可进入人工确认。", "parameters": {"type": "object", "properties": {"strategy_name": {"type": "string"}, "candidate_version": {"type": "string"}, "min_samples": {"type": "integer", "default": 30}}, "required": ["strategy_name", "candidate_version"]}}},
    {"type": "function", "function": {"name": "crypto_run_historical_replay", "description": "运行历史回放/回测，并由 GA/LLM 总结策略表现、市场状态和过拟合风险。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "interval": {"type": "string"}, "start_time": {"type": "integer"}, "end_time": {"type": "integer"}, "parquet_path": {"type": "string"}, "export_path": {"type": "string"}, "strategy_versions": {"type": "array", "items": {"type": "string"}}}, "required": ["symbol", "interval", "start_time", "end_time"]}}},
    {"type": "function", "function": {"name": "crypto_run_self_evolution", "description": "运行自进化闭环：复盘聚合、GA/LLM 生成 candidate patch、影子测试门禁；不会直接实盘。", "parameters": {"type": "object", "properties": {"strategy_name": {"type": "string", "default": "smc_pullback_long"}, "min_reviews": {"type": "integer", "default": 5}, "min_symbols": {"type": "integer", "default": 2}, "min_shadow_samples": {"type": "integer", "default": 30}}}}},
    {"type": "function", "function": {"name": "crypto_request_config_update", "description": "请求关键配置热更新，必须二次确认后才会生效并写入审计。", "parameters": {"type": "object", "properties": {"config_key": {"type": "string"}, "new_value": {}, "requested_by": {"type": "string"}, "request_text": {"type": "string"}}, "required": ["config_key", "new_value"]}}},
    {"type": "function", "function": {"name": "crypto_confirm_config_update", "description": "确认并应用待处理配置热更新，写入 config_hot_reload 和 runtime_config 审计。", "parameters": {"type": "object", "properties": {"change_id": {"type": "integer"}}, "required": ["change_id"]}}},
    {"type": "function", "function": {"name": "crypto_system_status", "description": "查看 CryptoGuard 系统状态、定时任务状态、队列积压、模拟盘数量和日志路径。", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "crypto_list_recent_errors", "description": "查看最近 agent_jobs 和 scheduler_runs 错误记录。", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}}},
    {"type": "function", "function": {"name": "crypto_paper_positions", "description": "查询模拟盘历史持仓记录，支持按币种和状态筛选。返回交易列表、胜率、累计盈亏。", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}, "symbol": {"type": "string", "description": "按币种筛选，如 BTCUSDT"}, "status": {"type": "string", "description": "open=持仓中, closed=已平仓, 不填=全部"}}}}},
]


def _install_handler_methods() -> None:
    try:
        from agent_loop import StepOutcome
        from ga import GenericAgentHandler
        from plugins.crypto_guard.tools import ga_crypto_tools as tools
    except Exception:
        return

    def _make(method_name: str):
        def _method(self, args, response):
            fn = getattr(tools, method_name)
            clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
            result = fn(**clean_args)
            yield json.dumps(result, ensure_ascii=False, indent=2)
            return StepOutcome(result, next_prompt=self._get_anchor_prompt(skip=args.get("_index", 0) > 0))

        return _method

    for schema in CRYPTO_TOOL_SCHEMAS:
        name = schema["function"]["name"]
        setattr(GenericAgentHandler, f"do_{name}", _make(name))


def _register_hooks() -> None:
    try:
        from plugins.hooks import register
    except Exception:
        return

    @register("agent_before")
    def _inject_tools(ctx: dict[str, Any]) -> dict[str, Any]:
        _install_handler_methods()
        tools_schema = ctx.get("tools_schema")
        if isinstance(tools_schema, list):
            existing = {item.get("function", {}).get("name") for item in tools_schema}
            for schema in CRYPTO_TOOL_SCHEMAS:
                if schema["function"]["name"] not in existing:
                    tools_schema.append(schema)
        return ctx


_register_hooks()
