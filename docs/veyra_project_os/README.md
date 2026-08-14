# Veyra Project OS

> Status: `CURRENT / CANONICAL LIVING TRUTH`
>
> 本目录自 2026-08-14 起承载 current/task Living truth：
> [`status/current.md`](status/current.md) 是当前状态，
> [`tasks/CURRENT.md`](tasks/CURRENT.md) 是当前执行窗口唯一任务。
> [`docs/README_Veyra.md`](../README_Veyra.md) 保留为 canonical 入口，负责链接和 dated checkpoint；
> 它不再复制本目录的高频状态快照。

## 项目宪章

Veyra 不是代码观察器、任务管理器、聊天壳，也不是另一个通用 Agent Runtime。

> **Veyra 是一个持续维护用户 Living Context 的认知系统。它理解用户正在经历什么、什么值得持续关注、哪些信息仍然缺失，并在合适的时候通过观察、询问、建议、等待或调用 Agent 提供帮助。**

更简洁地说：

> **Chat 完成一次对话，Agent 完成一次任务，Veyra 持续帮助用户看清自己的生活正在发生什么。**

`Living Context` 始终表示 **logical projection（逻辑上的当前综合理解）**。它不是单一对象、数据库、Manager、统一父类或第二套 truth。

Agent 与 Veyra 的长期边界是：

- Agent 提供可替换的研究、开放推理、规划和执行能力；
- Veyra 维护连续上下文、重要性判断、Attention、权限边界、反应选择、结果验证和后续校准；
- 模型、Agent、Tool、Skill、MCP 和外部服务可以替换；用户的目标、数据、授权与纠正权不可被替换。

## 当前阶段总原则

> **不要再造一个认知模块。先组织和贯通已有能力，让用户第一次获得可感知的持续帮助。**

任何新增能力，都必须让 Veyra 对用户当前生活的理解变得更准确、更连续或更有帮助。如果它只是增加系统复杂度，却无法说明它解锁了哪一种用户可感知能力，那么它不属于当前阶段的优先级。

基础设施工作并非没有价值，但必须明确标记为 `INFRASTRUCTURE`，并说明它为哪条具体用户闭环解除阻塞。

## 不可破坏的长期原则

1. 理解先于执行；信息不足时允许观察、询问、等待或沉默。
2. 沉默是合法反应，长期错误沉默是产品故障。
3. Observation、Inference、Prediction 和 Verification 必须区分。
4. Goal 不是唯一入口；Goal、Commitment、Risk、Opportunity 和 Relationship concern 都可能成为 Active Concern。
5. Active Concern 与 Living Context 都是 logical projection，不要求统一持久实体。
6. 新证据可以修正旧理解；冲突不能被静默覆盖。
7. 用户能够查看依据、纠正理解、调整主动程度、撤销授权、停止保留和删除数据。
8. 模型可以提出信息需求和方案，但不能自授权限、伪造事实或决定确定性安全边界。
9. Agent 的能力越强，Veyra 对长期上下文、Attention、授权和结果验证的责任越重要。
10. 认知必须最终影响未来理解、时机或反应，但任何学习后效都必须有证据、审计、回退和权限边界。

## 文档导航

| 文档 | 稳定性 | 回答的问题 |
|---|---|---|
| [philosophy.md](philosophy.md) | 很稳定 | 为什么 Veyra 值得存在？ |
| [living_context.md](living_context.md) | 稳定 | Veyra 需要理解什么？ |
| [product_experience_v1.md](product_experience_v1.md) | 中等 | 用户第一次应该感受到什么？ |
| [architecture.md](architecture.md) | 可演进 | 当前准备怎样实现？ |
| [roadmap.md](roadmap.md) | 经常调整 | 先验证哪个用户结果和研究问题？ |
| [definition_of_done.md](definition_of_done.md) | 稳定 | 什么才算真正完成？ |
| [status/current.md](status/current.md) | 高频更新 | 当前代码、运行态和证据是什么？ |
| [tasks/CURRENT.md](tasks/CURRENT.md) | 每个切片更新 | 当前窗口唯一允许完成什么？ |
| [../README_Veyra.md](../README_Veyra.md) | canonical 入口 | 项目链接、稳定手册和 dated checkpoint |
| [adr/README.md](adr/README.md) | 追加式 | 为什么做出重要架构决定？ |
| [documentation_policy.md](documentation_policy.md) | 稳定 | 这些文档如何维护和迁移？ |

## 阅读协议

执行窗口不应每次重读全部文档。默认顺序是：

1. 阅读本页，确认身份和不可破坏原则；
2. 阅读 [`tasks/CURRENT.md`](tasks/CURRENT.md)；
3. 只读取 Current Task 显式引用的概念、产品和架构章节；
4. 重新核验 Git、代码、运行态和验证证据；
5. 只完成 Current Task，不顺手扩展路线。

推荐执行 Prompt：

```text
继续处理 `/Users/spectre/PycharmProjects/veyra`。

先重新核验 Git、运行态与未提交修改。
阅读：
1. `docs/veyra_project_os/README.md`
2. `docs/veyra_project_os/tasks/CURRENT.md`
3. Current Task 明确引用的其他章节

本轮只完成 Current Task。Architecture 记录的是 current approach，
不是永久不能替换的实现。按照 Definition of Done 分别报告用户结果、
长期能力、自动化证据、live 证据、未证明假设和 authority delta。
```
