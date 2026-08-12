# Veyra Phase Cognitive Awakening 开发路线

> 历史设计与接受标准说明：本文保留本阶段开始时的基线和详细契约，不是当前运行真值。当前项目状态、下一步优先级和开发流程统一以 [README_Veyra.md](./README_Veyra.md) 为入口，并在每次工作前重新核对 Git、代码、测试和 live revision。
>
> 分支：`cognitive-awakening`
>
> 基线 commit：`8a8a3d2`
>
> 文档状态：`TARGET / DEVELOPMENT PLAN`
>
> 基线说明：本文中的运行数字是本轮开发开始前的实测快照。它们证明现有底座在运行，也证明认知价值闭环尚未完成；后续合成 smoke 不得改写这项事实。

## 1. 产品命题

Veyra 的定位不是第二个聊天大脑，也不是 OpenClaw 的改版，而是人和执行 Agent 之间的**认知控制面 / Cognitive OS**：维护世界状态，形成受证据约束的注意力判断，把可解释建议交给用户，再从明确反馈中认识自己的有效范围。

安全治理不是本阶段要削弱的负担，而是认知主动性能够存在的地基。Guardian、Verifier、Tool Proxy、审批、审计、恢复和权限边界继续保持权威；本阶段只验证一件新事情：

> Veyra 能否在不扩大权限的前提下，持续发现一个值得关注的变化，说明为什么，并通过用户反馈逐步校准自己。

这比继续增加 gate 更直接地验证产品价值，但不以绕过 gate 为代价。

### 1.1 阶段成功的含义

阶段成功不等于“后台模型开始定时思考”，也不等于“生成过一条建议”。成功至少同时满足：

1. 信号来自可信、结构化、owner-bound 的观察，而不是自由文本关键词或模型自信度；
2. 多次证据能形成可回放的 Attention Hypothesis；
3. 建议进入现有 SuggestionOutbox 沙箱，用户明确 opt-in，且每天最多一条；
4. 用户能给出分类反馈，反馈精确绑定到建议及其证据版本；
5. Self Model 只描述已观察到的表现，不猜“准确率”，也不自动改策略；
6. 全链路不产生执行、授权、路由、风险等级或外部推送变化。

## 2. 本轮开始前的真实基线

状态词：`CURRENT` 表示代码或落盘状态存在；`PARTIAL` 表示只覆盖子能力；`TARGET` 表示本阶段目标；`PENDING` 表示尚无真实用户价值证据。`VERIFIED` 只用于可由 smoke/gate 证明的治理不变量，不用于描述关系质量、建议价值或“懂用户”。

| 证据面 | 本轮开始前实测 | 结论 |
|---|---:|---|
| Cognitive Loop | 29 cycles；25 observed；0 candidates；25 个 observed 全部为 `quiet` | `CURRENT / SHADOW`：bounded model 已在只读循环中拥有发言位，但尚未形成价值候选或出口 |
| Belief | 250 claims；223 stale | `CURRENT / PARTIAL`：能落盘和过期，但保鲜预算未与价值相连 |
| GeneralSituation | 4 个，全部为 `context_only` | `CURRENT / PARTIAL`：有结构化聚合底座，没有 Attention Hypothesis 产品证明 |
| SuggestionOutbox | 0 proposal；默认 `record_only` | `CURRENT / EMPTY`：有既有出口容器，没有真实建议样本 |
| Feedback | 0 | `PENDING`：没有 usefulness、频率或证据质量反馈 |

因此当前不能宣称：Veyra 已会主动发现价值、已形成建议闭环、已学习用户反馈、已具备 Self Model，或已获得“认知觉醒”。这组基线的含义恰好相反：基础设施已存在，但用户价值验证仍为 `PENDING`。

### 2.1 正确解读现有 Cognitive Loop

`runtime/read_only_cognitive_loop.py` 已经是 **bounded model-in-the-loop**，不能描述为“模型完全不在循环”：

- server 先准备 exact-owner、只读、cached views；模型看不到可自由调用的 locator；
- 模型可以提出 observation plan，并生成 CognitiveBrief 的 `summary_if_asked`、`material_changes`、unknowns 与 material-change hypothesis；
- 模型产物保持 `epistemic_status: hypothesis`、`is_fact: false`；
- 模型不能执行工具或 probe，不能改变 Route、风险、权限，也不能通知用户。

所以 29 cycles / 25 observed / 0 candidate、且 25 个 observed 全部 quiet 的真实含义是：**模型已有受限发言位，但它的候选尚未安全接入 Attention Hypothesis 与 Suggestion Sandbox，也尚未证明能产生用户价值。** 下一刀不是再造一个后台思考循环，而是把现有 CognitiveBrief 的候选接入证据约束的闭环。

主要证据锚点：

- `state/runtime/cognitive_loop_state.json`
- `state/local/belief_state.json`
- `state/runtime/general_situation_state.json`
- `state/runtime/suggestion_outbox.json`
- `state/runtime/learning_calibration_state.json`
- `runtime/read_only_cognitive_loop.py`
- `runtime/general_situation_runtime.py`
- `awareness/general_attention_scheduler.py`
- `runtime/suggestion_outbox.py`
- `core/learning_record.py`

## 3. C1-C5 与 Self Model 分层状态

| 层 | 产品责任 | 当前状态 | 本阶段目标 |
|---|---|---|---|
| C1 World Model | 在 instant / task / identity 三个时间尺度维护可追溯状态 | `PARTIAL`：instant 与 task 已有多种素材；identity 仍稀薄；Belief 大量 stale | 为第一切片提供 owner-bound、typed、可验证的观察，并明确各时间尺度的证据与 TTL |
| C2 Belief Engine | 管理 claim 的来源、时效、置信边界与刷新 | `PARTIAL`：有 TTL、stale、source trust；没有 Belief Economy | 第一切片先完成 epistemic/refresh hygiene；完整 Economy 放第二切片 |
| C3 Attention Hypothesis | 把 CognitiveBrief 的候选绑定到可回放证据，并管理注意力准入 | `TARGET`：GeneralSituation 与 GeneralAttention 是确定性素材，不是认知本身，也不是已完成的假设生命周期 | 实现 `candidate → accumulating → confirmed / contradicted / expired`，让 readiness 只做证据外壳 |
| C4 Reflection Engine | 形成 `summary_if_asked`，并用相对前次的证据化变化表达 `why_now` | `SHADOW / PARTIAL`：bounded model 已产生只读 CognitiveBrief，但 25 个 observed 全部 quiet | 复用现有 CognitiveBrief；把有证据的新变化安全接入 hypothesis/sandbox，不再造自由运行模型循环 |
| C5 Suggestion Loop | 发现 → 建议 → 反馈 → 学习 | `PENDING`：Outbox 存在但 0 proposal、0 feedback | 复用 Outbox 建立 opt-in、每日一条的 Suggestion Sandbox |
| Self Model | 描述 Veyra 在不同域、规则与版本上的实际表现 | `TARGET`：当前没有可用样本 | 从显式分类反馈生成描述性校准；样本不足或无真值时明确 `unknown` |

这些层不是六条并行重写线。第一阶段只做一条贯穿 C1、C3、C4、C5 和 Self Model 的最小纵向切片；C2 的完整经济模型随后进入第二切片；C4 复用现有 bounded CognitiveBrief，不另建平行认知循环。

### 3.1 C1 的三种时间尺度

| 时间尺度 | 典型内容 | 更新与证据边界 |
|---|---|---|
| instant | 当前 runtime、资源、连接、最新 typed event | 秒到小时；短 TTL；只描述当前可验证观察 |
| task | Goal、Commitment、项目状态、跨事件 Situation | 小时到周；必须绑定 owner、结构化 task/project anchor 与 revision |
| identity | 用户显式偏好、稳定约束、长期纠正 | 周到月；以用户显式陈述和多次一致证据为主，不能从单轮情绪或模型印象猜测 |

三种时间尺度可以互相提供上下文，但不能互相冒充。例如 instant 的一次失败不能直接写成 identity 层的“用户习惯”，identity 的偏好也不能把缺失的 instant 事实补成已观察。

### 3.2 本分支实现检查点

截至本分支当前实现，以下仅表示代码与本地契约验证状态，不表示真实用户价值：

- `IMPLEMENTED / LOCAL VALIDATED`：durable AttentionHypothesis 状态与 `candidate → accumulating → confirmed` 三阶段；输入来自现有 GeneralSituation / GeneralAttention，重复证据纯重放，model confidence 不参与，状态损坏 fail closed；
- `IMPLEMENTED / LOCAL VALIDATED`：hypothesis identity 绑定不可变 GeneralSituation lineage；mutable session/common-anchor 投影不会分叉 identity；每次准入都核验 durable parent head、精确 child revision/digest、GeneralAttention scorer 与依赖 revision；
- `IMPLEMENTED / LOCAL VALIDATED`：结构化 evidence diversity 由服务端从 exact child refs 解析 producer、fact kind 与 UTC 时间桶；同一 child stream 的新 revision 只替换旧 revision，倒序 revision 或同 revision/digest 冲突 fail closed；
- `IMPLEMENTED / LOCAL VALIDATED`：confirmed surface 精确引用 hypothesis id/revision/ruleset/readiness semantics，并进入现有 SuggestionOutbox；
- `IMPLEMENTED / LOCAL VALIDATED`：Suggestion Sandbox 默认关闭、exact owner/session opt-in、`advise_only` 才可入 Console；按 owner 的显式本地时区自然日预算硬限制为 0 或 1；
- `IMPLEMENTED / LOCAL VALIDATED`：五类显式反馈、幂等与显式更正、exact proposal revision 绑定，以及只描述 usefulness 的校准摘要；
- `IMPLEMENTED / LOCAL VALIDATED`：Console 展示 hypothesis 阶段、Cognitive Loop 过度保守指标、Sandbox 开关、五类反馈与 descriptive calibration；
- `NOT IMPLEMENTED`：CognitiveBrief 到 AttentionHypothesis 的安全桥、`contradicted / expired` 生命周期、完整 `say / ask / wait / silent` disposition、按 domain/ruleset 分段 Self Model；
- `NOT IMPLEMENTED`：Belief Economy、自动策略调整、外部主动推送；
- `USER VALUE VALIDATION PENDING`：尚无真实 proposal 与人工 usefulness 样本，不能宣称 Veyra 已产生不可替代的主动价值。

### 3.3 2026-08-03 分支验收快照

以下是当前 `cognitive-awakening` 工作树的验证证据，不把合成样本提升为产品价值：

- `LOCAL AUTOMATED VALIDATED`：Conda `veyra` 下完整 Python gate `129/129` 通过；Route 非弱化矩阵覆盖 756 组 populated/corrupt Situation、AttentionHypothesis、Suggestion、协作与扩展状态；AttentionHypothesis 定向 smoke `21/21`、结构化观察控制面 `10/10`，Suggestion Sandbox、GeneralSituation + Suggestion、状态布局/完整性/隔离和 Python compileall 均通过；
- `FRONTEND BUILD VALIDATED`：Console 的 TypeScript 与 Vite 正式构建通过，生成 `index-BlYKp2h4.css` 与 `index-DKX8ECuz.js`；Codex 内置浏览器阻止访问 loopback，因此本轮不把实际可视渲染标记为已验证；
- `LOCAL LIVE VALIDATED`：LaunchAgent 使用 `/opt/anaconda3/envs/veyra/bin/python`（Python 3.11.15）重启，Veyra PID `80863 → 64820`，OpenClaw PID 保持 `58174`；新 Attention、Suggestion、Calibration 与 Cognitive Loop GET 均返回 HTTP 200；
- `LIVE BOUNDARY OBSERVED`：Suggestion 保持 `record_only / external_delivery_enabled=false`，live `hypothesis_count=0 / proposal_count=0`；相关四个认知状态文件在整组只读 GET 前后 SHA-256 不变，通用 `/state` 不暴露 AttentionHypothesis 或 SuggestionOutbox 私有状态；
- `LIVE DEGRADED / NOT A REGRESSION CLAIM`：`/health=degraded`，当前四项提示来自既有 Memory fallback、无本进程 Feishu 入站、历史 stale review 与 stale Belief；`/agent/status` 是 `snapshot_stale`，不能据此宣称当前 Agent 连接已重新验证；
- `NONBLOCKING P2`：Attention/Outbox 到达 2000 条上限后仍缺少淘汰策略；GeneralSituation 的完整语义级持久状态验证仍是既有防御纵深债务；
- `USER VALUE VALIDATION PENDING`：真实运行尚未产生 hypothesis 或 proposal，也没有本人 usefulness 反馈。上述验证只证明链路和治理边界，不证明“Veyra 已懂用户”。

## 4. 第一纵向切片

```text
trusted structured observations
              ↓
GeneralSituation / GeneralAttention
      (evidence/readiness shell)
              ↓
bounded CognitiveBrief candidate
(summary_if_asked / material_changes)
              ↓
AttentionHypothesis
(candidate → accumulating → confirmed)
              ↓
existing SuggestionOutbox Sandbox
(explicit opt-in, daily budget = 1)
              ↓
explicit categorical feedback
              ↓
descriptive self-calibration
```

### 4.1 Trusted structured observations

第一切片只接受注册过的 typed observation producer。每条证据必须包含精确 `user_id`、`session_id`、不可变事件引用、producer/ruleset 版本、时间与结构化 `fact_kind`。

以下内容不能作为确认证据：

- 聊天自由文本的关键词命中；
- 模型生成的相关性、因果或 confidence；
- 缺失 owner/session 的记录；
- 无法解析、已过期、revision 或 digest 不一致的引用；
- 同一事件、同一 producer/事实/时间桶的重复 tick。

第一切片始终要求 exact owner。非 durable anchor 只能在 exact session 内聚合；已有跨会话路径只允许依赖该 owner 拥有、仍有效且结构化的 Goal/Commitment 锚点，并由独立验收覆盖。session scope 是 lineage 的可变投影，不进入 hypothesis 的稳定 identity。

### 4.2 GeneralSituation / GeneralAttention

复用现有 `GeneralSituationRuntime` 和 `GeneralAttentionScheduler`，不另造第二套 Situation 或打分器：

- GeneralSituation 只聚合拥有共同结构化锚点的独立事件；
- parent 保持 `causality_asserted: false`；
- GeneralAttention 的未知分量保持 unknown，不能由文本或模型补值；
- 少于两个独立、当前有效的事件时不得进入确认路径；
- scorer 输出只表示注意力资格，不表示世界事实。

这套确定性 readiness 是**证据与准入外壳**，不是 cognition。它适合回答“证据是否足以让候选继续流动”，不负责独自回答“这件事意味着什么、为什么现在值得告诉用户”。如果继续堆规则权重并把分数包装成理解，只会再增加一层 gate，而不会产生认知价值。

### 4.3 Attention Hypothesis

建议使用独立、durable、owner-scoped 的 `attention_hypothesis_state`，避免把后台长期假设混入前台 turn focus。

最小语义：

```json
{
  "hypothesis_id": "stable_digest",
  "identity": {
    "general_situation_id": "...",
    "user_id": "...",
    "workspace_anchor_key": "...",
    "primary_anchor_key": "...",
    "general_attention_scorer_version": "...",
    "ruleset_version": "..."
  },
  "session_scope_keys": ["mutable-owner-session-projection"],
  "status": "candidate|accumulating|confirmed|contradicted|expired",
  "epistemic_status": "hypothesis",
  "is_fact": false,
  "causality_asserted": false,
  "evidence_refs": [],
  "readiness": {
    "value": 0.0,
    "semantics": "attention_readiness_not_truth_probability",
    "distinct_event_count": 0,
    "temporal_bucket_count": 0
  },
  "rule_binding": {"rule_id": "...", "ruleset_version": "..."},
  "cognitive_binding": {
    "cognitive_cycle_id": null,
    "world_digest": null,
    "brief_digest": null,
    "material_change_digest": null,
    "model_binding": null,
    "config_generation": null
  },
  "revision": 1
}
```

`cognitive_binding` 在纯 structured-observation hypothesis 中保持 `null`；一旦候选来自 CognitiveBrief，上述字段必须全部由服务端精确绑定并校验。缺少 cycle、world、brief、material-change 或 model/config 版本绑定的模型候选，不得进入 Sandbox。

`hypothesis_id` 必须由 schema、不可变 GeneralSituation lineage、owner、workspace/primary anchor 与 scorer/ruleset 版本稳定派生；会随 parent 演化的 session scope 和 common-anchor 集合不能参与 identity。Evidence accumulation 必须满足：

1. 同一不可变 child ref 重放时幂等；
2. 同 producer + fact kind + 时间桶的重复观察不能提高 diversity/readiness，重复频率不等于更多独立证据；
3. support 与 contradiction 分开累计；
4. strength 只能来自版本化服务端规则；**model confidence 的贡献恒为 0**；
5. `confirmed` 至少要求两个独立事件，并满足配置化的 producer 或时间跨度多样性；
6. 新的反证可以将假设降为 `contradicted`，过期证据不能继续维持 readiness。

Attention readiness 不是事实概率。即使 `confirmed`，该记录仍然：

- 不是事实；
- 不证明因果；
- 不提供执行、工具、授权、路由或风险变更权限；
- 只获得“可以进入建议资格检查”的权利。

### 4.4 Bounded CognitiveBrief 与 Reflection

第一切片复用现有 `read_only_cognitive_loop.py`，不另建“模型自主选 probe”的认知引擎。server-prepared cached views 仍是模型唯一输入面。

C4 的最小输出由两部分组成：

1. `summary_if_asked`：如果用户此刻问“有什么值得知道”，Veyra 会如何简洁回答；
2. `why_now`：本次 CognitiveBrief 相对前次 brief 出现了哪些 `new / escalated / reversed / resolved` 变化。

前后文本不同本身不构成变化证据。每个 `material_change` 和 `why_now` 都必须绑定本轮仍有效的 typed evidence refs；只改了措辞、引用已过期或没有新的结构化证据时，保持 `quiet`。前次 brief 只作为比较基线，不能反过来证明当前世界事实。

CognitiveBrief 提供受限的候选含义，GeneralAttention 提供确定性 evidence/readiness 外壳，AttentionHypothesis 保存两者的精确版本绑定。模型可以建议建立 candidate，但 model confidence 不增加 readiness，不能让 hypothesis 直接 confirmed。紧接第一切片的核心工作就是完成这座安全桥，再由 confirmed hypothesis 进入现有 Outbox。

### 4.5 复用 SuggestionOutbox Sandbox

第一切片必须复用现有 `SuggestionOutbox`，不新增并行 suggestion 表、队列或发送器。进入 Outbox 的 proposal 必须绑定：

- exact owner 与来源 session；
- `general_situation_id` 和 revision；
- `hypothesis_id` 和 revision；
- scorer/ruleset 版本；
- `why_now`、结构化 evidence refs 与已知 unknowns。

沙箱约束：

- 用户显式 opt-in 后才在 owner 专属 Console Sandbox 展示；
- 每个 owner 每个本地自然日预算最多 1 条；
- 默认仍为 `record_only`，无外部推送；
- proposal 只是信息性建议，不是 ActionProposal、审批、Grant 或执行请求；
- opt-out、quiet hours、cooldown 和 daily budget 任一命中即抑制；
- 所有链路维持 `external_delivery_enabled: false`、`agent_execution: false`、`tool_execution: false`。

### 4.6 显式分类反馈

反馈标签至少分为：

- `useful`
- `not_useful`
- `too_frequent`
- `wrong_timing`
- `wrong_evidence`

反馈必须绑定 owner、session 和 proposal revision；proposal revision 自身必须覆盖 GeneralSituation、AttentionHypothesis 与 scorer/ruleset 的精确引用，并支持幂等重放与显式更正。

`ack` / `dismiss` 只表示用户处理了界面状态，不能充当 usefulness 标签。`too_frequent`、`wrong_timing` 和 `wrong_evidence` 也不能被折叠成统一的“不喜欢”，因为它们诊断的是不同失败面。

### 4.7 Descriptive Self Model

Self Model 第一版只做描述，不做自动策略优化。按 owner、domain、hypothesis kind、scorer/ruleset 版本分段记录：

- proposal 数和有标签样本数；
- `useful` / `not_useful` 数；
- `too_frequent`、`wrong_timing`、`wrong_evidence` 数；
- 样本窗口、版本与 `insufficient_sample`；
- 只有存在外部可验证结果时才统计 outcome accuracy。

没有真实反馈时，usefulness 为 `unknown`；没有可证伪且已验证的 outcome 时，accuracy 为 `unavailable`。任何比例必须同时显示分子、分母和版本。第一切片 `policy_effect` 固定为 `none`，不得因少量反馈自动调高/调低阈值、扩展域、增加频率或打开推送。

## 5. Interaction Economics 与 Balance Metrics

认知闭环不能只回答“是否超过阈值”，还要在每次机会中选择 `say / ask / wait / silent`。概念上的判断是：

```text
interaction surplus = expected user value - interruption cost - uncertainty cost
```

这不是让模型生成一个看似精确的分数。每一项都必须有结构化来源；关键项 unknown 时选择 ask、wait 或 silent，不能用 model confidence 补齐。

| 决策 | 适用条件 | 第一切片的实际效果 |
|---|---|---|
| `say` | hypothesis confirmed；有 evidence-bound `why_now`；预计价值足以覆盖打扰成本 | 进入 Outbox 资格检查，不等于立即发送 |
| `ask` | 一个明确且用户可回答的缺口阻塞高价值判断，询问成本低于继续猜测 | 仅在 owner 已 opt-in 的 Console/前台交互中呈现，不主动外呼 |
| `wait` | hypothesis 正在 accumulating，或短期内有预期的新 typed evidence | 保留状态，等待新证据，不重复累计 tick |
| `silent` | 无实质变化、低价值、重复、证据争议、用户 opt-out | 不生成 proposal，并记录可解释原因 |

quiet hours、cooldown 和 daily budget 只负责**节流**，不是 cognition。系统应分别记录节流前的 `decision_disposition` 和节流后的 `delivery_disposition`：一个值得说但因 quiet hours 暂缓的候选，不应被统计成认知层“什么都没发现”。

这两个 disposition 复用 AttentionHypothesis / SuggestionOutbox 状态，不创建平行 Interaction 队列。最小记录必须包含 `decision_disposition`、`delivery_disposition`、结构化 reason、evidence refs、hypothesis/proposal revision 以及规则版本。`ask` 同样只能进入 exact-owner、已 opt-in 的 Console Sandbox；它不是新的外部追问或通知通道。

### 5.1 Balance Metrics

只追求低噪音会把系统推向永久沉默；过度保守和过度打扰都是失败。至少记录：

- `candidate_rate`、`accumulating_rate`、`confirmed_rate`；
- `say_rate`、`ask_rate`、`wait_rate`、`silence_rate`；
- `empty_focus_rate` 与“存在合格 evidence 但仍无 candidate”的次数；
- proposal rate、节流/预算抑制率、feedback coverage；
- usefulness、wrong-evidence、wrong-timing、too-frequent 的 count/denominator；
- 连续 quiet 窗口和 overconservative alert。

所有指标必须绑定时间窗口、owner/domain 和 scorer/ruleset/model 版本。第一阶段不设拍脑袋的全局“最佳比例”，也不根据指标自动调策略；它们用于发现两个极端：有证据却长期 0 candidate，以及缺少价值证据却持续 say/ask。

### 5.2 两种不同的验收证据

- **治理层**：owner isolation、幂等、daily budget、无外部 delivery、无 Agent/Tool/Route/Risk 变化等不变量，可以由 smoke/gate 验收并标记 `VERIFIED`。
- **关系与产品价值层**：建议是否有用、是否打扰、Veyra 是否显得“懂我”，只能写成带样本量的真实人工观察与显式反馈，例如 `LIVE OBSERVED: useful 3/7`。合成 smoke、fixture、模型自评或手工注入不能把它们标成 `VERIFIED`。

## 6. 第二纵向切片：Belief Economy

Belief Economy 不阻塞第一条建议闭环，但第一切片前必须完成两项 P0 hygiene，防止错误知识进入证据链：

1. **Epistemic hygiene**：在 Belief 及其所有消费上下文中保留 `observed / inference / prediction`、`is_fact` 和来源；模型 derived claim 不得伪装成 observed fact，也不得满足 Attention 的确认证据。
2. **Refresh hygiene**：可刷新 claim 必须携带结构化 `refresh_spec`，例如 `probe_kind + target_ref + resolver_id`；删除从 claim 自由文本推导 probe target 的 fallback。无合法 target 时 fail closed，不发 probe。

完整 Economy 的价值函数为：

```text
Belief Value = importance × change_probability × decision_impact
```

三项中的任意一项 unknown，则 `belief_value = null`、`evaluation_status = unknown`。模型不得猜默认值，文本关键词不得代填。Belief Value 表示刷新预算优先级，不表示真实性、权限或 Attention readiness。

最小字段：

```json
{
  "economy": {
    "importance": {"value": null, "source": "registered_policy_or_user_goal"},
    "change_probability": {"value": null, "source": "producer_policy"},
    "decision_impact": {"value": null, "source_refs": []},
    "belief_value": null,
    "evaluation_status": "unknown|complete",
    "next_refresh_at": null,
    "max_staleness_seconds": null
  },
  "refresh_spec": {
    "probe_kind": "registered_probe",
    "target_ref": "registered_target",
    "resolver_id": "versioned_resolver"
  }
}
```

调度先保证超出 `max_staleness` 的硬约束，再按已知 belief value 排序，并在 owner/scope 间轮转；不能让一个 owner 或高频 probe 占满全局容量。不可刷新的事件型 claim 应归档或过期，而不是永久排队 `refresh_probe`。

## 7. 明确非目标与权限边界

本阶段不做：

- 不把 OpenClaw 变成认知脑；OpenClaw、Codex 仍是下游执行/编码引擎；
- 不新增并行建议系统，唯一出口是现有 SuggestionOutbox；
- 不建立无限轮次、自由选工具的后台 LLM agent；
- 不把 deterministic readiness 或规则分数包装成“认知”；
- 不从自由文本关键词猜 topic、owner、因果、重要性或 refresh target；
- 不把模型 inference、confidence 或自我描述当事实；
- 不自动修改阈值、频率、路由、权限、风险或 delivery policy；
- 不让 `ack` / `dismiss` 充当 usefulness；
- 不把 Console Sandbox 等同于飞书或其他外部主动推送；
- 不把合成 smoke、fixture 或手工注入候选包装成真实 usefulness；
- 不宣称长期人格、情绪理解、预测未来或 Jarvis 级体验已完成；
- 不改变 Guardian、Verifier、Tool Proxy、审批、审计、恢复或 Phase 6 边界。

所有 read-only status GET 必须保持纯读：不得刷新、解析新 owner、运行模型/probe、创建 proposal 或修改 revision。状态损坏、scope 不明、证据不全时一律 fail closed。

## 8. 里程碑与接受标准

### M0：契约冻结与 P0 hygiene

接受标准：

- epistemic kind 与 `is_fact` 从生产者传到所有 Attention 消费面；
- derived/model claim 无法满足 confirmed evidence；
- 缺失合法 `refresh_spec` 时不产生 probe，且不存在 claim-text fallback；
- 新状态均有 schema/revision、owner scope、TTL、腐坏降级和迁移测试；
- 所有公开 Route、风险和权限输出保持不变。

### M1：Typed observation → GeneralSituation / GeneralAttention

接受标准：

- 单个 typed event 只形成 candidate，不产生建议；
- 两个不同 owner 的事件永不聚合；不同 session 只有共享 owner-bound、active durable Goal/Commitment anchor 时才可聚合；
- 相同 child ref 重放 100 次，parent/revision/score 不增长；
- 过期、future timestamp、digest/revision 不匹配的 child fail closed；
- unknown component 保持 null，不由模型或文本补齐。

### M2：Attention Hypothesis 生命周期

接受标准：

- 可观察 `candidate → accumulating → confirmed`，并能因反证转为 `contradicted`、因时效转为 `expired`；
- 同 producer/事实/时间桶重复观察不增加 readiness；
- producer、fact kind 或时间桶至少一个维度具有多样性的有效证据，才能满足确认门槛；
- CognitiveBrief 的 candidate 必须精确绑定 cached view、GeneralSituation/GeneralAttention 与各自 revision，才能进入 hypothesis；
- `summary_if_asked` 的文字变化若没有新的有效 evidence refs，不得生成 `why_now` 或提高 readiness；
- model confidence 为 0.99 且无 typed support 时仍不能 confirmed；
- confirmed 记录始终 `is_fact: false`、`causality_asserted: false`、无任何执行 authority。

### M3：Existing SuggestionOutbox Sandbox

接受标准：

- 只有 confirmed 且 revision 当前的 hypothesis 能生成 proposal；
- 未 opt-in、budget 用尽、quiet hours 或 cooldown 命中时 proposal 被抑制；
- 同一 owner 每日最多展示 1 条，重复运行幂等；
- 每个机会都记录 `say / ask / wait / silent` 及理由，并分开记录节流前 decision 与节流后 delivery；
- Balance Metrics 能识别连续 quiet、empty focus 和“有合格证据但 0 candidate”的过度保守窗口；
- proposal 能回溯到完整 Situation、Hypothesis、规则版本与 evidence refs；
- 外部发送、Agent、Tool、Grant、ActionProposal、Route 与风险状态均为 0 变化。

### M4：分类反馈与描述性 Self Model

接受标准：

- 五类标签均可精确写入、幂等重放、显式更正，跨 owner 写入被拒绝；
- `ack` / `dismiss` 不改变 usefulness 计数；
- Self Model 按域和版本展示 count/denominator；样本不足标为 `insufficient_sample`；
- 无验证 outcome 时 accuracy 显示 `unavailable`；
- `policy_effect` 保持 `none`。

### M5：真实用户价值观察

接受标准：

- 使用真实运行中的 typed observations，而不是 fixture 注入，产生至少一条可解释 Sandbox proposal；
- 用户本人完成 opt-in，并给出显式分类反馈；
- 保留真实人工观察日志，逐条记录用户看到的 proposal、当时的 disposition、反馈和上下文版本；
- proposal 的证据、`why_now` 与 unknown 可在 Console 逐项检查；
- 负反馈也原样保留，不通过删除样本美化结果；
- smoke/gate 只用于声明治理不变量；usefulness 只用 `LIVE OBSERVED: x/n` 表达，不标记 `VERIFIED`；
- 在获得足够真实样本前，阶段状态继续标记 `USER VALUE VALIDATION PENDING`。

### M6：Belief Economy（第二切片）

接受标准：

- 三因子及其来源可审计，任一 unknown 时 value 为 null；
- 高价值且到期的 belief 优先刷新，同时 owner 间无饥饿；
- 不可刷新 claim 不进入永久刷新循环；
- Economy 不改变 claim 真假、Attention 确认或执行权限；
- 对 stale 比例、有效 belief 覆盖和刷新预算给出前后真实运行对照。

## 9. 下一步真实观察场景

第一条 live 场景使用项目研发风险，但输入必须是可信 typed observation，而不是聊天中出现“awareness”“测试”等关键词：

1. Git/项目观察 producer 记录某 owner 在多个时间桶内对同一结构化项目锚点持续修改 awareness 相关文件；
2. 独立测试 producer 记录对应测试/覆盖证据没有同步变化；若覆盖不可测，则该分量保持 unknown；
3. GeneralSituation 只做共同项目锚点下的事件聚合，不声称修改导致风险；
4. 现有 Cognitive Loop 基于 server-prepared cached views 生成 evidence-bound `summary_if_asked`，并仅把相对前次的新证据表达为 `why_now`；
5. Attention Hypothesis 接收该 candidate，在满足独立证据与时间跨度后才 confirmed；readiness 仍不代表事实概率；
6. Interaction Economics 在 `say / ask / wait / silent` 中做出可解释选择，并记录节流前后 disposition；
7. SuggestionOutbox 在用户已 opt-in 且当日预算可用时展示一条：说明观察到了什么、为什么现在值得看、哪些仍未知；
8. 用户选择 `useful`、`not_useful`、`wrong_evidence`、`wrong_timing` 或 `too_frequent` 等明确标签；
9. Self Model 只增加该域、该规则版本下的描述性样本，不自动改变策略。

这个场景的成功不是“系统成功生成了一段聪明文字”，而是：真实信号被正确累积、建议理由可核查、用户反馈被准确记录，并且安全与权限边界全程没有变化。

在此之前，对 Phase Cognitive Awakening 最诚实的状态仍是：**实现切片进行中，用户价值验证 pending。**

## 10. 2026-08-12 trusted input 实现检查点

本节是对上面历史计划的实现补充，不改写阶段开始时的基线。

已经形成第一条 production-shaped、默认不扩权的输入路径：

```text
isolated Git / exact-SHA GitHub Actions observation
→ server-owned workspace_observer capability
→ StructuredObservationIngress / EventInbox
→ GeneralSituation / GeneralAttention
→ confirmed AttentionHypothesis
→ record_only SuggestionOutbox
```

它有这些边界：

- 只观察 `local_world.current_project` 精确绑定的一个 Git workspace，不接受任意命令、测试日志、路径或自由文本 fact；
- 配置必须绑定 exact owner/session/workspace、唯一 active Goal、Git origin/ref，以及可选的 GitHub Actions workflow/required jobs/app identity；
- 第一轮只建立 baseline；unchanged、docs-only、重复 observation 和 CI success 保持 silent；
- 非文档 dirty change 先形成 `change_signal`，超过 grace 且仍未验证时才形成 `risk_signal`；clean 新 SHA 只有 exact-SHA CI failure 才同时形成 change/risk，CI unknown 不推进 baseline；
- producer 通过进程内 object capability 进入既有认知链，HTTP caller 不能自报 `workspace_observer`；
- 默认 `not_configured / disabled`，Project Guardian 继续 disabled，external delivery、Agent、Tool、Route、Risk、Grant 和 execution authority 全部不变。

测试现在区分两条 lane：大量 smoke 继续验证安全和治理不变量；`trusted_workspace_observer_smoke.py` 单独作为 cognitive capability smoke，要求真实临时 Git worktree 的 code change 能到达 exact-owner confirmed Attention 和一条 `record_only` proposal，同时验证 silent、重放、Goal/config 竞态、CI unknown→failure 和伪造入口。这个自动化正例证明链路具备能力，仍不证明当前用户 workspace 已配置、长期 usefulness 或外部主动交付。

应用 revision `479b38d30934c0da1559f635b0f7e88001ed681c` 的本地检查为：`143/143` gate（`142` invariant + `1` capability）、9 Route `810/810`、OpenClaw plugin `32/32`、结构化观测控制面 `10/10`、compileall、Web build 与 Desktop frontend build 全部通过。observer 默认未配置，当前真实用户状态的历史 `0 candidate / overconservative` 不能用这组自动化结果改写。

Attention lifecycle 仍明确保持 fail-closed：`attention_lifecycle_producer_unavailable`。原先在早退之后的 contradiction 推导代码不可达，已经删除；本轮对抗审查也否决了继续堆一套没有真实 domain caller 的大型 producer。后续只有在某个受信领域能提供独立、可核验的反证事实时，才实现对应的最小 producer 与 lifecycle admission，不能先造通用脚手架再把 fixture 当能力。

下一项产品验证不是继续增加安全模块，而是由用户显式选择 active Goal 和 owner/session，配置 workspace observer，在真实项目中收集 bounded shadow/record-only 样本，并记录 candidate rate、证据质量、timing 与显式 feedback。当前历史 `0 candidate / overconservative` 仍是问题基线，不能被一个自动化正例抹掉。

实现结构上，workspace observer 已拆为 service、纯 state codec 与 durable delivery outbox；这比把全部状态机留在一个 1400 行文件更可审阅，但 service 仍约 1070 行，属于后续机械拆分债务。隔离 Git observation 的持久输出与文件读取有预算，Git status 的 2 MiB 检查仍是在子进程返回后执行，因此不能宣称拥有操作系统级流式资源硬限。
