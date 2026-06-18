# GA CryptoGuard 补充规格总结

## 0. 总体原则

本系统定位不变：

```text
不接实盘。
不执行真实交易。
不保存具备交易权限的 API Key。
所有开仓、平仓、止损、止盈、策略进化均只作用于模拟盘。
```

系统核心目标是打造一个：

```text
基于 GenericAgent / GA SOP 的自学习交易研究 Agent，
通过确定性计算工具 + GA 逻辑综合 + 模拟盘验证 + 飞书交互，
持续提升日内合约交易分析能力。
```

GA 不应该裸看 K 线自由判断。正确架构是：

```text
行情数据
  ↓
Pandas / NumPy / 本地计算工具做确定性预处理
  ↓
结构化市场特征
  ↓
GA 按 SOP 做逻辑综合、策略匹配、风险解释
  ↓
飞书推送 / 按钮回调 / 模拟盘 / 机会监控 / 长期产品池
  ↓
复盘归因 / 策略候选 / 影子测试 / 自进化
```

---

# 1. 飞书自然语言分析指令与按钮回调

## 1.1 用户发送分析指令

用户可以通过飞书发送自然语言，例如：

```text
分析一下 BTCUSDT
看一下 SOL 有没有机会
帮我看看 ETH 现在能不能做多
分析 WIFUSDT，短线有没有入场点
```

系统必须执行：

```text
1. 识别用户意图：ad_hoc_analysis
2. 识别产品 symbol
3. 检查产品是否在长期产品池
4. 如果不在，只进行临时分析，不自动加入长期监控
5. 拉取或读取缓存数据
6. 执行确定性分析模块
7. GA 综合输出分析结论
8. 飞书返回分析卡片
9. 根据分析结论提供按钮回调
```

---

## 1.2 分析后按钮回调

分析完成后，飞书卡片必须根据结论提供按钮。

基础按钮必须包括：

```text
[加入机会监控]
[加入长期产品池]
[忽略]
```

如果分析结果中存在完整且通过风控的交易计划，则额外提供：

```text
[加入模拟盘]
```

也就是说：

### 情况 A：有完整开单计划

必须满足：

```text
1. direction 存在：LONG / SHORT
2. entry_type 存在：market / limit / trigger
3. entry_price 或 trigger 条件存在
4. stop_loss 存在
5. take_profit 至少一个
6. RR >= 2
7. confidence >= 配置阈值，默认 0.72
8. 已经过风控层
9. 不违反高周期方向
10. 不处于极端行情禁止开仓状态
```

按钮：

```text
[加入模拟盘]
[加入机会监控]
[加入长期产品池]
[忽略]
```

### 情况 B：没有完整开单计划，但存在等待条件

例如：

```text
等待回踩
等待突破确认
等待 CVD 转强
等待 5m 反转
等待 BTC 环境改善
```

按钮：

```text
[加入机会监控]
[加入长期产品池]
[忽略]
```

### 情况 C：行情无优势 / 结构混乱

按钮：

```text
[加入长期产品池]
[忽略]
```

---

## 1.3 按钮回调行为

### 加入机会监控

创建 `opportunity_watch` 记录。

必须保存：

```text
symbol
direction
watch_reason
trigger_conditions
invalid_conditions
expires_at
source_analysis_id
status = active
```

触发后必须再次推送飞书。

失效或过期后必须更新状态：

```text
triggered
expired
invalidated
cancelled
```

---

### 加入长期产品池

写入或更新 `symbols` 表。

```text
symbol.enabled = true
symbol.source = user
symbol.category = custom / auto
symbol.default_timeframes = ["4h", "1h", "15m", "5m"]
```

后续定时任务自动覆盖该品种。

---

### 忽略

当前分析归档，不创建机会监控，不加入长期产品池，不创建模拟盘。

建议写入：

```text
ad_hoc_analyses.status = ignored
```

---

### 加入模拟盘

仅当分析结论包含完整 `trade_plan` 且通过风控时允许。

必须创建：

```text
paper_order
paper_position 或 pending order
signal
strategy_evaluation
```

如果风控不通过，即使用户点击，也必须拒绝并解释原因。

---

# 2. 缠论 / SMC / 订单流的确定性计算问题

## 2.1 允许引入轻量计算库

明确允许在 GA 工具层引入：

```text
Pandas
NumPy
本地技术指标计算函数
本地结构检测函数
轻量级成交量 / CVD / 分型 / 中枢 / FVG / OB 检测工具
```

目的：

```text
避免纯 LLM 对连续 K 线几何分割产生幻觉。
```

Codex 实现要求：

```text
LLM 不直接负责几何计算。
LLM 不直接从原始 K 线文本猜分型、中枢、FVG、OB。
LLM 只负责基于确定性工具输出做规则判定、语境综合、风险解释。
```

---

## 2.2 几何计算与逻辑判断的仲裁规则

当确定性计算引擎和 GA Skill 判断冲突时，必须按以下规则处理：

### 几何类结果：以计算引擎为准

包括：

```text
分型位置
笔的高低点
中枢上下沿
FVG 区间
OB 区间
前高前低
Swing High / Swing Low
BOS / CHoCH 的基础价格点位
ATR / RSI / MACD / CVD 数值
```

示例：

```text
计算引擎输出：中枢下沿 = 1950
LLM 判断：中枢下沿 = 1945

最终采用：1950
```

### 逻辑类结果：GA 可以综合判断

包括：

```text
是否结构破坏
是否假突破
是否适合开仓
是否只进入机会监控
是否趋势末期
是否风险过高
是否需要清仓
是否需要降级信号
```

但 GA 的逻辑判断必须引用确定性证据。

---

# 3. 日内策略的多周期框架

## 3.1 只做日内策略

当前系统不使用日线作为交易决策主链路。

日内分析框架固定为：

```text
4H      找方向
1H/15M  找趋势与结构
5M      找入场 / 反转 / 触发机会
```

日线可以作为背景参考或报表数据，但不作为默认交易决策权重核心。

---

## 3.2 “顺大逆小”的表达

默认分析逻辑：

```text
4H 是方向过滤器
1H / 15M 是趋势与 setup 判断
5M 是入场触发与反转机会
```

Codex 实现时可使用如下默认权重：

```yaml
intraday_timeframe_weights:
  4h:
    role: direction_filter
    weight: 0.35

  1h:
    role: trend_context
    weight: 0.25

  15m:
    role: setup_context
    weight: 0.20

  5m:
    role: entry_trigger
    weight: 0.20
```

更重要的规则：

```text
5M 只能触发入场，不能单独推翻 4H 方向。
如果 5M 有反转机会，但 4H 不支持，只能进入机会监控或低等级预警。
如果 4H 方向明确，1H/15M 趋势不支持，也不能直接开仓。
```

---

## 3.3 是否根据 ATR 动态调整权重

允许根据波动率动态调整，但 MVP 后续实现可以先配置化。

建议：

```text
高 ATR / 高波动：
    降低 5M 权重
    提高 4H 和 1H 权重
    降低模拟盘风险比例

低 ATR / 压缩震荡：
    降低趋势追单权重
    提高突破确认要求
```

配置示例：

```yaml
dynamic_weighting:
  enabled: true

  high_volatility:
    atr_percentile_threshold: 0.85
    adjust:
      4h: +0.05
      1h: +0.05
      15m: 0
      5m: -0.10

  low_volatility:
    atr_percentile_threshold: 0.20
    require_breakout_confirmation: true
```

---

# 4. 大周期收盘确认与预判开仓

## 4.1 不允许使用未收盘大周期 K 线确认方向

当 5M 出现入场信号时，如果 4H / 1H K 线尚未收盘，不能使用当前未收盘大周期 K 线作为最终结构确认。

必须使用：

```text
last_closed_4h_candle
last_closed_1h_candle
last_closed_15m_candle
```

禁止：

```text
用未收盘 4H K 线判断 4H 已经突破
用未收盘 1H K 线判断 1H 趋势已确认
```

---

## 4.2 5M 信号的作用

5M 可以用于：

```text
入场触发
短线反转提醒
机会监控触发
挂单触发判断
止损调整参考
```

5M 不可以单独用于：

```text
覆盖 4H 方向
覆盖 1H 趋势
绕过风控创建模拟盘订单
```

---

# 5. 模拟盘价格更新与成交假设

## 5.1 模拟盘价格同步频率

模拟盘价格同步频率固定为：

```text
每 3 分钟
```

用于：

```text
更新浮盈亏
更新 MFE / MAE
判断止盈
判断止损
判断挂单是否成交
判断机会监控是否触发
```

---

## 5.2 浮盈浮亏采用最新价格

浮盈亏计算采用：

```text
最新价格 latest_price
```

不使用加权移动平均价。

```text
unrealized_pnl = position_size * (latest_price - entry_price)
```

空单按相反方向计算。

---

## 5.3 市价模拟成交

如果是市价型模拟开仓：

```text
按下一根 K 线开盘价 ± 0.1% 滑点成交
```

默认滑点：

```yaml
paper_trading:
  market_slippage_pct: 0.001
```

多单：

```text
fill_price = next_candle.open * (1 + 0.001)
```

空单：

```text
fill_price = next_candle.open * (1 - 0.001)
```

---

## 5.4 高置信度挂单模拟成交

当 GA 判断置信度高，可以生成挂单计划。

挂单成交规则：

```text
后续价格更新时，检查 entry_price 是否落在该周期 K 线 high 和 low 范围内。
如果 low <= entry_price <= high，则视为挂单成交。
```

成交价：

```text
fill_price = entry_price
```

可选配置轻微滑点：

```yaml
paper_trading:
  limit_order_slippage_pct: 0.0002
```

默认可先使用：

```text
fill_price = entry_price
```

记录字段：

```text
fill_method = limit_range_touch
```

---

## 5.5 每小时推送必须包含交易计划和持仓情况

系统每小时摘要推送时，必须说明每个重点产品的：

```text
当前趋势判断
是否有交易计划
是否有机会监控
是否有模拟盘持仓
持仓方向
入场价
当前价
浮盈亏
止损价
止盈价
是否需要调整止损
是否需要平仓
```

---

# 6. 飞书推送频率、安全与失败重试

## 6.1 整点播报采用摘要合并

接受整点播报使用：

```text
摘要合并
飞书富文本卡片
附件或长卡片
```

不要对每个产品单独刷屏。

建议每小时推送一张汇总卡片：

```text
主流产品概览
当前持仓
机会监控
新触发信号
风险事件
需要用户确认的动作
```

---

## 6.2 静默期规则

系统需要设置静默期，但不能屏蔽关键交易事件。

### 可静默的消息

```text
普通重复预警
同币种同类型的低优先级提醒
加仓建议
C/D 级观察类提醒
重复结构提醒
```

默认静默期：

```yaml
feishu:
  quiet_period:
    normal_duplicate_alert_minutes: 5
```

### 不允许静默的消息

以下消息必须推送：

```text
开仓
平仓
调整止损
触发止损
触发止盈
强风控警报
机会监控触发
模拟盘订单成交
模拟盘订单失效
```

用户特别确认：

```text
加仓可以不推送。
调整止损、平仓、开仓都必须推送。
```

---

## 6.3 飞书失败不影响分析任务

如果飞书 API 临时不可用：

```text
分析任务不暂停
信号继续写库
模拟盘继续运行
预警消息进入本地队列
```

必须实现：

```text
SQLite alert queue
指数退避重试
最多重试 3 次
失败记录 alert_failure_log
```

建议表：

```sql
CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    symbol TEXT,
    priority INTEGER DEFAULT 5,
    payload_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alert_failure_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_outbox_id INTEGER,
    alert_type TEXT,
    symbol TEXT,
    error_message TEXT,
    retry_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

# 7. 自进化冷启动与影子模式

## 7.1 自进化触发后不得直接替换 active 策略

当系统因为：

```text
连续止损
回撤超过阈值
某类策略近期表现明显恶化
复盘发现共同亏损模式
```

生成策略修改建议时，必须创建：

```text
candidate strategy patch
```

不得直接覆盖 active 策略。

---

## 7.2 进化后进入观察 / 影子测试

用户确认：

```text
可以先测试后与之前的做对比，
看是否提高胜率或解决其他问题，
再恢复模拟盘开仓权限。
```

因此实现规则：

```text
新策略版本进入 shadow_testing。
candidate 策略并行运行。
接受 LLM Token 消耗翻倍。
candidate 策略先不直接创建模拟盘订单。
candidate 只记录“如果按新策略会产生什么信号”。
```

默认升级条件：

```text
至少产生 3 次有效模拟信号记录，作为最小观察门槛。
更推荐样本数 >= 30 后再正式升级。
```

对于被进化影响的策略：

```text
如果 active 策略触发严重回撤保护，可进入观察模式。
观察模式下只分析，不创建新的模拟盘开仓。
```

配置示例：

```yaml
evolution:
  shadow_testing_enabled: true
  allow_double_llm_cost: true
  min_observation_signals: 3
  preferred_promotion_sample_size: 30
  affected_strategy_observation_hours: 24
  auto_promote: false
```

---

## 7.3 策略恢复模拟盘开仓权限

candidate 策略只有在满足以下条件后，才能恢复模拟盘开仓权限：

```text
1. 样本数达到最小观察要求
2. 胜率或平均 R 优于原策略
3. 或明确解决了原策略的某类问题
4. 最大回撤未恶化
5. 未出现明显过拟合
6. 用户或管理员显式确认升级
```

---

# 8. 极端行情过滤与进化暂停

## 8.1 引入市场状态分类器

为避免把黑天鹅或极端行情误判为策略失效，必须引入简单市场状态分类器。

至少包含：

```text
ATR 分位数
资金费率突变
异常成交量
异常单根 K 线波幅
连续插针
流动性异常
```

市场状态：

```text
normal
high_volatility
extreme_volatility
low_liquidity
funding_shock
news_like_event
```

---

## 8.2 极端行情下暂停自进化触发

如果处于：

```text
extreme_volatility
funding_shock
news_like_event
low_liquidity
```

则：

```text
可以继续记录亏损
可以继续复盘
但暂停自动生成策略补丁
不因该阶段亏损直接判定策略失效
```

必须在 `trade_review` 中记录：

```text
market_regime_at_loss
evolution_trigger_allowed = false
```

---

# 9. 飞书指令与风控优先级

## 9.1 用户手动模拟开仓也必须经过风控

如果用户通过飞书发送：

```text
立即模拟开仓 BTC
帮我直接开多 ETH
```

系统不能绕过风控。

必须执行：

```text
1. 生成或读取当前 MarketStateSnapshot
2. 执行策略评估
3. 执行风控检查
4. 检查 RR >= 2
5. 检查 confidence >= 阈值
6. 检查高周期方向
7. 检查极端行情状态
8. 通过后才允许创建模拟盘订单
```

如果不通过，回复原因：

```text
当前不允许加入模拟盘，因为 RR 不足 / 置信度不足 / 高周期方向不支持 / 极端行情。
可以加入机会监控。
```

---

## 9.2 不允许人工指令绕过风控

明确规则：

```text
人工指令不能绕过风控。
飞书指令不能直接创建模拟盘订单。
所有模拟盘订单都必须通过风险层。
```

---

# 10. 配置热更新权限与审计

## 10.1 关键参数修改需要二次确认

通过飞书修改关键配置时，必须要求用户二次确认。

关键配置包括：

```text
风控参数
RR 阈值
confidence 阈值
产品池批量修改
策略启用 / 停用
模拟盘风险比例
静默期规则
自进化参数
```

流程：

```text
用户：把 BTC 的置信度阈值改成 0.65
系统：这是关键参数修改，请回复“确认”以执行。
用户：确认
系统：执行修改，写入审计日志，推送摘要。
```

---

## 10.2 所有热更新写入审计表

建议表：

```sql
CREATE TABLE IF NOT EXISTS config_hot_reload (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    requested_by TEXT,
    request_text TEXT,
    confirmation_required INTEGER DEFAULT 1,
    confirmed INTEGER DEFAULT 0,
    confirmed_at TEXT,
    status TEXT DEFAULT 'pending',
    applied_at TEXT,
    audit_summary TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

执行后必须向飞书推送审计摘要。

---

# 11. Codex 需要新增或修改的核心配置

建议新增或更新：

```yaml
trading_mode:
  live_trading_enabled: false
  paper_trading_enabled: true
  real_order_api_enabled: false

analysis:
  mode: intraday
  timeframes:
    direction: "4h"
    trend: ["1h", "15m"]
    entry: "5m"

  weights:
    4h: 0.35
    1h: 0.25
    15m: 0.20
    5m: 0.20

  require_closed_htf_candles: true

risk:
  min_rr: 2.0
  min_confidence_for_paper_order: 0.72
  allow_manual_bypass: false

paper_trading:
  price_update_interval_minutes: 3
  unrealized_pnl_price_source: "latest_price"
  market_fill_method: "next_candle_open_with_slippage"
  market_slippage_pct: 0.001
  limit_fill_method: "entry_price_between_high_low"
  limit_order_slippage_pct: 0.0

feishu:
  hourly_summary_enabled: true
  use_rich_card_summary: true
  quiet_period:
    normal_duplicate_alert_minutes: 5
    suppress_add_position_alerts: true
    never_silence:
      - open_position
      - close_position
      - stop_loss_adjustment
      - take_profit_hit
      - stop_loss_hit
      - risk_alert
      - opportunity_triggered
      - paper_order_filled

alerts:
  retry_enabled: true
  retry_max_attempts: 3
  retry_backoff: "exponential"
  store_failed_alerts: true

evolution:
  shadow_testing_enabled: true
  allow_double_llm_cost: true
  min_observation_signals: 3
  preferred_promotion_sample_size: 30
  affected_strategy_observation_hours: 24
  auto_promote: false
  pause_evolution_on_extreme_market: true

market_regime:
  classifier_enabled: true
  atr_percentile_extreme_threshold: 0.90
  funding_shock_detection: true
```

---

# 12. Codex 验收标准

Codex 实现后，必须满足以下验收条件。

## 12.1 飞书分析按钮验收

```text
用户发送“分析 BTCUSDT”
系统返回分析卡片
卡片包含按钮：
    加入机会监控
    加入长期产品池
    忽略
如果有完整交易计划，还包含：
    加入模拟盘
点击按钮后数据库状态正确变化
```

---

## 12.2 确定性计算验收

```text
缠论 / SMC / 价格行为 / 动能 / 订单流基础特征由工具计算
GA 不直接从原始 K 线自由生成几何点位
几何冲突时以计算引擎为准
逻辑判断必须引用结构化证据
```

---

## 12.3 多周期验收

```text
系统使用 4H -> 1H/15M -> 5M 日内链路
4H 找方向
1H/15M 找趋势
5M 找入场和反转机会
未收盘大周期 K 线不得作为确认依据
```

---

## 12.4 模拟盘验收

```text
价格每 3 分钟更新
浮盈亏使用最新价格
市价单按下一根 K 线开盘价 ±0.1% 滑点成交
高置信度挂单在 entry_price 落入 high/low 区间时成交
每小时摘要包含交易计划和持仓情况
```

---

## 12.5 飞书推送验收

```text
普通重复提醒遵守 5 分钟静默期
加仓提醒可以不推送
开仓 / 平仓 / 调整止损 / 止盈 / 止损必须推送
飞书失败时分析任务不暂停
失败消息进入 alert_outbox
最多重试 3 次
失败写入 alert_failure_log
```

---

## 12.6 自进化验收

```text
策略补丁不能直接覆盖 active 策略
candidate 策略进入 shadow_testing
接受双版本 GA 分析
candidate 初期只记录不创建模拟盘订单
至少 3 次观察信号后才允许考虑恢复模拟盘权限
推荐样本数达到 30 后再升级
极端行情下暂停自进化触发
```

---

## 12.7 风控验收

```text
用户手动发送“立即模拟开仓”也必须经过风控
RR < 2 不允许创建模拟盘订单
confidence 低于阈值不允许创建模拟盘订单
高周期不支持时不允许直接创建模拟盘订单
人工指令不能绕过风控
```

---

## 12.8 配置热更新验收

```text
关键参数修改需要二次确认
所有热更新写入 config_hot_reload
修改成功后推送飞书审计摘要
未确认前不得生效
```
