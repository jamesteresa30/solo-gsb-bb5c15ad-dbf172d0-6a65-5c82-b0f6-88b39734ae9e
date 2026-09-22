# 交叉实验顺序平衡 API

基于 **Python + FastAPI + SQLite** 的交叉（crossover）实验顺序分配与**按周期受控揭盲**服务。
创建实验时提交互异条件、受试者 ID 与随机种子，服务生成 **Williams 平衡拉丁方**序列
并映射给受试者，保证：

- 每个条件在每个位置上出现次数相等；
- 每个**有向相邻条件对**出现次数相等（平衡一阶残留效应）；
- 完整序列组：偶数条件含 `n` 条序列，奇数条件含 `2n` 条序列；
- 受试者数量必须是完整组大小的整数倍，否则返回 `422` 及最近可接受数量；
- 相同输入与相同种子产生完全相同的分配结果（确定性分配）。

## 受控揭盲（period-controlled unblinding）

完整条件序列**只保存在服务端**，避免受试者提前获知后续安排：

- 创建接口 `POST /experiments` 与详情接口 `GET /experiments/{id}` 只返回受试者名册
  （`assignments[].subject_id`）与条件清单，不包含任何人的条件序列或序列下标；
- 通过推进接口逐周期揭盲：提交 `subject_id`、`expected_period` 与 `request_id`，
  **仅当 `expected_period` 正好等于该受试者的下一周期**时返回当前周期条件并原子推进；
- **周期完成 + 洗脱期门禁**：每个周期揭盲后必须调用完成接口
  `POST /experiments/{id}/complete` 登记完成时间；推进下一周期（周期 ≥ 2）前，
  服务端校验上一周期已存在完成记录，且服务端当前时间已达到
  `completed_at + washout_seconds`，缺一不可；
- 相同 `request_id` 重试（接口超时、网络重试等）返回**首次结果**且不重复写入；
  同一 `request_id` 绑定不同受试者或周期返回 `409`（推进与完成使用各自独立的幂等空间）；
- **批量受控揭盲** `POST /experiments/{id}/advance-batch`：一次提交 `batch_id` 与
  1～100 个互异的 `subject_id / expected_period` 成员，服务在**单个 SQLite 写事务**内
  先校验全部成员均满足完成登记与洗脱期门禁，**全部通过才按输入顺序**揭盲并推进；
  任一项失败即返回 `409`，列出每个失败项的 `subject_id` 与原因（不含任何条件），
  且**整批零写入**。相同 `batch_id` 与相同成员列表（含顺序）重试返回首次结果，
  绑定不同成员或周期则返回 `409`；
- 并发请求或过期周期（落后 / 超前 / 实验已结束）返回 `409`，冲突路径不写入、不多揭露任何条件；
- 进度查询只返回**已揭盲记录（含各自的 `completed_at`）**、`next_period` 与
  `next_available_at`（下一周期最早可推进时间），不暴露后续周期安排。

## 实验级暂停 / 恢复（pause / resume）

管理员可在不删除实验的情况下整体冻结揭盲流程：

- **暂停** `POST /experiments/{id}/pause`：请求体携带调用方生成的唯一
  `operation_id`（如 UUID）与必填 `reason`。服务在**单个写事务**内把
  `status` 置为 `paused`、`state_version` 加一，并追加一条 SQLite 审计记录；
- **恢复** `POST /experiments/{id}/resume`：请求体携带 `expected_version`
  （调用方看到的暂停版本）。服务在写事务内做 **compare-and-set 原子校验**：
  仅当实验当前为 `paused` 且当前版本恰好等于 `expected_version` 时才恢复；
  版本过期（已被其他操作者变更）返回 `409 state_version_mismatch` 并回传
  服务端当前 `state_version` / `current_status`，不会覆盖他人的状态变更；
- **暂停期间的揭盲门禁**：单条推进与批量推进均返回
  `409 experiment_paused`，响应体不含任何条件、周期或后续安排，且**不消耗**
  `request_id` / `batch_id`；恢复后用原幂等键首次成功即可；
- **已成功操作的重放不受暂停影响**：暂停前已成功揭盲的 `request_id` /
  `batch_id`，暂停期间重试仍返回**首次结果**（重放检查先于暂停门禁）；
- 周期**完成登记不是揭盲**，暂停期间仍可登记，便于暂停前已揭盲周期正常收尾；
- 相同 `operation_id` 重放返回首次结果；同一 `operation_id` 绑定不同动作 /
  原因（或恢复时绑定不同的 `expected_version`）返回 `409 operation_id_conflict`。
  暂停与恢复共享同一个 `(实验, operation_id)` 幂等空间；
- **批量状态切换** `POST /experiments/state-batch`：一次提交 `batch_operation_id`
  与 1～100 项操作（每项含 `experiment_id`、`action`、`expected_version`、
  `operation_id` 与可选 `reason`），服务在**单个 SQLite 写事务**内先校验全部
  实验的状态与版本，**全部通过才按输入顺序执行**暂停 / 恢复并逐项追加审计；
  任一项冲突即返回 `409` 并**整批回滚、零写入**。相同 `batch_operation_id`
  与相同操作列表（含顺序）重试返回首次结果；
- 每次暂停 / 恢复都追加写入 `experiment_state_events` 审计表，可通过
  `GET /experiments/{id}/state-events` 按**时间倒序**（最新在前）分页查询，
  支持 `limit`（1～500，默认 100）与 `offset`。

实验的 `status`（`active` / `paused`）与 `state_version` 同时出现在创建、
详情与列表接口的响应中，调用方可据此做乐观并发控制。

## 实验级紧急揭盲（emergency unblinding）

针对严重不良事件等必须立即获知受试者分组的场景，提供**双人审批**的破窗式
（break-glass）紧急揭盲，与按周期推进、暂停 / 恢复流程相互独立：

- **双审批 + 15 分钟授权窗口** `POST /experiments/{id}/emergency-unblind`：
  请求体携带 `subject_id`、`approver_id`、`operation_id` 与 `reason`。
  首个审批人的申请开启 **15 分钟授权窗口**（返回 `pending_second_approval`，
  不含任何条件内容）；窗口内**第二个不同审批人**的申请才**一次性**返回该受试者的
  完整条件序列（`unblinded`）；
- **失败路径一律不泄露序列**：同一审批人重复申请返回 `409 same_approver`；
  窗口过期返回 `409 window_expired`（同时落一条过期审计，随后可重新发起首个
  审批开启新窗口，过期失败不消耗 `operation_id`）；该受试者已完成紧急揭盲返回
  `409 already_unblinded`（一次性揭盲不可重复）；实验或受试者不存在返回 `404`；
- **单事务与幂等**：每次审批（首开窗口 / 完成揭盲）都在**单个 SQLite 写事务**
  （`BEGIN IMMEDIATE`）内完成“幂等检查 → 会话状态判定 → 审计与结果落盘 →
  提交”。相同 `operation_id` 重试返回**首次结果**（首开窗口重放返回待审批回执，
  完成揭盲重放返回首次序列）；同一 `operation_id` 绑定不同受试者 / 审批人 /
  原因返回 `409 operation_id_conflict`。并发第二审批由写事务串行化，
  **只产生一次揭盲结果**，其余得到 `409 already_unblinded`；
- **审计接口不含条件内容** `GET /experiments/{id}/emergency-unblind-events`：
  按时间倒序分页返回 `window_opened` / `unblinded` / `window_expired`
  审计记录（审批人、原因、窗口截止时刻等元数据），支持 `limit`（1～500，
  默认 100）与 `offset`；
- 紧急揭盲是带外（out-of-band）流程：**不改变**受试者的周期推进进度，
  也不受实验暂停状态影响。

## 脱敏结果快照（result snapshots）

面向统计分析 / 数据监查的只读导出：把“当前时点已揭盲的结果”固化为**不可变
JSON 快照**，之后的揭盲 / 完成 / 暂停均不影响已生成快照：

- **生成快照** `POST /experiments/{id}/snapshots`：请求体携带字段白名单
  `fields`（`period` / `condition` / `revealed_at` / `completed_at`，
  至少 1 个且互异）与唯一 `operation_id`。服务在**单个 SQLite 一致性读事务**
  （`BEGIN IMMEDIATE`）内固定同一时点 `generated_at`，读取名册与已揭盲 /
  完成记录，按 `subject_id` **稳定排序**生成快照并落盘；
- **脱敏保证**：快照只含**已揭盲周期**及各自的完成时间（已揭盲但未完成的
  周期 `completed_at` 为 `null`）；未揭盲周期、完整序列与 `sequence_index`
  一律不进入快照，白名单之外的字段也不输出；
- **摘要哈希**：每份快照保存 `digest`——快照内容规范化 JSON（键排序、
  紧凑分隔符、UTF-8）的 SHA-256，可用于完整性校验与对账；
- **幂等**：相同 `(实验, operation_id)` 与相同字段白名单重试返回**同一快照**
  （同一 `snapshot_id`、同一内容、同一 `digest`）；同一 `operation_id`
  绑定不同字段白名单（含顺序）返回 `409 operation_id_conflict`；
- **按 ID 获取** `GET /experiments/{id}/snapshots/{snapshot_id}`：随时取回
  同一份不可变内容；快照只增不改；
- 快照是纯只读流程：不推进任何进度、不消耗推进 / 完成幂等键，
  也不受实验暂停状态影响。

## 受试者换号（subject replacement）

针对受试者编号录入错误等场景，提供**从未揭盲受试者**的换号通道：

- **换号** `POST /experiments/{id}/subjects/{subject_id}/replace`：请求体携带
  `replacement_subject_id`（新编号）、`operation_id`（唯一操作键）与必填
  `reason`。仅当原受试者**从未发生过任何揭盲**（无按周期揭盲记录，也未完成
  紧急揭盲）时才允许操作；服务在**单个 SQLite 写事务（`BEGIN IMMEDIATE`）**内
  把该受试者的隐藏分配（`assignments`，**序列内容与序列下标保持不变**）与
  进度（`subject_progress`）**原子转移**到新编号，并追加一条**不含条件内容**
  的换号审计记录；
- **原编号保留墓碑**：换号后原编号立即退役——不能作为新编号复用、不能再次
  换号、不能查询进度、不能推进 / 登记完成 / 发起紧急揭盲（单条与批量），
  这些操作对退役编号一律返回 `409 subject_replaced`；
- **冲突一律 `409` 且零写入**：新编号已存在（在册编号或已退役墓碑）返回
  `replacement_subject_exists`；原编号已揭盲返回 `subject_already_unblinded`；
  并发换号由写事务串行化，只有第一个提交的事务成功，其余看到墓碑返回
  `subject_replaced`——所有失败路径不写入任何数据、不消耗 `operation_id`；
- **幂等**：相同 `(实验, operation_id)` 且原 / 新编号与原因完全相同的重试
  返回**首次结果**；同一 `operation_id` 绑定不同原 / 新编号或原因返回
  `409 operation_id_conflict`；
- **审计接口** `GET /experiments/{id}/subject-replacements`：按**时间倒序**
  （最新在前）分页返回换号审计记录（原 / 新编号、原因、时间等元数据，
  不含任何条件内容），支持 `limit`（1～500，默认 100）与 `offset`；
- 换号不是揭盲：不受实验暂停状态影响，也不改变实验状态版本。

## 快速启动（Docker Compose，一条命令）

```bash
docker compose up --build
```

该命令会构建镜像并启动容器：构建时安装依赖，容器启动命令中再次校验依赖安装，
应用启动钩子（lifespan）自动完成 SQLite 建表与迁移。**旧版本数据库无需手工迁移**：
启动时新增的进度、揭盲、周期完成、单条幂等、批量揭盲、状态审计、批量状态切换、
紧急揭盲幂等、紧急揭盲审计、结果快照与受试者换号表通过 `CREATE TABLE IF NOT EXISTS`
自动补齐，
`experiments` 表新增的 `washout_seconds`、`status`、`state_version` 列通过幂等的
`ALTER TABLE ... DEFAULT` 补齐——**旧实验一律按 0 秒洗脱期、运行中（active）、
状态版本 0 处理**，旧数据保留且可继续揭盲、登记完成、暂停与恢复、紧急揭盲。

- **访问地址**：http://localhost:8000
- **交互式 API 文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 中修改）

### 常用 Docker 操作

```bash
# 后台运行
docker compose up --build -d

# 查看实时日志
docker compose logs -f

# 停止服务（保留数据卷）
docker compose down

# 停止并同时删除数据卷（⚠ 清空全部实验数据）
docker compose down -v

# 不使用 compose：直接构建并运行镜像
docker build -t crossover-api .
docker run --rm -p 8000:8000 \
  -v "$PWD/data:/app/data" \
  -e DATABASE_PATH=/app/data/experiments.db \
  crossover-api

# 在运行中的容器内执行测试（dev 依赖需自行安装）
docker compose exec api sh -c "pip install --no-cache-dir pytest httpx && pytest -q"
```

健康检查：`curl http://localhost:8000/health` 应返回 `{"status":"ok"}`。
若端口被占用，修改 `docker-compose.yml` 中 `ports` 的宿主机端口（如
`"18000:8000"`），随后访问 http://localhost:18000 。

## 必需配置

无强制配置，开箱即用。可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/app/data/experiments.db`（compose 已设置）；本地运行默认 `./experiments.db` | SQLite 数据库文件路径，目录不存在时自动创建 |

compose 中通过命名卷 `crossover-data` 持久化数据，删除容器数据不丢失。

## API 说明

### 创建实验 `POST /experiments`

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{
        "conditions": ["A", "B", "C"],
        "subject_ids": ["s1", "s2", "s3", "s4", "s5", "s6"],
        "seed": 42,
        "washout_seconds": 86400
      }'
```

- `conditions`：互异条件，至少 2 个；
- `subject_ids`：互异受试者 ID；数量必须是完整组大小（3 个条件时为 6）的整数倍；
- `seed`：随机种子（整数）。相同输入与种子 → 相同分配；
- `washout_seconds`（可选）：相邻周期间的**洗脱期秒数**，非负整数，默认 `0`。
  上一周期完成（`completed_at`）后必须再经过该时长，服务端才允许推进下一周期。

成功返回 `201`。响应中 `assignments` **仅为受试者名册**，完整条件序列由服务端持有：

```json
{
  "experiment_id": "…",
  "conditions": ["A", "B", "C"],
  "seed": 42,
  "group_size": 6,
  "num_subjects": 6,
  "num_periods": 3,
  "washout_seconds": 86400,
  "status": "active",
  "state_version": 0,
  "created_at": "…",
  "assignments": [
    {"subject_id": "s1"},
    {"subject_id": "s2"}
  ]
}
```

受试者数量不成整组时返回 `422`，并给出最近可接受数量：

```json
{
  "detail": {
    "message": "受试者数量 7 无法整分为完整的序列组（每组 6 条序列）",
    "subject_count": 7,
    "group_size": 6,
    "nearest_acceptable": [6, 12]
  }
}
```

### 推进揭盲 `POST /experiments/{experiment_id}/advance`

进入下一周期前调用，请求体：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `expected_period` | 调用方认为该受试者当前所处的下一周期（**从 1 开始**） |
| `request_id` | 调用方生成的幂等键（如 UUID），同一请求重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/advance \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "expected_period": 1, "request_id": "9f1c…"}'
```

仅当 `expected_period` 等于该受试者下一周期、**上一周期已登记完成且洗脱期已届满**
时返回 `200`，并把进度原子推进到下一周期：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "period": 1,
  "condition": "A",
  "request_id": "9f1c…",
  "revealed_at": "2026-09-21T08:00:00.000000+00:00"
}
```

冲突一律返回 `409`，响应体 `detail.reason` 区分原因，且**不揭露任何条件 / 后续安排**：

- `period_mismatch`：`expected_period` 落后或超前（并发抢先、重复提交、跳周期），
  同时回传服务端当前的 `next_period`；
- `experiment_complete`：该受试者已完成全部周期；
- `request_id_conflict`：相同 `request_id` 曾用于不同的 `subject_id` / `expected_period`；
- `prior_period_not_completed`：上一周期尚无完成记录（`washout_seconds=0` 时也必须先登记完成）；
- `washout_not_elapsed`：洗脱期尚未届满，回传
  `retry_after_seconds`（建议等待的整秒数）；响应不携带 `next_period` 或任何条件信息；
- `experiment_paused`：实验已暂停。暂停期间拒绝一切揭盲，响应不含任何条件 /
  周期信息，且不消耗 `request_id`；恢复后用原请求即可首次成功。
  （暂停前已成功的 `request_id` 重试仍返回首次结果，不受暂停影响。）

门禁判断以**服务端时间**为准并在写事务内完成，因此并发完成 / 推进串行化：
要么完成先落盘后放行，要么推进看到“未完成 / 未到期”而 `409`，不会出现绕过门禁的揭盲。
门禁失败（409）不写入幂等记录、不消耗 `request_id`，到期后用同一 `request_id` 重试即可首次成功。

幂等语义：相同 `(实验, request_id)` 且相同请求体重试，返回**首次的条件与 `revealed_at`**，
即使此时受试者已经推进到更后面的周期，也不会重复推进或改写历史。

### 批量受控揭盲 `POST /experiments/{experiment_id}/advance-batch`

需要为多名受试者同时揭盲当前周期时调用，请求体：

| 字段 | 说明 |
| --- | --- |
| `batch_id` | 批量幂等键（如 UUID），同一批请求重试必须复用 |
| `members` | 1～100 个成员，每个含 `subject_id` 与 `expected_period`；成员对必须互异，揭盲严格按该列表顺序执行 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/advance-batch \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_id": "b7c1…",
        "members": [
          {"subject_id": "s1", "expected_period": 1},
          {"subject_id": "s2", "expected_period": 1}
        ]
      }'
```

服务在**单个 SQLite 写事务（`BEGIN IMMEDIATE`）**内先对**全部**成员逐一执行与单条
推进完全相同的门禁校验（受试者存在、周期匹配 / 实验未结束、上一周期完成登记、
洗脱期届满）。任一项失败即返回 `409` 并**整批回滚、零写入**——即使排在前面的成员
本可通过，也不会被揭盲：

```json
{
  "detail": {
    "message": "批量中存在未通过门禁的成员，整批未揭盲",
    "reason": "batch_member_rejected",
    "batch_id": "b7c1…",
    "failures": [
      {"subject_id": "s1", "reason": "prior_period_not_completed"},
      {"subject_id": "s8", "reason": "period_mismatch", "next_period": 2},
      {"subject_id": "s9", "reason": "experiment_complete", "next_period": 4},
      {"subject_id": "s10", "reason": "washout_not_elapsed", "retry_after_seconds": 3600}
    ]
  }
}
```

- `failures[]` 只含失败成员的 `subject_id` 与 `reason`（周期类原因附 `next_period`，
  洗脱期原因附 `retry_after_seconds`），**不含任何条件或后续安排**；受试者不存在为
  `subject_not_found`（实验本身不存在仍返回 `404`）；
- 门禁失败不写入 `advance_batches`、不消耗 `batch_id`，到期 / 修正后用**同一个
  `batch_id`** 重试即首次成功；
- 全部通过时按**输入顺序**揭盲并推进，返回 `200`：

```json
{
  "experiment_id": "…",
  "batch_id": "b7c1…",
  "results": [
    {"experiment_id": "…", "subject_id": "s1", "period": 1, "condition": "A",
     "request_id": "batch:b7c1…:s1:1", "revealed_at": "…"},
    {"experiment_id": "…", "subject_id": "s2", "period": 1, "condition": "B",
     "request_id": "batch:b7c1…:s2:1", "revealed_at": "…"}
  ]
}
```

批量幂等语义：相同 `(实验, batch_id)` 与**完全相同的成员列表（含顺序）**重试，
返回首次的 `results`（含首次的条件与 `revealed_at`），不重复推进；同一 `batch_id`
绑定不同成员、不同周期或不同顺序均返回 `409`（`batch_id_conflict`）。
`batch_id` 与单条推进的 `request_id` 使用各自独立的幂等空间，互不影响。
成员数量不在 1～100、成员对重复或字段非法返回 `422`。

### 登记周期完成 `POST /experiments/{experiment_id}/complete`

每个周期揭盲结束后调用，服务端以自身当前时间登记完成时刻：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `period` | 要完成的周期，必须是**已揭盲且尚未完成的当前周期**（从 1 开始） |
| `request_id` | 幂等键（如 UUID），同一请求重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/complete \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "period": 1, "request_id": "7d2a…"}'
```

成功返回 `200`：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "period": 1,
  "request_id": "7d2a…",
  "completed_at": "2026-09-21T10:00:00.000000+00:00"
}
```

- 相同 `(实验, request_id)` 重试返回**首次的 `completed_at`**；同一 `request_id`
  绑定不同 `subject_id` / `period` 返回 `409`（`request_id_conflict`）；
- 周期尚未揭盲、已经完成、落后或超前（含实验未开始 / 已结束）均返回 `409`：
  `period_out_of_order`（越序，回传 `current_period`）或
  `period_already_completed`（重复登记，原完成时间不变）；
- 最后一个周期也可登记完成，登记后该受试者 `completed=true`；
- 实验或受试者不存在返回 `404`。

### 查询受试者进度 `GET /experiments/{experiment_id}/subjects/{subject_id}/progress`

只返回已揭盲 / 已完成记录、下一周期与下一周期最早可推进时间，不含完整序列或后续安排：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "next_period": 2,
  "num_periods": 3,
  "washout_seconds": 86400,
  "completed": false,
  "next_available_at": "2026-09-22T10:00:00.000000+00:00",
  "reveals": [
    {
      "period": 1,
      "condition": "A",
      "revealed_at": "…",
      "completed_at": "2026-09-21T10:00:00.000000+00:00"
    }
  ]
}
```

- `reveals[].completed_at`：该周期的完成登记时间，未登记为 `null`；
- `next_available_at`：下一周期最早可推进的时刻（上一周期 `completed_at + washout_seconds`）。
  周期 1 之前、实验已结束，或上一周期尚未完成时为 `null`。

实验或受试者不存在返回 `404`。

### 暂停实验 `POST /experiments/{experiment_id}/pause`

冻结整个实验的揭盲流程，请求体：

| 字段 | 说明 |
| --- | --- |
| `operation_id` | 调用方生成的唯一操作键（如 UUID），相同操作重试必须复用 |
| `reason` | 暂停原因（必填，非空字符串），随审计记录持久化 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/pause \
  -H 'Content-Type: application/json' \
  -d '{"operation_id": "5c2a…", "reason": "安全性复核，暂停全部揭盲"}'
```

成功返回 `200`，响应即本次状态变更审计记录：

```json
{
  "id": 1,
  "experiment_id": "…",
  "operation_id": "5c2a…",
  "action": "pause",
  "from_status": "active",
  "to_status": "paused",
  "state_version": 1,
  "reason": "安全性复核，暂停全部揭盲",
  "created_at": "2026-09-21T12:00:00.000000+00:00"
}
```

- 相同 `(实验, operation_id)` 与相同原因重试返回**首次结果**（版本不重复递增、
  不重复写审计）；同一 `operation_id` 绑定不同原因 / 动作返回
  `409 operation_id_conflict`；
- 实验已经是暂停状态，换用新 `operation_id` 再暂停返回
  `409 already_paused`（回传当前 `state_version`），且不写审计；
- 实验不存在返回 `404`；`operation_id` / `reason` 缺失或为空返回 `422`。

### 恢复实验 `POST /experiments/{experiment_id}/resume`

携带期望状态版本做原子 compare-and-set 恢复，请求体：

| 字段 | 说明 |
| --- | --- |
| `expected_version` | 调用方看到的暂停版本（暂停接口返回的 `state_version`，或详情接口读取） |
| `operation_id` | 可选的唯一操作键；携带后相同操作重试返回首次结果 |
| `reason` | 可选的恢复原因，随审计记录持久化 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/resume \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 1, "operation_id": "9d7f…", "reason": "复核通过"}'
```

仅当服务端当前状态为 `paused` 且 `state_version == expected_version` 时，
才在单个写事务内恢复为 `active` 并把版本加一（响应结构同暂停，`action` 为
`resume`）。冲突一律 `409`：

- `state_version_mismatch`：期望版本与服务端当前版本不一致（期间可能已被
  暂停 / 恢复多次），回传服务端当前 `state_version` 与 `current_status`，
  调用方应重新读取状态、确认后再操作；
- `already_active`：版本虽然匹配，但实验当前已在运行中（例如以版本 `0`
  恢复一个从未暂停的实验）；
- `operation_id_conflict`：`operation_id` 曾绑定其他动作 / 原因 / 期望版本。

恢复后单条与批量揭盲立即恢复；暂停期间被 `409 experiment_paused` 拒绝的
请求**未消耗幂等键**，用原 `request_id` / `batch_id` 重试即为首次成功。

### 批量状态切换 `POST /experiments/state-batch`

一次对**多个实验**执行暂停 / 恢复，请求体：

| 字段 | 说明 |
| --- | --- |
| `batch_operation_id` | 整批幂等键（如 UUID），同一批请求重试必须复用 |
| `operations` | 1～100 项操作，每项含 `experiment_id`、`action`（`pause` / `resume`）、`expected_version`、`operation_id` 与可选 `reason`；同一实验在同一批中最多出现一次，全部通过才按该列表顺序执行 |

```bash
curl -X POST http://localhost:8000/experiments/state-batch \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_operation_id": "c41d…",
        "operations": [
          {"experiment_id": "exp-1", "action": "pause", "expected_version": 0,
           "operation_id": "op-1", "reason": "安全性复核"},
          {"experiment_id": "exp-2", "action": "resume", "expected_version": 3,
           "operation_id": "op-2"}
        ]
      }'
```

服务在**单个 SQLite 写事务（`BEGIN IMMEDIATE`）**内先对**全部**操作逐项校验：
实验存在、`state_version == expected_version`、当前状态允许目标动作
（暂停要求 `active`，恢复要求 `paused`），且 `operation_id` 未绑定其他操作。
**全部通过才按输入顺序执行**状态切换并逐项追加 `experiment_state_events`
审计；任一项冲突即返回 `409` 并**整批回滚、零写入**：

```json
{
  "detail": {
    "message": "批量状态切换中存在未通过校验的操作，整批未执行",
    "reason": "batch_operation_rejected",
    "batch_operation_id": "c41d…",
    "failures": [
      {"experiment_id": "exp-2", "operation_id": "op-2",
       "reason": "state_version_mismatch", "current_status": "paused",
       "state_version": 4, "expected_version": 3}
    ]
  }
}
```

- `failures[]` 按输入顺序列出每项失败的 `experiment_id` / `operation_id` 与
  `reason`（`experiment_not_found` / `state_version_mismatch` / `already_paused` /
  `already_active` / `operation_id_conflict`），状态类原因附服务端当前的
  `current_status` / `state_version`；
- 校验失败不写入 `state_transition_batches`、不消耗 `batch_operation_id`，
  修正后用**同一个 `batch_operation_id`** 重试即首次成功；
- 全部通过时返回 `200`，`results` 为按输入顺序排列的审计事件
  （结构同单条暂停 / 恢复响应）：

```json
{
  "batch_operation_id": "c41d…",
  "results": [
    {"id": 7, "experiment_id": "exp-1", "operation_id": "op-1", "action": "pause",
     "from_status": "active", "to_status": "paused", "state_version": 1,
     "reason": "安全性复核", "created_at": "…"},
    {"id": 3, "experiment_id": "exp-2", "operation_id": "op-2", "action": "resume",
     "from_status": "paused", "to_status": "active", "state_version": 4,
     "reason": null, "created_at": "…"}
  ]
}
```

- 相同 `batch_operation_id` 与**完全相同的操作列表（含顺序）**重试返回首次
  结果，不重复变更、不重复写审计；绑定不同操作列表返回 `409`
  （`batch_operation_id_conflict`）；
- 单项 `operation_id` 与单条暂停 / 恢复共享同一个 `(实验, operation_id)`
  幂等空间：某项引用的 `operation_id` 已存在且动作 / 期望版本 / 原因完全
  一致时，该项按**幂等重放**返回首次审计事件（不重复执行），其余项照常
  校验执行；不一致则整批 `409`（`operation_id_conflict`）；
- 操作数量不在 1～100、同一实验重复出现或字段非法返回 `422`。

### 查询状态变更审计 `GET /experiments/{experiment_id}/state-events`

返回暂停 / 恢复的审计记录，**按时间倒序**（最新在前），支持分页：

```bash
curl 'http://localhost:8000/experiments/<experiment_id>/state-events?limit=100&offset=0'
```

```json
{
  "experiment_id": "…",
  "events": [
    {"id": 3, "experiment_id": "…", "operation_id": "9d7f…",
     "action": "resume", "from_status": "paused", "to_status": "active",
     "state_version": 2, "reason": "复核通过", "created_at": "…"},
    {"id": 2, "experiment_id": "…", "operation_id": "5c2a…",
     "action": "pause", "from_status": "active", "to_status": "paused",
     "state_version": 1, "reason": "安全性复核", "created_at": "…"}
  ]
}
```

- `limit`：1～500，默认 `100`；`offset`：分页偏移，默认 `0`；非法值返回 `422`；
- 审计记录只增不改；从未暂停 / 恢复过的实验返回 `events: []`；
- 实验不存在返回 `404`。

### 紧急揭盲 `POST /experiments/{experiment_id}/emergency-unblind`

双人审批的破窗式紧急揭盲，每次审批调用一次，请求体：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `approver_id` | 审批人 ID；完成揭盲的第二审批人必须与首个审批人不同 |
| `operation_id` | 本次审批的唯一操作键（如 UUID），相同操作重试必须复用 |
| `reason` | 紧急揭盲原因（必填，非空字符串），随审计记录持久化 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/emergency-unblind \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "approver_id": "alice",
       "operation_id": "3f9a…", "reason": "SAE 复核，需立即揭盲"}'
```

首个审批人的申请返回 `200` 与待审批回执，**不含任何条件内容**：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "operation_id": "3f9a…",
  "status": "pending_second_approval",
  "first_approver_id": "alice",
  "opened_at": "2026-09-22T08:00:00.000000+00:00",
  "window_expires_at": "2026-09-22T08:15:00.000000+00:00"
}
```

15 分钟授权窗口内，第二个**不同**审批人以自己的 `operation_id` 再调用一次，
返回 `200` 与该受试者的**完整条件序列**：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "operation_id": "8c2e…",
  "status": "unblinded",
  "first_approver_id": "alice",
  "second_approver_id": "bob",
  "opened_at": "2026-09-22T08:00:00.000000+00:00",
  "window_expires_at": "2026-09-22T08:15:00.000000+00:00",
  "revealed_at": "2026-09-22T08:03:00.000000+00:00",
  "sequence": ["A", "C", "B"]
}
```

冲突一律返回 `409`，且**不揭露任何条件 / 序列**：

- `same_approver`：第二审批人与首个审批人相同（双人复核未满足）；
- `window_expired`：15 分钟授权窗口已过期（过期审计已落盘，重新发起首个
  审批即可开启新窗口；过期失败不消耗 `operation_id`）；
- `already_unblinded`：该受试者已完成紧急揭盲，一次性揭盲不可重复；
- `operation_id_conflict`：相同 `operation_id` 曾绑定不同的受试者 / 审批人 / 原因。

幂等语义：相同 `(实验, operation_id)` 且相同请求体重试，返回**首次结果**——
首开窗口的重放返回原待审批回执（即使随后已完成揭盲），完成揭盲的重放返回
首次的完整序列，均不重复执行、不重复写审计。实验或受试者不存在返回 `404`；
字段缺失或为空返回 `422`。

### 查询紧急揭盲审计 `GET /experiments/{experiment_id}/emergency-unblind-events`

返回紧急揭盲的审计记录（**不含任何条件内容**），按时间倒序（最新在前），支持分页：

```bash
curl 'http://localhost:8000/experiments/<experiment_id>/emergency-unblind-events?limit=100&offset=0'
```

```json
{
  "experiment_id": "…",
  "events": [
    {"id": 2, "experiment_id": "…", "subject_id": "s1", "action": "unblinded",
     "approver_id": "bob", "operation_id": "8c2e…", "reason": "SAE 复核",
     "window_expires_at": null, "created_at": "…"},
    {"id": 1, "experiment_id": "…", "subject_id": "s1", "action": "window_opened",
     "approver_id": "alice", "operation_id": "3f9a…", "reason": "SAE 复核",
     "window_expires_at": "2026-09-22T08:15:00.000000+00:00", "created_at": "…"}
  ]
}
```

- `action`：`window_opened`（首个审批开启窗口）/ `unblinded`（第二审批完成揭盲）/
  `window_expired`（窗口过期；该事件 `operation_id` 为 `null`，过期失败不消耗幂等键）；
- `limit`：1～500，默认 `100`；`offset`：分页偏移，默认 `0`；非法值返回 `422`；
- 审计记录只增不改；从未申请过紧急揭盲的实验返回 `events: []`；
- 实验不存在返回 `404`。

### 生成脱敏结果快照 `POST /experiments/{experiment_id}/snapshots`

把当前时点已揭盲的结果固化为不可变 JSON 快照，请求体：

| 字段 | 说明 |
| --- | --- |
| `fields` | 输出字段白名单：`period` / `condition` / `revealed_at` / `completed_at`，至少 1 个且互异；未列入的字段不出现在快照中 |
| `operation_id` | 本次快照的唯一操作键（如 UUID），相同操作重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/snapshots \
  -H 'Content-Type: application/json' \
  -d '{"fields": ["period", "condition", "completed_at"],
       "operation_id": "6b1f…"}'
```

服务在**单个一致性读事务**内固定同一时点 `generated_at`，按 `subject_id`
稳定排序生成快照，成功返回 `200`：

```json
{
  "snapshot_id": "9da8…",
  "experiment_id": "…",
  "operation_id": "6b1f…",
  "fields": ["period", "condition", "completed_at"],
  "digest": "c8ba…（64 位十六进制 SHA-256）",
  "created_at": "2026-09-22T08:00:00.000000+00:00",
  "snapshot": {
    "experiment_id": "…",
    "generated_at": "2026-09-22T08:00:00.000000+00:00",
    "subjects": [
      {"subject_id": "s1", "periods": [
        {"period": 1, "condition": "A", "completed_at": "2026-09-22T07:30:00.000000+00:00"},
        {"period": 2, "condition": "C", "completed_at": null}
      ]},
      {"subject_id": "s2", "periods": []}
    ]
  }
}
```

- 快照覆盖名册内**全部受试者**（按 `subject_id` 稳定排序），但每个受试者
  只输出**已揭盲周期**；已揭盲但未完成的周期 `completed_at` 为 `null`，
  尚未揭盲任何周期的受试者 `periods` 为空列表；
- 快照绝不包含未揭盲周期、完整序列或 `sequence_index`；
- `digest` 为 `snapshot` 内容规范化 JSON（`sort_keys`、紧凑分隔符、UTF-8）
  的 SHA-256，调用方可按同一规则重算校验；
- 相同 `(实验, operation_id)` 与**完全相同的字段白名单（含顺序）**重试返回
  同一快照（同一 `snapshot_id` / 内容 / `digest`）；同一 `operation_id`
  绑定不同白名单返回 `409 operation_id_conflict`；
- 实验不存在返回 `404`；白名单为空、含未知字段或重复字段、`operation_id`
  缺失或为空均返回 `422`（校验失败不消耗 `operation_id`）。

### 按快照 ID 获取快照 `GET /experiments/{experiment_id}/snapshots/{snapshot_id}`

随时取回同一份不可变快照（响应结构与生成接口相同）：

```bash
curl http://localhost:8000/experiments/<experiment_id>/snapshots/<snapshot_id>
```

快照只增不改：之后的揭盲、完成、暂停 / 恢复均不影响已生成快照的内容与
`digest`。快照或实验不存在（含跨实验取快照）返回 `404`。

### 受试者换号 `POST /experiments/{experiment_id}/subjects/{subject_id}/replace`

把从未揭盲受试者的隐藏分配与进度转移到新编号，请求体：

| 字段 | 说明 |
| --- | --- |
| `replacement_subject_id` | 新受试者编号；必须未被占用（在册编号与已退役编号均不可复用） |
| `operation_id` | 本次换号的唯一操作键（如 UUID），相同操作重试必须复用 |
| `reason` | 换号原因（必填，非空字符串），随审计记录持久化 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/subjects/s1/replace \
  -H 'Content-Type: application/json' \
  -d '{"replacement_subject_id": "s1-new", "operation_id": "4e7b…",
       "reason": "受试者编号录入错误"}'
```

仅当原受试者**从未揭盲**（无按周期揭盲记录，也未完成紧急揭盲）时返回 `200`，
服务在单个写事务内把隐藏分配（序列内容与下标不变）与进度原子转移到新编号，
响应即本次换号审计记录（不含任何条件内容）：

```json
{
  "id": 1,
  "experiment_id": "…",
  "original_subject_id": "s1",
  "replacement_subject_id": "s1-new",
  "operation_id": "4e7b…",
  "reason": "受试者编号录入错误",
  "created_at": "2026-09-22T08:00:00.000000+00:00"
}
```

冲突一律返回 `409` 且**零写入**（不消耗 `operation_id`），响应不揭露任何条件：

- `replacement_subject_exists`：新编号已存在（在册编号或已退役墓碑）；
- `subject_already_unblinded`：原受试者已发生过揭盲，不允许换号；
- `subject_replaced`：原编号已退役（墓碑），不能再次换号；并发换号只有
  第一个提交的事务成功，其余同样收到该原因；
- `operation_id_conflict`：相同 `operation_id` 曾绑定不同的原 / 新编号或原因。

换号后原编号保留墓碑：不能作为新编号复用、不能查询进度、不能推进 / 登记
完成 / 发起紧急揭盲（这些操作对退役编号返回 `409 subject_replaced`）。
相同 `(实验, operation_id)` 且原 / 新编号与原因完全相同的重试返回首次结果。
实验或原受试者不存在返回 `404`；字段缺失或为空返回 `422`。

### 查询换号审计 `GET /experiments/{experiment_id}/subject-replacements`

返回换号审计记录（**不含任何条件内容**），按时间倒序（最新在前），支持分页：

```bash
curl 'http://localhost:8000/experiments/<experiment_id>/subject-replacements?limit=100&offset=0'
```

```json
{
  "experiment_id": "…",
  "replacements": [
    {"id": 2, "experiment_id": "…", "original_subject_id": "s2",
     "replacement_subject_id": "s2-new", "operation_id": "91cd…",
     "reason": "受试者退出后替补编号", "created_at": "…"},
    {"id": 1, "experiment_id": "…", "original_subject_id": "s1",
     "replacement_subject_id": "s1-new", "operation_id": "4e7b…",
     "reason": "受试者编号录入错误", "created_at": "…"}
  ]
}
```

- `limit`：1～500，默认 `100`；`offset`：分页偏移，默认 `0`；非法值返回 `422`；
- 审计记录只增不改；从未换号的实验返回 `replacements: []`；
- 实验不存在返回 `404`。

### 其他接口

- `GET /experiments/{experiment_id}`：查询实验（受试者名册 + 条件清单 +
  `status` / `state_version`，**不含条件序列**；不存在返回 `404`）；
- `GET /experiments`：列出全部实验摘要（含 `status` / `state_version`）；
- `GET /health`：健康检查。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                         # 运行测试（含序列平衡、幂等、门禁、409 并发、暂停/恢复、批量状态切换、紧急揭盲双审批、结果快照、受试者换号、旧库迁移校验）
uvicorn app.main:app --reload  # 本地启动，默认 http://127.0.0.1:8000
```

## 实现要点

- **Williams 序列**（`app/williams.py`）：第一行按 `0, n-1, 1, n-2, …` 从两端交替取值，
  其余行循环移位（模 n）；奇数阶再拼接镜像方阵，使每个有向相邻对出现次数相等。
- **确定性分配**（`app/main.py`）：将完整序列组复制 `受试者数 / 组大小` 份构成序列池，
  用 `random.Random(seed)` 打乱后依次映射给受试者，因此相同输入与种子结果一致。
  序列只写入服务端存储，不进入创建 / 详情响应。
- **存储**（`app/db.py`，SQLite）共十四张表，启动时幂等迁移（旧库自动补齐）：
  - `experiments`：含 `washout_seconds`、`status`（`active` / `paused`）与
    `state_version`（旧库缺列时分别 `ALTER TABLE ... DEFAULT 0 / 'active' / 0` 补齐）；
  - `assignments`：受试者完整序列（服务端持有）；
  - `subject_progress`：每受试者的 `next_period`；
  - `unblindings`：已揭盲记录（周期 → 条件，含幂等键与时间戳）；
  - `period_completions`：周期完成记录（周期 → `completed_at`，主键冲突即重复完成）；
  - `advance_requests` / `completion_requests`：推进与完成各自按
    `(实验, request_id)` 唯一约束保存首次结果，支持重放；
  - `advance_batches`：批量揭盲按 `(实验, batch_id)` 保存首次绑定的有序成员列表
    与首次结果（成员或顺序不同即 `409`）；
  - `experiment_state_events`：暂停 / 恢复的追加式审计记录（自增 `id`、
    `action`、前后状态、`state_version`、原因、时间戳），按
    `(实验, operation_id)` 唯一约束支持操作幂等重放，查询按 `id DESC` 时间倒序；
  - `state_transition_batches`：批量状态切换按 `batch_operation_id`（全局唯一）
    保存首次绑定的有序操作列表与首次结果（操作列表或顺序不同即 `409`）；
  - `emergency_unblind_operations`：紧急揭盲按 `(实验, operation_id)` 保存首次
    请求绑定的受试者 / 审批人 / 原因与首次结果（完成揭盲的首次结果含完整序列，
    仅供相同 `operation_id` 幂等重放）；
  - `emergency_unblind_events`：紧急揭盲的追加式审计记录（`window_opened` /
    `unblinded` / `window_expired`，自增 `id` 时间倒序），只含审批人、原因与
    时间等元数据，**不含任何条件内容**。
  - `result_snapshots`：脱敏结果快照，按 `(实验, operation_id)` 唯一约束
    保存首次请求绑定的字段白名单、不可变快照内容与 SHA-256 摘要
    （`id` 为快照 ID，可按需取回）；快照只增不改。
  - `subject_replacements`：受试者换号的墓碑与追加式审计（合一张表）：
    每行记录原编号（退役墓碑）、新编号、`operation_id`、原因与时间，
    按 `(实验, operation_id)` 唯一约束支持幂等重放，按自增 `id` 时间倒序
    分页查询；只含编号 / 原因 / 时间等元数据，**不含任何条件内容**。
- **暂停 / 恢复的原子性**：暂停与恢复都在单个 `BEGIN IMMEDIATE` 写事务内完成
  “幂等检查 → 状态 / 版本校验 → 更新 `experiments` 并追加审计 → 提交”。
  恢复以 `state_version` 做 compare-and-set，版本不符整体回滚并返回 `409`，
  并发恢复只有一个赢家；暂停门禁同样在揭盲写事务内判定，与暂停 / 恢复互斥：
  事务提交后的揭盲必看到 `paused` 而拒绝，不会出现暂停后仍漏出条件。
- **批量状态切换的原子性**：批量暂停 / 恢复在同一 `BEGIN IMMEDIATE` 事务内
  **先统一校验全部操作**（实验存在、版本 compare-and-set、状态机、单项幂等），
  全部通过才**按输入顺序**更新各实验并逐项追加审计，最后落盘整批幂等结果；
  任一项冲突整体回滚、零写入，并发批量只有一个赢家。单项 `operation_id`
  与单条暂停 / 恢复共享 `(实验, operation_id)` 幂等空间，已存在且参数一致的
  操作按幂等重放处理，不会重复变更或重复写审计。
- **原子推进与完成门禁**：推进、完成与批量推进都在单个 `BEGIN IMMEDIATE` 事务内完成
  “幂等检查 → 顺序 / 周期校验 → 写入并提交”，配合 `busy_timeout` 让并发请求串行化：
  每个周期只有一个完成请求、一次推进成功，其余得到 `409`；推进时**服务端时间在锁内读取**，
  与并发的完成登记互斥，`completed_at + washout_seconds` 的判断与揭盲写入原子一致；
  批量推进在同一事务内**先统一校验全部成员、再按输入顺序写入**，任何失败路径整体回滚，
  不会绕过门禁、部分揭盲或多揭露条件。
- **暂停期揭盲门禁与幂等重放的顺序**：单条推进与批量推进都**先做幂等命中检查**，
  已成功操作的重放即使在暂停期间也直接返回首次结果（只读，不重新推进）；
  未命中的新请求再经过暂停门禁，得到不含任何条件信息的 `409 experiment_paused`，
  且不写入幂等记录，恢复后原键仍可首次成功。
- **紧急揭盲的双审批原子性**：每次审批在单个 `BEGIN IMMEDIATE` 事务内完成
  “幂等检查 → 该受试者最近会话判定 → 审计与幂等结果落盘 → 提交”。
  首个审批落 `window_opened` 审计并开启 15 分钟授权窗口（窗口截止时刻由
  服务端时间在锁内计算）；窗口内第二个不同审批人在**同一事务**内落
  `unblinded` 审计与幂等结果，并一次性返回完整条件序列。并发第二审批被
  写事务串行化：只有第一个提交的事务看到“窗口仍开启”并完成揭盲，其余看到
  已完成而 `409 already_unblinded`——**序列只产生一次结果**。窗口过期时在
  同一事务内落 `window_expired` 审计并提交后拒绝（不消耗 `operation_id`），
  随后可重新发起首个审批开启新窗口；同一审批人重复、已完成重复申请等失败
  路径一律零写入、不泄露任何条件内容。
- **结果快照的一致性与不可变性**：快照生成在单个 `BEGIN IMMEDIATE` 事务内
  完成“幂等检查 → 固定时点（`generated_at`）→ 读取名册 / 已揭盲 / 完成记录
  → 计算 SHA-256 摘要并落盘 → 提交”。所有读取取自同一事务快照，并发写入
  要么在快照之前可见、要么完全不可见，不会出现半新半旧的拼接结果；
  生成全程不触碰 `assignments.sequence` / `sequence_index`，条件只来自
  `unblindings` 已揭盲记录。快照落盘后只增不改，相同 `operation_id` 重放
  与按快照 ID 取回的都是同一份内容；并发相同 `operation_id` 请求被写事务
  串行化，只生成一份快照，其余幂等重放。
- **受试者换号的原子性与墓碑语义**：换号在单个 `BEGIN IMMEDIATE` 写事务内
  完成“幂等检查 → 原编号在册且未退役校验 → 新编号未占用校验（在册与墓碑
  均不可复用）→ 原编号从未揭盲校验（`unblindings` 与紧急揭盲 `unblinded`
  审计均无记录）→ `assignments` 隐藏分配（序列内容与 `sequence_index` 不变）
  与 `subject_progress` 进度改挂新编号 → 写入原编号墓碑与换号审计 → 提交”。
  并发换号被写事务串行化：只有第一个提交的事务成功，其余看到墓碑而
  `409 subject_replaced`；所有失败路径整体回滚、零写入、不消耗
  `operation_id`。墓碑检查同样内嵌在单条 / 批量推进、完成登记、进度查询与
  紧急揭盲的写事务（或查询）中，退役编号的一切后续操作一律
  `409 subject_replaced`，不会泄露任何条件内容。
