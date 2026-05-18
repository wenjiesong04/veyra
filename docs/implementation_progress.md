# Veyra Implementation Progress

本文档把架构设计拆成可落地的大块，并记录当前代码进度。代码里的单一事实来源是：

- `core/definitions.py`: 生命周期状态、操作模式、风险等级、Guardian 决策、风险分类规则。
- `core/architecture.py`: 8 个架构大块、Veyra Core 子模块、状态文件定义、实施阶段。

## 8 个架构大块

| Block | 代码位置 | 当前状态 | 下一步 |
| --- | --- | --- | --- |
| Veyra Core | `core/`, `awareness/`, `decision/`, `foresight/`, `guardian/` | MVP foundation | 强化 Belief/Uncertainty、Agency、Verifier |
| Interface Adapter / Agent Adapter | `interface/` | MVP foundation | 补完整 Agent contract 测试和错误归一化 |
| Probe Tools | `probes/` | MVP foundation | 扩展更多真实 runtime probe 和异常模式识别 |
| Memory Bridge | `memory_bridge/` | MVP foundation | 加敏感信息过滤和过期 memory 标记 |
| Skill | `skills/` | MVP foundation | 把内置 skill 输出统一成 ExecutionResult |
| Tool Proxy | `tool_proxy/` | MVP foundation | shell/file/browser/api 全量接入统一风险策略 |
| Rollback / Audit | `rollback_audit/` | MVP foundation | 扩展 Action Journal 和 policy trace |
| Web Control UI | `web/`, `ui/` | MVP foundation | 接入 `/architecture` 和 `/definitions` |

## Veyra Core 子模块

| Module | 代码位置 | 当前状态 |
| --- | --- | --- |
| Runtime Entity | `core/runtime_entity.py` | MVP foundation |
| Awareness Loop | `core/awareness_loop.py` | MVP foundation |
| Attention Core | `awareness/attention_core.py` | MVP foundation |
| Belief & Uncertainty Core | `awareness/belief_core.py`, `awareness/uncertainty_core.py` | MVP foundation |
| WorldState | `core/world_state.py`, `state/*.json` | MVP foundation |
| Agency Core | `core/agency_core.py`, `agency/` | Placeholder |
| Perception Layer | `core/perception_layer.py` | MVP foundation |
| Persona Engine | `core/persona_engine.py`, `personas/` | MVP foundation |
| Decision Core | `core/decision_core.py`, `decision/` | MVP foundation |
| Foresight Engine | `core/foresight_engine.py`, `foresight/` | MVP foundation |
| Guardian / Execution Controller | `core/guardian_controller.py`, `execution/` | MVP foundation |
| Verifier | `core/verifier.py` | MVP foundation |
| Context / Patch Builder | `core/context_patch_builder.py`, `core/*_patch_builder.py` | MVP foundation |

## 生命周期状态

`core.definitions.LifecycleStatus` 定义 Veyra Runtime Entity 的状态：

| Status | 含义 |
| --- | --- |
| `online` | Runtime 已启动 |
| `idle` | 空闲等待事件 |
| `monitoring` | 主动观察或心跳检查中 |
| `thinking` | Awareness Loop 正在理解和决策 |
| `acting` | 正在调用 probe、skill、tool 或 Agent |
| `waiting_confirmation` | 等待用户确认中高风险动作 |
| `blocked` | Guardian 阻断动作 |
| `recovering` | 正在回滚或补偿 |
| `degraded` | 部分能力不可用 |
| `offline` | Runtime 不可用 |

## 风险等级

`core.definitions.RiskLevel` 和 `RISK_POLICIES` 定义当前风险策略：

| Level | 名称 | 默认决策 | 确认 | 快照 | 例子 |
| --- | --- | --- | --- | --- | --- |
| `R0` | answer_only | allow | 否 | 否 | 直接解释、缓存状态摘要 |
| `R1` | read_only | allow | 否 | 否 | 端口检查、git status、进程列表 |
| `R2` | low_risk_write | allow_with_constraints | 否 | 是 | 小范围代码修改、新建文件、本地 commit |
| `R3` | medium_change | ask_user | 是 | 是 | 修改配置、覆盖文件、数据库迁移 |
| `R4` | high_impact | ask_user | 是 | 是 | 重启服务、部署、sudo、删除文件 |
| `R5` | forbidden_or_manual_takeover | block | 是 | 是 | `rm -rf`、`curl | bash`、drop database、强推、密钥外发 |

## 状态文件定义

`core.architecture.STATE_DEFINITIONS` 和 `state/state_schema.json` 记录状态边界：

| State | 文件 | Owner | 用途 |
| --- | --- | --- | --- |
| UserWorld | `state/user_world.json` | WorldState.UserWorld | 当前用户目标、偏好、任务相关用户上下文 |
| LocalWorld | `state/local_world.json` | WorldState.LocalWorld | 项目、probe、运行环境和本地执行事实 |
| ExternalWorld | `state/external_world.json` | WorldState.ExternalWorld | 外部 watchlist 与相关资料摘要 |
| RiskState | `state/risk_state.json` | Guardian | 当前风险、风险目录、风险信号 |
| BeliefState | `state/belief_state.json` | BeliefCore | claim、confidence、source、TTL、fresh/stale/conflict |
| TaskState | `state/task_state.json` | AwarenessLoop | 当前任务、route、status、历史 |
| AttentionState | `state/attention_state.json` | AttentionCore | 当前关注切片、忽略噪声、上下文范围 |
| ExecutorState | `state/executor_state.json` | AgentAdapter | 选定执行体、连接状态、能力状态 |

## Probe 与 Belief 规则

P1 已将 probe 输出标准化为统一 envelope：

- `probe`: probe 名称，例如 `port_probe`
- `source`: 信息来源
- `target`: 观察目标
- `status`: 原始状态
- `confidence`: 置信度，范围 0-1
- `ttl_seconds`: 可相信的时间窗口
- `observed_at`: 观察时间
- `summary`: 面向人类的摘要
- `details`: probe 专属结构化细节
- `claims`: 可选显式 belief claims

Perception Layer 会把 probe envelope 转成 Belief claim。Belief claim 使用：

- `key`: 去重和刷新依据
- `claim`: 状态断言
- `confidence`: 置信度
- `source`: 来源
- `observed_at` / `updated_at` / `expires_at`
- `ttl_seconds`
- `status`: `fresh` / `stale` / `conflict`
- `next_action`: 例如 `refresh_probe`
- `evidence`: 支撑该 claim 的结构化证据

## Decision / Guardian 输出

P2 已开始把决策输出结构化。`Decision` 除了 `route` 和 `risk_level`，现在还包含：

- `intent`: `information` / `action` / `unknown`
- `complexity`: `simple` / `moderate` / `complex`
- `capability`: `native_answer` / `probe` / `skill` / `selected_agent_runtime` / `human_review` / `guardian`
- `signals`: 触发决策的证据信号
- `constraints`: 给执行器或 Agent 的约束

Guardian review 现在返回：

- `decision`: `allow` / `allow_with_constraints` / `ask_user` / `block`
- `risk_level`
- `policy`
- `foresight`
- `decision_trace`
- `required_preconditions`
- `forbidden`
- `message_to_executor`

## 当前实施阶段

| Phase | 名称 | 状态 |
| --- | --- | --- |
| P0 | Foundation definitions | Completed |
| P1 | State and probe hardening | Completed |
| P2 | Decision, Guardian, and Tool Proxy policy depth | In progress |
| P3 | Agent adapter execution contracts | Pending |
| P4 | Rollback, audit, and verifier depth | Pending |
| P5 | Web Control Console completeness | Pending |
