# GA 动态 Skill 合同

## 1. Skill 不是单纯代码

每个 Skill 都必须是：

```text
Prompt SOP + Deterministic Tools + Output Schema + Feedback Memory + Evolution Rule
```

## 2. Skill 标准目录

```text
plugins/crypto_guard/skills/{skill_name}/
  skill.yaml
  prompt.md
  tools.py
  schema.json
  feedback_rules.yaml
```

## 3. Skill 执行顺序

```text
GA Master Controller
  ↓
SkillOrchestrator loads skill.yaml
  ↓
读取 skill memory
  ↓
调用 tools.py 计算客观事实
  ↓
GA 根据 prompt.md 解释事实
  ↓
输出 SkillResult
  ↓
schema 校验
  ↓
写 skill_execution_logs
  ↓
返回 GA Master Controller
```

## 4. 工具层禁止事项

`tools.py` 禁止输出：

- `signal_grade`
- `create_paper_order`
- `final_decision`
- `feishu_buttons`
- `opportunity_watch_recommended`

工具层只输出事实，例如：

- swing points
- BOS / CHoCH candidates
- FVG ranges
- order blocks
- CVD slope
- RSI slope
- Zhongshu ranges
- divergence metrics

## 5. GA Skill 可以判断

GA Skill 可以基于工具事实判断：

- 结构含义
- 信号可靠性
- 风险
- 是否需要等待确认
- 是否 degraded
- confidence

但最终交易结论仍然只能由 GA Master Controller 输出。

## 6. Skill 自进化

每日复盘和交易复盘可以写入 `skill_feedback_memory`。

Skill patch 可以修改：

- prompt
- tool 参数
- confidence 规则
- 过滤条件

但必须：

```text
candidate -> shadow_testing -> 用户确认 -> active
```

不能自动覆盖 active。
