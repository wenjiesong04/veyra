# Runtime Architecture

## Status

`CURRENT APPROACH / PARTIALLY IMPLEMENTED`

## 目的

Runtime 负责可靠地让 Veyra 在正确时机重新评估状态。它不是认知本身，也不应把“固定频率调用模型”等同于持续理解。

## 唤醒来源

Veyra 应支持：

1. 用户消息或纠正立即唤醒；
2. trusted event 到达立即唤醒；
3. Commitment/deadline 到点唤醒；
4. ObservationRequest 到期或完成唤醒；
5. Evidence/Belief 过期或冲突唤醒；
6. Situation material change 唤醒；
7. 低频 heartbeat 负责防漏和运维维护。

Heartbeat 不能成为唯一触发器。没有变化、到期义务或 unresolved Information Need 时，不应每个 tick 盲目调用模型。

## 前台与后台

### 前台

用户消息链需要低延迟完成：

- normalize/dedupe；
- scope；
- understanding；
- context update；
- route/risk；
- response；
- effect verification；
- state/audit。

### 后台

后台循环处理：

- due observations；
- stale evidence；
- pending Agent/review；
- Situation reevaluation；
- suggestion timing；
- retention；
- bounded recovery。

长模型调用和外部观察不得阻塞整个 runtime tick。Reservation、generation fence、timeout、replay 和 crash recovery 应保持明确。

## 运行合同

- 所有后台步骤有界；
- 同一义务幂等或有明确 at-least-once 语义；
- 没有 durable admission 不报告已调度；
- 运行失败不覆盖旧健康事实；
- disabled 必须真正 no-op；
- status GET 不触发 probe、Agent、Git 或业务写；
- stale/unknown/degraded 分开；
- 每次运行说明处理、等待、失败与下一时间点。

## 当前开放问题

- 如何从固定 300 秒 tick 迁移为 deadline/event 驱动而不制造新竞态；
- 如何统一前台、event-driven 和 CognitiveBrief 的 wake/candidate 指标；
- 哪些 cognition 适合持续 worker，哪些适合按需计算；
- 多进程和桌面重启的 durable wake 恢复。
