# Product Experience V1

## 1. V1 用户承诺

> **连续使用两周后，用户明显感觉：Veyra 没有替我定义生活，但它能持续帮我看清最近在忙什么、什么正在变化、什么快被忘记，以及现在真正值得关注什么。**

这是一条产品验收标准，不是营销口号。若真实使用无法支持它，V1 尚未完成。

## 2. 统一体验原则

> **Every product screen must help answer: “What is Veyra understanding right now?”**

中文表达：

> **产品中的每一个页面，都应该帮助用户理解：Veyra 此刻正在理解什么。**

不同页面回答不同层次：

| 页面 | 应回答的问题 |
|---|---|
| Today | Veyra 如何理解我最近的生活？ |
| Situation | Veyra 如何理解这一件事？ |
| Questions | Veyra 还不知道什么，为什么要问？ |
| Suggestions | Veyra 为什么认为现在值得说？ |
| Conversation | 这句话改变了什么理解？ |
| Sources & Permissions | Veyra 被允许理解和使用什么？ |
| Developer Console | 系统为什么形成这个判断，证据和边界是什么？ |

纯设置页不需要伪装成认知页面，但必须清楚展示设置如何影响感知、保存、主动程度和权限。

## 3. 首页：Today

Today 不是聊天首页，也不是运维仪表盘。它应优先显示：

- 当前最重要的 3–5 个 Active Concerns；
- 最近发生的 material changes；
- 接近截止或容易遗漏的事情；
- Veyra 当前等待什么；
- 需要用户回答的问题；
- 有依据的建议及 `why now`；
- 下一次计划观察什么；
- 纠正、暂停和查看来源的入口。

用户应在十秒内回答：“Veyra 最近在帮我关注什么？”

## 4. Situation 详情

每个 Situation 页面包含：

- Goal 或 Concern；
- 当前状态摘要；
- 关键时间线；
- Known / Unknown / Assumptions；
- Evidence、来源和 freshness；
- 最近变化；
- 下一 Observation；
- Questions；
- Suggestions 与 reaction history；
- 用户反馈、纠正、pause、stop 和 archive。

默认使用自然语言，不把 hypothesis ID、revision、digest 和 Route 暴露给普通用户。技术证据可以渐进展开。

## 5. Questions

一个 V1 问题只有满足以下条件才应出现：

- 它绑定一个真实 Active Concern/Situation；
- 信息只有用户知道，或向用户询问明显比外部观察更合适；
- 答案会改变后续理解或反应；
- 问题说明 why now；
- 不重复已经回答或拒绝的问题；
- 用户可以选择稍后、忽略或停止关注。

问答后，界面应说明“你的回答改变了什么”，而不是只显示一条聊天消息。

## 6. Suggestions

Suggestion 必须回答：

- 发生了什么；
- 为什么和用户有关；
- 为什么是现在；
- 哪些是事实，哪些是推断；
- Veyra 建议什么；
- 用户可以怎样反馈或纠正。

V1 优先应用内 suggest/ask。外部推送必须单独 opt in，并遵守 quiet hours、cooldown 和作用域。

## 7. Conversation 的位置

Conversation 用于：

- 表达新 Concern/Goal；
- 补充信息；
- 回答问题；
- 纠正 Living Context；
- 临时问答；
- 请求观察、建议或行动。

Conversation 不是唯一状态来源，也不是产品信息架构的中心。每次重要输入应能说明它更新了哪个 Situation、Known、Unknown 或 Preference。

## 8. Developer Console

现有 `Awareness / Governance / Runtime / Ops / Audit / Logs` 工作台保留为 Developer Console / Advanced Mode。

它用于：

- 运行健康；
- Evidence 与 revision；
- authority；
- Route/Risk；
- Agent/Tool/Review；
- degraded 与 repair action；
- audit/log。

它不能继续承担普通用户首页的职责。

## 9. V1 场景覆盖

同一核心模型至少应支持三个真实、低风险场景，而不是分别硬编码三套产品：

1. 旅行、会议或活动安排；
2. 求职、申请或学习计划；
3. 搬家、个人项目或家庭计划。

代码项目观察可以继续作为 Developer canary，但不计入三类生活场景覆盖。

## 10. 两周验证

至少记录：

- 正确维持的 Situation 数量；
- material change 漏报；
- false alerts；
- false silence；
- 重复提醒；
- wrong timing；
- 必要/不必要 ask；
- Information Need resolution rate/time；
- suggestion useful/not useful；
- 用户纠正后是否改变未来理解；
- 用户是否知道信息来源并能停止。

自动化证明合同和不变量，真实两周样本证明产品价值。两者不能互相替代。

## 11. V1 非目标

- 替用户自动定义长期人生目标；
- 任意外部写入和通用工具自治；
- 无限制记录原始生活数据；
- 医疗、法律或财务高风险建议自治；
- 电影式人格模拟；
- 以“消息数量”或“自动执行次数”证明主动性。
