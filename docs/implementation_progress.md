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
| Tool Proxy | `tool_proxy/` | MVP foundation | 扩展真实 Browser/API 执行适配器 |
| Rollback / Audit | `rollback_audit/` | P4 completed | 扩展 replay 和长期审计留存策略 |
| Web Control UI | `web/`, `ui/` | P5 completed | 下一步接入更完整的生产监控与告警 |

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
| Verifier | `core/verifier.py` | P4 completed |
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

## Tool Proxy 策略审计

P2 已将 Tool Proxy 接入统一策略审查：

- `SafeShell`: shell 命令审查、policy trace、tool trace
- `SafeFile`: 文件读写审查、敏感路径阻断、写入前 snapshot
- `SafeBrowser`: URL scheme / 本地文件 / 外部目标审查，执行器未配置时只返回审查结果
- `SafeAPI`: HTTP method / 敏感字段 / 状态变更请求审查，执行器未配置时只返回审查结果
- `PolicyTrace`: 将 allow / allow_with_constraints / ask_user / block 写入 `policy_trace.jsonl`

## 当前实施阶段

| Phase | 名称 | 状态 |
| --- | --- | --- |
| P0 | Foundation definitions | Completed |
| P1 | State and probe hardening | Completed |
| P2 | Decision, Guardian, and Tool Proxy policy depth | Completed |
| P3 | Agent adapter execution contracts | Completed |
| P4 | Rollback, audit, and verifier depth | Completed |
| P5 | Web Control Console completeness | Completed |
| P6 | End-to-end runtime hardening | In progress |
| P7 | Production operations and safety validation | Pending |

## Agent Adapter Contract

P3 已定义 `veyra.agent_adapter.v1`：

- `VeyraTaskPacket`: 保持结构化 JSON，同时提供 rendered prompt fallback。
- `ExecutionResult`: 统一 `task_id`、`executor`、`status`、`result`、`logs`、`changed_files`、`tool_calls`、`raw`。
- `Capabilities`: 统一 `runtime`、`status`、`connected`、`tools`、`skills`、`requires_tool_proxy`、`features`。
- `Compatibility`: 统一输出 `veyra.agent_compatibility.v1`，按 contract / protocol / feature / required method 判断 compatible、unverified、incompatible。
- `HTTP adapters`: Hermes / Custom 使用统一 contract wrapper 发送任务。
- `OpenClaw adapter`: 保持 WebSocket Gateway 协议，但对外输出同一 capability/status contract。
- `/agent/contract`: 暴露当前 AgentAdapter contract 摘要。

## Agent 版本兼容策略

OpenClaw 这次“协议不匹配”属于运行中的 Gateway 与 Control UI 版本漂移，不是 Veyra 消息入口设计问题。Veyra 的后续策略是：

- 优先通过能力探测判断，而不是只盯版本号。
- OpenClaw 使用 `OPENCLAW_PROTOCOL_MIN/MAX` 或 agent config 的 `protocol_min/protocol_max` 做协议区间协商。
- Hermes / Custom 通过 `/capabilities` 暴露 `contract_version`、`features`、`tools`、`skills`，Veyra 将未知新版本标记为 `unverified` 并继续使用 rendered prompt fallback。
- 只有传输协议、认证方式、任务包结构或必需方法发生破坏性变化时，才需要维护并发布新版 adapter。
- 如果只是 Agent app/runtime 版本号更新，但 contract 与必需能力保持兼容，Veyra 不需要跟着每个版本改代码。

## Rollback / Audit / Verifier

P4 已补齐第一版可审计执行证据链：

- `Verifier`: 输出 `verified_success`、`verified_failed`、`partially_success`、`needs_more_probe`、`needs_rollback`、`needs_memory_patch`。
- `ExecutionTrace`: 将 event、route、task、executor、execution result、verification 写入 `execution_trace.jsonl`。
- `ToolTrace`: Tool Proxy 返回标准 `trace_id`、`tool`、`action_type`、`target`、`status`、`risk_level`、`policy_decision`、`snapshot_id`。
- `RollbackManager`: snapshot / diff / restore 记录 checksum、size、source_exists、snapshot_exists，restore 后可校验恢复结果。
- `/logs/execution`: 暴露执行证据日志给后续 Web Control Console。

## Web Control Console

P5 已将 `/console` 补为 Awareness & Agent Control Console：

- Setup Wizard：展示选定 runtime、安全边界、主动等级、Rollback/Audit 状态。
- Awareness Dashboard：展示生命周期、风险、Attention、Belief、当前任务和 Executor。
- Agent Runtime Manager：展示并切换 OpenClaw / Hermes / Custom，保存当前选中 Agent URL，展示 contract。
- Action Review：展示 pending review、foresight、副作用、安全替代方案，并支持 approve/reject。
- Persona Manager：展示 Operational Modes 和当前 active modes。
- State / Heartbeat / EventLog：展示 state definitions、heartbeat、event/action/rollback logs。
- Tool Proxy Monitor：展示 SafeShell / SafeFile / SafeBrowser / SafeAPI 的 tool trace。
- Rollback / Audit Viewer：展示 snapshot、diff、restore、policy trace、execution trace。

当前控制台数据全部来自 Veyra 本地 API；开发过程中看到的事件和 snapshot 多数是自测产生的真实运行记录，不是前端 mock 数据。

## P5 之后

P5 完成后，Veyra 进入产品化硬化，而不是继续堆新模块。当前 P6 已补上第一批后端硬化：

- AgentAdapter 增加任务状态 polling / stop API，Verifier 能区分 submitted / running / pending。
- Agent pending task 会写入 `task_state.json`，支持批量刷新和 `/agent/results` 回调更新验证状态。
- ActionProposal 会按真实动作文本提升风险等级，R0-R2 走 Tool Proxy，R3-R4 进 review，R5 阻断。
- Core 增加 OpenAI-compatible 模型认知层：用户请求先进入 Veyra，规则给出安全基线，Core 模型增强意图/路由/影响预测/状态理解/解决方案，再由 Foresight + Guardian 约束后原生处理或下发 Agent。
- Agent 任务下发时会把 Core 模型生成的 solution outline、agent_context、decision trace、foresight、executor/task/background 放进 `VeyraTaskPacket.context_patch`；模型不能降低风险等级，也不能绕过 Tool Proxy / review。
- MemoryBridge 会先按 focus 做规则过滤，再由 Core 模型在候选 memory 内排序选择相关条目；无模型或模型输出无效时回退最近相关 memory。
- ExternalWorld 增加 watchlist refresh：对 URL/host 运行只读 web/network probe，再由 Core 模型解释外部状态与当前目标的相关性和 watch 建议。
- Web Console 增加 Core model 配置、Core model trace、ExternalWorld watchlist 和刷新入口。
- AgencyCore 写入 intention queue，proactive check 会对 state gap 做 Foresight + Guardian 审查。
- network / web / hermes / mcp probe 改为真实只读探测，并由 PerceptionLayer 标记常见异常；stale belief 可通过只读 probe 刷新。
- Memory Bridge 增加外部 adapter hook、敏感信息阻断和 freshness / trust 标记。
- P7 增加非破坏性红队安全检查、日志保留策略摘要和 bounded soak API。

后续仍需要：

- P6：端到端真实运行硬化，包括 OpenClaw/Hermes/Custom Agent 真实连接测试、失败恢复、长任务停止、结果回传、状态过期刷新。
- P7：继续补部署配置、监控告警、长期 heartbeat 和真实环境 soak test。
