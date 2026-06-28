# feat: pending order 生命周期管理

## Goal

解决模拟盘挂单堆积问题，增加 TTL、方向冲突取消、信号替换、入场失效等机制。

## Problem

当前 9 笔 pending 挂单，最老的已挂 97 小时。现有逻辑只做"价格触及→成交"，没有过期、冲突取消、替换机制。

## Requirements

### P0 - 基础清理

1. **TTL 过期机制**
   - `pending_order_expiry` job 定时扫描
   - 不同订单类型不同有效期：
     - market: 立即执行，不应长期 pending
     - limit pullback: 4-8 小时
     - breakout/retest: 2-4 小时
     - 高周期 swing: 最多 24 小时
     - 默认兜底: 8 小时
   - 超过 TTL 自动 `expired`

2. **方向冲突取消**
   - 若 pending SHORT，但最新 GA bias = bullish，且 signal_grade >= B → 自动 `conflict_cancelled`
   - 若 pending LONG，但最新 GA bias = bearish，且 signal_grade >= B → 自动 `conflict_cancelled`
   - neutral/mixed → 标记 `needs_recheck`，不立刻取消

3. **通知**
   - `paper_order_expired` 通知
   - `conflict_cancelled` 通知

### P1 - 智能验证

4. **revalidate_pending_orders()**
   - 用最新 GA decision 判断保留/取消/替换
   - 入场未触发但市场已走远（超过 1R 或 0.5-1 ATR）→ 取消

5. **新信号替换旧挂单**
   - 同 symbol + side 已有 pending
   - 新信号更晚且等级 >= 旧信号
   - 新 entry/SL 与旧计划差异明显
   - → 旧订单 `replaced`，保留新订单

### P2 - UX 改进

6. **订单创建时写入 expires_at**
7. **订单卡片增加"取消挂单/重新确认"按钮**
8. **GA context 包含 pending order 详情**

## 数据库变更

```sql
ALTER TABLE paper_orders ADD COLUMN expires_at TEXT;
ALTER TABLE paper_orders ADD COLUMN cancelled_at TEXT;
ALTER TABLE paper_orders ADD COLUMN cancel_reason TEXT;
ALTER TABLE paper_orders ADD COLUMN invalidated_by_ga_decision_id INTEGER;
```

新状态：`expired`, `cancelled`, `invalidated`, `conflict_cancelled`, `replaced`

## 立即清理建议

当前 9 笔 pending：
- #21 XRPUSDT 97h → 取消
- #47 ADAUSDT 45h → 取消
- #59 ETHUSDT 21h → 重新确认
- #60-#63 15-16h → 重新确认
- #64 DOGEUSDT 9h → 观察
- #66 BNBUSDT 1.4h → 保留

## Acceptance Criteria

- [ ] 超过 24h 的 pending 自动 expired
- [ ] 方向冲突的 pending 自动 conflict_cancelled
- [ ] 发送相关通知
- [ ] 新订单有 expires_at
- [ ] 测试通过
