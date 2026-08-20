# Definition of Done

## 顶层规则

任何新增能力必须同时回答：

1. 代码实现了什么？
2. 用户第一次能够感受到什么？
3. 系统新增了什么可持续的长期能力？
4. 哪些假设仍未被证明？

如果第 2 点无法回答，本次工作必须标记为 `INFRASTRUCTURE`，不得宣称产品阶段完成。

## 产品切片 DoD

### User Outcome

- 有一个具体用户、场景和 before/after；
- 用户能观察到差异；
- 不是只增加 Console 计数或内部 JSON；
- 失败和无结果时同样有诚实体验。

### Long-term Capability

- 跨 turn/restart 保持；
- 新证据能够更新；
- 用户纠正能够改变未来理解；
- 去重、过期和 lifecycle 明确；
- 不依赖一次性 fixture 或手工状态注入。

### Engineering

- 职责边界清晰；
- 不新增 God Object；
- 不复制 authoritative truth；
- schema、CAS、replay、crash、capacity 有合同；
- 默认/disabled/failure fail closed；
- GET 纯读；
- 相关 consumer 与 migration 已处理。

### Authority

- 明确 authority delta；
- owner/session/source scope；
- Agent/Tool/Route/Risk/external delivery 是否变化；
- consent、privacy、retention；
- receipt 和 verification；
- 无新增权限时自动化证明 non-regression。

### Evidence

分别报告：

- static/compile；
- targeted automated；
- full gate；
- frontend/build；
- exact-SHA CI；
- clean runtime；
- bounded live；
- user validation。

不得把其中一种冒充另一种。

#### V1 alpha evidence labels

V1-001 使用以下不可互相升级的证据层级：

| Label | Meaning | Promotion rule |
|---|---|---|
| `PRIVATE LOCAL ALPHA` | 发布/使用范围是本机私有 alpha | 不代表 live、用户价值或公开发布 |
| `IMPLEMENTED` | 代码路径与边界存在 | 不代表配置、运行态或价值 |
| `AUTOMATED_VALIDATED` | targeted/full gate、合同、负向和构建检查通过 | 不代表真实模型、浏览器、live 或用户价值 |
| `REAL_MODEL_VALIDATED` | 配置中的真实模型按最终可复现脚本通过自然输入链 | 只有 root 在最终修复后明确重跑并确认才能填写；此前必须 `PENDING` |
| `BOUNDED_LIVE` | 当前 revision 的干净运行态、真实 source/浏览器等现场证据闭合 | 不能由 build、fixture 或历史 live 继承 |
| `14_DAY_USEFULNESS` | 真实用户两周样本达到预设 usefulness/漏报/误报/时机阈值 | 未采样或未达阈值必须 `PENDING` |
| `USER_VALIDATED` | owner/用户明确验收并可说明实际改进 | 不得由自动化或模型样本替代 |

这些标签是独立 evidence positions，不是累加计数；任一层缺失时，相关结论
必须保持 `PENDING`、`PARTIAL` 或 `DEGRADED`。V1 alpha 不把 P3、Phase 6、
Agent research、外部 delivery 或执行 authority 作为完成条件。

### Documentation

- Architecture 记录 current approach；
- 有重大长期决定时新增 ADR；
- Roadmap 更新证据和 Research Question；
- `status/current.md` 更新运行事实；
- Current Task 标记完成、剩余和下一步；
- 不因一个切片频繁改写 README/Philosophy。

## Infrastructure DoD

基础设施工作必须额外回答：

- 它解除哪个明确产品阻塞；
- 哪条未来纵向切片依赖它；
- 为什么不能在该纵向切片内以更小方式完成；
- 如何防止基础设施范围继续扩大；
- 何时验证其真实用户价值。

若无法回答，应停止或降级优先级。

## 完成汇报模板

```markdown
## Outcome

## User-visible change

## Durable capability added

## Architecture and files

## Authority delta

## Automated evidence

## Live/user evidence

## Assumptions not yet proven

## Known degraded items

## Next product loop
```
