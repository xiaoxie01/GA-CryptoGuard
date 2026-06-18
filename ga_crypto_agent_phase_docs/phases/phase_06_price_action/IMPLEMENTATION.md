# Phase 06 实现任务

## Codex 实现要求

请在现有 GA CryptoGuard 项目中实现本阶段功能。实现时必须遵守：

- 不接实盘。
- 用户消息优先。
- UTC 时间。
- 已收盘 K 线。
- 所有输出结构化保存。
- 任务幂等，可重试，可审计。

## 任务列表

### 1. 实现 Swing High/Low

实现该能力，并添加必要测试、日志和异常处理。

### 2. 识别 HH/HL/LH/LL

实现该能力，并添加必要测试、日志和异常处理。

### 3. 识别 BOS/CHoCH

实现该能力，并添加必要测试、日志和异常处理。

### 4. 识别突破回踩/假突破/区间

实现该能力，并添加必要测试、日志和异常处理。

### 5. 输出 price_action JSON

实现该能力，并添加必要测试、日志和异常处理。


## 推荐代码位置

```text
plugins/crypto_guard/
  scheduler/
  data/
  analysis/
  reasoning/
  paper/
  review/
  strategy/
  notify/
  storage/
  tools/
```

## 禁止事项

- 不要调用 Binance 实盘交易接口。
- 不要让后台任务复用用户 session。
- 不要直接修改 active 策略，除非当前阶段明确要求且通过验收流程。
- 不要在没有完整交易计划时创建模拟盘订单。
