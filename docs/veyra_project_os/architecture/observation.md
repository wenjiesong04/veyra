# Observation Architecture

## Status

`CURRENT APPROACH / FIRST TRUSTED PRODUCER VALIDATED`

## 目标

Observation 将“Veyra 需要知道什么”转换成一条有界、可信、可审计的信息获取过程。

它必须区分：

- Information Need：缺什么、为什么缺；
- ObservationRequest：server 准许怎样获取；
- Source Capability：哪个来源能够提供；
- Observation：来源返回的带 scope/time/provenance 数据；
- Evidence Admission：是否能够影响 Belief/Situation。

## 推荐流程

```text
Information Need candidate
        ↓ validation
Governed ObservationRequest
        ↓ source mapping
Allowlisted source capability
        ↓ receipt
Typed Observation
        ↓ admission
Evidence / Belief / Situation update
```

## Source 类别

V1 优先：

- 用户回答；
- Calendar；
- Email/Message 或用户授权的更新流；
- Weather/Public Web；
- local read-only Probe；
- bounded Agent research。

代码 Workspace Observer 保留为 Developer canary，不计入真实生活 source coverage。

## Source Registry 原则

Registry/adapter 是 current approach，不是产品概念。无论实现如何变化，都必须保持：

- server-owned registration；
- exact scope；
- source identity；
- data category；
- read/write capability；
- freshness/TTL；
- cost/rate/budget；
- consent；
- timeout/failure；
- typed receipt；
- no authority laundering。

## 模型边界

模型可以表达 evidence need，不得自由生成：

- filesystem path；
- arbitrary URL；
- shell command；
- tool args；
- credentials；
- external recipient；
- write operation。

Server 将需求映射到预注册能力。找不到合法来源时，应 ask、wait、expire 或明确 unavailable。

## 隐私与最小化

- 只收集与 Active Concern 有关的数据；
- 优先保留结构化结论和 provenance，而非无界原文；
- 敏感来源需要显式授权和用途；
- 用户能查看来源、暂停、撤销和删除；
- 不把一次授权扩展为全部历史或未来数据权限。

## 验收

每个新 source 必须证明：

- 一条真实正例；
- scope/consent 反例；
- stale/replay/tamper；
- timeout/unknown；
- 去重和 crash recovery；
- 不扩大 Route/Risk/Agent/Tool authority；
- 用户第一次因此获得的结果。
