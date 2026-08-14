# Scheduler Architecture

## Status

`PARTIALLY IMPLEMENTED / PRODUCT TIMING UNVALIDATED`

## 目的

Scheduler 决定什么时候重新观察、重新理解或触达用户。它不决定事实，也不能以“定时运行”冒充主动认知。

## 调度信号

- event arrival；
- deadline；
- next observation time；
- evidence expiry/max staleness；
- unresolved Information Need；
- material change；
- user interaction；
- retry/cooldown；
- low-frequency heartbeat。

## 优先级因素

- Concern importance；
- urgency/deadline；
- risk；
- information value；
- evidence quality；
- cost/budget；
- owner fairness；
- recent interruption；
- quiet hours；
- historical usefulness；
- duplicate/suppression state。

## 关键规则

1. Hard deadline 不应被低价值排序饿死。
2. Future observation time 不应被提前执行，除非出现更高优先级变化。
3. Conflict 需要明确 cadence，不能每个 heartbeat 无限重试。
4. Unknown value 不等于零，也不能自动最高优先。
5. 不可刷新义务需要 terminal/archive/review 语义，不能永久入队。
6. 同一 due generation 成功消费后不能每 tick 重复。
7. transient failure、permanent unsupported 和 authority denied 必须区分。
8. Scheduler 失败不能伪装成 `silent` 或 `success`。

## 模型调用预算

模型只在以下情况下运行：

- 有新 material evidence；
- Information Need 状态改变；
- Situation 到达决策点；
- 用户输入需要更新持续理解；
- 低频质量复盘明确到期。

维护型 heartbeat 不应默认触发完整认知模型调用。

## 产品验收

除了 deterministic virtual-clock 测试，还需要真实使用验证：

- 是否漏掉重要变化；
- 是否在错误时间打扰；
- 是否重复；
- 是否过度保守；
- 是否因预算/故障沉默；
- 同一用户多个 Concern 是否公平；
- 两周后 timing 是否因反馈改善。
