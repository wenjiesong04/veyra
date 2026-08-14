# Authority Architecture

## Status

`STRONG ENGINEERING BASELINE / USER PRINCIPAL STILL LOCAL-LOGICAL`

## 目的

Authority 确保持续理解不会变成持续越权。它是产品可信度的必要条件，但不能替代产品价值。

## 基本边界

- 用户拥有 Goal、数据、主动程度和授权的最终控制权；
- Veyra 选择 Attention、组织上下文、约束能力并验证结果；
- Agent/模型不能自授权限；
- Tool/Source 不能借 Observation 绕过 effect policy；
- 高风险或不可逆动作需要显式确认；
- 无 receipt 不声称现实副作用已经发生。

## 作用域

所有长期认知和操作必须绑定：

- owner；
- session 或明确的跨 session policy；
- workspace/account/source scope；
- Goal/Concern/Situation reference；
- revision/generation；
- 有效时间。

当前 local API 的 owner/session 仍可能是 caller-declared logical principal。这可以支撑本地单用户产品验证，但不能宣传为 auth-derived 多租户安全。

## Reaction Authority

不同 Reaction 的权限不同：

- `silent/wait`：无外部副作用；
- `ask/say/suggest`：应用内触达，需 owner、timing、cooldown；
- `observe/search`：受 source consent、budget 和数据用途约束；
- `delegate`：受上下文最小化、Agent policy 和预算约束；
- `act`：单独授权、review、receipt 和 verification。

不能因为前一层是低风险就自动授权后一层。

## 用户控制

产品必须提供：

- 查看来源和推断；
- 纠正事实；
- pause/stop Situation；
- 调整 quiet hours 和主动等级；
- 撤销 source；
- 清除 pending action；
- 导出/删除长期数据。

## 非弱化验收

每个切片都要声明 authority delta，并验证：

- Route/Risk 没有意外变化；
- Agent/Tool/Grant/external delivery 默认未启用；
- 错 scope、stale、replay、tamper fail closed；
- 公开投影不泄露私有 locator、token、raw path 或跨 owner 内容；
- disabled 真正停止感知/触达或按合同完成 drain。
