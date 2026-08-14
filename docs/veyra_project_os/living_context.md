# Living Context

## 1. 定义

`Living Context (logical projection)` 是 Veyra 在某个 owner/session、某个时间点，对用户当前生活的有证据约束、可修正、带未知和权限边界的综合理解。

它不是：

- 一个新的数据库；
- 一个 `LivingContextManager`；
- 所有状态的复制；
- 单一 Prompt；
- 对用户生活的绝对真相；
- 用于绕过原有 scope 或 authority 的统一对象。

它由现有权威状态按需组合产生。每项内容必须能够追溯到来源、新鲜度和作用域。

## 2. 组成视角

Living Context 可以综合以下视角，但不要求它们共享同一存储结构：

- User：偏好、约束、纠正和主动程度；
- Time：当前时间、截止期、周期和未来窗口；
- Goal：用户希望实现的结果；
- Commitment：用户或系统已经承诺的事情；
- Situation：正在演化的一段现实；
- Relationship：与人、组织和角色相关的上下文；
- Evidence / Belief：观察、报告、推断、冲突和新鲜度；
- Unknown / Assumption：仍缺少的信息和暂时假设；
- Attention：为什么现在值得关注；
- Memory：能够帮助当前理解的历史，而非事实权威本身；
- Risk / Authority：能否询问、观察、委托、建议或行动；
- Self State：Veyra 当前能力、限制和故障状态。

## 3. Active Concern

`Active Concern (logical projection)` 是 Living Context 中当前值得持续关注的一件事。它统一的是关注视角，而不是底层实体类型。

一个 Concern 可能源自：

- Goal；
- Commitment；
- Risk；
- Opportunity；
- Relationship concern；
- 尚未形成 Goal 的重要变化。

Goal 很重要，但不是唯一入口。现实变化可以先形成 Concern，随后 Veyra 询问用户是否值得持续关注。不得因为检测到变化就擅自建立长期 Goal。

一个 Active Concern 至少需要回答：

- 对谁有效；
- 它来自什么；
- 为什么重要；
- 当前处于什么状态；
- 最近发生了什么变化；
- 哪些信息仍未知；
- 下一个需要观察或确认的时间点；
- 用户是否允许持续关注。

## 4. Situation

Situation 是一段正在演化、能够被新事件更新的具体情境。它不是一次消息，也不是一个静态标签。

Situation 应具备：

- exact owner/session scope；
- 与 Concern 的可追溯关系；
- 时间范围和生命周期；
- 关键实体或锚点；
- 已知事实、未知、假设和冲突；
- 支撑它的 evidence references；
- 最近 material change；
- 当前 Attention 与 reaction history。

Situation 可以：

- `emerging`：刚出现，证据不足；
- `active`：正在持续演化；
- `waiting`：等待时间、外部事件或用户信息；
- `resolved`：目标或问题已解决；
- `expired`：上下文已失效；
- `contradicted`：核心理解被当前有效反证推翻；
- `archived`：不再主动维护，但历史仍可追溯。

状态名可以随实现演进，语义必须保持可审计且不能静默复活。

## 5. Information Need

Information Need 表达：

> 为了理解或推进某个 Concern/Situation，Veyra 目前缺少哪类信息，以及为什么现在需要它。

它必须绑定：

- owner/session；
- Concern/Situation；
- 被阻塞的判断；
- 所需 evidence kind；
- why now；
- urgency / expiry；
- 允许的数据来源类别；
- 获取失败时的反应；
- authority ceiling。

Information Need 不是 Tool Call，不包含任意 path、URL、command、query 或 tool arguments。它可以由模型建议，但必须由 server 验证、规范化和映射。

## 6. Evidence 与 Belief Update

所有进入 Living Context 的内容应区分：

- `reported`：某个来源声称；
- `observed`：受信 producer 直接观察；
- `inferred`：由证据推导；
- `predicted`：关于未来；
- `verified`：通过匹配验证；
- `contradicted`：被当前有效反证冲突；
- `expired`：超出有效时间；
- `indeterminate`：证据不足。

Belief Update 必须保留 provenance、freshness、scope 和 conflict。新证据可以修正当前理解，但不能因来源更新就把旧冲突静默抹掉。

## 7. Attention 与 Reaction

Attention 回答“为什么是现在”。它应考虑：

- Concern 对用户的重要性；
- material change；
- deadline 和 urgency；
- risk；
- unknown 的信息价值；
- evidence quality；
- 历史反馈；
- 打扰成本；
- quiet hours 和用户偏好。

Reaction 不只有执行：

```text
say / explain / suggest / warn / ask
observe / search / delegate / constrain / act
wait / silent
```

Reaction 必须与当前证据、authority 和用户偏好匹配。高质量的 `wait`、`ask` 或 `silent` 与高质量建议同样重要。

## 8. 反馈闭环

用户的回答、纠正、拒绝、useful/not useful、现实结果和后续变化，都应回到 Living Context。

反馈首先影响：

- 当前 Situation 的理解；
- unknown 是否被解决；
- 建议是否仍成立；
- 未来 Attention 和 timing；
- 重复提醒和 cooldown。

它不能自动授予 Agent、Tool 或外部写入权限。

## 9. 示例

用户说：“下周我要去上海出差。”

这不是直接触发“查天气”的命令。合理的逻辑投影是：

```text
Active Concern
  顺利完成上海出差

Situation
  行程正在形成，会议和交通存在时间依赖

Known
  地点：上海
  时间：下周

Unknown
  具体日期、会议时间、住宿、交通、天气敏感性

Information Need
  先询问只有用户知道的日期与会议信息

Reaction
  ask：你具体哪天出发，会议时间已经确定了吗？
```

用户回答后，Veyra 再决定是否需要日历、天气或公共信息观察，而不是无目的收集数据。
