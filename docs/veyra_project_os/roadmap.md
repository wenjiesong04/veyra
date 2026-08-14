# Product Roadmap

Roadmap 按用户结果和研究问题排序，不按模块数量排序。阶段编号不等于现有代码中的 P0/P1/P2；映射需要在 Current Task 中显式说明。

## 路线

| 阶段 | User Outcome | Research Question | 最小证据 | Exit 条件 |
|---|---|---|---|---|
| R0 诚实基线 | 用户知道 Veyra 当前能理解什么、不能做什么 | 现有状态能否被准确、可纠正地投影？ | current-revision state review、scope/privacy tests | Living Context 只读投影不复制 truth，状态与证据语言诚实 |
| R1 一个持续 Situation | 用户表达一件真实生活中的事，Veyra 跨天保持上下文 | Situation 是否比 conversation/task 更能维持连续理解？ | 一条真实非代码 Situation、restart/replay、用户纠正 | 两次以上独立更新后仍正确维护 Known/Unknown/时间线 |
| R2 一个 Information Need | Veyra 识别自己缺什么，而不是盲目观察 | 显式 Information Need 是否比更大 Prompt 更有效？ | needs_observation → ask/source → evidence | 信息需求被解决或诚实 expiry，且改变后续理解 |
| R3 必要的 Ask | 用户收到一个真正值得回答的问题 | Ask 能否减少错误建议，而不过度打扰？ | necessary/duplicate/dismiss/answered live samples | 问题有 why-now、不重复、回答能更新 Situation |
| R4 两个真实来源 | Veyra 能感知对真实生活有意义的变化 | Calendar + Message/Web 是否能跨场景复用？ | 至少两个非代码 source、三个场景 | exact scope、consent、freshness、真实 material changes |
| R5 应用内帮助 | 用户在 Today/Situation 中看到变化、问题和建议 | Situation-first UI 是否比 Chat/Console 更易理解？ | task-based usability、行为日志、访谈 | 用户能解释 Veyra 关注什么、为什么、如何停止 |
| R6 两周使用 | 用户明显感到 Veyra 帮助管理近期生活上下文 | 有用感来自持续理解，还是提醒数量？ | 14 天真实样本 | usefulness、漏报、误报、时机、纠正均达到 owner 设定阈值 |
| R7 受治理校准 | 系统根据反馈减少重复和错误时机 | 非权限性 aftereffect 是否稳定改善体验？ | versioned A/B 或 before/after | 可回退改善，且没有 authority/privacy 扩张 |
| R8 有限委托 | Veyra 在需要时让 Agent 研究并验证结果 | Agent 是否真正补强理解而非制造更多文本？ | bounded research + independent verification | 结果进入 Situation、可追溯、成本和失败可控 |

## 当前优先级

1. 先完成真实生活的 R1/R2 纵向闭环；
2. 再启用受治理 Ask；
3. 接入两个广覆盖只读来源；
4. 建立用户产品体验和两周验证；
5. 最后扩大 Agent 委托和受治理学习。

## 当前暂缓

- 继续扩大代码观察器作为产品主线；
- Phase 6 自扩展投入；
- 任意 Tool 和外部写入；
- 复杂 EvidenceGraph 新功能；
- P3 全面身份/关系模型；
- 自动自修改；
- 为每个生活场景分别硬编码；
- 在产品闭环前扩大多平台发布范围。

## 研究纪律

每阶段退出时必须回答：

- 研究问题的答案是什么；
- 哪些证据支持；
- 哪些样本可能偏置；
- 哪些结论只适用于当前 owner/source/revision；
- 下一阶段是否仍值得继续原假设。
