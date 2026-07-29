# Veyra Implementation Progress

本文档把架构设计拆成可落地的大块，并记录当前代码进度。代码里的单一事实来源是：

- `core/definitions.py`: 生命周期状态、操作模式、风险等级、Guardian 决策、风险分类规则。
- `core/architecture.py`: 8 个架构大块、Veyra Core 子模块、状态文件定义、实施阶段。

## 8 个架构大块

| Block | 代码位置 | 当前状态 | 下一步 |
| --- | --- | --- | --- |
| Veyra Core | `core/`, `awareness/`, `decision/`, `foresight/`, `guardian/`, `runtime/active_loop.py` | P8 continuous entity | 生产 supervisor 下的长期运行验收 |
| Interface Adapter / Agent Adapter | `interface/`, `runtime/agent_orchestrator.py` | P9 Feishu channel / multi-Agent | 连接真实 Feishu、Hermes、Custom 后做多 runtime soak |
| Probe Tools | `probes/` | P7 implemented | 按真实部署扩展更多环境专属 probe |
| Memory Bridge | `memory_bridge/` | P8 deep TTL metadata | 外部 memory provider 的生产语义验收 |
| Skill | `skills/` | P6 implemented | 增加更多低风险内置 skill 时保持 ExecutionResult 统一 |
| Tool Proxy | `tool_proxy/` | P7 implemented | 按生产 allowlist 开启 Browser/API executor |
| Rollback / Audit | `rollback_audit/` | P9 guarded auto replay | 非 snapshot 副作用 replay 继续保持人工确认边界 |
| Web Control UI | `web/`, `ui/` | P7 fixed workbench | 后续只接入新增本地运行指标，不引入 mock 数据或 SaaS 门户假设 |

## Veyra Core 子模块

| Module | 代码位置 | 当前状态 |
| --- | --- | --- |
| Runtime Entity | `core/runtime_entity.py` | P8 continuous entity |
| Awareness Loop | `core/awareness_loop.py`, `runtime/active_loop.py` | P8 continuous entity |
| Attention Core | `awareness/attention_core.py` | MVP foundation |
| Belief & Uncertainty Core | `awareness/belief_core.py`, `awareness/uncertainty_core.py` | P8 deep TTL/source trust |
| WorldState | `core/world_state.py`, `state/*.json` | MVP foundation |
| Agency Core | `core/agency_core.py`, `agency/` | P8 proactive runtime |
| Perception Layer | `core/perception_layer.py` | MVP foundation |
| Persona Engine | `core/persona_engine.py`, `personas/` | P9 channel/Agent binding |
| Decision Core | `core/decision_core.py`, `decision/` | MVP foundation |
| Foresight Engine | `core/foresight_engine.py`, `foresight/` | MVP foundation |
| Guardian / Execution Controller | `core/guardian_controller.py`, `execution/` | MVP foundation |
| Verifier | `core/verifier.py` | P4 completed |
| Context / Patch Builder | `core/context_patch_builder.py`, `core/*_patch_builder.py` | MVP foundation |
| Agent Orchestrator | `runtime/agent_orchestrator.py` | P8 validated multi-Agent |
| Runtime Cron | `runtime/cron.py` | P9 bounded scheduler |
| Replay Runtime | `rollback_audit/replay_runtime.py` | P9 guarded auto-execute |
| Runtime Trace / Metrics | `runtime/routing_trace.py`, `runtime/routing_metrics.py`, `routers/runtime_observability.py` | Runtime Stabilization telemetry |
| Context Drift Detector | `core/context_drift_detector.py`, `core/turn_context_builder.py` | Runtime Stabilization context guard |
| Route Split | `routers/`, `docs/main_route_inventory.md` | `main.py` first-stage route slimming |

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
| ChannelState | `state/channel_state.json` | Interface.ChannelRouter | 多通道 session、dedupe、outbox |
| PersonaState | `state/persona_state.json` | PersonaEngine | 当前 persona mode、通道、风险、Agent 绑定 |
| ActiveLoopState | `state/active_loop_state.json` | Runtime.ActiveRuntimeLoop | 定时主动循环状态、tick、步骤结果 |
| RuntimeCronState | `state/runtime_cron_state.json` | Runtime.Cron | bounded scheduler job 状态 |
| ReplayRuntimeState | `state/replay_runtime_state.json` | RollbackAudit.ReplayRuntime | 自动 replay 候选、job、scan 时间 |

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
- `ActionProposal`: 写入 GuardianDecision、ToolTrace、Verifier verdict 和 ActionRecord/Audit；高危动作不能直接执行。

## 当前实施阶段

| Phase | 名称 | 状态 |
| --- | --- | --- |
| P0 | Foundation definitions | Completed |
| P1 | State and probe hardening | Completed |
| P2 | Decision, Guardian, and Tool Proxy policy depth | Completed |
| P3 | Agent adapter execution contracts | Completed |
| P4 | Rollback, audit, and verifier depth | Completed |
| P5 | Web Control Console completeness | Completed |
| P6 | End-to-end runtime hardening | Implemented, live runtime validation pending |
| P7 | Production operations and safety validation | Implemented, production soak validation pending |
| P8 | Continuous awareness entity runtime | Implemented, bounded local self-tested |
| P9 | Chat app integration and guarded automation | Implemented, Feishu local OpenAPI self-tested |
| P10 | Runtime Stabilization and local release hardening | Implemented, real Feishu/OpenClaw soak pending |

## Agent Adapter Contract

P3 当前定义 `veyra.agent_adapter.v2`：

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
- MemoryBridge 支持 `local`、`selected`、显式 runtime provider 和 `all` fan-out；写入仍先经过敏感信息过滤，再写本地或提交外部 adapter。
- MemoryBridge 增加 provider diagnostics，可只读检查外部 summary 语义，也可显式执行 write probe。
- ExternalWorld 增加 watchlist refresh：对 URL/host 运行只读 web/network probe，再由 Core 模型解释外部状态与当前目标的相关性和 watch 建议。
- Web Console 增加 Core model 配置、Core model trace、ExternalWorld watchlist 和刷新入口。
- Agent 任务包增加 `tool_proxy_contract`，Verifier 会检查 Agent 回传的高风险 `tool_calls` 是否有 ActionProposal / review / policy / Tool Proxy trace 证据；无证据的 R3-R4 不给 verified success，R5 直接标记 forbidden。
- Tool Proxy 增加 `/tool-proxy/status` 和 `/tool-proxy/config`，SafeBrowser/SafeAPI 可显式启用 executor hook，并在执行前校验 host allowlist；默认继续关闭。
- Rollback/Audit 增加 ActionJournal、time-travel summary、非破坏性 replay plan 和受 ReviewQueue 保护的 replay compensation proposal；控制台展示 journal 来源统计和最近审计事件。
- P7 OpsMonitor 增加 `/ops/health`、`/ops/alerts`、`/ops/deployment`，控制台展示健康状态、告警数和部署 readiness。
- P7 AlertDispatcher 增加本地 `alert_log.jsonl` 和可选 webhook 投递；控制台可手动 dispatch alerts。
- P7 RetentionPolicy 增加 `/ops/retention/enforce`，对超限 JSONL trace 先归档再截断，并写入审计记录。
- P7 SoakRunner 增加 `/ops/soak/status`、`/ops/soak/start`、`/ops/soak/stop`，把长期健康巡检写入 `ops_soak_state.json` 并支持停止。
- P7 DeploymentConfigValidator 增加 `/ops/deployment/config`，检查 Agent URL、Core model、Alerting、Tool Proxy executor 和 state root 配置。
- P7 RuntimeMatrix 增加 `/ops/runtime-matrix` 和 `/ops/runtime-matrix/run`，逐个检查 OpenClaw/Hermes/Custom 的连接、capabilities、memory diagnostics 和 task status 降级。
- AgencyCore 写入 intention queue，proactive check 会对 state gap 做 Foresight + Guardian 审查。
- network / web / hermes / mcp probe 改为真实只读探测，并由 PerceptionLayer 标记常见异常；stale belief 可通过只读 probe 刷新。
- Memory Bridge 增加外部 adapter hook、敏感信息阻断和 freshness / trust 标记。
- P7 增加非破坏性红队安全检查、日志保留策略执行和 bounded/session soak API。
- P8 ActiveRuntimeLoop 增加 `/runtime/active-loop`、start/stop/tick API：定时执行 heartbeat、pending Agent task refresh、stale Belief refresh、主动 probe、ExternalWorld refresh、Replay runtime 和 retention summary；可选 runtime matrix。
- P8 AgentOrchestrator 增加 `/agents/certification`、`/agents/certification/run`、`/agents/invoke`：用户可以指定一个或多个 Agent；未配置或未认证 adapter 会被跳过，R3/R4 进入 review，R5 阻断。
- P8 Interface 增加本地多通道 state：`/channels`、`/channels/{channel}/messages`、`/channels/outbox`、`/channels/sessions`，记录 session、dedupe 和 outbox，不伪造外部 IM 平台投递成功。
- P8 BeliefCore 增加 TTL remaining、source trust、refresh_count、history、expired/stale/conflict 汇总，并开放 `/belief/status` 和 `/belief/refresh`。
- P8 ReplayRuntime 增加 `/audit/replay/runtime/status|scan|run`，从 ActionJournal 自动扫描失败/需回滚候选，生成受 Guardian review 保护的 R4 compensation proposal，不自动执行 restore。
- P9 Feishu channel 增加 `/channels/feishu/config`、`/channels/feishu/send`、`/integrations/feishu/events`、`/integrations/feishu/import-openclaw`、`/integrations/feishu/ws/*`：支持 app credential 获取 tenant token、发送文本消息、URL verification、`im.message.receive_v1` 文本回调、从 OpenClaw 本地配置导入、WebSocket 长连接和同会话回复。
- P9 Runtime Cron 将 `runtime/cron.py` 从占位替换为持久化 bounded scheduler，可触发 active awareness tick，不执行任意外部命令。
- P9 PersonaEngine 绑定 channel/risk/route/Agent policy，写入 `persona_state.json`，并进入 Agent task packet、execution trace 和 runtime API。
- P9 ReplayRuntime 增加 `/audit/replay/runtime/config` 和 `/audit/replay/runtime/execute`：只有显式配置和请求同时允许 R4 snapshot restore 时，才会自动 approve review 并通过 ActionExecutor 执行 restore。
- P10 RuntimeTraceRecorder 记录真实消息 routing trace：route chain、latency、model/probe/agent 使用、context chars、estimated tokens、final route、failure reason 和 OpenClaw involvement；敏感 token、secret、完整私密消息不会写入 trace。
- P10 Telemetry API 增加 `/runtime/traces/recent`、`/runtime/traces/{trace_id}`、`/runtime/soak/status`、`/runtime/metrics/summary|routes|model-cost|failures`，供后续 UI 直接消费。
- P10 ContextDriftDetector 在 TurnContextBuilder 后、Core reasoning 前检测 context 过大、stale belief 注入、governance/persona 污染、上一轮 Agent 错误影响、persona 异常切换和 memory 过度注入；高分时压缩 context、移除 stale beliefs、降低历史权重并记录 warning。
- P10 ToolProxy guard smoke 覆盖文件读、普通文件写、`rm -rf`、`.env` 读取、restart service、`git push --force`；所有高危动作必须有 trace，不能直接执行。
- P10 本地发行硬化增加 `.env.example`、`scripts/install_local.sh`、`scripts/start_local.sh`、`scripts/status_local.sh`、`scripts/reset_local_state.sh`、`apps/desktop` Tauri 桌面壳、`scripts/start_desktop_dev.sh`、`scripts/build_desktop.sh`、`/setup/status`、`/setup/env` 和 GitHub gate smoke workflow。安装/启动现在共享 fail-closed Python 3.11 解析与 pinned dependency/CA 预检，LaunchAgent 使用校验后的 exact interpreter 并原子替换；Feishu 状态区分 worker alive、current-run connected 和 fresh inbound event。GitHub release 不应包含本地 `state/`、secret、OpenClaw device 或个人 commitments。
- P10 之后 route layer split 已继续覆盖 Cases、Tool Governance、Project Guardian、Phase 5 和 Phase 6 控制面。隔离导入得到 211 个 Starlette/FastAPI route objects：206 条 callable product `APIRoute`、4 条 OpenAPI/Swagger/Redoc framework route，以及 `/console` static mount。完整清单和 domain 分类见 `docs/main_route_inventory.md`；易漂移的源文件行数不再作为验收信号。

后续仍需要的是真实环境验收，而不是本地功能补洞。接口现在明确区分 `implemented`、`configured`、`validated`、`validation_pending`，未配置或未连通的真实运行时不会被标成已连接：

- P6/P8/P9：连接真实 OpenClaw/Hermes/Custom 和 Feishu 后运行 certification、multi-Agent invoke、runtime matrix、长任务停止、结果回传、memory diagnostics、Feishu callback/send 和状态过期刷新验收。
- P7/P8/P9：配置真实 alert webhook、生产 allowlist、API supervisor 和多 runtime/chat soak session 后做长时间验收。
- P10：2026-07-30 已验证本机 Conda `veyra` Python 3.11.15、LaunchAgent exact interpreter，以及飞书 current-run TLS 连接、真实 websocket 入站、`direct_answer`/`probe` 处理、provider-sent 回复和 duplicate 抑制。下一阶段重点仍是 fresh clone 本地启动验收、真实飞书/OpenClaw 长测、桌面后端 sidecar 打包、首次启动配置 UI、Telemetry UI、Context Drift 调参、ToolProxy 闭环生产验证和 UI 拆分；不建议继续盲目新增模块，也不按 SaaS 账号/租户/计费方向扩展。
