# Project Guardian Held-out Replay 评估协议

## 1. 目的与边界

本协议用于回答一个有限问题：

> Project Guardian 对未参与开发调参的真实项目事件，能否稳定识别
> `project_release_risk`，并给出证据正确、对人有用的只读建议？

协议不是在线运行时，也不是新的状态读取通道。实现
`scripts/project_guardian_replay.py`：

- 只读取调用者显式提供、已经脱敏并冻结的 manifest、episodes、
  predictions、gold labels 和 post-prediction reviews 工件；
- 不读取或写入 Veyra 的真实 state；
- 不调用 Git、Agent、模型、通知、工具、代码执行或部署；
- 不改变 Project Guardian 的运行模式、授权或执行能力；
- `ready` 只表示这批真实 held-out 评估证据达标，不表示可以进入
  advise、通知或主动执行阶段。

真实事件的导出、脱敏和标注必须在独立的数据准备流程中完成。replay
脚本不会直接连接生产系统或工作区。

## 2. 标签隔离

评估严格分为两个进程步骤。

### 2.1 Predict

```bash
python3 scripts/project_guardian_replay.py predict \
  --manifest /evaluation/frozen/manifest.json \
  --episodes /evaluation/frozen/episodes.json \
  --output /evaluation/frozen/predictions.json
```

`predict` 子命令没有 `--labels` 参数，`predict()` Python 接口也没有
labels 参数。labels 不允许嵌入 manifest 或 episode。传入任何
`label`、`expected`、`ground_truth`、`ratings` 等标签字段都会被拒绝。
manifest 只暴露 gold-label 工件的 SHA-256，不暴露 label 内容；人工
reviews 此时尚不存在。

输出 predictions 使用独占创建；若文件已存在，命令拒绝覆盖。输出内的
`prediction_set_sha256` 绑定规范化后的完整预测内容，并记录显式
`evaluator_ruleset`。ruleset 与当前代码常量不一致时，score 拒绝把旧
prediction 当成当前规则证据；任何改变资格语义的实现都必须提升该版本。

### 2.2 Score

```bash
python3 scripts/project_guardian_replay.py score \
  --manifest /evaluation/frozen/manifest.json \
  --episodes /evaluation/frozen/episodes.json \
  --predictions /evaluation/frozen/predictions.json \
  --labels /evaluation/frozen/labels.json \
  --reviews /evaluation/frozen/reviews.json \
  --output /evaluation/frozen/report.json
```

`score` 独立读取冻结的 predictions 与 labels。它会重新读取 manifest
和 episodes，并在读取 labels/reviews 前用当前 evaluator 重算每个
episode 的完整 prediction，要求 candidate identity/revision、association、
evidence、review payload 和 authority locks 与冻结 artifact 精确一致。
因此普通 SHA-256 自绑定不能把手写 prediction 冒充为 label-blind predict
输出。随后再读取 prediction 生成后完成的 reviews，检查 dataset、哈希与
episode 覆盖关系，然后才计算指标。reviews 必须精确绑定
`prediction_set_sha256`；report 同样使用独占创建，不能覆盖已有报告。

为了保持真正的 held-out：

1. 在预测前冻结 episode 集合、数据切分和独立 gold labels；
2. predict 运行环境不得获得 labels 文件；
3. 预测完成后冻结 predictions；
4. Reviewer 只查看冻结的 prediction review payload，生成独立 reviews
   工件，并绑定 `prediction_set_sha256`；
5. 由独立 score 步骤同时读取 gold labels 与 reviews；
6. 任何调参都必须产生新的数据版本，不能在同一 held-out 集合上反复
   调参后继续称其为 held-out。

## 3. 冻结工件

### 3.1 Manifest

manifest 只允许以下字段：

```json
{
  "schema_version": "veyra.project_guardian_replay_manifest.v1",
  "dataset_id": "guardian-release-risk-2026q3-v1",
  "split": "held_out",
  "data_class": "real_project",
  "anonymization_version": "guardian-opaque-export-v1",
  "label_policy_version": "guardian-release-risk-label-v1",
  "episodes_sha256": "<episodes.json 的文件字节 SHA-256>",
  "labels_sha256": "<labels.json 的文件字节 SHA-256>"
}
```

`data_class` 可为：

- `real_project`：全部 episode 必须来自真实项目；
- `synthetic`：全部 episode 为合成或 fixture；
- `mixed`：同时含真实和合成 episode。

只有 `real_project` 有资格得到 `ready`。`synthetic` 和 `mixed` 无论
指标多高都必须为 `not_ready`。

predictions 记录 manifest 原始文件字节的 SHA-256。score 若收到哪怕
只改变空白字符的另一份 manifest，也会拒绝评分。manifest 对 episodes
和 labels 的原始文件字节哈希提供绑定。这是完整性与冻结检测，不是
HMAC、数字签名或来源真实性证明。

manifest、episodes、labels 和 reviews 的协议读取都只打开一次受
64 MiB 上限约束的文件描述符；SHA-256 与 UTF-8/JSON 解析来自同一份
bytes。不能先 hash 路径 A、再因并发替换从同一路径解析内容 B。

`anonymization_version` 和 `label_policy_version` 都是冻结数据合同的一
部分；版本变化必须生成新的 manifest。reviews 是 prediction 之后的人工
产物，因此不进入 predict 前的 manifest，而由自身
`prediction_set_sha256` 和最终 report 的 `reviews_sha256` 冻结。

predictions 和 report 还必须绑定
`ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION`。candidate schema
只描述输出结构，不能代替规则实现身份；ruleset 变化后的旧 predictions
即使结构未变，也不得继续代表当前 evaluator。当前版本号由代码评审流程
显式维护，工具不能自动证明“实现改变时一定已升版”；因此 ruleset 升版
检查仍是合入资格语义变更时的流程约束。

### 3.2 Episodes

episodes 顶层格式：

```json
{
  "schema_version": "veyra.project_guardian_replay_episodes.v1",
  "dataset_id": "guardian-release-risk-2026q3-v1",
  "episodes": [
    {
      "episode_id": "episode-0001",
      "group_id": "release-group-opaque-0001",
      "source_fingerprint": "<独立准备流程生成的 opaque 来源 SHA-256>",
      "source_class": "real_project",
      "evaluated_at": "2026-07-20T03:00:00+00:00",
      "goals_state": {"goals": []},
      "event_inbox_state": {
        "schema_version": "veyra.project_guardian_signal_frontier.v1",
        "events": {}
      }
    }
  ]
}
```

每个 episode 是评估时点的最小、结构化、离线快照。不得复制
`user_goals.json`、`event_inbox.json` 或其他真实 state 文件给 replay
脚本；数据准备流程必须只导出 evaluator 需要的字段。

`group_id` 表示一个独立 release evaluation opportunity；同一来源不能
通过改 episode ID 重复计数。一个数据集中的 `episode_id`、`group_id`
和 `source_fingerprint` 必须分别唯一。replay 还会在删除这三个外部标识
后，对严格验证过的输入生成 evaluator decision-semantic projection 并
拒绝重复。projection 只保留 evaluator 当前接受、且至少与一个 accepted
signal 匹配的 active Goal identity，以及 signal freshness、资格字段、
信号间相对顺序/间隔和 Goal-window membership；全局时间平移会被
规范化，evaluator 跳过的 Goal/signal 完全不能改变摘要。只用于结构
验证但不改变 evaluator 决策的 Goal
`state_revision/target_sha`、receipt、绝对 `valid_until` 或仍保持相同
admission/window membership 的时间边界也不能用来换壳。正例、负例和
人工评价最低支持量都按独立 group 统计。

这些检查只能发现工件内部的重复或不一致，不能从一个自报的
`real_project` 字符串证明来源真实性。独立数据准备流程必须保留不交给
predict 进程的来源台账，证明每个 `source_fingerprint/group_id` 对应
不同 release opportunity；没有这份可审计来源证据时，即使工具输出
`ready` 也不能作为 Phase 2 放权依据。

其中 release Goal 必须满足当前受控 Goal schema/source、正整数
`state_revision`、语义 `revision`、`active / paused / completed`
lifecycle、合法时间区间和目标 SHA 合同。replay 能验证这些结构化字段，
同一快照内的受控 `goal_id` 必须唯一，不能重复同一 Goal 增加 negative
support；但它不能从可复制的 `source` 字符串证明 Goal 确实经过注册。
来源真实性依赖可信本地 state 与独立数据准备台账，不是 replay 提供的
密码学证明。

episodes 中禁止携带：

- raw text、用户消息、prompt、content 或 body；
- diff、patch、日志、stdout、stderr 或 command；
- 本地 path、file path、filename、cwd 或 worktree；
- labels、expected outcome、rating 或 adjudication；
- 任意 `raw_*` 字段。

字段名检查是故意保守的。需要的新结构化字段应先更新协议和 smoke，
不能借用通用 `details` 或 `content` 容器绕过审查。

实现不只依赖字段名黑名单。`goals_state`、frontier record、Event
envelope、source、payload、signal、producer attestation 和 evidence
ref 均使用精确字段白名单，并被重新构造成 canonical evaluator 输入；
字符串首尾空白、Goal 顺序、evidence 顺序和 evaluator 不读取的 frontier
字典 key 也会规范化，任何未知字段都会被拒绝。因此真实 state 中的
revision metadata、claim、错误消息或其他私有字段不能顺带进入 replay
进程。规范化后，每个 frontier event 还必须恰好成为一个 evaluator
接受且未被 dedupe 的 signal，并且每个 `user / Goal revision / scope /
kind` 只能有一个当前记录，符合 runtime compact frontier 合同；被
evaluator 忽略的错误 schema/channel/privacy/attestation/evidence 记录
或被更晚同类状态覆盖的旧记录不能充当支持。最后再使用上述
decision-semantic projection 拒绝无语义别名。JSON 解析在任意嵌套层级
发现重复 object key 都会直接失败，不能用 last-key-wins 隐藏 labels、
raw text 或其他禁用字段。

所有时间字段必须使用规范 UTC `+00:00` ISO-8601 表示；等价时区 offset
不能生成另一个样本。signal frontier record 必须是 `recorded`，
Event 必须是 `observation`，`timestamp` 必须等于 `occurred_at`，并且
每个 signal 只允许一个 canonical evidence ref。`pending` 等 evaluator
会忽略差异的状态、额外 evidence 或时间别名不能换壳增加 group 支持量。

### 3.3 Labels

labels 必须覆盖每个 episode，不能多也不能少：

```json
{
  "schema_version": "veyra.project_guardian_replay_labels.v1",
  "dataset_id": "guardian-release-risk-2026q3-v1",
  "episodes": [
    {
      "episode_id": "episode-0001",
      "expected_candidates": [
        {
          "association": {
            "candidate_kind": "project_release_risk",
            "user_id": "user-opaque-01",
            "goal_id": "goal-release-01",
            "goal_revision": "7",
            "scope": {
              "workspace_id": "workspace-opaque-01",
              "repo_id": "organization/repository",
              "target_ref": "refs/heads/main",
              "target_environment": "production",
              "release_cycle": "release-2026q3"
            }
          },
          "evidence_ref_ids": [
            "event:event-ci-01",
            "event:event-git-01"
          ]
        }
      ]
    }
  ]
}
```

负例的 `expected_candidates` 是空列表。标注必须使用完整 association，
不能只用 candidate kind 或 goal id 做宽松匹配。

Gold labels 必须在 predict 前冻结，只包含 expected association 与
expected evidence，不得包含 usefulness review 或
`candidate_revision`。

### 3.4 Post-prediction Reviews

reviews 在 prediction 冻结后创建：

```json
{
  "schema_version": "veyra.project_guardian_replay_reviews.v1",
  "dataset_id": "guardian-release-risk-2026q3-v1",
  "prediction_set_sha256": "<predictions 中的冻结摘要>",
  "episodes": [
    {
      "episode_id": "episode-0001",
      "human_reviews": []
    }
  ]
}
```

reviews 必须逐 episode 完整覆盖数据集，且每项评价只能引用该 episode
冻结 prediction 中实际存在的 exact association 和
`candidate_revision`。引用不存在的 prediction、旧 revision 或另一份
prediction set 都会被拒绝。最终 report 保存 reviews 文件字节的
SHA-256。

## 4. 精确指标定义

association 由以下字段的完整规范化值构成：

- `candidate_kind`
- `user_id`
- `goal_id`
- `goal_revision`
- `workspace_id`
- `repo_id`
- `target_ref`
- `target_environment`
- `release_cycle`

逐 episode 做集合比较：

- **TP**：prediction association 与 frozen label association 完全相同；
- **FP**：prediction association 不存在于该 episode 的 frozen labels；
- **FN**：frozen label association 没有对应 prediction；
- **false association**：保守地定义为全部 FP，包括负例上的额外预测和
  正例上绑定到错误 Goal、用户、仓库、ref、环境或发布周期的预测。

不使用 `max(1, denominator)` 平滑：

- `precision = TP / (TP + FP)`；分母为零时是 `null`；
- `recall = TP / (TP + FN)`；分母为零时是 `null`。

证据正确性使用精确集合：

- TP 的 `evidence_ref_ids` 与 frozen labels 完全相同才算正确；
- 每个 FP 自动计为一项证据错误；
- FN 没有预测证据，不进入证据正确性分母，由 recall 单独惩罚；
- `evidence_correctness = evidence_correct /
  (evidence_correct + evidence_incorrect)`；分母为零时是 `null`。

报告同时保存所有整数计数和逐 episode 的 TP、FP、FN，避免只看比例
掩盖低支持量。

## 5. 人工 usefulness

usefulness review 必须绑定 exact association、`candidate_revision` 和
顶层 `prediction_set_sha256`，防止旧评价套用到新建议。评价覆盖所有
prediction，而不是先看 gold labels 后只挑 TP。

无 adjudication 时，至少需要两位不同 rater，且结论一致：

```json
{
  "association": {"candidate_kind": "project_release_risk"},
  "candidate_revision": "pgr_example",
  "ratings": [
    {"rater_id": "reviewer-a", "useful": true},
    {"rater_id": "reviewer-b", "useful": true}
  ]
}
```

示例省略的 association 字段在真实文件中仍必须完整。

若只有一项初评，或多位 rater 结论不一致，必须由未参与初评的独立
adjudicator 裁决：

```json
{
  "association": {"candidate_kind": "project_release_risk"},
  "candidate_revision": "pgr_example",
  "ratings": [
    {"rater_id": "reviewer-a", "useful": false}
  ],
  "adjudication": {
    "adjudicator_id": "reviewer-c",
    "useful": true,
    "reason_code": "evidence_and_actionability_review"
  }
}
```

单人且无裁决、rater 重复、裁决人与初评人相同、争议无裁决都会使
reviews 工件无效。缺失 review、引用不存在的 prediction 或绑定旧
candidate revision 时不计算为有效 human support，并使报告保持
`not_ready`。

`human_usefulness = useful / human_reviewed_predictions`，分母为零时为
`null`。

## 6. Ready 门槛

以下条件必须同时满足：

| 条件 | 门槛 |
| --- | ---: |
| 数据来源 | 仅 `real_project` |
| 独立正例 group | 至少 20 |
| 独立负例 group | 至少 20 |
| 有有效人工 review 的独立 prediction group | 至少 20 |
| precision | ≥ 0.95 |
| recall | ≥ 0.85 |
| false association | 0 |
| evidence correctness | 1.0 |
| human usefulness | ≥ 0.80 |

空集合、任一比例未定义、group/source/semantic input 重复、低于任一
独立支持量、人工覆盖不完整，均明确输出 `not_ready` 或协议错误及原因。
合成 benchmark 可以验证协议和实现，但永远不能替代真实项目 held-out
证据。CLI 对 `ready` 返回 0，对有效但 `not_ready` 的报告返回 1，对
协议/工件错误返回 2。

## 7. 验证

定向 smoke：

```bash
python3 scripts/project_guardian_replay_smoke.py
```

smoke 覆盖：

- predict CLI 不接受 labels；
- predictions 与 score 分进程、分工件；
- score 在读取 labels/reviews 前重算 evaluator 输出，拒绝可自行重算
  SHA 的手写/伪造 prediction artifact；
- manifest、episodes、labels、predictions 和 reviews 的哈希/绑定检测，
  且协议输入的 hash/parse 来自同一次受限 bytes 读取；
- evaluator ruleset 不一致时拒绝旧 predictions；
- 精确 TP、FP、FN、false association 与 evidence correctness；
- 满分 synthetic、空集合、低支持量仍为 `not_ready`；
- 重复 group、source fingerprint 和 decision-semantic evaluator input
  被拒绝；
- 只改变 Goal state revision/target SHA、等价 Goal window/evaluation
  time、signal valid-until 并重算 receipt 的换壳样本被拒绝；
- 等价时区、frontier status 和无语义 timestamp 别名被拒绝；
- 不受支持的 Goal status 与 evaluator 不接受的 signal contract 不能
  作为负例支持；
- evaluator 跳过的 paused/future/unmatched Goal 与旧同类 frontier
  record 不能换壳增加支持；
- 重复 controlled Goal identity 与整组时间戳的全局平移不能增加支持；
- 任意层级 duplicate JSON object key 被拒绝，不能隐藏 label/leakage；
- Goal、Event 和 signal 的未知嵌套字段被严格白名单拒绝；
- 双人一致与独立 adjudication；
- 单人评价、未裁决争议评价被拒绝；
- reviews 只能在 prediction 后生成并绑定 `prediction_set_sha256`；
- raw text、diff、log、path 和嵌入 labels 被拒绝；
- CLI 对 `ready`、`not_ready` 和协议错误分别返回 0、1、2；
- replay 模块没有 runtime state、Git、Agent、通知或工具调用入口。
