# GA Master Controller 实现细节

## 1. 建议文件结构

```text
plugins/crypto_guard/ga_master/
  __init__.py
  controller.py
  context_builder.py
  skill_orchestrator.py
  decision_schema.py
  decision_persistence.py
  feishu_action_builder.py
  risk_gate.py
  report_adapter.py
```

## 2. Controller 伪代码

```python
class GAMasterController:
    def analyze_symbol(self, request: GAAnalysisRequest) -> GADecision:
        context = self.context_builder.build(request)
        skill_plan = self.plan_skills(context)
        skill_results = self.skill_orchestrator.run(skill_plan, context)
        reasoning = self.reason_multitimeframe(context, skill_results)
        risk_check = self.risk_gate.check(reasoning, context)
        decision = self.build_decision(context, skill_results, reasoning, risk_check)
        self.persistence.save(decision, context, skill_results)
        return decision
```

## 3. ContextBuilder 必须读取

- previous `analysis_state`
- active `opportunity_watch`
- open `paper_position`
- pending `paper_order`
- symbol profile
- skill_feedback_memory
- Redis latest price
- closed K line windows
- market regime

## 4. SkillOrchestrator 必须做

1. 加载 `skill.yaml`。
2. 读取 `prompt.md`。
3. 调用 `tools.py` 里的确定性工具。
4. 把 tool result 和 memory 交给 GA 解释。
5. 校验 `schema.json`。
6. 写入 `skill_execution_logs`。

## 5. RiskGate 必须检查

- `min_rr >= 2.0`
- `confidence >= 0.72`
- `trade_plan` 是否完整
- `invalid_level` 是否存在
- 止损和止盈是否存在
- 高周期方向是否支持
- 是否极端行情
- 反向证据是否过强
- 用户手动指令不能绕过风控

## 6. FeishuActionBuilder 规则

```python
if grade in ['D', 'C']:
    buttons = ['add_to_long_term_pool', 'ignore']
elif grade == 'B':
    buttons = ['add_to_opportunity_watch', 'add_to_long_term_pool', 'ignore']
elif grade in ['A', 'S'] and trade_plan_complete and risk_passed:
    buttons = ['add_to_paper_trading', 'add_to_opportunity_watch', 'add_to_long_term_pool', 'ignore']
elif grade in ['A', 'S']:
    buttons = ['add_to_opportunity_watch', 'add_to_long_term_pool', 'ignore']
```

禁止 D/C 级显示机会监控。

## 7. next_analysis_time

必须对齐 K 线边界：

```python
def get_next_closed_candle_analysis_time(now_utc, interval_minutes, delay_seconds=30):
    # 06:09:59 + 15m -> close 06:15:00 -> analysis 06:15:30
```

禁止使用当前时间作为“下一根 15m 已收盘确认”。
