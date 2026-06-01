# 05. 市场分析 SOP 设计

## 1. SOP 总原则

GA 采用 SOP 自动化，因此交易能力也必须 SOP 化。

每个分析能力拆成：

```text
数据输入 → 确定性工具识别特征 → SOP 步骤 → 标准 JSON 输出 → 策略评分 → 复盘反馈
```

代码负责看见事实，GA 负责组织事实、交叉验证、解释和迭代。

## 2. 总 SOP：多周期市场分析

```text
SOP_MULTI_TIMEFRAME_MARKET_ANALYSIS

Input:
- symbol
- analysis_time_utc
- mode: scheduled / ad_hoc / opportunity_watch / paper_review
- timeframes: 1D, 4H, 1H, 15m, 5m/3m

Step 1: 数据完整性检查
- 检查 K 线是否完整。
- 检查是否只使用已收盘 K 线。
- 检查是否有缺口。
- 检查是否有未来函数风险。

Step 2: 高周期背景分析
- 读取 1D profile。
- 读取 4H profile。
- 判断大方向、趋势阶段、关键区域。

Step 3: 主周期结构分析
- 对 1H / 15m 执行：
  - 价格行为 SOP。
  - SMC SOP。
  - 动能 SOP。
  - 缠论 SOP，第二阶段实现。

Step 4: 低周期触发分析
- 对 5m / 3m 执行：
  - 订单流 SOP。
  - 入场触发判断。
  - 是否追价。
  - 是否等待回踩。

Step 5: 反向证据检查
- 主动寻找反对做多/做空的证据。
- 输出 contradiction_level。

Step 6: 趋势阶段判断
- early / middle / late / range / transition。

Step 7: 策略匹配
- 匹配 strategy templates。
- 输出策略评分。

Step 8: 动作决策
- trade_plan_available。
- wait_for_pullback。
- wait_for_breakout。
- monitor_only。
- no_edge。

Step 9: 输出标准 GA decision JSON。
```

## 3. 价格行为 SOP

### 3.1 工具输出

- swing_highs。
- swing_lows。
- HH / HL / LH / LL。
- BOS。
- CHoCH。
- range。
- breakout。
- retest。
- fake breakout。
- support / resistance。

### 3.2 SOP

```text
SOP_PRICE_ACTION_ANALYSIS

1. 获取指定周期 K 线。
2. 识别 swing highs / lows。
3. 判断结构序列：HH/HL、LH/LL 或无序震荡。
4. 判断最近结构事件：BOS、CHoCH、fakeout、breakout retest。
5. 标记关键价格区。
6. 判断当前价格位置。
7. 输出结构方向、失效点和置信度。
```

### 3.3 输出示例

```json
{
  "module": "price_action",
  "market_structure": "bullish",
  "swing_sequence": "HH_HL",
  "last_event": "bullish_bos",
  "range_status": "breakout_retest",
  "key_levels": {
    "support": [68120, 67680],
    "resistance": [69200, 69850]
  },
  "entry_context": "waiting_for_retest",
  "invalid_level": 68120,
  "confidence": 0.72
}
```

## 4. SMC SOP

### 4.1 工具输出

- liquidity sweep。
- equal high / equal low。
- FVG。
- order block。
- premium / discount。
- mitigation。
- BOS / CHoCH。

### 4.2 SOP

```text
SOP_SMC_ANALYSIS

1. 识别外部流动性：前高、前低、等高、等低。
2. 检查是否发生 sweep high / sweep low。
3. 检查扫流动性后是否回收。
4. 检查是否出现 CHoCH / BOS。
5. 查找 FVG / OB 区域。
6. 判断价格处于 premium 还是 discount。
7. 判断是否存在可等待的回踩区域。
8. 输出 SMC 方向倾向和等待条件。
```

### 4.3 输出示例

```json
{
  "module": "smc",
  "liquidity": {
    "last_event": "sell_side_liquidity_sweep",
    "reclaimed": true
  },
  "structure_shift": {
    "choch": true,
    "bos": false,
    "direction": "bullish"
  },
  "fvg": {
    "exists": true,
    "direction": "bullish",
    "range": [68200, 68480],
    "status": "unfilled"
  },
  "order_block": {
    "exists": true,
    "direction": "bullish",
    "range": [67880, 68150],
    "mitigated": false
  },
  "premium_discount": "discount",
  "setup": "bullish_reversal_after_sweep",
  "confidence": 0.74
}
```

## 5. 订单流 SOP

### 5.1 工具输出

- CVD。
- CVD slope。
- aggressive_buy_ratio。
- aggressive_sell_ratio。
- large_trade_bias。
- delta divergence。
- volume impulse。

### 5.2 SOP

```text
SOP_ORDER_FLOW_ANALYSIS

1. 读取最近 3m / 5m / 15m aggTrade 数据。
2. 计算主动买入和主动卖出比例。
3. 计算 CVD 方向和斜率。
4. 检查价格-CVD 背离。
5. 判断成交冲击方向。
6. 与 SMC / 价格行为结构交叉验证。
7. 输出订单流确认程度。
```

### 5.3 输出示例

```json
{
  "module": "order_flow",
  "cvd_slope": "up",
  "aggressive_buy_ratio": 0.62,
  "large_trade_bias": "buy",
  "delta_divergence": false,
  "volume_impulse": true,
  "flow_confirmation": "supports_long",
  "confidence": 0.69
}
```

## 6. 动能 SOP

### 6.1 工具输出

- ROC。
- RSI slope。
- MACD histogram expansion。
- ATR expansion。
- volume impulse。
- candle body expansion。
- pullback strength。
- CVD slope。

### 6.2 SOP

```text
SOP_MOMENTUM_ANALYSIS

1. 判断价格动能。
2. 判断成交量动能。
3. 判断波动动能。
4. 判断指标动能。
5. 判断订单流动能。
6. 判断动能质量：healthy / extended / exhausted / divergent。
7. 输出 momentum score。
```

### 6.3 输出示例

```json
{
  "module": "momentum",
  "direction": "bullish",
  "momentum_score": 78,
  "quality": "strong_but_extended",
  "price_momentum": "expanding",
  "volume_confirmed": true,
  "atr_state": "expanding",
  "divergence": false,
  "risk": "short_term_overextended"
}
```

## 7. 趋势阶段 SOP

### 7.1 状态

- early：趋势初期。
- middle：趋势中期。
- late：趋势末期。
- range：震荡。
- transition：转换期。

### 7.2 SOP

```text
SOP_TREND_STAGE_ANALYSIS

1. 读取 1D / 4H / 1H 背景。
2. 判断是否从震荡进入方向性突破。
3. 判断结构是否形成稳定 HH/HL 或 LH/LL。
4. 判断动能状态：刚开始扩张、健康延续、加速、衰竭。
5. 判断末端特征：背驰、放量滞涨、资金费率极端、liquidity grab 失败。
6. 输出趋势阶段和主风险。
```

## 8. 缠论 SOP

缠论模块建议第二阶段实现。

### 8.1 工具输出

- K 线包含关系处理结果。
- 顶底分型。
- 笔。
- 线段。
- 中枢。
- 背驰。
- 买卖点候选。

### 8.2 SOP

```text
SOP_CHANLUN_ANALYSIS

1. 读取指定周期 K 线。
2. 处理包含关系。
3. 识别顶底分型。
4. 生成笔。
5. 生成线段。
6. 识别中枢。
7. 判断背驰。
8. 判断一买/二买/三买或一卖/二卖/三卖候选。
9. 输出结构化结论。
```

### 8.3 输出示例

```json
{
  "module": "chanlun",
  "trend_direction": "up",
  "current_structure": "pullback_after_zhongshu_breakout",
  "zhongshu": {
    "exists": true,
    "range_low": 68000,
    "range_high": 68800,
    "position_of_price": "above"
  },
  "bi": {
    "current_direction": "down",
    "strength": "weak"
  },
  "divergence": {
    "exists": false,
    "type": null
  },
  "signal": "class_2_buy_candidate",
  "confidence": 0.68
}
```

## 9. 反向证据机制

每次分析必须输出：

```json
{
  "bullish_evidence": [],
  "bearish_evidence": [],
  "neutral_or_risk_evidence": [],
  "contradiction_level": "low|medium|high"
}
```

如果 contradiction_level 为 high，则不得直接创建模拟盘，只能输出 monitor_only 或 no_edge。
