# Feedback and Calibration Architecture

## Status

`RECORDING + BOUNDED EFFECTS IMPLEMENTED / REAL_MODEL_VALIDATED PENDING`

## 反馈类型

- 用户回答 Information Need；
- 用户纠正事实或 Goal；
- useful / not useful；
- too early / too late；
- repeated / irrelevant；
- accept / reject / dismiss；
- 现实结果验证；
- source failure 或 contradiction。

## 两种更新

### 立即语义更新

用户回答和纠正应立即更新当前 Situation、Known/Unknown 和后续建议有效性。

当前 alpha 已把自然语言反馈绑定到 exact owner/session/Situation/reaction
revision，并记录可重放的受限 effect。`useful/not useful`、`too early/too late`、
`repeated/irrelevant`、`accept/reject/dismiss` 等反馈可以影响当前 reaction 的
timing、cooldown 或 suppression；它们不改变事实权威。

### 长期校准

只有足够样本后，才允许影响：

- ranking；
- timing；
- cooldown；
- suppression；
- source preference；
- response length/style。

## 禁止的自动后效

反馈不能自动扩大：

- Agent 权限；
- Tool/Grant；
- external delivery；
- Route/Risk ceiling；
- data retention scope；
- sensitive source access。

## 校准合同

每个 policy effect 需要：

- minimum sample threshold；
- exact owner/scope；
- input evidence ledger；
- old/new policy revision；
- bounded effect；
- expiry/review；
- rollback；
- 用户可见和可纠正。

`policy_effect=none` 是安全早期阶段，但不是长期终点。没有可审计后效的 feedback 只是日志。

V1 alpha 的可用后效只停留在非权限行为：timing、cooldown、suppression。真实
Moonshot feedback 的最终可复现 run 和两周 usefulness 尚未完成，因此不能把
当前持久化 effect 标为 `REAL_MODEL_VALIDATED`、`BOUNDED_LIVE` 或用户价值。

## 评估

不能只统计 proposal 数量。至少跟踪：

- usefulness；
- false silence；
- wrong timing；
- repeat rate；
- correction recurrence；
- information-need resolution；
- user trust/stop behavior；
- calibration 前后差异。
