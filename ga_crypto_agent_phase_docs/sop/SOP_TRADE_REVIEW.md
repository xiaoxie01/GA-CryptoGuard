# SOP 交易复盘

## 输入

- trade_id
- 开单时 snapshot
- 交易路径
- 平仓原因
- 策略版本

## 步骤

1. 读取开单时 MarketStateSnapshot。
2. 读取持仓过程价格路径。
3. 计算 MFE / MAE / Entry Efficiency / Exit Efficiency。
4. 对照开单理由逐条验证。
5. 主动寻找忽略的反向证据。
6. 分类亏损或盈利原因。
7. 生成改进建议。
8. 如有必要，生成 candidate strategy patch。

## 输出

必须符合 `schemas/trade_review.schema.json`。
