# Feedback and Calibration Architecture

## Status

`RECORDING IMPLEMENTED / GOVERNED AFTEREFFECT NOT PRODUCT-VALIDATED`

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
