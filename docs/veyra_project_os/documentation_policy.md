# Documentation Policy

## 目的

这套文档系统将“长期方向”“概念模型”“当前实现”“路线”“实时事实”和“本次任务”分开，避免：

- 把今天的实现方案写成永久产品真理；
- README 为追赶 SHA、PID 和测试数量不断变化；
- Codex 每次执行都重新阅读一份超长 RFC；
- Product、Architecture、UI 与 Current Task 相互污染；
- 文档声称的完成度高于真实代码和用户证据。

## 文档层级与更新频率

| 层级 | 文档 | 典型更新频率 | 禁止内容 |
|---|---|---:|---|
| 宪章 | `README.md`、`philosophy.md` | 年/季度 | SHA、PID、当前模块施工细节 |
| 概念 | `living_context.md` | 月/季度 | 类名、API、文件路径 |
| 产品 | `product_experience_v1.md` | 每轮用户研究 | Runtime 实现细节 |
| 架构 | `architecture.md`、`architecture/*` | 每个重要切片 | 把 current approach 写成永恒真理 |
| 决策 | `adr/*` | 重大决定时追加 | 日常小修改、重复架构正文 |
| 路线 | `roadmap.md` | 阶段验收后 | 文件级施工清单 |
| 状态 | `status/current.md` | 每次交接 | North Star 改写 |
| 任务 | `tasks/CURRENT.md` | 每个窗口/切片 | 多阶段愿望清单 |

## 对补充建议的取舍

### Architecture 拆分：采纳，但不制造空壳

`architecture.md` 只做索引；子文档按职责拆分。只有当一个边界有独立合同、消费者或演进节奏时才新增文件，不能为了目录漂亮制造七个没有内容的模块名。

### ADR：采纳，但只记录真正的决定

ADR 用于有明确替代方案、长期后果或迁移成本的决定。命名、局部重构和普通 bugfix 不写 ADR。

ADR 是追加式历史。决定变化时新增 ADR 并标记旧 ADR `Superseded`，不悄悄改写过去。

### Roadmap Research Question：采纳

Veyra 的主动性、时机、长期 usefulness 和“被持续理解”的体验仍包含研究问题。Roadmap 必须把未知写出来，不能把所有阶段伪装成确定的工程交付。

### Philosophy：采纳

哲学文档解释为什么，不直接指导文件、类、API 或测试。任何工程要求必须落到 Architecture、Task 或 Definition of Done。

### “Every screen”原则：采纳并限定范围

每个**产品页面**都应帮助用户理解“Veyra 此刻正在理解什么”。Developer Console 则回答“Veyra 为什么这样判断、证据和边界是什么”。纯设置页面还必须回答“Veyra 被允许理解和使用什么”。

### 冻结 README：原则上采纳，迁移后执行

现有 [`docs/README_Veyra.md`](../README_Veyra.md) 在完成 owner review、链接迁移和事实下沉前仍是 canonical hub，不能立即宣布冻结。采纳本体系后，稳定宪章应少改，运行快照全部进入 `status/current.md`。

### 最终裁判原则：采纳并增加诚实标签

新增能力需要说明它如何提升持续理解或解锁明确用户结果。无法直接产生用户结果的工作可以继续，但必须标记为 `INFRASTRUCTURE`，说明阻塞关系，不能冒充产品阶段完成。

## 事实与证据纪律

1. Git、代码、配置、运行态和 exact-SHA CI 高于旧文档。
2. `IMPLEMENTED`、`CONFIGURED`、`AUTOMATED_VALIDATED`、`LIVE_OBSERVED`、`USER_VALIDATED` 分开报告。
3. Fixture、smoke 和 synthetic replay 不能证明长期 usefulness。
4. 历史 live 样本不能自动继承到新 revision。
5. `status/current.md` 必须标注观察时间和 revision；过期时宁可写 `UNKNOWN`。
6. Current Task 完成后先更新状态和任务，不因一个切片改写哲学或 North Star。

## 迁移原则

1. 不批量删除或重命名现有文档。
2. 先让本目录独立可读，再由 owner 逐份确认。
3. 确认后，在现有 canonical README 加入本目录入口。
4. 将易过期事实逐步下沉到 `status/current.md`。
5. 将仍有效的架构内容按主题迁移，不复制两套相互漂移的权威正文。
6. 迁移完成后明确唯一 canonical 位置，并给旧文档加 archived/superseded 标记。
