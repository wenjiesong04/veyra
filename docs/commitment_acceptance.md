# User Commitment + Proactive Push 验收指南

本文档描述如何在 **真实运行的 Veyra API** 上验收用户承诺（Commitment）与主动推送闭环。

## 前置条件

- Python 环境与项目依赖已安装（`pip install -r requirements.txt`）
- 网络可访问 Open-Meteo（`weather_probe` 只读拉取天气）
- 使用独立 `state/` 目录时，建议备份后再验收

## 1. 启动 API

```bash
cd /path/to/veyra
# 建议使用干净 state 目录，避免旧数据或损坏 JSON 影响验收
export VEYRA_STATE_ROOT=/tmp/veyra-accept-state
export VEYRA_AGENCY_ROOT=/tmp/veyra-accept-agency
mkdir -p "$VEYRA_AGENCY_ROOT" && echo '{}' > "$VEYRA_AGENCY_ROOT/goals.json"
echo '[]' > "$VEYRA_AGENCY_ROOT/intention_queue.json"
uvicorn main:app --host 127.0.0.1 --port 8000
```

确认运行正常：

```bash
curl -s http://127.0.0.1:8000/runtime | head
```

## 2. 自动化验收（推荐）

对 **已启动的 API** 执行：

```bash
python scripts/commitment_runtime_acceptance.py http://127.0.0.1:8000
```

无服务时可用嵌入式模式（CI / 本地无端口）：

```bash
python scripts/commitment_runtime_acceptance.py --embedded
```

验收项包括：

- 创建 `active` commitment 且 `next_run_at` 已到期
- `run-due` 识别 due 项
- `weather_probe` 不编造（不可用时报状态而非假数据）
- Guardian 决策写入结果
- `api` 通道消息进入 **outbox**（未配置外部 IM 时不丢失）
- `push_history` / `last_run_at` / `next_run_at` 更新
- `action_record.jsonl` 写入 `commitment_push_due` 与 `commitment_push_attempt`
- Active Loop tick 包含 `commitment_push` 步骤
- 安全边界：未确认 / 暂停 / 取消 / 冷却 / Guardian 拒绝

回归：

```bash
python scripts/commitment_smoke.py
python scripts/mvp_self_test.py
```

## 3. 手动创建 Commitment

```bash
curl -s -X POST http://127.0.0.1:8000/commitments \
  -H 'Content-Type: application/json' \
  -d '{
    "kind": "weather_daily",
    "title": "每日北京天气",
    "user_id": "demo-user",
    "channel": "api",
    "session_id": "demo-session",
    "status": "active",
    "next_run_at": "2020-01-01T00:00:00+00:00",
    "schedule": {"kind": "daily", "time_local": "08:00", "timezone": "Asia/Shanghai"},
    "payload": {"location": "北京", "topic": "weather"}
  }'
```

记下返回的 `commitment_id`。

## 4. 确认 Commitment（对话或 API）

**API 确认：**

```bash
curl -s -X POST http://127.0.0.1:8000/commitments/<commitment_id>/confirm
```

**对话确认：** 先问天气，在 Veyra 询问是否订阅后回复「好的」。

仅 `status=active` 且存在 `confirmed_at` 的承诺才会进入 due 队列。

## 5. 触发 run-due

**直接触发：**

```bash
curl -s -X POST http://127.0.0.1:8000/commitments/run-due \
  -H 'Content-Type: application/json' \
  -d '{"limit": 5, "reason": "manual_acceptance"}'
```

**Cron job：**

```bash
curl -s -X POST http://127.0.0.1:8000/runtime/cron/run \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "commitment_push_due", "reason": "manual_cron"}'
```

**Active Loop：**

```bash
curl -s -X POST http://127.0.0.1:8000/runtime/active-loop/start \
  -H 'Content-Type: application/json' \
  -d '{"interval_seconds": 300}'
curl -s -X POST http://127.0.0.1:8000/runtime/active-loop/tick \
  -H 'Content-Type: application/json' \
  -d '{"reason": "manual_tick"}'
```

## 6. 查看 outbox

本地 `api` 通道默认 `delivery=local_outbox`，推送会追加到 channel state：

```bash
curl -s 'http://127.0.0.1:8000/channels/outbox?limit=20'
```

在返回的 `items` 中查找 `metadata.commitment_id` 与 `metadata.route=commitment_push`。

## 7. 查看 push_history 与 next_run_at

```bash
curl -s http://127.0.0.1:8000/commitments/<commitment_id>
```

检查：

- `push_history` 新增一条，`status` 为 `queued` / `sent` / `not_configured` 等
- `last_run_at` 已更新
- `next_run_at` 已推进到下一次计划时间

## 8. 查看审计日志

```bash
curl -s 'http://127.0.0.1:8000/logs/actions?limit=50'
```

或直接读文件：

```text
state/logs/action_record.jsonl
```

关注 `route`：

- `commitment_push_due` — 批次汇总
- `commitment_push_attempt` — 单次推送尝试（含 skipped / blocked）

## 9. 安全边界说明

| 场景 | 行为 |
| --- | --- |
| `pending_confirmation` | 不进入 due，不推送 |
| `paused` / `cancelled` | 不进入 due，不推送 |
| 未设置 `confirmed_at` 的 active | 不推送（见下文语义） |
| Guardian `block` | 不进入 outbox；`next_run_at` 不推进；`last_attempt_at` 更新以进入 60s 冷却 |
| 60s 内重复 run-due | `skipped` + `reason=push_cooldown`；**不**修改 `next_run_at` |
| 通道未配置（如 api/local_outbox） | 视为投递成功：`push_history` + 推进 `next_run_at`，避免重复堆 outbox |

### `status` 与 `confirmed_at` 语义

- `status=active` 仅表示承诺**已启用**（可被调度扫描）。
- **可推送**必须同时满足：`status=active` **且** `confirmed_at` 非空。
- `confirmed_at` 表示用户或运维方**已明确授权**出站推送（对话回复「好的」或 `POST .../confirm`）。
- `POST /commitments` 若直接创建 `status=active`，服务端会**自动写入** `confirmed_at=now`，语义为：该 API 调用本身视为显式授权（适合运维/验收脚本）。
- 若需“先创建、后确认”流程，请创建 `status=pending_confirmation`，再调用 `POST /commitments/{id}/confirm`。

### 冷却与 `next_run_at`

- 冷却只比较 `last_run_at` / `last_attempt_at` 与当前时间，**不会**因冷却而跳过早期的 `next_run_at`。
- 冷却期内 due 仍成立，但返回 `skipped: push_cooldown`，调度时间不变。

### 已知限制（后续）

- Guardian 连续 block 时目前仅依赖冷却与审计；后续可增加 `blocked_count` / `needs_review` 状态，避免长期 due 且不可发送。

## 10. 推送前本地检查清单

```bash
python scripts/commitment_smoke.py
python scripts/mvp_self_test.py
python scripts/commitment_runtime_acceptance.py --embedded

lsof -i :8000          # 如有旧进程则 kill
uvicorn main:app --host 127.0.0.1 --port 8000
python scripts/commitment_runtime_acceptance.py http://127.0.0.1:8000
```

## 11. 清理

验收脚本使用独立临时目录（`--embedded`）或你指定的运行 `state/`。不需要的测试 commitment 可：

```bash
curl -s -X POST http://127.0.0.1:8000/commitments/<id>/cancel
```

验收脚本可在不再需要时删除：`scripts/commitment_runtime_acceptance.py`。
