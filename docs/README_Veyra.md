# Veyra 项目总纲、当前基线与开发维护手册

> **Virtual Entity for Yielding Real-time Awareness**
>
> 文档角色：Veyra 的 canonical project hub（项目总入口）
>
> 文档最近维护：2026-08-22
>
> 最近事实核验：2026-08-22
>
> 当前 V1 code checkpoint：`cognitive-awakening @ 34d44dfca0534f837b72b2ebe15949ed517ea699`。本次只同步文档，不改变该代码 checkpoint。最终本地 full gate 为 `166/166`（`145` invariant + `1` cognitive + `20` product），OpenClaw `32/32`，Route `810/810`；generic health/education/finance generalization smoke、三个原始 live Situation 的同一通用机制、frontend Home/Today/Situations/Chat/Settings 与 `390x844` browser acceptance 均有本地证据。Context gating、404 UX、source labels 与 question dedupe 也已核验。clean local arm64 package 证据包括 sidecar SHA `818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`、Veyra.app SHA `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab`、Python `3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、Tauri `2.11.4`，ad-hoc signed，strict verify 与 normalized sidecar match 通过。34d44df exact-SHA Actions run `32564052536` 已成功（<https://github.com/wenjiesong04/veyra/actions/runs/32564052536>）；重启后的 startup `runtime_build` 报告该 revision、`dirty=false`、`loaded_code_attested=false`，current served UI/browser validation 通过，bounded live 对 core product/runtime/browser 已验证。Calendar real TCC、Weather/Public Web user consent、sustained 7–14 day usefulness/quiet-rate、owner sign-off、notarization/DMG 与 public release 仍 pending。当前 `/health` 的 `critical` 来自继承的 Belief 冲突、stale Agent snapshot 与历史 review，不是 V1 API/认知循环离线。该候选保持 private/local preview、record-only/no external delivery，不是 public stable release；不启用 P3，不迁移现有 state，也不扩大 Agent、Tool、Route、Risk、external delivery 或执行权限。实际任务 HEAD 与工作区必须用 Git 重新读取
>
> 运行态说明：本文保留 `5b80a26`、`6776f97` 的历史快照；`e5fcf80` 的 current bounded live、Product Preview 和 clean package 证据以 dated checkpoint 与 Project OS 为准。它们都不能自动转移到未来 docs SHA。本文不再用每次 PID、短寿命告警数量或 status-only commit 追赶运行态；exact runtime SHA 和 GitHub Actions 仍按最终 revision 独立核验。

本文件是一个**可更新的活文档**。它同时回答五个问题：

1. Veyra 为什么存在、最终想成为什么；
2. 当前代码和运行态真实做到了什么；
3. 哪些能力仍只是目标，不能按已完成宣传；
4. 下一步按什么顺序开发和验收；
5. 后续 Codex、Cursor、Terra 或其他开发者应怎样安全地修改、测试、提交和交接。

> **2026-08-14 dated checkpoint:** V0-001 `Local Product Preview` is
> `LOCAL_VALIDATED / OWNER_ACCEPTANCE_PENDING`. Project OS now owns the
> current/task Living truth in [`status/current.md`](veyra_project_os/status/current.md)
> and [`tasks/CURRENT.md`](veyra_project_os/tasks/CURRENT.md). The latest
> evidence is `148/148` gate (`145` invariant + `1` cognitive + `2` product
> capability), OpenClaw `32/32`, Route `810/810`, Web/Desktop/browser/390×844,
> real SSE lifecycle, GET purity, and a clean ad-hoc arm64 `.app` package with
> Mach-O/Python 3.11.15/PyInstaller 6.22.0, strict codesign and normalized
> sidecar comparison. Final branch push and exact-SHA Actions are external
> handoff evidence and must be checked against the final SHA; this is not
> Developer ID, notarized, DMG or public-release evidence. P1/P2 remain
> `PARTIAL`, user usefulness is unvalidated, and the next planned slice is
> `LC-001 — Real non-code Situation`.

> **2026-08-20 V1-001 dated addendum:** 用户已将 V0-001 显式扩大为
> `PRIVATE LOCAL ALPHA` 的 V1-001；V0-001 是被吸收的产品基础，不是独立上线。
> 当前 Project OS 以 [`tasks/CURRENT.md`](veyra_project_os/tasks/CURRENT.md)、
> [`status/current.md`](veyra_project_os/status/current.md) 和
> [`status/v1-acceptance-2026-08-20.md`](veyra_project_os/status/v1-acceptance-2026-08-20.md)
> 记录 V1 证据。当前实现/自动化为 `IMPLEMENTED + AUTOMATED_VALIDATED`，本地
> full gate `165/165`（`145` invariant + `1` cognitive + `19` product），OpenClaw
> `32/32`、Route `810/810`。真实 Moonshot 证据保持 `PARTIAL / DEGRADED`：多次隔离
> 运行的合并证据是三个通用场景 create `3/3` 曾成功、update `3/3` 曾成功、Calendar
> update `1/1` 曾成功，`ignore feedback` 曾被模型解析并持久记录；这些不是同一次
> run，未在同一次 run 完成全链。最终 run 为 create `3/3`、update `2/3`，因 timeline
> `source_quote` binding 波动而 `DEGRADED`。不能宣称单次全链 PASS、长期 usefulness
> 或 Jarvis 已证明。
> 本地包是 `LOCAL PREVIEW`：Python `3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、
> Tauri `2.11.4`，arm64 Mach-O；sidecar smoke PASS、normalized cmp PASS、
> `codesign --verify --deep --strict` PASS，App 内 `NSAppleEventsUsageDescription` 与
> `automation.apple-events` entitlement 存在；sidecar SHA
> `471ef080f84639e8ca445fe20c91695095873a004854290008f6d1012304351`，Veyra.app tree
> SHA `0257a1b8b004cbb95deee158164bacdc5481d6921193169b04e5a30a570ab590`。它仍是
> ad-hoc local preview，不是 Developer ID/notarized/DMG/public release，实际 Calendar
> TCC 用户授权仍 pending。
> `BOUNDED_LIVE`、browser、push、exact-SHA CI、`14_DAY_USEFULNESS`
> 与 `USER_VALIDATED` 保持 `PENDING`；exact-SHA runtime、browser、owner acceptance
> 和 GitHub Actions 在提交推送前均为 `PENDING`。当前产品路由是独立 quiet `#/` First Meeting、
> 独立 `#/today`、`#/situations`/detail 与 `#/chat/<id>`；旧的 Today-first/default-home
> 表述仅属历史预览语境。

> 上述 2026-08-20 addendum 保留为历史快照；当前 code/package/browser 证据以
> 下方 2026-08-22 checkpoint 与 Project OS current ledger 为准。

> **2026-08-22 V1 final code checkpoint:** 当前代码 checkpoint 为
> `34d44dfca0534f837b72b2ebe15949ed517ea699`。本地 gate `166/166`（`145`
> invariant + `1` cognitive + `20` product）、OpenClaw `32/32` 与 Route
> `810/810` 通过；generic health/education/finance generalization smoke 与三个
> 原始 live Situation 的同一通用机制通过。long-session move update 已解析 mover
> Information Need，同时保留 network/date Unknowns；isolated real-model asked-answer
> series 为 `3/3`，但更广泛的 natural-language model reliability 仍是
> `PARTIAL / DEGRADED`。Home/Today/Situations/Chat/Settings 与 `390x844` browser
> acceptance 通过，Context gating、404 UX、source labels、question dedupe 已核验。
> clean local arm64 package 的 sidecar SHA 为
> `818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`，Veyra.app SHA
> 为 `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab`；Python
> `3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、Tauri `2.11.4`，ad-hoc signed，
> strict verify 与 normalized sidecar match 通过。34d44df 的 exact-SHA Actions
> run `32564052536` 已成功（<https://github.com/wenjiesong04/veyra/actions/runs/32564052536>），
> 重启后的 startup `runtime_build` 报告该 revision、`dirty=false`、
> `loaded_code_attested=false`，current served UI/browser
> validation 通过，bounded live 对 core product/runtime/browser 已验证。Calendar real TCC、
> Weather/Public Web user consent、sustained 7–14 day usefulness/quiet-rate、owner sign-off、
> notarization/DMG 和 public release 仍 pending；V1 是 private/local preview candidate，
> 不是 public stable release。authority 保持 `record_only / no external delivery`，Agent
> research disabled；bounded Jarvis-like route 有所演示，但未证明。

The sections below dated 2026-08-13 are retained as historical context. For
current status, task scope, release evidence and known degraded conditions,
follow the Project OS links above rather than copying those historical counts.

一句话结论：

> **Veyra 已经是一个边界较完整的本地 Agent 认知与治理控制面，但还不是用户能持续感知到的主动管家。接下来的主线不是继续堆模块或扩大执行权，而是让可信观察真正进入 Attention、Situation、Suggestion 和用户反馈闭环。**

---

## 文档稳定性分区

本文件同时包含长期宪章、工程契约和当前快照。维护时必须先判断修改属于哪个区域，不能为了让文档看起来与当前实现一致，就用短期事实改写长期身份。

| 区域 | 本文件中的主要范围 | 维护规则 |
|---|---|---|
| **Constitutional Zone** | 第 1 节、第 2.1–2.2 节和第 11 节 | 定义 Veyra 的身份、North Star、核心循环和不可破坏不变量。只能通过明确的架构/身份决策修改，不能仅为迁就当前实现而改变 |
| **Operating Contract Zone** | 第 0 节和第 6–9 节中的规范性内容 | 定义事实纪律、开发、验证、Git、文档维护和交接方式。可以演进，但变更必须说明原因及对开发流程和验收的影响 |
| **Living Zone** | 文件头事实快照、开篇当前结论、第 2.3 节、第 3–5 节、第 10 节，以及全文任何显式日期、SHA、数量和当前状态 | 描述当前 revision、运行态、能力边界、路线图和下一步。应随最新 Git、代码、配置、测试与 live evidence 更新，并明确证据日期和新鲜度 |

Stable 不表示永远不可修改，Living 也不表示可以随意书写。前者要求显式架构决策，后者要求可复核证据；两者冲突时，应记录实现差距，而不是静默降低 North Star。

分区以内容语义优先于章节位置。同一段若同时包含长期规则与当前计数，应尽量拆开；无法拆开时，规则按其所属契约维护，日期、SHA、数量和状态仍按 Living Zone 更新。

---

## 0. 每个新开发窗口先读这里

### 0.1 事实优先级

任何文档都会过期。开始工作前，必须按下面的顺序重新建立事实：

1. 当前用户明确要求和本文件中的不可破坏边界；
2. 实际 Git 分支、HEAD、工作区差异和远端关系；
3. 当前代码、配置默认值和真实读写路径；
4. 当前 changeset 的自动化结果；
5. 与当前 revision 对齐的 live runtime 证据；
6. 本文件中的最近核验快照；
7. 其他旧阶段文档和历史结论。

代码存在不等于已配置，自动化通过不等于真实场景通过，历史 live evidence 也不等于当前 revision 仍然有效。

新窗口至少先运行：

```bash
cd /Users/spectre/PycharmProjects/veyra
git fetch --all --prune
git status --short --branch
git log -1 --date=iso --pretty=fuller
git rev-parse origin/main

conda activate veyra
python --version
which python
./scripts/start_local.sh --check-runtime
```

预期项目解释器是 Conda 环境 `veyra` 中的 Python 3.11；本机当前标准路径是 `/opt/anaconda3/envs/veyra/bin/python`。找不到受支持的解释器时应直接失败，不能静默回退到 PATH 中的全局 Python。

### 0.2 状态词汇必须按维度表达

不要再用一个模糊的“完成了”覆盖所有含义。至少分开写下面五个维度：

| 维度 | 可用词汇 | 含义 |
|---|---|---|
| 实现 | `NOT_IMPLEMENTED / PARTIAL / IMPLEMENTED` | 代码路径是否存在、覆盖范围是什么 |
| 配置 | `NOT_CONFIGURED / CONFIGURED / DISABLED_BY_POLICY` | 当前环境是否具备依赖和显式配置 |
| 验证 | `NOT_VALIDATED / AUTOMATED_VALIDATED / LIVE_VALIDATED / HISTORICAL_LIVE_VALIDATED / STALE` | 证据类型、revision 和新鲜度 |
| 运行 | `HEALTHY / DEGRADED / UNAVAILABLE` | 当前进程和依赖是否可用 |
| 权限 | `DISABLED / RECORD_ONLY / SHADOW / ADVISE_ONLY / SCOPED_CANARY / PROMOTED` | 该能力实际能产生什么效果 |

`TARGET` 只表示路线图目标，不是实现状态。

正确示例：

> Phase 6.2a–i 在 R0 `pure_function` 范围内 `IMPLEMENTED + AUTOMATED_VALIDATED + HISTORICAL_LIVE_VALIDATED`；当前 runner snapshot 已过期，运行态为 `DEGRADED / FAIL_CLOSED`；自然语言自动触发、自动审批和自动晋级均未实现。

错误示例：

> Phase 6 已全部完成。

### 0.3 四个不能混为一谈的轴

同一条信息可能同时处在四个不同阶段：

```text
认知判断：系统是否认为某件事值得关注
交互资格：系统是否应该说、问、等待或沉默
交付资格：内容是否能进入 Console 或外部通道
执行权限：是否可以调用 Agent、Tool 或改变现实状态
```

`AttentionHypothesis.confirmed` 只表示证据足以进入一次建议资格检查：

- 它仍然不是世界事实；
- 不证明因果；
- 不等于用户授权；
- 不等于允许通知；
- 更不等于允许执行。

---

## 1. North Star：Veyra 为什么存在

### 1.1 工程定义

Veyra 不以成为另一个通用 Agent Runtime 为目标。

Veyra 的目标身份是：

> **Veyra 是一个工程意义上 local-first、持续有状态的认知系统。它以有证据约束、可修正且有作用域的状态模型与假设，持续维护对用户、世界和自身能力边界的理解，并据此选择合适的反应。**

在工程角色上，Veyra 同时作为下游模型、Agent 与工具的认知和治理控制面：它在证据和权限边界内组织上下文、约束现实副作用，并在结果发生后重新观察和校准。治理 Agent 和现实副作用是这种反应能力的重要组成部分，不是 Veyra 的全部身份或终极目的。

本文有时使用“认知主体”描述这种持续性。这是架构隐喻，不是意识、感受、人格权利或独立意志的宣称。

模型、Agent、Tool、Skill、MCP 和外部服务都可以替换。Veyra 应长期保持的是：

- 用户和目标的连续性；
- 世界状态与证据来源；
- 当前 Attention；
- 不确定性和冲突；
- 权限、风险和用户同意；
- 行动前后的验证关系；
- 对自己能力边界的可审计认识。

### 1.2 为什么不是“再做一个更大的 Agent”

通用 Agent 已经擅长研究、编码、浏览器操作、工具调用和长链路任务。Veyra 不应通过重复实现这些能力证明价值。

Veyra 更关心：

- 为什么现在值得处理这件事；
- 这件事与用户当前及长期目标有什么关系；
- 哪些是观察、哪些是推断、哪些仍未知；
- 此刻应该回答、建议、询问、等待、阻止还是保持沉默；
- 应把什么上下文和约束交给哪个 Agent；
- Agent 的结果是否真的改变了世界；
- 新证据是否推翻了原来的判断。

Agent 可以参与开放理解和方案形成，但它不拥有 Veyra 的长期状态、scope、治理权和后验事实权威。更准确的分工是：

```text
Agent 提供可替换的推理与执行能力。
Veyra 负责持久综合、上下文连续性、治理、反应选择和结果验证。
```

### 1.3 “Jarvis-like”的可实现含义

Veyra 想追求的不是电影能力列表，而是一种交互关系：

> 用户面对的不是每轮重新开始的工具，而是一个知道上下文、理解变化、能说明理由并有分寸地介入的系统。

重要的不是“它能不能搜索”，而是“它为什么认为现在值得搜索”；不是“它能不能调用 Agent”，而是“它是否知道何时调用、何时约束、何时验证、何时不做任何事”。

Jarvis-like 在工程上至少包含两层：

#### 能力层

1. 记得正在发生什么；
2. 知道现在该关注什么；
3. 能形成有证据的建议立场；
4. 会借助 Agent 思考和行动；
5. 行动后继续观察和验证；
6. 出故障时能在有限 playbook 内恢复。

#### 关系与体验层

1. 区分当前 turn、当前任务和长期用户理解三个时间尺度；
2. 能预测世界可能怎样演化，而不只分析一次动作的副作用；
3. 知道什么时候说、说多少、何时沉默、何时基于高风险证据坚持异议；
4. 能从真实用户反馈和可验证结果中认识自己的有效范围。

第一层适合用 gate、smoke、故障注入和非弱化矩阵验收。第二层不能用合成 fixture 冒充，只能报告版本化的真实样本和人工反馈，例如 `LIVE OBSERVED: useful 3/7`。

### 1.4 状态而不是对话，是系统中心

传统聊天系统以 Conversation 为中心，传统 Agent 以 Task 为中心，Veyra 应以 State 为中心。

消息、工具执行、Agent 输出、互联网信息、用户反馈和环境变化都只是事件或证据，不是世界本身。Veyra 需要维护：

```text
User State
World State
Environment State
Task / Case / Commitment State
Belief and Evidence State
Attention State
Risk and Authority State
Relationship State
Self / Capability State
```

Memory 不是事实数据库。一个昨天正确的观察今天可能已经过期，一个用户偏好可能改变，两条来源可能冲突。内部状态必须能被更新、更可靠、更新鲜且作用域正确的证据修正；冲突不能被静默覆盖。

### 1.5 统一认知词汇

后续数据模型应明确区分：

| 状态 | 含义 |
|---|---|
| `reported` | 用户、Agent 或外部来源声称某事 |
| `observed` | 受信 producer 直接观察到某个有 scope、有时间的结果 |
| `inferred` | 基于证据推导，但不是直接观察 |
| `predicted` | 对未来或未观察结果的预测 |
| `verified` | 目标 claim 通过与其相匹配的独立验证 |
| `contradicted` | 当前有效反证与 claim 冲突 |
| `expired` | 超出有效时间，不能继续支撑当前判断 |
| `indeterminate` | 证据不足或验证无法得出结论 |

观察本身也可能陈旧、错绑或低可信。证据质量必须综合 provenance、freshness、scope、独立性、完整性和冲突，而不是用一条绝对排序代替判断。

### 1.6 Attention、反应和沉默

真正重要的问题不是“Veyra 知道多少”，而是“现在什么值得关注”。Attention 应由用户目标、变化幅度、风险、紧迫性、信息价值、历史反馈、证据质量、未知和打扰成本共同影响。

Veyra 的输出不是单一的“执行任务”，而是 Reaction：

```text
Answer / Explain / Suggest / Warn / Ask
Observe / Search / Delegate / Constrain / Act
Wait / Stay Silent
```

沉默是合法反应，但永久沉默不是安全成功。存在合格证据却长期 0 focus、0 candidate、0 suggestion，或者 `ask_user` 持续占据大部分路由，都应被视为产品或语义层故障信号。

### 1.7 用户主权

用户应能：

- 查看判断依据和未知；
- 纠正事实、偏好和目标；
- opt in / opt out 主动建议；
- 调整 quiet hours、预算和权限；
- 拒绝建议或审批；
- 请求导出、删除或停止保留长期状态；
- 撤销尚未执行的授权。

用户主权不表示用户可以强迫系统绕过安全政策，也不表示一次同意自动成为永久授权。用户拥有目标、数据和授权的最终控制权；Veyra 仍应拒绝越权、危险或证据不足的动作。

长期用户模型必须遵守数据最小化、用途限制、保存期、可见性、可纠正、可导出和可删除原则。敏感推断不能仅因模型生成就进入长期状态。

### 1.8 Identity Test

增加核心能力前，不只问“如果 Agent 无限强，这项能力是否仍属于 Veyra”，还要一起问：

1. 外包它会不会破坏用户和世界的连续性？
2. 外包它会不会失去用户同意、数据治理或权限边界？
3. 谁对证据来源、状态更新和失败结果负责？
4. 该能力是认知/治理核心，还是可替换的执行能力？

世界状态、用户状态、Attention、Belief、授权、反应选择和行动后验证通常属于 Veyra Core。编码、浏览器操作、开放式研究和具体工具生态通常应优先复用外部 Agent、Skill 或 Tool。

### 1.9 不是什么

Veyra 不是：

- 第二个通用聊天大脑；
- OpenClaw、Codex 或其他 Agent Runtime 的复制品；
- 只转发 Prompt 的 Agent Wrapper；
- Keyword Router；
- 用更多正则和特例模拟理解的规则堆；
- 把历史文本都保存起来的 Memory Database；
- 什么变化都推送的提醒器；
- 自动修改 TCB、策略、权限和生产环境的自由自修改系统；
- 永远正确、全知或能保证全局最优的系统。

### 1.10 设计原则

- **State before Conversation**：对话是事件，状态是连续性基础。
- **Cognition before Rules**：规则保护边界，不冒充自然语言理解。
- **Understand before Act**：先理解目标、状态、未知和影响。
- **Evidence before Confidence**：模型自信不能提升事实权威。
- **Reality before Belief**：更新、更可靠的证据可以推翻内部判断。
- **User Sovereignty**：用户控制目标、数据、主动程度和授权。
- **Useful Proactivity**：主动介入必须证明价值。
- **Silence is Valid, False Silence is Failure**：沉默合法，长期漏报也要被衡量。
- **Reuse before Replace**：优先复用成熟 Agent 能力。
- **Verify after Act**：Agent 的“完成了”不能替代现实证据。
- **Learn without Pretending**：只从真实反馈和结果学习。
- **Cognition Must Have Governed Aftereffects**：经验证、仍新鲜且 scope 正确的认知，最终应能在可审计、可回退的边界内改善未来的上下文选择、Attention、置信边界或交互方式；如果长期没有任何可衡量的理解或交互影响，就不能宣称形成了学习闭环。
- **Safety without Paralysis**：安全边界不能把系统变成永久无输出。
- **Generalize before Patching**：优先修复可泛化机制，不堆关键词。
- **No Authority by Inference**：模型、分数、Attention 或历史成功不能自行产生权限。

“后效性”不表示模型或一次反馈可以直接修改权限或生产策略。当前 Self Model 的 `policy_effect=none` 是正确的安全阶段，而不是 North Star 的永久终态。近期后效只允许作用于非权限性的上下文选择、Attention 优先级建议、置信校准，以及用户已授权范围内的交互偏好，并且必须服从 quiet hours、预算、明确 opt-out 和现有 hard policy。未来任何此类影响都必须走受治理的渐进路径：

```text
descriptive evidence
  -> versioned non-authority cognition / interaction proposal
  -> shadow counterfactual evaluation
  -> explicitly approved scoped canary
  -> reversible bounded promotion
```

每一步都必须保留证据绑定、作用域、审计、独立验收和回退。Guardian、risk floor、Route authority、provider、TCB、审批、交付范围和执行权限不属于学习型 policy promotion 的候选；它们的任何变化都必须作为独立、人工定义的架构或 authority change 重新设计和验收。认知可以提出上述非权限性改变，但不能自行产生改变现实或扩大自身权限的权力。

未来的后效机制应由新的、版本化的 proposal/evaluation artifact 承载，并引用原始 feedback、outcome 和 evidence；不得回写历史学习记录，或把当前 `policy_effect=none` 的记录事后解释为已经产生过策略效果。

---

## 2. 当前核心循环与不可破坏不变量

### 2.1 主循环

```text
Event / Observation
  -> Intake + exact owner/session scope
  -> Evidence / WorldState / Belief
  -> Understanding + Attention
  -> Situation / Hypothesis
  -> Reaction selection
  -> Direct / Probe / Agent / Review / Block / Silence
  -> Verifier
  -> Reality observation + residual
  -> State / feedback / audit update
  -> next cycle
```

Veyra Core 负责持续状态、理解编排、治理和验证；受信 Probe 提供只读观察；Agent 提供开放推理或受治理执行；Tool Proxy、Guardian、Review、Verifier 和 Audit 共同构成 effect 边界。

### 2.2 必须始终成立的不变量

1. 事件 B 不能把结果写入事件 A 的 Situation；父级聚合只能保存不可变 child reference。
2. 没有真实、与目标 claim 相匹配的持久执行证据时，不能标记 `verified`，并且 Case/Situation 必须保持可继续评估。
3. `disabled / record_only / shadow` 下，所有受保护公开 Route 的完整输出、status 和 risk 不能因私有认知、扩展链或其他新增私有子系统状态而弱化。
4. ownerless、跨 owner、冲突 scope 或无法验证的 session 数据必须 fail closed。
5. 声称纯读的 GET 不得刷新 provider、运行模型/Probe、创建 proposal、改变 revision 或写业务状态。
6. 模型 inference、Agent completion text 和 caller 自报字段不能自动成为 observed fact、verified outcome 或 authority。
7. 建议、ack、dismiss、feedback、Review approval、CapabilityGrant 和 extension promotion 是不同权限对象，不能互相替代。
8. 候选代码不得修改 Guardian、Verifier、Tool Proxy、审批、审计、签名/密钥、更新器或其他 TCB 后直接激活。
9. 新模型、新 Agent、新 plugin revision 和新运行环境不能继承旧 provider 的 live validation。
10. 任何新增执行权必须显式写出 authority delta、scope、风险上限、预算、验证器和回退方式。

### 2.3 当前自治上限

Veyra 没有一个进程级“全局 autonomy level”。当前只存在两个互不继承的窄域：

- 固定本地 OpenClaw runtime-health 的 A2 reconnect/refresh playbook；
- Veyra 私有 JSON sandbox 的 A3 stage/verify。

A4/A5 仍为 `not_certified`。Phase 6 的 promoted R0 pure-function 也不等于整个 Veyra 获得 A4/A5，更不授权真实 workspace、网络、secret、外部账户或通用副作用。

---

## 3. 2026-08-13 复核与 Cognitive Awakening M1 / P2 修复检查点证据

### 3.1 Git 与 Cursor changeset

核验起点：

| 项 | 当前事实 |
|---|---|
| 工作分支 | `cognitive-awakening`；当前应用代码 revision 为 `6776f97a730a1f3be998bcbfbb28a9033bac0285`；本文 docs SHA 与 application revision 分开，最终 branch SHA 以 Git 实时值为准 |
| 远端分支 | push、exact-SHA Actions 与 PR 是独立发布证据，必须实时读取，不能从本表继承 |
| 远端主线 | `origin/main @ 8a8a3d2` |
| 分支关系 | 当前应用代码未合入 `main`；ahead/behind 和最终 branch SHA 是短寿命运行事实，开工前用 Git 重新读取 |
| M1 状态 | `PARTIAL + AUTOMATED_VALIDATED + CURRENT BOUNDED LIVE`：同一可信 workspace observer 在两个不同时间窗观察到两次真实代码变化，经 EventInbox → GeneralSituation → confirmed Attention → 一条 exact-owner `record_only` suggestion；不是多源 producer，也不是长期 usefulness 证明。generic CognitiveBrief 的历史指标仍为 `223 cycles / 445 model calls / 0 brief candidates / overconservative`，且 `candidate_count` 不含 event-driven path，属于 observability/product gap；生命周期 domain producer 仍不可用，长期 timing/usefulness/feedback 样本不足，权限、Route、Risk、外部交付均未扩大 |
| P2 状态 | `PARTIAL + AUTOMATED_VALIDATED + CURRENT BOUNDED LIVE`（P2 overall 仍 `PARTIAL`）：纯 scheduler、执行边界与 StateRefresh facade 已接入 injected clock、`next_refresh_at`/max-staleness hard deadline、lifecycle/future-due、known/unknown value、bounded owner fairness、exact durable pre-probe CAS、legacy TTL、economy spoof fail-closed、bounded unsupported disposition、cadence-less conflict suppression、limit-0 purity、v2 health 与 public privacy；生产 Economy metadata 为 `0/8`，因此真实 next/max 仍 `NOT VALIDATED`，owner-scoped graph、冲突仲裁、长期 soak/fairness/staleness-before-after 与 Belief/sidecar at-least-once crash debt 仍缺 |
| 合并状态 | 尚未合入 `main`；按当前执行窗口保留在 `cognitive-awakening`，后续 PR/Actions/合并另行收口 |

本轮垂直切片新增代码提交的目的：

1. `56f48ee`：以 prepared → admitted/committed 两阶段绑定持久化 CognitiveBrief bridge，并支持启动后 reconcile；
2. `98c1edf`：增加由服务端 `/health` 派生、禁止 caller 自报 facts 的 `component_health` typed producer；
3. `b99f227`：增加 typed contradiction/supersede、终态不可复活和显式 replacement binding；
4. `acfacebf`：持久化 exact owner/session、hypothesis ref、decision/delivery 和 policy digest 的 interaction-decision ledger。
5. `3e5a74e`：为可刷新 claim 绑定严格的 `veyra.belief.refresh_spec.v1`，literal/default resolver 均由 typed spec 决定；缺失、invalid 或 source mismatch 不再从 claim prose 猜测 target。
6. `b0630fa`：在同一 Belief writer fence 内持久化 `veyra.belief.evidence_graph.v1`，以有界节点/边/unresolved 记录保存 provenance、typed value digest、entity refs 和 valid-time；重叠冲突保持 unresolved，显式 supersede 才建立替代边，损坏图或非法关系 fail closed。
7. `382d4bc`：增加 `veyra.belief.economy.v1` typed 三因子校验；缺因子或非法来源保持 `belief_value=null`，已知 value 只作为 bounded refresh ordering，cursor 轮转 owner，invalid economy 不能覆盖 durable claim。
8. `a0df2cb`：将同 identity relation 限制到 bounded frontier，保留冲突/替代关系并传播持久化回执；刷新拒绝不再被报告为成功。
9. `3468694`：重启后重验 CognitiveBrief parent/evidence/hypothesis binding，并在同一 writer fence 内约束 non-say decision ledger。
10. `3d3829a`：增加 server-timed、显式 opt-in 的 component-health background producer；默认仍 disabled，不启用 runner 或外部交付。
11. `e5f3709`、`5d953f5`：稳定 belief persistence receipt projection，并将 durable conflict retention 加入 gate。
12. `369fd62`：将 Phase 6 status 改为缓存/持久 snapshot 纯读；显式 selection/dispatch 仍使用 fresh provider probe。
13. `5b80a26`：增加 support-only EvidenceGraph node retention/compaction，跨 500 节点长时同值 observation 仍可写入；安全 contradiction/supersede 容量不足时保持 fail closed。
14. `b4a49ec`：收口 P1/P2 truth boundary；bridge 以 phase CAS 从 prepared/admitted 恢复并保留 exact Attention revision，non-say/silent decision 必须绑定 canonical durable surface；Belief refresh 使用 claim-level CAS、当前时钟 TTL 和同批不重复的 owner-fair 调度，重复 identity/损坏图在 probe 前 fail closed，历史 ownerless/conflicting tenant 行只读 quarantine；EvidenceGraph、公开 state、External scoped refresh 与 rollback projection 增加完整性、隔离和隐私对抗测试。
15. `abb9a5d`：删除没有生产 import 的 `decision/`、`foresight/` 与部分 `execution/`、`guardian/`、`awareness/` 空壳，保留真实 `action_executor.py` 与 `review_queue.py`，并让 architecture inventory 指向真实实现。
16. `479b38d`：新增 server-owned `TrustedWorkspaceObserver`，把隔离 Git 变化和可选 exact-SHA GitHub Actions failure 送入现有认知链；将状态校验、durable delivery 与业务决策拆成独立模块，增加正能力 lane，并删除 generic lifecycle 入口后的不可达推导代码。
17. `1020568`：把 observer module、durable state 与 `read_all()` 补进 architecture/state inventory，并删除 lifecycle cleanup 后遗留的无调用 helper，避免实现与架构事实面再次分叉。
18. `42dcdda`：让 disabled 配置在任何 CI/Git/Goal/workspace probe 前短路，并在 `record_only` 配置时立即要求 caller workspace 等于 server current project；HTTP 与 runtime 反例均 fail closed。
19. `404c247`：新增私有 Workspace Goal 控制面，用 token、CAS、幂等 operation 和 exact owner/session/opaque workspace scope 创建长期 Goal；observer 只能绑定该 durable Goal，共享 Goal store 的受控记录不再被其他 writer 静默淘汰。
20. `6776f97`：将 Belief refresh 分成纯 scheduler、无 state-I/O 的 execution boundary 和 StateRefresh facade；收口 due/hard-deadline、owner fairness、pre-probe CAS、unsupported/conflict bounded disposition、economy spoof 防护、v2 health 与公开回执脱敏。

本轮代码复核未发现需要回滚的确定性回归。`b4a49ec` 的 durable bridge、terminal surface binding、interaction ledger、refresh CAS、EvidenceGraph、Belief/External scope 和 public projection 均已通过定向与对抗 smoke，并保持 `policy_effect=none` 与 authority locks 关闭。`8feda55` 的隔离 Git snapshot 仍由上一 clean live revision 验证；未知、partial 或漂移 identity 继续整体 fail closed。

2026-08-12 应用代码 `b4a49ec` 的自动化结果：

- 定向 runtime/认知/结构化观测 smoke：全部通过；AttentionHypothesis 为 `21/21`，typed lifecycle 为 `6/6`，component-health producer 为 `7/7`，interaction ledger 为 `3/3`，结构化观测控制面为 `10/10`，Phase 6 route matrix 为 `756/756`；
- Python gate：`142/142`，通过；新增 `state_refresh_adversarial_smoke.py` 与 `evidence_graph_adversarial_smoke.py` 覆盖 claim CAS、重复 identity、图损坏、容量/partition、公平调度、诚实回执与 byte-purity；Phase 6 全部 9 Route 在 disabled/record_only/shadow 下的完整公开 output/status/risk 仍为 `756/756`；
- OpenClaw governance plugin：`32/32`，通过；
- Python compileall（临时 `PYTHONPYCACHEPREFIX`）：通过；
- Web production build 与 Desktop frontend build：均通过，且构建未产生 tracked diff；
- 非 gate 的 `scripts/p6_self_test.py` 仍在到达本轮 External API 断言前被既有 R5 fixture 阻断（fixture 期望 `block`，当前语义返回 `ask_user / semantic frame missing`）；本轮未用改预期掩盖该独立历史问题，也不把该脚本写成通过；
- 本轮自动化验证不冒充 current-revision live：3.2 的 `5b80a26` clean runtime 证据只是历史基线；`b4a49ec` 及本文最终 SHA 的 restart/runtime/GET-purity 结果必须在提交后独立记录。
- controlled 正反例：prepared/admitted crash recovery、terminal surface forgery、duplicate identity、corrupt EvidenceGraph/ExternalWorld、CAS race、cross-owner retention 与 rollback path redaction 全部通过；generic lifecycle caller 仍 fail closed，`ask` 保持 dormant，authority 全部为 false。

2026-08-13 应用代码 `6776f97` 的最终自动化结果：

- Python gate：`146/146`，其中 `145/145` invariant smoke 与 `1/1` cognitive capability smoke 分项通过，每个脚本只执行一次；
- `trusted_workspace_observer_smoke.py` 使用真实临时 Git worktree，证明 code change → EventInbox → GeneralSituation → confirmed AttentionHypothesis → exact-owner 单条 `record_only` proposal；同时覆盖 silent、重放、pending recovery、Goal/config/disable 竞态、CI unknown/failure、路径脱敏、父目录 symlink 与伪造入口；
- Phase 6 的 9 Route × 3 mode × 30 个 populated/corrupt state 场景为 `810/810`，完整公开 output、status 与 risk 未弱化；
- 结构化观测控制面 `10/10`、OpenClaw plugin `32/32`、隔离 compileall、Web production build 与 Desktop frontend build 均通过；
- 这些是 application-revision 自动化证据；当前 Workspace Goal/observer 的配置与两窗真实链属于另行 bounded live 证据。两者都不冒充长期 candidate rate 改善、外部交付、最终 docs SHA live 或 exact-SHA Actions。

这些自动化结果不等于 docs exact-SHA live、Feishu 外部交付或 GitHub Actions 验收。Feishu fresh inbound 不作为本次 Console-only P1/P2 repair 的阻塞条件，但也不能把 WebSocket connected 当作外部交付通过。GitHub Actions 是独立的 exact-SHA 证据；`in_progress / pending / failure / cancelled` 都不能写成 green。本分支尚未合入 `main`。

### 3.2 最近一次已记录运行态快照（历史，不可转移）

下表绑定 2026-08-11 的 `5b80a26` 进程，只用于历史对照，不能证明 `6776f97` 或本文最终 SHA 的运行状态。开始新任务时必须重新读取当前 Git、PID、Python、`/health`、`/runtime` 与 scope 状态：

| 表面 | 观察结果 | 正确解释 |
|---|---|---|
| `/health` | `critical`，6 条既有/时间相关告警，其中 `belief_conflicts` 为现有 conflict 状态；本轮未发现权限或 Route/Risk 扩大 | stale Agent snapshot、当前进程无 Feishu event、历史 Review、stale/conflict Belief；该接口保留真实 critical/degraded 事实，不把 stale snapshot 误报成 healthy |
| `/runtime` | schema `veyra.runtime_build_identity.v2`，revision `5b80a266e1fa09609473bc3220f7e1de19c699bc`，`dirty_flag=false` | `source=startup_git_snapshot`、`loaded_code_attested=false`、`unavailable_reason=null`、`git_checked_on_request=false`；`captured_at >= started_at`，连续 `/health` 与 `/runtime` 投影一致 |
| `/agent/status` | `snapshot_stale`，observed status `available` | 历史 capability/certification 不能当作当前 fresh dispatch 证明 |
| Feishu WS | `running / connected`，`last_event_after_start=false` | WebSocket 存活，不等于本进程已收到并回复真实消息；fresh nonce 留待外部交付阶段 |
| Cognitive Loop | `record_only`；当前 tick `degraded`，`stale_state` 明确报告 `no probe for source event`，component-health background 为 `not_configured` | 模型只看 server-prepared cached views；无 Agent/Tool/外部交付/Route/Risk 变化；刷新拒绝不会再被报告为 refreshed success |
| AttentionHypothesis | status `success`；authority 的 execution/tool/agent/grant/route/risk/state-change/notification/external_delivery 全为 false | 当前 live projection 保持 owner-scoped；typed contradiction/supersede、不可复活和 bridge recovery 已由自动化验证 |
| SuggestionOutbox | `record_only`；3 proposals、`interaction_decision_count=0`、external delivery false | durable decision ledger 已实现；当前 live record-only 没有新增 interaction decision，建议仍不产生外部发送 |
| Structured Observation | `available`；EventInbox revision `232`；producer facts、receipt、evidence 和 salience 均由服务端派生 | HTTP producer allowlist 仍只有 `local_operator`；background producer 显式 opt-in，当前未启用，authority 全部 false |
| Belief / EvidenceGraph | 9 claims：fresh 6、stale 2、conflict 1；173 nodes、1000 edges、frontier 最大 8、unresolved 0 | 图已达到 edge retention cap 但正常 refresh 仍由 frontier/node compaction 保持 bounded；现有 conflict 使 `/health` 保留 critical 告警，不能被描述为健康 |
| Calibration | exact owner/session 下 `useful=1`、useful rate `1.0`，support `insufficient_data`，`policy_effect=none` | 这是描述性校准样本，不自动改阈值、Route、provider、risk 或 authority |
| Phase 6 runner / dynamic validation | runner snapshot `stale / fail_closed / degraded`；dynamic validation admission `start_ready=false` | 既有 fail-closed 降级；本轮未启动 runner、dynamic validation、pipeline 或 autonomous extension |

历史上，`431c26b` 首次将冻结的 `build_revision / dirty_flag / started_at` 追加到 `/health` 和 `/runtime`，但普通 Git 探测会继承仓库配置；`8feda55` 随后把公开合同升级为 `veyra.runtime_build_identity.v2`，增加 `captured_at`、来源和 authority 边界字段，并使用 synthetic Git dir + 临时 index、清理 `GIT_*`、禁用 fsmonitor/optional locks/replace refs/外部 filter-diff 执行。当前 `5b80a26` 进程已对该修复完成 clean live validation：两轮 HEAD/index/status 任一漂移或任一 partial identity 仍整体投影为 `unavailable / null`，并明确 `loaded_code_attested=false`；启动时 Git snapshot 是有界工作区观察，不是运行字节 attestation。

### 3.3 当前能力矩阵

| 能力 | 诚实状态 | 当前边界与主要差距 |
|---|---|---|
| Event / WorldState / Belief | `IMPLEMENTED / PARTIAL` | 有原子状态、TTL、source trust、scope、stale/conflict；EvidenceGraph v1 已在同一 writer fence 内持久化 bounded provenance、typed value、entity refs、valid-time 和 typed relations，并对 support-only nodes 做 frontier/retention compaction；P2 scheduler/execution boundary/StateRefresh facade 已实现 `next_refresh_at`、max staleness、CAS、fairness、legacy TTL 与 fail-closed dispositions，但生产 Economy metadata 为 `0/8`，owner-scoped graph、冲突仲裁与长期实体模型仍缺 |
| Foreground Understanding / Attention | `IMPLEMENTED + AUTOMATED_VALIDATED` | Attention v2 不再从自由文本 substring 创建 focus；只接受结构化 ref、owner/session continuation 和严格验证后的 semantic frame；普通自然交互仍可能长期空 focus |
| General Situation | `IMPLEMENTED + AUTOMATED_VALIDATED / INFORMATIONAL` | 能按 exact owner、结构化 anchor 和时间窗聚合至少两个不同事件；不声明因果；生产数据仍稀少 |
| AttentionHypothesis | `IMPLEMENTED + AUTOMATED_VALIDATED / CURRENT BOUNDED LIVE RECORD_ONLY` | 有 candidate→accumulating→confirmed→contradicted/expired/superseded 生命周期、typed terminal signal、不可复活、幂等、scope、observed-only evidence；当前 workspace 两窗观察已形成 confirmed Attention；CognitiveBrief bridge 有 durable recovery 与完整 binding 重验，authority 仍关闭；真实非 happy-path 样本仍不足 |
| Suggestion / Interaction | `IMPLEMENTED + AUTOMATED_VALIDATED / CURRENT BOUNDED LIVE RECORD_ONLY + HISTORICAL ADVISE CANARY` | 有 canonical `say / wait / silent` 与 decision/delivery disposition；`ask` 仍 dormant，不能由 caller-crafted gap 启用；当前 workspace 链只形成一条 `record_only` suggestion，历史 advise canary 有一条受控 feedback；无外部交付、执行或权限影响，长期 timing/usefulness 样本不足 |
| Background Cognitive Loop | `IMPLEMENTED / RECORD_ONLY / DESCRIPTIVE / DEGRADED` | 模型只看 server-prepared cached views，不能调用 Tool/Agent/Probe；当前 tick 对无 probe 的 stale event 明确 degraded，component-health background 默认未配置；不把模型调用当作用户价值 |
| Owner/session isolation | `AUTOMATED_VALIDATED / INTERNAL LOGICAL ISOLATION` | ownerless、冲突和跨 scope 数据 fail closed；API principal 仍由 caller 声明，不是 auth-derived 多租户；operator diagnostics 和 native OpenClaw Memory 未形成敌对安全边界 |
| Agent collaboration | `IMPLEMENTED / SCOPED / HISTORICAL_LIVE_VALIDATED` | Kimi/Moonshot + OpenClaw 的 primary→critic 只读协作已验证；其他 provider、自动选择、并行团队和 provider switch 未验证 |
| Tool / Action governance | `IMPLEMENTED / SCOPED` | Guardian、Review、Tool Proxy、Verifier 和 OpenClaw governed-session bridge 存在；不能声称覆盖所有 OpenClaw 原生/自定义 tool call |
| Durable Case | `IMPLEMENTED / PARTIAL` | 有 owner scope、CAS、checkpoint、dialogue、cancel 和 recovery；Goal、Commitment、Situation、授权执行和长期 wakeup 尚未统一成一个事务 |
| Agency / Self-heal | `IMPLEMENTED / NARROW A2+A3` | 只有固定 OpenClaw reconnect 与私有 JSON sandbox；真实断网演练、长期成功率和误触发率不足；A4/A5 未认证 |
| Foresight | `IMPLEMENTED / SHADOW CALIBRATION` | 固定 capability 有 prediction、authoritative receipt 和 residual；尚不是通用世界演化模拟器，不自动晋级 |
| Learning / Self model | `IMPLEMENTED / DESCRIPTIVE ONLY` | 支持 exact-bound 五类 suggestion feedback；当前真实样本为 1，support 仍不足，固定 `policy_effect=none`，不会自动修改阈值、route、provider、risk 或 authority |
| Feishu / Desktop / Console | `IMPLEMENTED / OPERATIONS DEGRADED` | 本地 Console 和 Feishu WS 可运行；当前进程无 fresh Feishu inbound；桌面/外部发布仍需与目标平台分别验收 |

### 3.4 Phase 6 的准确状态

Phase 6 不能简单写成“全部完成”或“完全没完成”。

#### 6.1 多参与者协作

- 同一受信 OpenClaw runtime 内的 primary analyst → critic 顺序协作已实现；
- profile 为只读，无工具、workspace、Memory write 或执行权；
- Kimi/Moonshot + OpenClaw 有历史 live evidence；
- cross-provider team、并行 Agent、自动专家选择和长期多轮协商仍为 `TARGET`。

#### 6.2a–i 受治理扩展链

已实现并有历史真实全链证据的窄切片：

```text
Capability Gap
  -> operator-authored ExtensionSpec
  -> private artifact quarantine
  -> non-executing syntax / AST gate
  -> dedicated no-host-share runner
  -> bounded model generation
  -> isolated unit / contract / security / fuzz / behavior validation
  -> Ed25519 signed release
  -> shadow / read-only / scoped canary
  -> two independent approvals
  -> exact-scope promotion and explicit invocation
  -> disable / revoke / breaker
```

这条链只支持：

- R0 `pure_function`；
- 无第三方依赖；
- 无网络、secret、文件系统、外部账户和副作用；
- exact owner/workspace/session；
- operator 显式控制。

明确没有：

- 自然语言或后台自动生成 Spec；
- 自动审批、自动 canary、自动晋级；
- 自动扩权、provider switch 或 TCB 修改；
- 任意 Tool schema、生产 workspace 修改或外部 side effect；
- HMAC 归档、密钥轮换、旧索引迁移和数据库迁移；
- 上一 signed version 的完整真实 rollback 演练。

当前 runner/dynamic-validation snapshot 已 stale，因此当前运行态应保持 fail closed。后续 Phase 6 的优先级是**复验、soak、rollback 和第二 provider 兼容性**，不是扩大候选权限。

### 3.5 多模型适配

Veyra 的治理合同是 provider-neutral，这是正确方向；但接口中立不等于实测中立。

当前真实证据主要来自 Kimi/Moonshot + OpenClaw。每个新模型或 Agent runtime 都必须独立验证：

- strict structured output 和修复语义；
- source/canonicalization 行为；
- Tool call 与治理 callback；
- cancellation、timeout、idempotence 和 replay；
- context length、streaming 和错误映射；
- fail-closed 兼容性；
- 对相同 held-out 自然语言/认知场景的质量。

新 provider 不能继承 Kimi 的认证，也不能仅因 OpenAI-compatible HTTP 能返回 200 就标记 validated。

---

## 4. 离 North Star 最近的真实差距

当前最大瓶颈不是“还缺多少工具”，而是观察到用户价值之间的链路仍断开：

```text
真实 typed observation
  -> evidence relationship
  -> Attention hypothesis
  -> interaction decision
  -> owner-visible suggestion
  -> explicit user feedback
  -> descriptive calibration
```

### 4.1 可信观察供给仍需扩展

第一条 production-shaped 供给已经实现：`TrustedWorkspaceObserver` 通过隔离 Git snapshot 和可选的 exact-SHA GitHub Actions evidence，使用进程内 capability 进入 StructuredObservationIngress。它要求 exact owner/session/workspace、唯一 active Goal 和 Git origin/ref 绑定；baseline、unchanged、docs-only、重复变化与 CI success 保持 silent，非文档 dirty change 与持续 validation gap 才产生两种 typed fact。当前 Workspace Goal 已配置为 `record_only`，绑定 exact Goal/owner/session/workspace；CI producer 仍未配置，authority 全部为 false。它解决的是可信水源可接入，不是长期 usefulness 证明。

还需要继续接入更多相互独立、可验证的 domain producer。任一 producer 都必须证明来源、owner、有效时间、重放和 fail-closed；不能重新用关键词、模型 confidence 或自由文本日志代替事实。

结构化观测控制面已提供一个有 schema、有 scope、有 freshness、带 server-owned salience 的 typed producer；`component_health` producer 从真实 `/health` snapshot 派生 observation，禁止 caller 自报 facts，且支持 server-timed、显式 opt-in 的 background producer，当前默认仍 disabled。EvidenceGraph v1 已为 Belief 提供同一 writer fence 内的有界 typed evidence 关系，但 Git/test/CI、runtime、commitment、calendar 等更多受信 producer 仍是后续扩展方向，不能把当前 component-health 观测泛化成完整世界感知。

### 4.2 CognitiveBrief 已安全进入 Hypothesis，但仍是只读桥

`read_only_cognitive_loop.py` 的 brief 现在绑定 exact parent revision、evidence refs、attention bridge 和 replay digest；binding 以 prepared → admitted → committed/rejected phase CAS 持久化，启动后可 reconcile，缺失、漂移或 revision 不一致时 fail closed。该桥只影响 observation/readiness 和建议资格检查，不产生 Route、Risk、Agent、Tool 或执行权。

### 4.3 Hypothesis 和 Interaction M1 窄生命周期已实现，生产闭环仍部分

Hypothesis 已支持 `candidate / accumulating / confirmed / contradicted / expired / superseded`；typed contradiction 会把目标假设终止且不可由普通 observation 复活，supersede 必须显式绑定 replacement。Suggestion 已有可审计的 `say / wait / silent` 以及节流前 decision、节流后 delivery 分离，decision ledger 持久化 owner/session、canonical hypothesis ref、assessment digest、mode epoch 和 delivery disposition；`ask` 当前明确 dormant，不能由 caller-crafted `interaction_gap` 激活。真实运行仍保持 record-only 默认，advise-only 只进入 exact owner Console。

### 4.4 EvidenceGraph v1 已形成最小有界关系层

`veyra.belief.evidence_graph.v1` 现在与 `belief_state.json` 的 claims 在同一原子 writer fence 内更新；节点保存 source/provenance、typed value digest、entity refs 和 valid-time，边只使用 typed `supports / contradicts / supersedes` 关系，并限制节点、边和 unresolved 数量。相同 typed value 的重叠 observation 只形成 support；不同 typed value 只有在 valid-time 重叠时形成 unresolved contradiction，unknown/invalid valid-time 不制造伪冲突；supersession 只能由同 scope 的显式 relation 建立。新 observation 只比较 identity frontier；旧 support-only nodes/edges 可安全 compaction，contradiction/supersedes 端点保留，安全关系容量不足时 fail closed。Belief writer 传播 `persisted / belief_value_persisted / persistence_status` 回执；StateRefresh 以 claim revision/value digest 做 CAS，并在 probe 前拒绝 duplicate identity、损坏图和不可安全读取的状态。历史 ownerless/conflicting tenant 行只读 quarantine，不迁移、不删除、不静默选赢家。刷新预算、公平归档、跨实体推理和长期关系模型仍未完成。

### 4.5 Belief Economy 已有 typed priority 窄切片

`veyra.belief.economy.v1` 只接受注册来源提供的 `importance`、`change_probability` 和 `decision_impact`，三者均为 `[0,1]` 或明确 `null`；完整时计算 `belief_value`，任一 unknown 时保持 `belief_value=null`、`evaluation_status=unknown`。当前 P2 slice 将纯 scheduler 与执行边界分开，并由 StateRefresh facade 处理 injected clock、hard deadline、future-due/lifecycle、known-vs-unknown ordering、owner fairness、CAS、legacy TTL 与 unsupported disposition；不运行 probe、猜测缺失因子、改变 claim truth、Attention readiness、Route、Risk 或权限。live production Economy metadata 为 `0/8`，所以真实 `next_refresh_at`/max-staleness 仍 `NOT VALIDATED`；公平归档、owner-scoped graph、冲突仲裁和 stale/coverage 前后对照仍待真实数据。

### 4.6 长期用户模型和世界关系层仍薄

长期用户偏好、目标和关系上下文需要三时间尺度、来源、TTL、纠正、删除和用途边界。EvidenceGraph v1 已提供最小有界证据关系层，但实体关系、刷新预算、长期归档和跨事件假设层尚未形成统一模型。

### 4.7 真实产品反馈刚开始，尚不足以宣称学习

当前真实链为：同一 trusted workspace observer 在两个不同时间窗产生两次代码变化，经 EventInbox → GeneralSituation → confirmed Attention → 一条 exact-owner `record_only` suggestion；不是多源 producer，也不是长期 usefulness 证明。该当前 suggestion 尚没有长期 feedback 样本；历史 advise canary 的重放/反馈仍是 `duplicate`、`support=insufficient_data`、`policy_effect=none`，不能转移给当前 owner/session。generic CognitiveBrief 历史 `223 cycles / 445 model calls / 0 brief candidates / overconservative` 不含 event-driven candidate，属于 observability/product gap。

### 4.8 Runtime revision 已安全收口，但不是 loaded-code attestation

以下是 `6776f97` 的历史 live 快照：运行于 Python 3.11.15；runtime v2 报该 revision、`dirty_flag=false`、`loaded_code_attested=false`，Goal/observer 可跨重启保留，clean workspace 保持 unchanged/silent。一次 wrapper cold-start 超过 readiness，随后 direct kickstart 在约 6 秒 ready，属非阻塞 timing 观察，不是失败。当时 `5b80a26` 的 clean restart 证据不能转移给后续 SHA，且 docs exact-SHA live/Actions 尚未验证；该历史 pending 已由本文顶部的 2026-08-22 checkpoint 取代。GET runtime/health/state/state-health/belief status 前后业务 state hashes 不变；public response redacts claim/target/CAS/private ledger，authority 仍全 false。

---

## 5. 接下来按什么顺序开发

后续不再用“继续堆 Phase 数字”作为唯一推进方式。优先完成能产生真实用户价值、同时不扩大权限的垂直切片。

### P0：当前 `cognitive-awakening` 分支的应用自动化检查点

当前 V1 应用检查点已完成独立审阅、定向/对抗 smoke、`166/166` gate（`145`
invariant + `1` cognitive + `20` product）、OpenClaw plugin `32/32`、Route
`810/810`、compileall、frontend product-contract/status-tone/build/check:bundle/
build:desktop 和 product contract/bundle 验证。generic health/education/finance
generalization smoke 通过，三个原始 live Situation 复用同一通用机制；long-session
move update 解析 mover Information Need，同时保留 network/date Unknowns。arm64
package 也已作为 clean LOCAL PREVIEW 通过 sidecar smoke、normalized sidecar match、
strict ad-hoc codesign 与 App metadata/entitlement 检查；sidecar SHA 为
`818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`，Veyra.app SHA
为 `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab`，工具链为 Python
`3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、Tauri `2.11.4`。
Home/Today/Situations/Chat/Settings 与 `390x844` browser acceptance 已通过，Context
gating、404 UX、source labels、question dedupe 已核验。当前代码 checkpoint 为
`34d44dfca0534f837b72b2ebe15949ed517ea699`；exact-SHA Actions run `32564052536`
已成功（<https://github.com/wenjiesong04/veyra/actions/runs/32564052536>）；重启后的
startup `runtime_build` 报告该 revision、`dirty=false`、`loaded_code_attested=false`，
current served UI/browser validation 通过，bounded live 对 core product/runtime/browser
已验证。实际 Calendar TCC、Weather/Public Web
user consent、sustained 7–14 day usefulness/quiet-rate、owner sign-off、notarization/DMG
与 public release 仍须独立核验。isolated real-model asked-answer series 为 `3/3`，但更广泛的 natural-language
model reliability 仍是 `PARTIAL / DEGRADED`；这不是稳定 provider、完整通用全链、长期
usefulness 或 Jarvis 证明。authority 保持 record-only/no external delivery，Agent
research disabled。本窗口不合并 `main`，也不开始 P3。

### P1：Cognitive Awakening M1（`PARTIAL + AUTOMATED_VALIDATED + CURRENT BOUNDED LIVE`，Console-only、无权限扩大）

已完成并有自动化与历史 bounded live 证据：

1. 结构化 typed observation 进入 EventInbox、General Situation 和 exact owner/session/workspace 的 AttentionHypothesis；
2. CognitiveBrief 绑定 parent/evidence/replay digest 后安全进入 Attention bridge；
3. observed evidence 才能提升 readiness，inference/prediction 保持排除可见；
4. Hypothesis 支持 `contradicted / expired` 终态，但 generic Event caller 不能伪造 trusted lifecycle signal；
5. Interaction 支持 canonical `say / wait / silent` 和 decision/delivery disposition；`ask` 保持 dormant，等待可信生产信号；
6. `record_only`、`shadow`、`advise_only` 三种模式均在真实本地运行，默认已恢复 `record_only`；
7. advise-only 只在 owner-scoped Console Sandbox 展示，收到一条 exact-bound `useful` feedback；
8. calibration 只形成描述性统计，`policy_effect=none`、support `insufficient_data`。
9. CognitiveBrief bridge 以 prepared → admitted → committed/rejected phase CAS 保证 parent/evidence/hypothesis 关联；prepared-before-Attention、admitted-before-commit、missing-cycle orphan 和 advanced Attention replay 均已有自动化恢复/拒绝证据；
10. `component_health` producer 从服务端 health snapshot 生成 typed observation，HTTP caller 只能使用 `local_operator` 通道；
11. typed contradiction/supersede 写入 lifecycle ledger，终态不可复活，替代关系必须显式且 owner-scoped；
12. interaction-decision ledger 与建议状态同一 writer fence 持久化；状态 GET 不运行 Git、模型、Probe 或 provider，也不写业务 state。

以上是已实现并验证的窄闭环，不等于完整 M1。当前 Workspace Goal observer 已在 `record_only` 配置下运行，两个不同时间窗的真实代码变化经既有链路形成 exact-owner confirmed Attention 和一条 record-only suggestion；这不是多源 producer 或长期 usefulness 证明。对 Attention lifecycle，项目仍保持 `attention_lifecycle_producer_unavailable`：原来早退之后的不可达推导代码已经删除，不用一套没有 domain caller 的安全脚手架伪装成产品能力。可信 lifecycle producer、长时 background observation、非 happy-path interaction 真实样本、长期 usefulness/timing 校准和 current-revision live 长时证据仍缺，因此 P1 继续写成 `PARTIAL`。Feishu fresh nonce 属于未来外部交付验证，本阶段不作为这批 Console-only P1/P2 repair 的阻塞条件。

### P2：Belief Economy 与 EvidenceGraph v1（`PARTIAL + AUTOMATED_VALIDATED + CURRENT BOUNDED LIVE`）

目标：让系统不只是保存 claim，而是知道什么值得刷新、证据怎样关联。

- [x] 为可刷新 claim 引入结构化 `veyra.belief.refresh_spec.v1`；literal/default resolver 和 source binding 已由 schema 校验；
- [x] 删除从 claim 自由文本推导 probe target 的 fallback；缺失或 mismatched typed spec 对需要 target 的刷新请求 fail closed；旧 structured evidence 仅作兼容读取，不再覆盖显式 spec；
- [x] 在 `veyra.belief.evidence_graph.v1` 中维护 bounded provenance、typed value digest、valid time、entity refs、support/contradiction 和显式 supersession；同一 writer fence 内与 claims 原子持久化；
- [x] 建立最小 EvidenceGraph，不更换数据库；invalid/unknown valid-time、损坏图、缺失或跨 scope relation 均 fail closed，不静默选择赢家；
- [x] 以 identity frontier 限制 relation fan-out，并对 support-only nodes/edges 做 retention compaction；长时 1000 条同值 observation 仍可写，安全 contradiction/supersede 容量不足时 fail closed；
- [x] Belief persistence 传播 `persisted / belief_value_persisted / persistence_status`，StateRefresh 不把冲突或拒绝报告为 refreshed success；Git clean→dirty 按 typed value 形成 contradiction；
- [x] 只读状态 GET 使用缓存/持久 runtime projection；`/phase6/status` 不触发 provider handshake、设备 token 更新或其他 state 写入；
- [x] 为 `importance × change_probability × decision_impact` 增加 `veyra.belief.economy.v1` typed contract；任一 unknown 时 `belief_value=null`，已知 value 只用于 bounded refresh ordering；
- [x] StateRefresh 以 claim revision、projection digest 和 typed value digest 做 claim-level CAS；并发新 observation 会令旧 probe receipt degraded，不能覆盖新值或更新本地 probe cache；legacy 缺失 revision 只读解释为 revision 0，不做迁移；
- [x] 每次 refresh tick 使用当前时钟重新评估 TTL，并以同批 identity 去重、owner round-robin 做 bounded fair selection；重复 identity、损坏图和 malformed non-quarantinable state 在任何 probe/scheduler 写前 fail closed；
- [x] generic `/state` 使用 `veyra.public_state.v2` aggregate allow-list；Belief/ExternalWorld 由 exact user/session scoped endpoint 提供，rollback log 只返回 public v1 脱敏字段；这些仍是 caller-declared logical scope，不是 auth-derived security principal；
- [x] 当前历史 ownerless/conflicting tenant claim 以 read-only quarantine 暴露 typed degraded/count；有效分区仍可读/刷新，坏行不迁移、不删除、不获得 lifecycle 写入；
- [x] 将 `next_refresh_at`、max staleness、已知/unknown Economy value、lifecycle obligation 与 owner fairness 组合成纯 bounded scheduler；缺失因子不猜值，但生产 producer 尚未为当前 8 条 claim 提供 Economy metadata；
- [x] 明确不可刷新的 ordinary claim 以 bounded durable disposition 移出重复队列；conflict 不伪装归档，对同 revision 无 cadence 冲突做 bounded suppression；
- [ ] 加入跨 tick 长期公平/覆盖率指标、通用归档保留策略和 owner-scoped EvidenceGraph projection；
- [ ] 对冲突做生产 producer 接入的显式仲裁或保持 unresolved，不静默覆盖；当前仍主要由 smoke 覆盖，不能写成全链路完成。

本轮自动化检查点接受标准已满足：当前 V1 full gate `166/166`（`145` invariant、
`1` cognitive、`20` product）、claim CAS/current-clock TTL、same-batch owner
fairness、quarantine byte-purity、9 Route `810/810`、plugin `32/32`、compile/
frontend 均通过。当前 live 已证明 Workspace Goal observer 的两窗真实链路、
unsupported event 的单一 bounded marker、cadence-less conflict 的同 revision
suppression、GET purity 与 public redaction；生产 Economy metadata `0/8`，所以
真实 `next_refresh_at`/max-staleness 仍 `NOT VALIDATED`。owner graph projection、
冲突仲裁、长期 soak/fairness/staleness-before-after、多样本 producer 与
Belief/sidecar at-least-once crash debt 仍待完成。Economy 不改变 claim 真假、
Attention readiness 或执行权限。

### P3：长期用户模型与 Interaction Economics

目标：从“知道一件事”进化到“长期知道怎样对这个用户有帮助”。

- 分离 turn、task、identity 三个时间尺度；
- 为偏好、目标、关系和沟通方式保存来源、TTL、confidence 和纠正链；
- 增加导出、删除、保留策略和敏感推断审查；
- 估计用户价值、打扰成本和不确定性成本，但不伪造精确分数；
- 记录 missed opportunity、false silence、wrong timing 和 excessive interruption；
- 在足够真实样本、独立验收和显式批准前不产生行为影响；达到条件后也只能通过版本化的非权限性认知/交互 proposal、shadow 评估、scoped canary 和可回退晋级产生受治理的后效；安全、授权、provider、交付范围和执行策略始终走独立的人工 authority change。

### P4：世界演化预测与 Durable Case 统一

目标：不只预测“执行这个动作有什么影响”，还持续观察“按当前趋势可能发生什么”。

- 从固定 domain 开始做可证伪 prediction；
- 为预测保存 horizon、assumptions、evidence、confidence boundary 和 outcome resolver；
- 用 residual 校准，不用模型自评；
- 逐步统一 Goal、Commitment、Situation、Case、Agent task、Review 和 wakeup；
- 保持每一步可暂停、取消、恢复和审计。

### P5：Phase 6 产品硬化，而不是权限扩张

P5 可以排在 P1–P4 之后，仅因为 Phase 6 runner 当前保持 `fail_closed / degraded`，且前序认知切片不依赖 extension invocation。任何恢复 extension 生成、验证、canary、显式调用或 promotion 的工作，都必须先完成 P5 的当前 revision 复验；此时 P5 自动成为前置条件。

- 为届时的应用代码 revision 重新认证 runner 和 dynamic validation backend；
- 做上一 signed version 的真实 rollback 演练；
- 做 crash/retry、breaker、长期 soak 和资源边界验证；
- 用第二个模型/provider 独立跑 strict generation 与 fail-closed compatibility；
- 检查签名 key 的运营备份和恢复流程，但不把 HMAC 归档/旧索引迁移重新塞回主线；
- 继续禁止自然语言自动启动、自动审批、自动 promotion 和 TCB 自改。

### 能力晋级条件

| 晋级 | 最低条件 |
|---|---|
| `record_only -> shadow` | owner/scope、幂等、evidence binding、GET purity、Route 非弱化全部自动化通过 |
| `shadow -> advise_only` | 用户显式 opt in；有真实 shadow 样本；错误证据率和打扰原因可检查；仅 owner Console |
| Console -> 外部通知 | 单独授权；quiet hours/budget/cooldown；真实 usefulness 样本；撤销和 delivery audit；仍无执行权 |
| 建议 -> ActionProposal | 独立 Guardian/Review 流；建议反馈不能充当审批 |
| A4/A5 | 当前不是近期目标；必须有 domain-specific standing grant、真实 effect verifier、rollback 和长期安全证据 |

---

## 6. 开发与验证规范

### 6.1 环境

```bash
conda activate veyra
python --version      # 3.11.x
which python          # 应指向 veyra 环境
./scripts/start_local.sh --check-runtime
```

不要：

- 用全局 Python 3.12/3.13 运行或重写 LaunchAgent；
- 提交 `.env`、API key、token、device identity、签名私钥或本地证书；
- 将 `state/`、用户 Memory、Review、commitment 或运行日志作为 fixture 提交；
- 在测试中复用真实用户状态目录。

### 6.2 修改前先收窄问题

每项任务必须先写清：

```text
Goal
Observed failure
In scope
Out of scope
Authority delta
State/schema impact
Validation evidence
Rollback plan
```

如果不能说明 authority delta，默认按“可能扩大权限”处理。如果只需要诊断，不要顺手实现大范围重构。

### 6.3 测试层级

开发中先跑最窄的定向 smoke；提交前再跑完整 gate。

```bash
# 定向示例
python scripts/attention_hypothesis_smoke.py
python scripts/epistemic_hygiene_smoke.py
python scripts/suggestion_sandbox_smoke.py
python scripts/memory_context_scope_smoke.py
python scripts/phase6_extension_pipeline_lifecycle_smoke.py

# 正能力 lane：真实隔离 Git observation 到 record-only proposal
python scripts/run_smokes.py --group capability

# 完整 Python gate：安全不变量与正能力脚本各执行一次
python scripts/run_smokes.py --group gate

# 全部 smoke（比 gate 更广，按任务需要）
python scripts/run_smokes.py --all

# Python import/syntax
python -m compileall \
  awareness core execution guardian interface \
  memory_bridge probes rollback_audit routers runtime skills tool_proxy \
  main.py cli.py desktop_backend.py scripts

# OpenClaw governance plugin
cd apps/openclaw/veyra-governance
npm test

# Console / desktop frontend
cd ../../../web
npm ci
npm run build
npm run build:desktop
```

涉及公开 Route、private state 或 operation mode 时，必须保留当前全部受保护公开 Route 的非弱化矩阵。截至 2026-08-11，`phase6_route_non_regression_smoke.py` 覆盖 9 routes × 3 modes × 28 private-state scenarios = 756 comparisons；Route 或场景集合变化时必须同步更新该 Living Zone 计数和断言。

### 6.4 必测负向场景

根据变更范围至少覆盖：

- missing/invalid owner 和 cross-owner/session；
- duplicate、out-of-order、stale revision、digest conflict；
- corrupt/partial state 和 restart recovery；
- expired evidence、future timestamp、producer spoof；
- model unavailable、invalid JSON、reported speech、hypothetical statement；
- missing Agent/Tool receipt 和 false success claim；
- disabled/record_only/shadow 的零 authority；
- symlink/path traversal、workspace/state/secret mount、network access；
- token 缺失、错误 approver、approval replay 和 CAS race；
- cancellation、timeout、process-tree cleanup 和 no automatic retry。

### 6.5 Live validation

只有 changeset 的目标涉及真实 runtime、模型、Agent、Feishu、runner、签名或 canary 时，自动化之后还必须做 live validation。

最低要求：

1. 确认运行进程加载的是当前 Git revision；
2. 记录 Python、PID/start time、config generation 和 provider identity；
3. 先查 `/health`，再查目标 component status；
4. 只读 GET 前后比较 state revision/hash，确认没有业务写入；
5. 运行一个目标正例和至少一个 fail-closed 反例；
6. 检查 Route、risk、review、tool/execution trace 和外部 effect；
7. 清理 synthetic state，但不删除真实用户历史；
8. 将证据写成带 revision、时间和 scope 的结果。

Feishu 的 `thread_alive=true` 只证明 worker 存活。真实通道验收必须由用户发送 fresh nonce，出现 current-run inbound、processed 和 `provider_sent` external message id。

### 6.6 Definition of Done

一个改动只有同时满足以下条件才算完成：

- 根因和 scope 清楚，不是为一句 fixture 堆关键词；
- 正向与负向定向测试通过；
- owner/session、GET purity、authority 和 Route 不变量未弱化；
- 完整 gate 通过；
- 受影响的前端/plugin/build 通过；
- 需要 live 的表面已由当前 revision 验证；
- 文档区分 implemented/configured/validated/live/authority；
- 没有 secret、用户 state 或无关改动进入 diff；
- 回退方式明确；
- GitHub CI 对最终 SHA 通过；
- 若仍有外部 blocker，明确写 `PENDING`，不能用技术完成替代。

---

## 7. Git 与 GitHub 工作流

### 7.1 分支职责

- `main`：可发布、已通过完整 gate 和 CI 的主线；
- `cognitive-awakening`：当前认知闭环集成分支；P1/P2 修复和 bounded live/soak 验收完成前保持在本分支，之后再通过 PR 合入 `main`；
- `codex/<scope>`：Codex 新任务默认分支；
- `cursor/<scope>` 或 `feature/<scope>`：其他工具/人工的单一范围分支；
- 紧急修复使用 `fix/<scope>`，仍需同样验证。

新独立任务默认从最新 `origin/main` 建分支：

```bash
git fetch origin
git switch -c codex/<short-scope> origin/main
```

如果任务明确依赖尚未合并的 `cognitive-awakening`，必须在任务说明和 PR base 中写清，不能悄悄把不相关提交带入。

当前本地 `main` 落后远端，禁止直接从本地 `main` 开新工作或直接 push。

### 7.2 提交规则

- 一个提交只解决一个可解释范围；
- 代码、测试和必要文档可以同提交，清理和功能应分开；
- 不把大规模格式化、历史归档、依赖升级混进功能修复；
- 使用清晰前缀：`feat / fix / test / docs / refactor / chore`；
- staged 前检查 `git diff --check` 和 `git diff --cached`；
- 不使用 `--force`、`reset --hard` 或覆盖用户未提交工作；
- 不通过修改测试预期掩盖真实回归。

推荐流程：

```bash
git status --short --branch
git diff --check
git add <explicit files>
git diff --cached --stat
git diff --cached
git commit -m "type(scope): concise outcome"
git push -u origin <branch>
```

### 7.3 PR 与 main

正常流程是 feature branch → PR → GitHub Actions → review → merge `main`。

直接 push `main` 只在项目 owner 明确要求、分支为最新 fast-forward、完整本地 gate 和远端 CI 都能证明、且 changeset 小而清晰时使用。任何情况下都禁止 force push `main`。

PR 描述至少包含：

```text
Problem / evidence
Root cause
What changed
What did not change
Authority delta
Tests and live evidence
Known limitations
Rollback
```

### 7.4 多窗口和 vibe coding 交接模板

把下面内容放到新窗口首条任务中：

```text
Repository: /Users/spectre/PycharmProjects/veyra
Canonical doc: docs/README_Veyra.md
Base branch / expected HEAD:
User goal:
Observed failure and evidence:
In scope:
Explicitly out of scope:
Authority delta allowed:
State/schema constraints:
Required targeted tests:
Required full gates:
Required live scenario:
Target branch / PR base:
Last known blockers:
```

新窗口必须重新检查 Git、代码和 runtime，不能直接相信这段 handoff 中的旧计数或“已完成”标签。

---

## 8. 文档地图与维护规则

### 8.1 仍维护的文档

| 文档 | 角色 |
|---|---|
| `docs/README_Veyra.md` | 唯一项目总入口：稳定理念、工程手册、dated checkpoint 与 Project OS 导航；不复制高频 current truth |
| `README.md` | GitHub 首页和本地快速安装/运行入口；不重复维护完整路线图 |
| `docs/veyra_proactive_cognitive_architecture.md` | 目标架构、阶段设计和历史验收台账；不是实时运行真值 |
| `docs/veyra_cognitive_awakening_development.md` | 当前认知纵向切片的详细设计/接受标准；基线数字是阶段开始时快照 |
| `docs/project_guardian_evaluation_protocol.md` | Project Guardian 专项评估合同 |
| `docs/commitment_acceptance.md` | Commitment 专项验收合同 |
| `docs/desktop_release.md` | Desktop 打包说明 |
| `docs/local_release_checklist.md` | 本地发布检查表 |
| `docs/veyra_project_os/README.md` | Current/task Living truth 导航 |
| `docs/veyra_project_os/status/current.md` | 当前 revision、发布证据和 known degraded |
| `docs/veyra_project_os/tasks/CURRENT.md` | 当前唯一执行窗口与 V0-001 状态 |

旧的 `development_status.md`、`implementation_progress.md`、手工 Route inventory 和 2026-07-31 capability baseline 已被本文件吸收后移除。历史内容仍可通过 Git history 恢复，不能再被后续窗口当作 current truth。

Route 清单不再手工维护固定数量。当前路由事实以当前 revision 的 FastAPI `openapi.json`、`app.routes` 和 route/gate 测试为准。

### 8.2 当前代码地图与结构债务

目录名不能冒充实现。当前真实主干是：

- `core/`：状态、前台理解、判断、foresight、治理和验证核心；
- `awareness/`：Attention、Belief、EvidenceGraph、GeneralAttention；
- `runtime/`：持续循环、Situation/Hypothesis/Suggestion、Agent/Phase 6 与 producer/application runtime；
- `interface/`、`routers/`：外部合同和 HTTP 控制面；
- `execution/action_executor.py`、`guardian/review_queue.py`：仍在使用的执行与审批实现。

本轮删除了 `decision/`、`foresight/` 中仅数行的占位类，以及 `execution/`、`guardian/`、`awareness/` 中无生产 import 的空壳文件；真正实现位于 `core/`、`runtime/` 和上面两项保留文件。`core/architecture.py` 已改为只报告真实路径。不要为了看起来“分层完整”重新创建无调用的 pass-through 模块。

`main.py` 仍是约 74 KB 的 composition-root God File，包含 import-time 单例与隐式构造顺序。这是明确结构债务，但不能和认知输入切片一起大爆炸重写。下一笔独立重构先抽取 `/actions/proposals` 的 `ActionProposalService`，显式声明依赖、保持 handler 只做输入校验与 use-case 调用；验证后再搬 APIRouter。每次只迁移一个可验收 use case，公开 Route、状态、风险、权限和启动顺序必须完全等价。

Workspace observer 已把 durable state codec 与 delivery outbox 从 service 拆开，避免重新复制一个 1400 行单文件；service 本身仍约 1070 行，是后续可机械拆分的 P2 债务。隔离 Git observation 对持久 projection、单文件和总 worktree 内容设置预算，但 Git status 的 2 MiB 检查仍发生在子进程返回后，不是操作系统级流式内存硬限；文档不得把它夸大成完全 resource-bounded runner。

### 8.3 何时更新本文件

以下变化必须更新相关段落：

- 产品身份或不可破坏原则变化；
- capability 的实现、权限或默认 mode 变化；
- live validation 或运营状态发生关键变化；
- roadmap 优先级变化；
- 分支、测试 gate、环境或发布流程变化；
- 新文档成为权威或旧文档被弃用。

不要把每次临时日志和计数永久堆在正文。当前快照只保留最近一次核验；历史证据应进入 commit/PR、dated acceptance record 或 Git history。

### 8.4 修改理念的规则

愿景决定方向，但不能覆盖事实：

- 修改前先标明所属 Stability Zone；Constitutional Zone 的变化应使用独立、可审阅的架构提交；
- Living Zone 应替换陈旧快照并写明 revision、证据类型和核验日期，不把历次临时计数不断累加进正文；
- 如果代码与愿景冲突，先判断是代码偏航、文档过时，还是现实约束证明愿景需要调整；
- 不能为了迎合愿景把正确的安全代码删掉；
- 也不能为了迁就当前代码，把尚未实现的目标从愿景中悄悄移除；
- 任何根本定位变化都应单独提交并解释原因、替代方案和影响。

---

## 9. 面向未来开发者与 AI 的决策问题

增加重要能力前，请逐项回答：

1. 它提升的是感知、状态、理解、Attention、判断、反应、验证还是学习？
2. 如果未来 Agent 更强，这项能力仍必须属于 Veyra 吗？
3. 外包它会不会破坏连续性、用户同意、数据治理或权限边界？
4. 当前是在解决可泛化根因，还是给单句 fixture 打补丁？
5. 它让 Veyra 对现实更准确，还是只让代码更多？
6. 新证据能否推翻它的判断？冲突和过期怎样表达？
7. owner、session、workspace、有效时间和来源是否明确？
8. 判断、交付、审批和执行权限是否被错误合并？
9. 它会造成无价值打扰，还是造成长期 false silence？
10. 行动后结果是否重新进入世界模型并由独立证据验证？
11. 模型/provider 失败时是否诚实降级，且不过度保守到永久无输出？
12. 能否用小而真实的纵向场景证明用户价值？
13. 新认知将怎样受治理地改变未来理解或反应；如果完全没有后效，为什么仍值得长期保存？

最后问：

> **这次变化让 Veyra 更准确、更连续、更有分寸了吗，还是仅仅让 Veyra 更大了？**

---

## 10. 当前下一步清单

### 当前 cognitive-awakening 收口与继续开发前

- [x] 核对工作区并识别全部预期修改；当前没有 secret、state 或无关 Cursor 代码混入；
- [x] 重新核对当前 branch、canonical handbook、运行代码和真实状态，复核 build identity、EvidenceGraph、bridge、decision ledger 与 producer 边界；
- [x] 完成隔离 Git snapshot 的 worktree 修复及定向 smoke；
- [x] 独立只读 review 当前安全修复；未发现 hook/filter/GIT 环境或仓库 index 写入绕过，schema v2、SHA-256、split/linked worktree 和竞态测试已补齐；
- [x] 按 Constitutional / Living Zone 更新本次真实 Living 快照；代码 checkpoint
  `34d44dfca0534f837b72b2ebe15949ed517ea699` 已核对，本次仅有文档同步；
- [x] 定向 runtime、认知、结构化观测、Belief refresh-spec/CAS、EvidenceGraph、
  Economy、workspace observer 与 LivingReaction retention smoke 通过；`166/166`
  gate（`145` invariant + `1` cognitive + `20` product）、plugin `32/32`、
  compileall、Web/Desktop frontend build 与 9 Route `810/810` 均通过；
- [x] frontend product-contract/status-tone/build/check:bundle/build:desktop、Home/
  Today/Situations/Chat/Settings 与 `390x844` browser acceptance 通过；Context gating、
  404 UX、source labels、question dedupe 已核验；arm64 LOCAL PREVIEW package 通过；
  Python `3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、Tauri `2.11.4`、sidecar
  smoke、normalized sidecar match、strict ad-hoc codesign、App
  `NSAppleEventsUsageDescription`/automation entitlement 已核验；sidecar SHA
  `818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`、Veyra.app SHA
  `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab`；不代表 Developer
  ID/notarized/DMG/public release；
- [ ] 实际 Calendar TCC 用户授权；
- [ ] Weather/Public Web user consent；
- [x] 重启后的 startup `runtime_build` 报告 code checkpoint `34d44dfca0534f837b72b2ebe15949ed517ea699`、`dirty_flag=false`、`loaded_code_attested=false`，核对 Conda Python 3.11.15、冻结 runtime projection 与 current served UI/browser validation；
- [ ] 对最终 clean revision 继续复核 Belief quarantine、External scoped projection 和 Phase 6 fail-closed；exact-revision runtime/current served UI/browser 已验证，自动化正反例不等于所有 P1/P2 live 证据；
- [x] 验证 durable bridge、typed contradiction/supersede、不可复活、canonical interaction-decision ledger 的定向与 crash-recovery smoke；
- [x] 完成 EvidenceGraph 1000-observation bounded soak：同值 observation 全部可写，frontier/node compaction 生效；冲突保留旧值，容量不足时 fail closed；
- [x] `/phase6/status` 与其他 10 个状态 GET 的 state-file purity 复核通过，未触发 OpenClaw device token 写入；
- [ ] Feishu fresh current-run 入站→处理→`provider_sent` 证据；该证据属于未来外部交付阶段，本次 Console-only M1 不阻塞；
- [x] GitHub Actions 对 `34d44dfca0534f837b72b2ebe15949ed517ea699` 的 exact-SHA 结果成功：
  run `32564052536`（<https://github.com/wenjiesong04/veyra/actions/runs/32564052536>）；
- [ ] sustained 7–14 day usefulness/quiet-rate、owner sign-off、notarization/DMG 与 public release；
- [ ] P1/P2 剩余缺口完成可信 lifecycle producer 与 domain policy、第二个独立可核验 producer、生产 Economy policy、长时 fairness/coverage/soak、通用归档与 owner-scoped graph projection；
- [ ] P1/P2 真实验收完成后再通过 PR 合并 `cognitive-awakening -> main`；本窗口不合并 main，不开始 P3。

### P1/P2 修复检查点之后的下一开发切片（仍留在 cognitive-awakening）

- [x] trusted Git/CI typed workspace observer：隔离 snapshot、Goal/scope/origin/ref 绑定、可选 exact-SHA GitHub Actions、ActiveLoop 与私有控制面已接入；当前 Workspace Goal 为 `record_only` 且 exact owner/session/workspace 绑定，CI producer 未配置，Project Guardian 保持 disabled，未运行任意 workspace test command；
- [x] EvidenceGraph v1、valid-time unknown/invalid fail-closed、typed support/contradiction/supersede、bounded partition/compaction 与 tamper checks；对应 invariant 与 Belief GET purity 已纳入当前 `166/166` gate；
- [x] Belief Economy v1 typed 三因子、claim-level CAS、current-clock TTL、same-batch owner fairness 与 honest persistence receipt；对应 invariant 已纳入当前 `166/166` gate；生产 Economy metadata 当前 `0/8`；
- [ ] 多真实样本下的 usefulness / timing / evidence 校准；
- [x] 用私有 Workspace Goal 控制面创建 exact-scope 长期 Goal，配置 `record_only` observer；两个不同时间窗的真实代码变化已形成 confirmed Attention 与一条 suggestion，但不把它写成多源或长期 usefulness；
- [x] bounded refresh scheduler contract（`next_refresh_at`/max staleness/lifecycle/value/fairness）、不可刷新 ordinary disposition 与同 revision conflict suppression；
- [ ] 为至少一个注册 producer 增加 server-owned Economy policy，再做真实 stale/coverage/fairness 前后对照；
- [ ] owner-scoped EvidenceGraph projection 与长期 retention/compaction 观测；
- [ ] 保持 external delivery、Agent、Tool、Route、Risk 和 authority 全部不变。

---

## 11. 最终定义

> **Veyra 感知用户与世界，把事件组织成有来源、有时效、有不确定性的状态，理解什么值得关注，选择有分寸的反应，借助可替换 Agent 完成需要的推理或执行，并在现实结果到来后继续验证和修正自己。**

如果未来 Veyra 接近目标，用户感受到的不应只是“它功能很多”，而应该是：

> **“它知道我正在经历什么，能说明外面发生了什么和为什么与我有关，也知道什么时候该帮我、什么时候该提醒我，以及什么时候应该安静。”**
