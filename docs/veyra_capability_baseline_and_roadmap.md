# Veyra 能力基线与发展路线

> 核验基线：2026-07-31，提交 `d929ccf`
>
> 证据来源：当次代码阅读、`state/` 真实落盘数据、本机运行时探测。不采信任何未经核对的文档宣称。
>
> 本文回答三个问题：**现在真实做到了什么**、**我们要达到什么标准**、**接下来按什么顺序做**。
>
> 与其他文档的关系：[veyra_proactive_cognitive_architecture.md](./veyra_proactive_cognitive_architecture.md) 是目标架构与逐阶段实现记录，[development_status.md](./development_status.md) 是运行交付状态。本文不重复它们，只做**跨文档的真实基线对照**和**下一步取舍**。三者冲突时，以本文核验过的代码事实为准。

---

## 1. 状态词汇纪律

本文和后续所有提交必须使用同一套词汇，不允许把左边写成右边：

| 词 | 含义 | 判据 |
|---|---|---|
| `VERIFIED` | 代码存在，且有真实运行证据 | 主进程真实运行留下的状态/日志/PID/计数证据 |
| `CURRENT` | 代码存在且能跑，但缺目标场景的运行证据 | 自动化通过，无 live 场景 |
| `SHADOW` | 代码存在，但默认不产生任何对外效果 | 配置默认 `disabled` 或 `record_only` |
| `PARTIAL` | 只覆盖目标能力的一部分 | 必须写清覆盖了哪一部分 |
| `TARGET` | 尚未实现 | 不得出现在能力宣传中 |

三条硬性禁止：

- 不得把 `technical_complete` 写成 `validated`；
- 不得把 fixture 通过写成 live 验证；
- 不得把 SHA-256 完整性写成签名或来源证明。

---

## 2. 当前真实基线

### 2.1 运行环境（VERIFIED）

| 项 | 事实 |
|---|---|
| 解释器 | `/opt/anaconda3/envs/veyra/bin/python3.11`（3.11.15），禁止静默回退全局 Python |
| 启动 | `scripts/start_local.sh` 预检解释器 + 依赖 + CA，事务式替换 LaunchAgent |
| 飞书 | 当前进程真实收发已验证：`connected / last_processed_after_start / provider_sent`，重复投递被抑制 |
| 自动化 | Python gate `91/91`，OpenClaw governance `32/32`，compileall、前端构建、`pip check` 均通过 |
| `/health` | `degraded`，仅剩 Memory fallback、历史审批、Belief 运营债务；Feishu / Model / Agent 均 `ok` |
| 隔离能力 | **无 Docker / Podman / Colima / Lima / bwrap / firejail / nsjail**，仅有 deprecated `/usr/bin/sandbox-exec` |

最后一行是 Phase 6 继续推进的**硬阻塞**，见 §5.1。

### 2.2 世界状态与感知

三层世界状态都不是空壳，本机有真实数据：

| 状态 | 文件与规模 | 写入者与时机 | 状态 |
|---|---|---|---|
| UserWorld | `state/user/user_world.json`，1.5 KB | 主要由 `CommitmentCore` 在 commitment/goal 生命周期写入；**不是每轮对话写** | `VERIFIED / PARTIAL` |
| LocalWorld | `state/local/local_world.json`，27 KB，13 个 probe 缓存 | `PerceptionLayer.interpret_probe_result`，每次 probe 执行后 | `VERIFIED` |
| ExternalWorld | `state/external/external_world.json`，31 KB，2 watchlist / 2 summaries | `ExternalWorldRefresh`，Active Loop 每 tick（limit=5） | `VERIFIED / PARTIAL` |
| Belief | `state/local/belief_state.json`，250 claims：**223 stale / 27 fresh / 0 conflict** | Perception + event | `VERIFIED / PARTIAL` |

关键差距：

- **没有 EvidenceGraph**。全库无该结构。证据是嵌在单条 claim 里的 `evidence: dict`，没有实体关系、有效时间、假设层，也没有跨 claim 的因果链。
- **冲突只标记不消解**。同 key 值不同 → `status=conflict` + `next_action=refresh_probe`，无仲裁逻辑。当前实测 0 conflict。
- **89% 的 claim 已 stale**。它记住了很多事，但绝大部分按自己的 TTL 规则已不可信。这是诚实的降级，不是 bug，但意味着"世界模型"的实际有效面远小于条目数。

感知解释层（`core/perception_layer.py`）：

- 默认路径 = probe 结果 → 规则异常检测 → 模板 claim → 写 LocalWorld + Belief。`VERIFIED`
- 模型解释 = 仅对 `web_probe / log_probe` 等或已检出 anomaly 时触发，且需 core model 启用。`CURRENT / 条件触发`
- 17 个 probe 均为真实实现，非 stub。

**注意力（最大差距）**：`awareness/attention_core.py` 的主路径是一张 substring 映射表（`"端口" → ports`、`"日志" → logs`），无语义相关性打分、无目标关联度。真正有确定性 score、Goal 绑定、quiet hours、预算和 dismiss 优先级的那套在 `awareness/project_guardian_attention.py`，但**只覆盖发布风险一个领域，默认 `disabled`，输出只到反事实 `would_suggest`，无行动权**。

### 2.3 主动性链路

| 组件 | 事实 | 状态 |
|---|---|---|
| Active Loop | `state/runtime/active_loop_state.json` = `running`，间隔 **300 秒**，12+ 子步骤 | `VERIFIED` |
| Agency gap → intentions | `agency/intention_queue.json` 200 条意图 | `VERIFIED`，但**不直接通知用户** |
| SituationEvaluator | `state/runtime/situation_state.json`，34 个 situation | `SHADOW`（默认 `record_only`） |
| 多事件 Situation 聚合 | 每事件投影为一个 candidate，stable id；聚合逻辑仅存在于默认关闭的 project_guardian 分支 | `TARGET` |
| Commitment 定时推送 | Active Loop 调用 `CommitmentPushRuntime.run_due` | `VERIFIED` |

**唯一能主动触达用户的通道是 commitment 到期推送**——那是用户自己设定的承诺，不是 Veyra 基于观察形成的建议。Agency 的 200 条意图没有出口。

### 2.4 Agent 协作与安全扩展

| 能力 | 状态 |
|---|---|
| Phase 6.1 同运行时只读协作（primary → critic） | `VERIFIED`，Kimi/Moonshot + OpenClaw 真实链 |
| Phase 6.2a ExtensionSpec 规范隔离门 | `VERIFIED / specification only` |
| Phase 6.2b 私有非执行 artifact quarantine | `VERIFIED / inert bytes only` |
| Phase 6.2c 非执行语法 + 窄 AST policy gate | `VERIFIED / non-executing only` |
| 隔离 runner、动态测试、签名、canary、晋级 | `TARGET` |
| 跨 provider 协作、自动专家选择、并行 Agent | `TARGET` |

Phase 6.2a–c 三刀全部是**从未运行过候选代码**的静态工作。链路已推进到"必须运行候选"这一步，而运行前提（隔离边界）不存在。

### 2.5 治理与验证（VERIFIED / SCOPED）

- Verifier 不轻信 Agent 自报：无结构化持久执行证据时保持 `needs_more_probe`，不标 `verified_success`。这是 PDF 第 15 节的核心要求，已真实做到。
- Tool Governance 只覆盖注册的 `veyra_governed_openclaw_sessions` 和逐 run sandbox，不能宣称覆盖所有 OpenClaw tool call。
- 9 个公开 Route 在 3 种 event mode × 各子系统 populated/corrupt 下有 216 组非弱化对照。

---

## 3. 我们要实现的标准

### 3.1 六条能力的可验收定义

沿用 [架构文档第 2 节](./veyra_proactive_cognitive_architecture.md) 对"贾维斯感"的拆解。每条给出**达标判据**，避免用感觉验收：

| # | 能力 | 达标判据 | 当前 |
|---|---|---|---|
| 1 | 记得正在发生什么 | 能对任一结论回答"依据哪几条证据、各自何时观察、是否仍新鲜、是否互相冲突"，且证据可跨事件追溯 | `PARTIAL`：有 claim 和 TTL，无证据图与实体关系 |
| 2 | 知道现在该关注什么 | 关注项由**目标相关性 + 紧急度 + 异常 + 信息价值**的确定性打分产生，可解释、可复算、可 dismiss，且不依赖 substring | `TARGET`：主路径为关键词表 |
| 3 | 能形成自己的建议立场 | 能主动产出带 why-now、证据、备选方案和不确定性的建议，并经 Guardian 后触达用户 | `TARGET`：意图无出口 |
| 4 | 会借助 Agent 思考行动 | 向 Agent 请求研究/挑战/方案，接受严格协议回复，Agent 可反过来质疑前提 | `VERIFIED / SCOPED` |
| 5 | 行动后继续观察 | 不接受"Agent 说完成"，必须由独立证据判定 verified / partial / indeterminate | `VERIFIED` |
| 6 | 出故障时能有限恢复 | 在已注册、低风险、可验证的 playbook 内恢复；超界熔断并请人 | `SHADOW`：默认 shadow，真实故障演练未做 |

**目标是把 1、2、3 从 PARTIAL/TARGET 推到 VERIFIED，而不是把 4、5 做得更花哨。**

### 3.2 不可破坏的原则

继承架构文档 §5，任何切片都不得违反：

```text
用户主权 > 已签发的治理策略与能力范围 > Veyra 的决策与主动意图 > Agent 的计划与工具选择
真实观察证据 > 独立验证结果 > Agent/Veyra 的预测 > 无来源的模型陈述
```

具体到每次提交：

1. 没有 `case_id + trace_id + user_id` 的主动动作不得执行。
2. 没有有效 `CapabilityGrant` 的副作用调用不得执行。
3. Agent 不能提高自己的风险上限、自治等级或 capability scope。
4. 模型预测只能提高风险或增加检查，不能授予权限。
5. 生成候选代码在获得**可证明的隔离边界**之前不得运行。
6. TCB（Guardian、Tool Proxy、Verifier、审批、审计、密钥、签名器、更新器）不能被 Agent 自动修改并激活。

### 3.3 什么才算"完成"

一个切片可以宣布完成，必须同时满足：

- 自动化 gate 全绿（Python gate、OpenClaw governance、compileall、前端构建）；
- **真实主进程运行证据**，不是 fixture；
- 9 个公开 Route 在各 event mode 下的完整 response/status/risk 非弱化对照；
- 明确写出本次**没有**获得哪些权限；
- 状态词汇符合 §1。

缺任何一条，只能记为 `technical_complete`，不能记为 `validated`。

---

## 4. 两条主线

这是本文最重要的判断：**当前有两条独立的发展主线，瓶颈不同，不互相阻塞。**

### 4.1 主线 A：安全扩展执行

解决"Veyra 能不能安全地长出新工具"。

```text
CapabilityGap → ExtensionSpec → artifact quarantine → 静态 AST gate
→ 【隔离 runner】← 当前卡在这里
→ 动态测试 → 签名 → canary → 人工晋级 → 监控/回滚
```

瓶颈是**本机没有可证明的隔离后端**。这是纯粹的前置条件问题，装好容器运行时后是清晰的工程推进。

### 4.2 主线 B：认知深度

解决"Veyra 能不能理解世界并主动开口"。

```text
probe/event → 【证据关系】→ 【目标相关性注意力】→ 【多事件情境】→ 建议 → 触达用户
                  ↑ 缺失          ↑ 关键词表           ↑ 每事件一条      ↑ 无出口
```

瓶颈是**认知层三个组件都停在最简实现**。这是设计问题，不是环境问题。

### 4.3 为什么必须分清

Phase 6 的全部工作都在主线 A。如果目标是"更像贾维斯"，继续推 Phase 6 **不会**让它更像——它会让 Veyra 更安全地扩展工具，但注意力仍是 substring 表，情境仍不聚合，建议仍没有出口。

反过来，主线 B 不需要隔离 runner，因为它全是只读观察和建议生成，不涉及运行不可信代码。

**两条可以并行，但不要以为做完 A 就自动得到 B。**

---

## 5. 下一步具体切片

### 5.1 A-1：Phase 6.2d Trusted Isolated Runner

**前置决策（必须先做）**：选定隔离后端。

| 选项 | 评价 |
|---|---|
| Colima + Docker | 最贴近"可证明边界的 OS/container runner"，**推荐** |
| Lima / Podman | 可行替代 |
| `sandbox-exec` | Apple 已弃用且未文档化，不能作为验收权威 |
| 进程内 / SafeShell / git worktree | **禁止**，只隔离目录不隔离权限 |

**切片范围**（只建骨架，不跑业务候选）：

- 私有控制面恰好 5 条 route：status / list / detail / integrity / start；
- 只接受已 `SOURCE_CHECK_PASSED` 的 exact artifact binding；
- 隔离要求：不挂载 `.env` / `state/` / 签名密钥，默认无网络，强制 CPU / 内存 / 进程数 / 超时 / 输出预算；
- 固定 harness 由 Veyra 提供，候选不可修改 runner、harness 或 policy input；
- CAS、operation replay、owner scope、fail-closed；
- 状态名 `technical_complete_isolated_runner_only`，全部动态 authority 保持 `false`。

**验收判据**：能在隔离内启动固定 harness 并取得可信退出证据；恶意候选无法逃逸到宿主文件系统或网络；候选修改 harness 的尝试被拒绝；Veyra 主进程状态、Git worktree、OpenClaw PID 不变。

**明确不做**：真实动态测试套件、签名、安装、注册、canary、晋级。

### 5.2 B-1：通用 Attention 的确定性相关性打分

**目标**：把 `awareness/attention_core.py` 从 substring 映射换成可解释、可复算的相关性打分。

**可复用素材**：`awareness/project_guardian_attention.py` 已有一套经验证的确定性 scorer——显式 Goal/policy 绑定、priority、deadline、quiet hours、每日预算、dismiss/cooldown 优先级。把它从"发布风险"单领域推广为通用打分器，比从零设计安全得多。

**打分维度**（对应 §3.1 第 2 条判据）：目标相关性、紧急度、异常程度、信息价值。

**硬性约束**：

- 不得靠堆关键词或正则提升指标。架构文档已明确"未达非弱化门槛时保留旧链并调查，不用补关键词掩盖根因"。
- 新旧 Attention **双写对比**，只有旧链保持权威；新链先 `shadow`。
- 非弱化验收通过前不切换权威链。

**验收判据**：held-out 真实对话样本上，新链的关注项 precision/recall 不劣于旧链；每个关注项能输出可复算的分数构成；同输入两次运行结果一致。

### 5.3 B-2：多事件 Situation 聚合

**目标**：让 `SituationEvaluator` 从"每事件一个候选"变成真正的多事件聚合。

**关键安全约束**（架构文档第 27 节反例 1）：**两个时间相近但无共同实体/目标的事件不能被宣称有因果关系。**

因此第一刀只允许**显式 join key** 聚合，不允许模型推断关联：

- 允许：同 `goal_id` / `commitment_id` / `case_id` / 显式 `correlation_id` / 同一实体（如同一 workspace、同一端口、同一 Agent run）；
- 禁止：时间邻近、文本相似、模型判断"看起来相关"。

**已有素材**：`SituationEvaluator` 的 `correlation_id` 与 `observation_sequence`，以及 `ProjectGuardianAttentionRuntime._aggregate` 的分组实现。

**验收判据**：错误关联数 = 0；聚合后的 Situation 能列出每个成员事件及其加入理由；`record_only` 下不改变任何公开 Route 输出。

### 5.4 B-3：建议出口（依赖 B-1 + B-2）

前两刀完成后，才谈把 Agency 的意图变成能触达用户的建议。必须携带：why-now、关联 Goal/Commitment、已知事实与新鲜度、未知与冲突、建议与备选、当前授权范围、如何暂停/纠正。

受通知预算和 quiet hours 约束，低紧急度必须聚合或延后。

### 5.5 并行运营债务（不挡主线）

- 飞书 / OpenClaw **长时间 soak**（当前仍标 pending，单次收发不等于稳定性）；
- `/health=degraded` 的三项清理：Memory fallback、历史 pending review、stale Belief；
- **语义 eval**：已知缺陷——"讨论执行边界"被误判为"请求执行"（真实案例误入 `ASK_USER`）。必须进 eval 数据集，禁止用关键词修补。

### 5.6 建议顺序

若目标是**更安全的自扩展** → A-1 优先，先装 Colima。

若目标是**更像贾维斯** → B-1 → B-2 → B-3，且 B-1 是真正的起点。

两条都想要 → 并行。B 线不需要隔离后端，A 线在等环境时 B 线可以推进。

---

## 6. 开发工作方式约定

这些约定是让 Veyra"按我们的想法"发展的实际保障：

1. **垂直切片**。每刀必须能独立验收、独立回滚，不做跨阶段大重构。
2. **每刀声明不扩权**。提交说明必须写出本次**没有**获得哪些权限（Agent / 工具 / 签名 / 执行 / 晋级）。
3. **真实运行证据优先于 fixture**。fixture 全绿只能记 `technical_complete`。
4. **新链先 shadow**。`disabled → record_only → shadow → advise_only → user_opt_in → scoped_canary → default_on`，每级独立 feature flag。
5. **非弱化是硬门槛**。9 个公开 Route × 3 种 event mode 的完整 response/status/risk 对照必须通过。
6. **不用关键词掩盖根因**。语义问题进 eval，不进 if-else。
7. **状态存储保持有界 JSON**。不为了名称对齐迁移 SQLite 或重写 AwarenessLoop。
8. **负面证据同样记录**。provider 限流、验收中断、失败尝试都要写进文档，不能只留成功路径。

---

## 7. 每次切片的验收清单

提交前逐条勾：

- [ ] Python gate 全通过（当前基线 `91/91`）
- [ ] OpenClaw governance plugin 全通过（当前基线 `32/32`）
- [ ] `python -m compileall` 通过
- [ ] Web browser / desktop 前端构建通过
- [ ] 9 Route × 3 event mode 非弱化对照通过
- [ ] 真实主进程运行证据已记录（PID、状态计数、状态文件哈希前后对照）
- [ ] OpenClaw PID 未被意外重启
- [ ] Tool Governance 计数（`dispatch_registered / execution_started / execution_completed / execution_failed / started_without_reservation`）符合预期
- [ ] 核心状态文件与 Git worktree 未被本次切片意外改动
- [ ] 提交说明写清"本次没有获得的权限"
- [ ] 文档状态词汇符合 §1，未把 technical_complete 写成 validated
- [ ] 负面结果与未完成项已如实记录

---

## 8. 明确不做的事

以下内容在本路线内**不做**，写下来是为了防止范围失控：

- 不重新实现完整 Agent Runtime、完整 Memory、完整 Tool Calling；
- 不做 SaaS / 多租户对外服务（当前隔离是 loopback 逻辑边界，不是认证多租户）；
- 不做运行时自动切换 provider / model；
- 不在获得隔离边界前运行任何候选代码；
- 不让 Agent 生成、批准、激活并验证自己的工具；
- 不做全局百分比灰度（canary 只按用户 / workspace / capability 划范围）；
- 不宣称 Veyra 具有感受、意识或人格；
- 不为了指标好看而堆关键词、改 fixture 或放宽验收词汇。

---

## 9. 一句话总结

Veyra 当前是一个**持续运行、真实观察环境、不轻信 Agent 自报、治理边界清晰**的系统；它还不是一个**会主动注意到问题并开口建议**的系统。

前者已经 `VERIFIED`，后者的瓶颈是注意力、情境聚合和建议出口这三个认知组件——它们与 Phase 6 的隔离 runner 是两条独立的路。
