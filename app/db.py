"""SQLite 存储层。

数据库路径通过环境变量 ``DATABASE_PATH`` 配置，默认为当前目录下的
``experiments.db``。表结构在应用启动时通过 :func:`init_db` 创建，
新增表均使用 ``CREATE TABLE IF NOT EXISTS``，新增列通过幂等的
``ALTER TABLE`` 补齐，因此已有数据库在启动时会自动迁移，无需手工介入。

表说明：
* ``experiments`` / ``assignments``：实验与受试者的完整条件序列，
  序列只保存在服务端，不通过创建 / 详情接口下发；
  ``experiments.washout_seconds`` 为周期之间的洗脱期秒数（旧库默认 0）；
* ``subject_progress``：每个受试者下一待揭盲周期 ``next_period``；
* ``unblindings``：已揭盲记录（周期 -> 条件）；
* ``period_completions``：周期完成记录（周期 -> completed_at），
  推进下一周期前上一周期必须已完成且洗脱期已届满；
* ``advance_requests`` / ``completion_requests``：推进与完成接口的
  幂等结果，按 ``(实验, request_id)`` 去重。
* ``advance_batches``：批量受控揭盲的幂等结果，按 ``(实验, batch_id)``
  去重，保存首次请求绑定的有序成员列表与首次揭盲结果。
* ``experiments.status`` / ``experiments.state_version``：实验级状态
  （``active`` / ``paused``）与单调递增的状态版本，暂停 / 恢复通过
  ``BEGIN IMMEDIATE`` 事务做 compare-and-set；旧库缺列时以
  ``ALTER TABLE ... DEFAULT`` 补齐，旧实验一律按 active / 版本 0 处理。
* ``experiment_state_events``：暂停 / 恢复的追加式审计记录，
  按 ``(实验, operation_id)`` 唯一约束支持操作幂等重放，查询按时间倒序。
* ``state_transition_batches``：跨实验批量状态切换（暂停 / 恢复）的幂等结果，
  按 ``batch_operation_id`` 全局去重，保存首次请求绑定的有序操作列表
  与首次执行结果；整批在同一事务内“先统一校验、再一起写入”。
* ``emergency_unblind_operations``：紧急揭盲的幂等结果，按
  ``(实验, operation_id)`` 去重，保存首次请求绑定的受试者 / 审批人 / 原因
  与首次结果（完成揭盲的首次结果含完整条件序列，仅供相同 operation_id
  幂等重放）。
* ``emergency_unblind_events``：紧急揭盲的追加式审计记录
  （``window_opened`` / ``unblinded`` / ``window_expired``），只含审批人、
  原因与时间等元数据，**不含任何条件内容**；按自增 id 时间倒序分页查询。
* ``result_snapshots``：脱敏结果快照，按 ``(实验, operation_id)`` 去重，
  保存在单个一致性读事务内固定同一时点生成的不可变 JSON 快照
  （仅已揭盲周期与完成时间，按 ``subject_id`` 稳定排序，绝不包含
  ``sequence_index`` 或未揭盲安排）及其规范化 JSON 的 SHA-256 摘要；
  快照只增不改，可按快照 ID 随时取回同一份内容。
* ``allocation_commitments``：创建实验时为每个稳定分配槽位 / 周期生成
  128 位随机 nonce 与 SHA-256 盲态承诺。承诺输入为键排序、紧凑 UTF-8 JSON
  数组 ``[experiment_id, allocation_slot_id, period, condition, nonce]``；
  查询接口只返回当前归属受试者、槽位、周期与承诺，未揭盲项不返回 nonce。
  换号只迁移槽位归属，不重算承诺；旧实验没有承诺，拒绝事后补造。
* ``subject_replacements``：受试者换号的墓碑与追加式审计（合一张表）。
  每行记录一次成功换号：原编号（``original_subject_id``，即退役墓碑）、
  新编号（``replacement_subject_id``）、``operation_id`` 与原因，
  按 ``(实验, operation_id)`` 唯一约束支持幂等重放，按自增 id 时间倒序
  分页查询；只含编号 / 原因 / 时间等元数据，**不含任何条件内容**。
  换号在单个 ``BEGIN IMMEDIATE`` 事务内把 ``assignments`` 的隐藏分配
  （序列内容及下标不变）与 ``subject_progress`` 进度原子改挂到新编号，
  仅允许原编号从未发生过任何揭盲（按周期揭盲或完成紧急揭盲）时操作。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 写事务排队等待锁的时间，配合 BEGIN IMMEDIATE 保证并发推进串行化
BUSY_TIMEOUT_MS = 10_000

# 紧急揭盲授权窗口：首个审批人开启后，第二个不同审批人必须在窗口内批准
EMERGENCY_UNBLIND_WINDOW_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id              TEXT PRIMARY KEY,
    conditions      TEXT NOT NULL,          -- JSON 数组
    seed            INTEGER NOT NULL,
    group_size      INTEGER NOT NULL,       -- 一组完整序列的条数
    num_subjects    INTEGER NOT NULL,
    washout_seconds INTEGER NOT NULL DEFAULT 0,  -- 周期间洗脱期（秒），旧数据按 0
    status         TEXT NOT NULL DEFAULT 'active',  -- active | paused，旧库按 active
    state_version  INTEGER NOT NULL DEFAULT 0,      -- 暂停 / 恢复后单调递增
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    experiment_id  TEXT NOT NULL REFERENCES experiments(id),
    subject_id     TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,     -- 命中的 Williams 序列下标
    sequence       TEXT NOT NULL,        -- JSON 数组，映射后的完整条件序列（服务端持有）
    PRIMARY KEY (experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS subject_progress (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    next_period   INTEGER NOT NULL,      -- 下一待揭盲周期（从 1 开始）
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS unblindings (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,      -- 已揭盲周期（从 1 开始）
    condition     TEXT NOT NULL,         -- 该周期揭盲的条件
    request_id    TEXT NOT NULL,         -- 首次成功推进的幂等键
    revealed_at   TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE TABLE IF NOT EXISTS period_completions (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,      -- 已完成周期（必须先已揭盲）
    request_id    TEXT NOT NULL,         -- 首次成功完成的幂等键
    completed_at  TEXT NOT NULL,         -- 服务端登记的完成时刻
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE TABLE IF NOT EXISTS advance_requests (
    experiment_id   TEXT NOT NULL REFERENCES experiments(id),
    request_id      TEXT NOT NULL,
    subject_id      TEXT NOT NULL,       -- 首次请求绑定的受试者
    expected_period INTEGER NOT NULL,    -- 首次请求绑定的预期周期
    period          INTEGER NOT NULL,    -- 首次结果揭盲的周期
    condition       TEXT NOT NULL,       -- 首次结果揭盲的条件
    revealed_at     TEXT NOT NULL,
    PRIMARY KEY (experiment_id, request_id)
);

CREATE TABLE IF NOT EXISTS completion_requests (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    request_id    TEXT NOT NULL,
    subject_id    TEXT NOT NULL,         -- 首次请求绑定的受试者
    period        INTEGER NOT NULL,      -- 首次请求绑定 / 完成的周期
    completed_at  TEXT NOT NULL,         -- 首次结果登记的完成时刻
    PRIMARY KEY (experiment_id, request_id)
);

CREATE TABLE IF NOT EXISTS advance_batches (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    batch_id      TEXT NOT NULL,
    members       TEXT NOT NULL,         -- JSON：首次请求绑定的有序成员
                                          -- [{subject_id, expected_period}]
    results       TEXT NOT NULL,         -- JSON：首次结果（按输入顺序的揭盲列表）
    created_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, batch_id)
);

CREATE TABLE IF NOT EXISTS experiment_state_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- 单调主键，兼作时间倒序
    experiment_id  TEXT NOT NULL REFERENCES experiments(id),
    operation_id   TEXT,                  -- 调用方幂等键；SQLite 唯一索引允许多个 NULL
    action         TEXT NOT NULL,         -- pause | resume
    from_status    TEXT NOT NULL,         -- 变更前状态（active | paused）
    to_status      TEXT NOT NULL,         -- 变更后状态（paused | active）
    state_version  INTEGER NOT NULL,      -- 本次变更后的状态版本
    reason         TEXT,                  -- 暂停 / 恢复原因（可空）
    created_at     TEXT NOT NULL,
    UNIQUE (experiment_id, operation_id)
);

CREATE TABLE IF NOT EXISTS state_transition_batches (
    batch_operation_id TEXT PRIMARY KEY,  -- 整批幂等键（跨实验，全局唯一）
    operations         TEXT NOT NULL,     -- JSON：首次请求绑定的有序操作列表
                                          -- [{experiment_id, action, expected_version,
                                          --   operation_id, reason}]
    results            TEXT NOT NULL,     -- JSON：首次结果（按输入顺序的审计事件列表）
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emergency_unblind_operations (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    operation_id  TEXT NOT NULL,
    subject_id    TEXT NOT NULL,       -- 首次请求绑定的受试者
    approver_id   TEXT NOT NULL,       -- 首次请求绑定的审批人
    reason        TEXT NOT NULL,       -- 首次请求绑定的原因
    result        TEXT NOT NULL,       -- JSON：该操作的首次结果
                                       -- （unblinded 含完整序列，仅供幂等重放）
    created_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, operation_id)
);

CREATE TABLE IF NOT EXISTS emergency_unblind_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- 单调主键，兼作时间倒序
    experiment_id     TEXT NOT NULL REFERENCES experiments(id),
    subject_id        TEXT NOT NULL,
    action            TEXT NOT NULL,     -- window_opened | unblinded | window_expired
    approver_id       TEXT NOT NULL,     -- 触发本事件的审批人
    operation_id      TEXT,              -- 触发操作键；window_expired 为 NULL
                                         -- （失败不消耗幂等键）
    reason            TEXT,              -- 触发请求携带的原因
    window_expires_at TEXT,              -- 仅 window_opened：授权窗口截止时刻
    created_at        TEXT NOT NULL,
    UNIQUE (experiment_id, operation_id)
);

CREATE TABLE IF NOT EXISTS result_snapshots (
    id            TEXT PRIMARY KEY,      -- 快照 ID（UUID hex）
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    operation_id  TEXT NOT NULL,         -- 调用方幂等键
    fields        TEXT NOT NULL,         -- JSON：首次请求绑定的字段白名单（有序）
    snapshot      TEXT NOT NULL,         -- JSON：不可变快照内容（仅已揭盲周期）
    digest        TEXT NOT NULL,         -- snapshot 规范化 JSON 的 SHA-256（hex）
    created_at    TEXT NOT NULL,         -- 与快照内 generated_at 同一固定时点
    UNIQUE (experiment_id, operation_id)
);

CREATE TABLE IF NOT EXISTS subject_replacements (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,  -- 单调主键，兼作时间倒序
    experiment_id           TEXT NOT NULL REFERENCES experiments(id),
    original_subject_id     TEXT NOT NULL,   -- 原编号（退役墓碑，不可复用 / 查询 / 推进）
    replacement_subject_id  TEXT NOT NULL,   -- 新编号（接管隐藏分配与进度）
    operation_id            TEXT NOT NULL,   -- 调用方幂等键
    reason                  TEXT NOT NULL,   -- 换号原因（必填）
    created_at              TEXT NOT NULL,
    UNIQUE (experiment_id, operation_id),
    UNIQUE (experiment_id, original_subject_id)
);

CREATE TABLE IF NOT EXISTS allocation_commitments (
    experiment_id       TEXT NOT NULL REFERENCES experiments(id),
    allocation_slot_id  TEXT NOT NULL,       -- 创建时固定的稳定槽位 ID
    period              INTEGER NOT NULL,
    subject_id          TEXT NOT NULL,       -- 当前归属；换号只迁移该归属
    condition           TEXT NOT NULL,       -- 创建时安排的条件（服务端持有）
    nonce               TEXT NOT NULL,       -- 128 位随机 nonce（32 位 hex）
    commitment          TEXT NOT NULL,       -- 规范化承诺 JSON 的 SHA-256 hex
    PRIMARY KEY (experiment_id, allocation_slot_id, period)
);
"""


class StalePeriodError(Exception):
    """expected_period 与受试者下一周期不一致（或实验已结束）。"""

    def __init__(self, next_period: int, complete: bool) -> None:
        self.next_period = next_period
        self.complete = complete
        super().__init__(
            f"expected period mismatch: next_period={next_period}, complete={complete}"
        )


class RequestIdConflictError(Exception):
    """request_id 已被另一条请求（不同 subject_id / period）占用。"""


class PriorPeriodNotCompletedError(Exception):
    """推进下一周期时，上一周期尚无完成记录。

    不携带任何后续周期信息，避免提前揭盲。
    """


class WashoutNotElapsedError(Exception):
    """推进下一周期时，洗脱期尚未届满。

    :param retry_after_seconds: 距离 ``completed_at + washout_seconds``
        还需等待的整秒数（至少 1）。
    """

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"washout not elapsed: retry_after={retry_after_seconds}s")


class CompletionOrderError(Exception):
    """登记完成的周期越序：尚未揭盲 / 已完成 / 落后或超前于当前周期。"""

    def __init__(self, current_period: int, already_completed: bool) -> None:
        self.current_period = current_period
        self.already_completed = already_completed
        super().__init__(
            f"completion out of order: current_period={current_period}, "
            f"already_completed={already_completed}"
        )


class BatchIdConflictError(Exception):
    """batch_id 已被成员列表不同的另一次批量请求占用。"""


class BatchMembersRejectedError(Exception):
    """批量揭盲中至少一个成员未通过门禁，整批必须零写入。

    :param failures: 每个失败成员一项，键为 ``subject_id`` / ``reason``，
        并按原因附带 ``next_period`` 或 ``retry_after_seconds``；
        不含任何条件 / 后续安排信息。
    """

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = failures
        super().__init__(f"batch members rejected: {len(failures)} failure(s)")


class ExperimentPausedError(Exception):
    """实验已暂停：暂停期间拒绝一切揭盲（单条与批量）。

    冲突路径不携带任何条件 / 后续安排信息。
    """


class OperationIdConflictError(Exception):
    """operation_id 已被另一个操作（不同动作 / 原因 / 期望版本）占用。"""


class BatchOperationIdConflictError(Exception):
    """batch_operation_id 已被操作列表不同的另一次批量状态切换请求占用。"""


class BatchStateTransitionsRejectedError(Exception):
    """批量状态切换中至少一项未通过状态 / 版本校验，整批必须零写入。

    :param failures: 每个失败操作一项，键为 ``experiment_id`` /
        ``operation_id`` / ``reason``；状态类原因附带服务端当前的
        ``current_status`` / ``state_version`` 与调用方的 ``expected_version``。
    """

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = failures
        super().__init__(
            f"batch state transitions rejected: {len(failures)} failure(s)"
        )


class EmergencySameApproverError(Exception):
    """第二审批人与首个审批人相同：双人复核未满足，不得揭盲。

    冲突路径不携带任何条件 / 序列信息。
    """


class EmergencyAlreadyUnblindedError(Exception):
    """该受试者已完成紧急揭盲：一次性揭盲不可重复申请。

    冲突路径不携带任何条件 / 序列信息。
    """


class EmergencyWindowExpiredError(Exception):
    """授权窗口已过期：过期审计已在写事务内落盘提交，本次申请被拒绝。

    冲突路径不携带任何条件 / 序列信息；失败不消耗 operation_id，
    重新发起首个审批即可开启新窗口。
    """


class EmergencyOperationIdConflictError(Exception):
    """operation_id 已被另一个紧急揭盲申请（不同受试者 / 审批人 / 原因）占用。"""


class SnapshotOperationIdConflictError(Exception):
    """operation_id 已被字段白名单不同的另一次快照请求占用。

    冲突路径不携带任何条件 / 序列信息。
    """


class CommitmentsUnavailableError(Exception):
    """旧实验没有创建时生成的盲态承诺，且不允许事后补造。"""


class SubjectReplacedError(Exception):
    """受试者编号已退役（换号墓碑）：不能复用、查询进度或推进。

    冲突路径不携带任何条件 / 序列信息。
    """


class ReplacementSubjectExistsError(Exception):
    """换号新编号已存在（在册编号或已退役墓碑），不能复用。"""


class SubjectAlreadyUnblindedError(Exception):
    """原受试者已发生过揭盲（按周期揭盲或完成紧急揭盲），不允许换号。

    冲突路径不携带任何条件 / 序列信息。
    """


class ReplacementOperationIdConflictError(Exception):
    """operation_id 已被另一个换号操作（不同原 / 新编号或原因）占用。

    冲突路径不携带任何条件 / 序列信息。
    """


class InvalidStateTransitionError(Exception):
    """暂停 / 恢复状态转换不合法（状态不符或期望版本过期）。

    :param reason: ``already_paused`` / ``already_active`` /
        ``state_version_mismatch``。
    :param current_status: 服务端当前状态。
    :param state_version: 服务端当前状态版本。
    :param expected_version: 调用方提交的期望版本（仅版本冲突时存在）。
    """

    def __init__(
        self,
        reason: str,
        current_status: str,
        state_version: int,
        expected_version: int | None = None,
    ) -> None:
        self.reason = reason
        self.current_status = current_status
        self.state_version = state_version
        self.expected_version = expected_version
        super().__init__(
            f"invalid state transition: {reason}, status={current_status}, "
            f"version={state_version}"
        )


def utc_now() -> datetime:
    """服务端当前 UTC 时刻；测试可 monkeypatch 本函数以冻结时间。"""
    return datetime.now(timezone.utc)


def _database_path() -> str:
    return os.environ.get("DATABASE_PATH", "experiments.db")


def get_connection() -> sqlite3.Connection:
    path = Path(_database_path())
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def init_db() -> None:
    """建表 / 迁移（幂等），在应用启动时调用。

    * 新增表通过 ``CREATE TABLE IF NOT EXISTS`` 补齐；盲态承诺等新模块只建表，
      不为旧实验回填创建时数据；
    * 旧库的 ``experiments`` 表缺少 ``washout_seconds`` 列时以
      ``ALTER TABLE ... DEFAULT 0`` 补齐，旧实验按 0 秒洗脱期处理；
    * 旧库缺少 ``status`` / ``state_version`` 列时分别以
      ``ALTER TABLE ... DEFAULT 'active' / 0`` 补齐，
      旧实验一律按“运行中、版本 0”处理。
    """
    with closing(get_connection()) as conn:
        with conn:
            conn.executescript(SCHEMA)
            if not _column_exists(conn, "experiments", "washout_seconds"):
                conn.execute(
                    "ALTER TABLE experiments "
                    "ADD COLUMN washout_seconds INTEGER NOT NULL DEFAULT 0"
                )
            if not _column_exists(conn, "experiments", "status"):
                conn.execute(
                    "ALTER TABLE experiments "
                    "ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
                )
            if not _column_exists(conn, "experiments", "state_version"):
                conn.execute(
                    "ALTER TABLE experiments "
                    "ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0"
                )


def _commitment_digest(
    experiment_id: str,
    allocation_slot_id: str,
    period: int,
    condition: str,
    nonce: str,
) -> str:
    """计算盲态承诺：数组按规范 JSON 编码后取 SHA-256。"""
    payload = [
        experiment_id,
        allocation_slot_id,
        period,
        condition,
        nonce,
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def insert_experiment(
    experiment_id: str,
    conditions: list[str],
    seed: int,
    group_size: int,
    created_at: str,
    assignments: list[dict[str, Any]],
    washout_seconds: int = 0,
) -> None:
    with closing(get_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO experiments "
                "(id, conditions, seed, group_size, num_subjects, "
                "washout_seconds, status, state_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?)",
                (
                    experiment_id,
                    json.dumps(conditions, ensure_ascii=False),
                    seed,
                    group_size,
                    len(assignments),
                    washout_seconds,
                    created_at,
                ),
            )
            assignment_rows = []
            commitment_rows = []
            for assignment in assignments:
                slot_id = assignment.get(
                    "allocation_slot_id", uuid.uuid4().hex
                )
                assignment_rows.append(
                    (
                        experiment_id,
                        assignment["subject_id"],
                        assignment["sequence_index"],
                        json.dumps(assignment["sequence"], ensure_ascii=False),
                    )
                )
                for period, condition in enumerate(assignment["sequence"], start=1):
                    nonce = secrets.token_hex(16)  # 128 位随机 nonce
                    commitment_rows.append(
                        (
                            experiment_id,
                            slot_id,
                            period,
                            assignment["subject_id"],
                            condition,
                            nonce,
                            _commitment_digest(
                                experiment_id, slot_id, period, condition, nonce
                            ),
                        )
                    )
            conn.executemany(
                "INSERT INTO assignments "
                "(experiment_id, subject_id, sequence_index, sequence) "
                "VALUES (?, ?, ?, ?)",
                assignment_rows,
            )
            conn.executemany(
                "INSERT INTO allocation_commitments "
                "(experiment_id, allocation_slot_id, period, subject_id, "
                "condition, nonce, commitment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                commitment_rows,
            )


def advance_period(
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
) -> dict[str, Any] | None:
    """原子推进一个受试者的揭盲进度。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果，
       不再次推进；若同一 request_id 绑定了不同的 subject_id /
       expected_period，抛出 :class:`RequestIdConflictError`；
    2. 周期校验：``expected_period`` 必须等于受试者当前 ``next_period``，
       且实验尚未结束，否则抛出 :class:`StalePeriodError`（409），
       失败路径不写入任何数据、不揭露任何条件；
    3. 完成与洗脱期门禁：目标周期大于 1 时，上一周期必须存在完成记录，
       否则抛出 :class:`PriorPeriodNotCompletedError`；且服务端当前时间
       必须已达到 ``completed_at + washout_seconds``，否则抛出
       :class:`WashoutNotElapsedError`（携带 retry_after_seconds）。
       门禁失败只返回“需要等待”，不揭露下一周期条件；
    4. 揭盲：写入 ``unblindings``、推进 ``subject_progress``、
       落盘 ``advance_requests``，三者一起提交或一起回滚；
    5. 新实验还在响应中返回该槽位 / 周期创建时保存的 ``allocation_slot_id``
       与 ``nonce``；旧实验无承诺，故不返回这两个字段。

    服务端时间在事务内读取，保证并发完成 / 推进下判断与写入原子一致。
    返回首次 / 重放的揭盲结果；实验或受试者不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        # 显式事务，避免 sqlite3 隐式的 deferred 事务
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _advance_locked(
                conn,
                experiment_id,
                subject_id,
                expected_period,
                request_id,
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                # 重放（只读）、409、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()


def _get_washout(conn: sqlite3.Connection, experiment_id: str) -> int | None:
    row = conn.execute(
        "SELECT washout_seconds FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    return None if row is None else int(row["washout_seconds"])


def _get_experiment_meta(
    conn: sqlite3.Connection, experiment_id: str
) -> dict[str, Any] | None:
    """读取实验的门禁相关元数据；实验不存在时返回 ``None``。"""
    row = conn.execute(
        "SELECT washout_seconds, status, state_version "
        "FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "washout_seconds": int(row["washout_seconds"]),
        "status": row["status"],
        "state_version": int(row["state_version"]),
    }


def _is_tombstoned(
    conn: sqlite3.Connection, experiment_id: str, subject_id: str
) -> bool:
    """该编号是否已因换号退役（存在墓碑记录）。"""
    row = conn.execute(
        "SELECT 1 FROM subject_replacements "
        "WHERE experiment_id = ? AND original_subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    return row is not None


def _get_commitment_for_reveal(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    period: int,
) -> dict[str, str] | None:
    """读取已到揭盲时刻的槽位承诺 secret；旧实验没有承诺时返回 None。"""
    row = conn.execute(
        "SELECT allocation_slot_id, nonce FROM allocation_commitments "
        "WHERE experiment_id = ? AND subject_id = ? AND period = ?",
        (experiment_id, subject_id, period),
    ).fetchone()
    if row is None:
        return None
    return {
        "allocation_slot_id": row["allocation_slot_id"],
        "nonce": row["nonce"],
    }


def _advance_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回首次结果，绝不重复推进
    request_row = conn.execute(
        "SELECT ar.subject_id, ar.expected_period, ar.period, ar.condition, "
        "ar.revealed_at, ac.allocation_slot_id, ac.nonce "
        "FROM advance_requests ar "
        "LEFT JOIN allocation_commitments ac "
        "ON ac.experiment_id = ar.experiment_id "
        "AND ac.subject_id = ar.subject_id "
        "AND ac.period = ar.period "
        "WHERE ar.experiment_id = ? AND ar.request_id = ?",
        (experiment_id, request_id),
    ).fetchone()
    if request_row is not None:
        if (
            request_row["subject_id"] != subject_id
            or request_row["expected_period"] != expected_period
        ):
            raise RequestIdConflictError()
        result = {
            "experiment_id": experiment_id,
            "subject_id": subject_id,
            "period": request_row["period"],
            "condition": request_row["condition"],
            "request_id": request_id,
            "revealed_at": request_row["revealed_at"],
            "_replayed": True,
        }
        if request_row["allocation_slot_id"] is not None:
            result["allocation_slot_id"] = request_row["allocation_slot_id"]
            result["nonce"] = request_row["nonce"]
        return result

    # 2) 实验必须存在（决定洗脱期 / 暂停状态），受试者序列必须存在（均不做下发）
    meta = _get_experiment_meta(conn, experiment_id)
    if meta is None:
        return None
    washout = meta["washout_seconds"]

    # 暂停期间拒绝一切揭盲：幂等重放已在上方放行，此处不泄露任何条件
    if meta["status"] == "paused":
        raise ExperimentPausedError()

    # 已退役编号（换号墓碑）不能推进：不揭露任何条件
    if _is_tombstoned(conn, experiment_id, subject_id):
        raise SubjectReplacedError()

    assignment_row = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    sequence = json.loads(assignment_row["sequence"])
    num_periods = len(sequence)

    progress_row = conn.execute(
        "SELECT next_period FROM subject_progress "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    next_period = progress_row["next_period"] if progress_row is not None else 1

    # 3) 仅当请求的正是下一周期且实验未结束时才继续（不揭露任何条件）
    if next_period > num_periods or expected_period != next_period:
        raise StalePeriodError(
            next_period=next_period, complete=next_period > num_periods
        )

    # 4) 周期完成与洗脱期门禁（周期 1 无前置周期）
    if next_period > 1:
        completion_row = conn.execute(
            "SELECT completed_at FROM period_completions "
            "WHERE experiment_id = ? AND subject_id = ? AND period = ?",
            (experiment_id, subject_id, next_period - 1),
        ).fetchone()
        if completion_row is None:
            raise PriorPeriodNotCompletedError()
        completed_at = datetime.fromisoformat(completion_row["completed_at"])
        # 服务端时间在锁内读取：与并发的完成登记互斥，判断结果原子一致
        now = utc_now()
        next_available_at = completed_at + timedelta(seconds=washout)
        if now < next_available_at:
            retry_after = math.ceil((next_available_at - now).total_seconds())
            raise WashoutNotElapsedError(max(1, retry_after))
        revealed_at = now.isoformat()
    else:
        revealed_at = utc_now().isoformat()

    condition = sequence[next_period - 1]
    commitment_secret = _get_commitment_for_reveal(
        conn, experiment_id, subject_id, next_period
    )

    conn.execute(
        "INSERT INTO unblindings "
        "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            subject_id,
            next_period,
            condition,
            request_id,
            revealed_at,
        ),
    )
    conn.execute(
        "INSERT INTO subject_progress "
        "(experiment_id, subject_id, next_period, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(experiment_id, subject_id) DO UPDATE SET "
        "next_period = excluded.next_period, updated_at = excluded.updated_at",
        (experiment_id, subject_id, next_period + 1, revealed_at),
    )
    conn.execute(
        "INSERT INTO advance_requests "
        "(experiment_id, request_id, subject_id, expected_period, "
        "period, condition, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            request_id,
            subject_id,
            expected_period,
            next_period,
            condition,
            revealed_at,
        ),
    )

    result = {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": next_period,
        "condition": condition,
        "request_id": request_id,
        "revealed_at": revealed_at,
        "_replayed": False,
    }
    if commitment_secret is not None:
        result.update(commitment_secret)
    return result


def advance_batch(
    experiment_id: str,
    batch_id: str,
    members: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """在单个写事务内批量受控揭盲并推进。

    流程（全部在一个 ``BEGIN IMMEDIATE`` 事务内完成）：

    1. 幂等检查：``(experiment_id, batch_id)`` 已存在时，仅当本次成员列表
       （``subject_id`` / ``expected_period`` 及顺序）与首次完全一致才返回
       首次结果；成员或顺序不同则抛出 :class:`BatchIdConflictError`；
    2. 门禁校验：对每个成员**先统一**做现有单条推进的全部校验——
       受试者必须存在、``expected_period`` 必须等于其下一周期且实验未结束、
       周期大于 1 时上一周期必须已完成且洗脱期已届满。门禁时刻以同一
       服务端时间读取，任一项失败即汇总全部失败项并抛出
       :class:`BatchMembersRejectedError`，**整批回滚、零写入**；
    3. 揭盲：仅当全部成员通过后，才**按输入顺序**写入每条揭盲记录、
       推进 ``subject_progress``，最后落盘 ``advance_batches`` 幂等结果，
       一起提交或一起回滚；新实验的每条结果附加稳定槽位 ID 与 nonce。

    失败项只携带 ``subject_id`` 与原因（及安全的 ``next_period`` /
    ``retry_after_seconds``），不附带任何条件。实验不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _advance_batch_locked(
                conn, experiment_id, batch_id, members
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                conn.rollback()
    finally:
        conn.close()


def _attach_commitment_secrets(
    conn: sqlite3.Connection,
    experiment_id: str,
    results: list[dict[str, Any]],
) -> None:
    """为已揭盲结果附加槽位 ID 与 nonce；旧实验无承诺时保持原样。"""
    for result in results:
        secret = _get_commitment_for_reveal(
            conn,
            experiment_id,
            result["subject_id"],
            result["period"],
        )
        if secret is not None:
            result.update(secret)


def _advance_batch_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    batch_id: str,
    members: list[dict[str, Any]],
) -> dict[str, Any] | None:
    # 1) 幂等命中：成员列表（含顺序）一致才返回首次结果，否则 409
    batch_row = conn.execute(
        "SELECT members, results FROM advance_batches "
        "WHERE experiment_id = ? AND batch_id = ?",
        (experiment_id, batch_id),
    ).fetchone()
    if batch_row is not None:
        bound_members = [
            {"subject_id": m["subject_id"], "expected_period": m["expected_period"]}
            for m in json.loads(batch_row["members"])
        ]
        request_members = [
            {"subject_id": m["subject_id"], "expected_period": m["expected_period"]}
            for m in members
        ]
        if bound_members != request_members:
            raise BatchIdConflictError()
        replay_results = json.loads(batch_row["results"])
        _attach_commitment_secrets(conn, experiment_id, replay_results)
        return {
            "experiment_id": experiment_id,
            "batch_id": batch_id,
            "results": replay_results,
            "_replayed": True,
        }

    # 2) 实验必须存在（决定洗脱期 / 暂停状态）
    meta = _get_experiment_meta(conn, experiment_id)
    if meta is None:
        return None
    washout = meta["washout_seconds"]

    # 暂停期间整批拒绝：幂等重放已在上方放行，失败项不携带任何条件
    if meta["status"] == "paused":
        raise ExperimentPausedError()

    # 一次取齐全部成员的序列与进度（同一事务快照，期间无其他写入）
    subject_ids = [m["subject_id"] for m in members]
    placeholders = ",".join("?" for _ in subject_ids)
    assignment_rows = {
        row["subject_id"]: row["sequence"]
        for row in conn.execute(
            f"SELECT subject_id, sequence FROM assignments "
            f"WHERE experiment_id = ? AND subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }
    progress_rows = {
        row["subject_id"]: row["next_period"]
        for row in conn.execute(
            f"SELECT subject_id, next_period FROM subject_progress "
            f"WHERE experiment_id = ? AND subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }
    # 已退役编号（换号墓碑）不能参与批量揭盲
    tombstoned_subjects = {
        row["original_subject_id"]
        for row in conn.execute(
            f"SELECT original_subject_id FROM subject_replacements "
            f"WHERE experiment_id = ? AND original_subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }

    # 同一批内同一受试者出现两次（周期不同）时，第二个人工复制的成员
    # 会以“推进后的下一周期”视角参与门禁校验，确保整批零写入而非部分推进
    pending_next: dict[str, int] = {}
    # 只取本批成员的上一周期完成时间（周期 1 无需完成记录）
    completion_rows = {
        (row["subject_id"], row["period"]): row["completed_at"]
        for row in conn.execute(
            f"SELECT subject_id, period, completed_at FROM period_completions "
            f"WHERE experiment_id = ? AND subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }

    # 门禁时刻：整批使用同一服务端时间（在写锁内读取）
    gate_now = utc_now()

    # 计划列表：通过门禁的成员收集其待揭盲信息；失败则汇总后整批拒绝
    plan: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for member in members:
        subject_id = member["subject_id"]
        expected_period = member["expected_period"]
        failure: dict[str, Any] | None = None

        sequence_json = assignment_rows.get(subject_id)
        if subject_id in tombstoned_subjects:
            failure = {"subject_id": subject_id, "reason": "subject_replaced"}
        elif sequence_json is None:
            failure = {"subject_id": subject_id, "reason": "subject_not_found"}
        else:
            sequence = json.loads(sequence_json)
            num_periods = len(sequence)
            next_period = pending_next.get(
                subject_id, progress_rows.get(subject_id, 1)
            )

            if next_period > num_periods or expected_period != next_period:
                failure = {
                    "subject_id": subject_id,
                    "reason": (
                        "experiment_complete"
                        if next_period > num_periods
                        else "period_mismatch"
                    ),
                    "next_period": next_period,
                }
            elif next_period > 1:
                completed_at_text = completion_rows.get(
                    (subject_id, next_period - 1)
                )
                if completed_at_text is None:
                    failure = {
                        "subject_id": subject_id,
                        "reason": "prior_period_not_completed",
                    }
                else:
                    completed_at = datetime.fromisoformat(completed_at_text)
                    next_available_at = completed_at + timedelta(seconds=washout)
                    if gate_now < next_available_at:
                        retry_after = math.ceil(
                            (next_available_at - gate_now).total_seconds()
                        )
                        failure = {
                            "subject_id": subject_id,
                            "reason": "washout_not_elapsed",
                            "retry_after_seconds": max(1, retry_after),
                        }

        if failure is None:
            plan.append(
                {
                    "subject_id": subject_id,
                    "period": next_period,
                    "condition": sequence[next_period - 1],
                }
            )
            # 同受试者后续成员看到推进后的下一周期
            pending_next[subject_id] = next_period + 1
        else:
            failures.append(failure)

    if failures:
        # 整批零写入：不揭盲任何条件，由外层事务回滚
        raise BatchMembersRejectedError(failures)

    # 3) 全部通过：按输入顺序揭盲并推进（每条时间戳在锁内顺序生成）
    results: list[dict[str, Any]] = []
    for item in plan:
        subject_id = item["subject_id"]
        period = item["period"]
        condition = item["condition"]
        # 批量揭盲在 advance_requests 之外使用带前缀的派生幂等键，
        # 与单条推进的 request_id 空间互不干扰
        derived_request_id = f"batch:{batch_id}:{subject_id}:{period}"
        revealed_at = utc_now().isoformat()

        conn.execute(
            "INSERT INTO unblindings "
            "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                subject_id,
                period,
                condition,
                derived_request_id,
                revealed_at,
            ),
        )
        conn.execute(
            "INSERT INTO subject_progress "
            "(experiment_id, subject_id, next_period, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(experiment_id, subject_id) DO UPDATE SET "
            "next_period = excluded.next_period, updated_at = excluded.updated_at",
            (experiment_id, subject_id, period + 1, revealed_at),
        )
        results.append(
            {
                "experiment_id": experiment_id,
                "subject_id": subject_id,
                "period": period,
                "condition": condition,
                "request_id": derived_request_id,
                "revealed_at": revealed_at,
            }
        )

    _attach_commitment_secrets(conn, experiment_id, results)

    conn.execute(
        "INSERT INTO advance_batches "
        "(experiment_id, batch_id, members, results, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            experiment_id,
            batch_id,
            json.dumps(
                [
                    {"subject_id": m["subject_id"], "expected_period": m["expected_period"]}
                    for m in members
                ],
                ensure_ascii=False,
            ),
            json.dumps(results, ensure_ascii=False),
            gate_now.isoformat(),
        ),
    )

    return {
        "experiment_id": experiment_id,
        "batch_id": batch_id,
        "results": results,
        "_replayed": False,
    }


def complete_period(
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str,
) -> dict[str, Any] | None:
    """原子登记一个受试者当前周期的完成时间。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果；
       同一 request_id 绑定不同 subject_id / period 时抛出
       :class:`RequestIdConflictError`；
    2. 越序校验：只有“已揭盲且尚未完成的当前周期”可登记完成。
       周期尚未揭盲、已经完成、落后 / 超前于当前周期（含实验未开始或
       已结束），均抛出 :class:`CompletionOrderError`（409）；
    3. 登记：服务端当前时间写入 ``period_completions`` 并落盘
       ``completion_requests``，一起提交或一起回滚。

    返回首次 / 重放的完成结果；实验或受试者不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _complete_locked(
                conn, experiment_id, subject_id, period, request_id
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                conn.rollback()
    finally:
        conn.close()


def _complete_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回首次结果
    request_row = conn.execute(
        "SELECT subject_id, period, completed_at FROM completion_requests "
        "WHERE experiment_id = ? AND request_id = ?",
        (experiment_id, request_id),
    ).fetchone()
    if request_row is not None:
        if request_row["subject_id"] != subject_id or request_row["period"] != period:
            raise RequestIdConflictError()
        return {
            "experiment_id": experiment_id,
            "subject_id": subject_id,
            "period": request_row["period"],
            "request_id": request_id,
            "completed_at": request_row["completed_at"],
            "_replayed": True,
        }

    # 2) 实验 / 受试者必须存在；已退役编号（换号墓碑）不能登记完成
    if _get_washout(conn, experiment_id) is None:
        return None
    if _is_tombstoned(conn, experiment_id, subject_id):
        raise SubjectReplacedError()
    assignment_row = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    num_periods = len(json.loads(assignment_row["sequence"]))

    progress_row = conn.execute(
        "SELECT next_period FROM subject_progress "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    next_period = progress_row["next_period"] if progress_row is not None else 1
    current_period = next_period - 1  # 已揭盲但未必已完成的当前周期

    # 3) 只有已揭盲（period == current_period）且尚未完成的周期可登记
    if not (1 <= period <= num_periods) or period != current_period:
        raise CompletionOrderError(
            current_period=max(current_period, 0), already_completed=False
        )
    completion_row = conn.execute(
        "SELECT 1 FROM period_completions "
        "WHERE experiment_id = ? AND subject_id = ? AND period = ?",
        (experiment_id, subject_id, period),
    ).fetchone()
    if completion_row is not None:
        raise CompletionOrderError(
            current_period=current_period, already_completed=True
        )

    # 服务端时间在锁内生成
    completed_at = utc_now().isoformat()
    conn.execute(
        "INSERT INTO period_completions "
        "(experiment_id, subject_id, period, request_id, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (experiment_id, subject_id, period, request_id, completed_at),
    )
    conn.execute(
        "INSERT INTO completion_requests "
        "(experiment_id, request_id, subject_id, period, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (experiment_id, request_id, subject_id, period, completed_at),
    )

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": period,
        "request_id": request_id,
        "completed_at": completed_at,
        "_replayed": False,
    }


def _state_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "operation_id": row["operation_id"],
        "action": row["action"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "state_version": row["state_version"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _event_payload(row: sqlite3.Row, replayed: bool) -> dict[str, Any]:
    """审计行转事件字典，并标记是否为幂等重放（重放只读、不重新提交）。"""
    event = _state_event_dict(row)
    event["_replayed"] = replayed
    return event


def _run_state_change(conn: sqlite3.Connection, fn: Any) -> dict[str, Any] | None:
    """在已开启的 ``BEGIN IMMEDIATE`` 事务内执行状态变更函数并提交。

    ``fn`` 返回事件字典（``_replayed`` 标记重放）；实验不存在时返回 ``None``。
    """
    event = fn(conn)
    if event is None:
        return None
    if not event["_replayed"]:
        conn.execute("COMMIT")
    event.pop("_replayed", None)
    return event


def _transactional_state_change(fn: Any, *args: Any) -> dict[str, Any] | None:
    """以 ``BEGIN IMMEDIATE`` 包裹一次状态变更，统一提交 / 回滚 / 关闭。"""
    conn = get_connection()
    committed = False
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _run_state_change(conn, lambda c: fn(c, *args))
            committed = True
            return result
        finally:
            if not committed:
                # 重放（只读）、409、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()


def pause_experiment(
    experiment_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any] | None:
    """原子暂停实验。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(实验, operation_id)`` 已存在时，仅当首条记录也是
       “暂停且原因相同”才重放首次结果；该 operation_id 属于其他动作 /
       原因时抛出 :class:`OperationIdConflictError`；
    2. 状态校验：实验当前必须为 ``active``，已经是 ``paused`` 时抛出
       :class:`InvalidStateTransitionError`（``already_paused``）；
    3. 变更：``status`` 置为 ``paused``、``state_version`` 加一，
       追加一条 ``experiment_state_events`` 审计记录，一起提交或回滚。

    实验不存在时返回 ``None``。
    """
    return _transactional_state_change(
        _pause_locked, experiment_id, operation_id, reason
    )


def _pause_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any] | None:
    event_row = conn.execute(
        "SELECT action, reason FROM experiment_state_events "
        "WHERE experiment_id = ? AND operation_id = ?",
        (experiment_id, operation_id),
    ).fetchone()
    if event_row is not None:
        if event_row["action"] != "pause" or event_row["reason"] != reason:
            raise OperationIdConflictError()
        row = conn.execute(
            "SELECT * FROM experiment_state_events "
            "WHERE experiment_id = ? AND operation_id = ?",
            (experiment_id, operation_id),
        ).fetchone()
        return _event_payload(row, True)

    row = conn.execute(
        "SELECT status, state_version FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    if row["status"] == "paused":
        raise InvalidStateTransitionError(
            reason="already_paused",
            current_status="paused",
            state_version=int(row["state_version"]),
        )

    created_at = utc_now().isoformat()
    new_version = int(row["state_version"]) + 1
    conn.execute(
        "UPDATE experiments SET status = 'paused', state_version = ? "
        "WHERE id = ?",
        (new_version, experiment_id),
    )
    cursor = conn.execute(
        "INSERT INTO experiment_state_events "
        "(experiment_id, operation_id, action, from_status, to_status, "
        "state_version, reason, created_at) "
        "VALUES (?, ?, 'pause', 'active', 'paused', ?, ?, ?)",
        (experiment_id, operation_id, new_version, reason, created_at),
    )
    inserted = conn.execute(
        "SELECT * FROM experiment_state_events WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _event_payload(inserted, False)


def resume_experiment(
    experiment_id: str,
    expected_version: int,
    operation_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """原子恢复实验（compare-and-set 状态版本）。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：携带 ``operation_id`` 且 ``(实验, operation_id)`` 已存在时，
       仅当首条记录也是“恢复、期望版本与原因都相同”才重放首次结果；
       不一致抛出 :class:`OperationIdConflictError`；
    2. 原子版本校验：实验当前必须为 ``paused`` 且
       ``state_version == expected_version``；版本不符抛出
       :class:`InvalidStateTransitionError`（``state_version_mismatch``，
       回传服务端当前版本与状态）；实验已在运行中则为 ``already_active``；
    3. 变更：``status`` 置为 ``active``、``state_version`` 加一，
       追加审计记录（恢复原因可空），一起提交或回滚。

    实验不存在时返回 ``None``。
    """
    return _transactional_state_change(
        _resume_locked,
        experiment_id,
        expected_version,
        operation_id,
        reason,
    )


def _resume_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    expected_version: int,
    operation_id: str | None,
    reason: str | None,
) -> dict[str, Any] | None:
    if operation_id is not None:
        event_row = conn.execute(
            "SELECT action, state_version, reason FROM experiment_state_events "
            "WHERE experiment_id = ? AND operation_id = ?",
            (experiment_id, operation_id),
        ).fetchone()
        if event_row is not None:
            # 审计记录的 state_version 是恢复后的版本；期望版本应恰好小 1
            if (
                event_row["action"] != "resume"
                or int(event_row["state_version"]) - 1 != expected_version
                or event_row["reason"] != reason
            ):
                raise OperationIdConflictError()
            row = conn.execute(
                "SELECT * FROM experiment_state_events "
                "WHERE experiment_id = ? AND operation_id = ?",
                (experiment_id, operation_id),
            ).fetchone()
            return _event_payload(row, True)

    row = conn.execute(
        "SELECT status, state_version FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    current_status = row["status"]
    current_version = int(row["state_version"])
    if current_version != expected_version:
        raise InvalidStateTransitionError(
            reason="state_version_mismatch",
            current_status=current_status,
            state_version=current_version,
            expected_version=expected_version,
        )
    if current_status == "active":
        raise InvalidStateTransitionError(
            reason="already_active",
            current_status="active",
            state_version=current_version,
            expected_version=expected_version,
        )

    created_at = utc_now().isoformat()
    new_version = current_version + 1
    conn.execute(
        "UPDATE experiments SET status = 'active', state_version = ? "
        "WHERE id = ?",
        (new_version, experiment_id),
    )
    cursor = conn.execute(
        "INSERT INTO experiment_state_events "
        "(experiment_id, operation_id, action, from_status, to_status, "
        "state_version, reason, created_at) "
        "VALUES (?, ?, 'resume', 'paused', 'active', ?, ?, ?)",
        (experiment_id, operation_id, new_version, reason, created_at),
    )
    inserted = conn.execute(
        "SELECT * FROM experiment_state_events WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _event_payload(inserted, False)


def apply_state_transition_batch(
    batch_operation_id: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """在单个写事务内对多个实验批量执行暂停 / 恢复。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 整批幂等检查：``batch_operation_id`` 已存在时，仅当本次操作列表
       （``experiment_id`` / ``action`` / ``expected_version`` /
       ``operation_id`` / ``reason`` 及顺序）与首次完全一致才返回首次结果，
       否则抛出 :class:`BatchOperationIdConflictError`；
    2. 统一校验：对每项操作先做单项幂等检查——``(实验, operation_id)``
       已存在且动作 / 期望版本 / 原因与首条审计一致时，该项直接重放首次
       审计事件（不重复执行），不一致记为 ``operation_id_conflict`` 失败；
       未命中幂等键的项再校验实验存在、
       ``state_version == expected_version``（compare-and-set）、当前状态
       允许目标动作（暂停要求 ``active``，恢复要求 ``paused``）。
       任一项失败即汇总全部失败项并抛出
       :class:`BatchStateTransitionsRejectedError`，**整批回滚、零写入**；
    3. 执行：全部通过后按输入顺序更新 ``experiments``、逐项追加
       ``experiment_state_events`` 审计，最后落盘
       ``state_transition_batches`` 幂等结果，一起提交或一起回滚。

    返回 ``batch_operation_id`` 与按输入顺序排列的审计事件列表。
    """
    return _transactional_state_change(
        _state_transition_batch_locked, batch_operation_id, operations
    )


def _state_transition_batch_locked(
    conn: sqlite3.Connection,
    batch_operation_id: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    # 操作身份：用于整批幂等绑定与持久化（字段固定、顺序敏感）
    request_operations = [
        {
            "experiment_id": op["experiment_id"],
            "action": op["action"],
            "expected_version": op["expected_version"],
            "operation_id": op["operation_id"],
            "reason": op["reason"],
        }
        for op in operations
    ]

    # 1) 整批幂等：操作列表（含顺序）一致才返回首次结果，否则 409
    batch_row = conn.execute(
        "SELECT operations, results FROM state_transition_batches "
        "WHERE batch_operation_id = ?",
        (batch_operation_id,),
    ).fetchone()
    if batch_row is not None:
        if json.loads(batch_row["operations"]) != request_operations:
            raise BatchOperationIdConflictError()
        return {
            "batch_operation_id": batch_operation_id,
            "results": json.loads(batch_row["results"]),
            "_replayed": True,
        }

    # 2) 统一校验：单项幂等 → 实验存在 → 版本 compare-and-set → 状态机校验。
    #    计划列表只收集通过项；任一项失败都先汇总，校验阶段零写入。
    results: list[dict[str, Any] | None] = [None] * len(request_operations)
    plan: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for index, op in enumerate(request_operations):
        experiment_id = op["experiment_id"]
        action = op["action"]
        expected_version = op["expected_version"]
        operation_id = op["operation_id"]
        reason = op["reason"]

        experiment_row = conn.execute(
            "SELECT status, state_version FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        if experiment_row is None:
            failures.append(
                {
                    "experiment_id": experiment_id,
                    "operation_id": operation_id,
                    "reason": "experiment_not_found",
                }
            )
            continue

        # 单项幂等：与单条暂停 / 恢复共享同一 (实验, operation_id) 空间，
        # 命中且参数一致时重放首条审计事件（审计版本恰为期望版本 + 1）
        event_row = conn.execute(
            "SELECT * FROM experiment_state_events "
            "WHERE experiment_id = ? AND operation_id = ?",
            (experiment_id, operation_id),
        ).fetchone()
        if event_row is not None:
            if (
                event_row["action"] != action
                or int(event_row["state_version"]) - 1 != expected_version
                or event_row["reason"] != reason
            ):
                failures.append(
                    {
                        "experiment_id": experiment_id,
                        "operation_id": operation_id,
                        "reason": "operation_id_conflict",
                    }
                )
            else:
                results[index] = _state_event_dict(event_row)
            continue

        current_status = experiment_row["status"]
        current_version = int(experiment_row["state_version"])
        failure: dict[str, Any] | None = None
        if current_version != expected_version:
            failure = {
                "experiment_id": experiment_id,
                "operation_id": operation_id,
                "reason": "state_version_mismatch",
                "current_status": current_status,
                "state_version": current_version,
                "expected_version": expected_version,
            }
        elif action == "pause" and current_status != "active":
            failure = {
                "experiment_id": experiment_id,
                "operation_id": operation_id,
                "reason": "already_paused",
                "current_status": current_status,
                "state_version": current_version,
                "expected_version": expected_version,
            }
        elif action == "resume" and current_status != "paused":
            failure = {
                "experiment_id": experiment_id,
                "operation_id": operation_id,
                "reason": "already_active",
                "current_status": current_status,
                "state_version": current_version,
                "expected_version": expected_version,
            }

        if failure is not None:
            failures.append(failure)
        else:
            plan.append(
                {
                    "index": index,
                    "experiment_id": experiment_id,
                    "action": action,
                    "from_status": current_status,
                    "to_status": "paused" if action == "pause" else "active",
                    "new_version": current_version + 1,
                    "operation_id": operation_id,
                    "reason": reason,
                }
            )

    if failures:
        # 整批零写入：不变更任何实验、不写审计，由外层事务回滚
        raise BatchStateTransitionsRejectedError(failures)

    # 3) 全部通过：按输入顺序执行状态切换并追加审计（时间戳在锁内生成）
    for item in plan:
        created_at = utc_now().isoformat()
        conn.execute(
            "UPDATE experiments SET status = ?, state_version = ? WHERE id = ?",
            (item["to_status"], item["new_version"], item["experiment_id"]),
        )
        cursor = conn.execute(
            "INSERT INTO experiment_state_events "
            "(experiment_id, operation_id, action, from_status, to_status, "
            "state_version, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["experiment_id"],
                item["operation_id"],
                item["action"],
                item["from_status"],
                item["to_status"],
                item["new_version"],
                item["reason"],
                created_at,
            ),
        )
        inserted = conn.execute(
            "SELECT * FROM experiment_state_events WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        results[item["index"]] = _state_event_dict(inserted)

    # 校验全部通过后，每项都有重放或新写入的审计事件（无空洞）
    final_results = [event for event in results if event is not None]
    conn.execute(
        "INSERT INTO state_transition_batches "
        "(batch_operation_id, operations, results, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            batch_operation_id,
            json.dumps(request_operations, ensure_ascii=False),
            json.dumps(final_results, ensure_ascii=False),
            utc_now().isoformat(),
        ),
    )

    return {
        "batch_operation_id": batch_operation_id,
        "results": final_results,
        "_replayed": False,
    }


def emergency_unblind(
    experiment_id: str,
    subject_id: str,
    approver_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any] | None:
    """双人审批的紧急揭盲：每次调用即一次审批，全部在单个写事务内处理。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, operation_id)`` 已存在时，仅当本次
       ``subject_id`` / ``approver_id`` / ``reason`` 与首次完全一致才重放
       首次结果（首开窗口的重放仍返回待审批回执，完成揭盲的重放仍返回
       首次序列）；任一字段不同抛出
       :class:`EmergencyOperationIdConflictError`；
    2. 存在性检查：实验与受试者必须存在，否则返回 ``None``（404）；
    3. 会话状态机（以该受试者最近一次审计事件判定，同一事务快照）：

       * 无会话或上一会话已过期关闭：本次为**首个审批**，开启 15 分钟
         授权窗口，落 ``window_opened`` 审计与幂等结果，返回待审批回执
         （不含任何条件内容）；
       * 上一会话已完成揭盲：抛出 :class:`EmergencyAlreadyUnblindedError`
         （一次性揭盲不可重复）；
       * 窗口仍在有效期内：审批人与首个审批人相同则抛出
         :class:`EmergencySameApproverError`；不同即为**第二审批**，
         同事务落 ``unblinded`` 审计与幂等结果，一次性返回完整条件序列；
       * 窗口已过期：同事务落 ``window_expired`` 审计并提交，随后抛出
         :class:`EmergencyWindowExpiredError`（不消耗 operation_id，
         重新发起即可开启新窗口）。

    并发第二审批由 ``BEGIN IMMEDIATE`` 串行化：只有第一个提交的事务看到
    “窗口仍开启”并完成揭盲，其余看到已完成而 409，序列只产生一次结果。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        result: dict[str, Any] | None = None
        try:
            result = _emergency_unblind_locked(
                conn, experiment_id, subject_id, approver_id, operation_id, reason
            )
            if result is not None and result.pop("_commit", False):
                conn.execute("COMMIT")
                committed = True
        finally:
            if not committed:
                # 幂等重放（只读）、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()
    if result is not None and result.pop("_error", None) == "window_expired":
        # 过期审计已随事务提交；拒绝本次申请（不泄露任何序列）
        raise EmergencyWindowExpiredError()
    return result


def _emergency_unblind_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    approver_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回该操作的首次结果，绝不重复执行
    operation_row = conn.execute(
        "SELECT subject_id, approver_id, reason, result "
        "FROM emergency_unblind_operations "
        "WHERE experiment_id = ? AND operation_id = ?",
        (experiment_id, operation_id),
    ).fetchone()
    if operation_row is not None:
        if (
            operation_row["subject_id"] != subject_id
            or operation_row["approver_id"] != approver_id
            or operation_row["reason"] != reason
        ):
            raise EmergencyOperationIdConflictError()
        replay = json.loads(operation_row["result"])
        replay["_commit"] = False
        return replay

    # 2) 实验与受试者必须存在（404 路径不泄露任何信息）；
    #    已退役编号（换号墓碑）不能发起紧急揭盲
    experiment_row = conn.execute(
        "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment_row is None:
        return None
    if _is_tombstoned(conn, experiment_id, subject_id):
        raise SubjectReplacedError()
    assignment_row = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    # 3) 该受试者最近一次紧急揭盲会话（写锁内读取，期间无其他写入）
    last_event = conn.execute(
        "SELECT action, approver_id, window_expires_at, created_at "
        "FROM emergency_unblind_events "
        "WHERE experiment_id = ? AND subject_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (experiment_id, subject_id),
    ).fetchone()

    # 服务端时间在锁内读取：窗口判定与审计 / 结果写入原子一致
    now = utc_now()
    now_iso = now.isoformat()

    if last_event is not None and last_event["action"] == "unblinded":
        # 一次性揭盲：已完成即永久关闭（重放请使用原 operation_id）
        raise EmergencyAlreadyUnblindedError()

    if last_event is not None and last_event["action"] == "window_opened":
        window_expires_at = datetime.fromisoformat(last_event["window_expires_at"])
        if now <= window_expires_at:
            if approver_id == last_event["approver_id"]:
                # 双人复核：第二审批人必须不同于首个审批人
                raise EmergencySameApproverError()
            # 窗口内第二个不同审批人：一次性返回完整条件序列
            sequence = json.loads(assignment_row["sequence"])
            body = {
                "experiment_id": experiment_id,
                "subject_id": subject_id,
                "operation_id": operation_id,
                "status": "unblinded",
                "first_approver_id": last_event["approver_id"],
                "second_approver_id": approver_id,
                "opened_at": last_event["created_at"],
                "window_expires_at": last_event["window_expires_at"],
                "revealed_at": now_iso,
                "sequence": sequence,
            }
            conn.execute(
                "INSERT INTO emergency_unblind_events "
                "(experiment_id, subject_id, action, approver_id, operation_id, "
                "reason, window_expires_at, created_at) "
                "VALUES (?, ?, 'unblinded', ?, ?, ?, NULL, ?)",
                (
                    experiment_id,
                    subject_id,
                    approver_id,
                    operation_id,
                    reason,
                    now_iso,
                ),
            )
            _insert_emergency_operation(
                conn,
                experiment_id,
                operation_id,
                subject_id,
                approver_id,
                reason,
                body,
                now_iso,
            )
            body["_commit"] = True
            return body
        # 窗口已过期：落过期审计（随事务提交）后拒绝，不泄露任何序列
        conn.execute(
            "INSERT INTO emergency_unblind_events "
            "(experiment_id, subject_id, action, approver_id, operation_id, "
            "reason, window_expires_at, created_at) "
            "VALUES (?, ?, 'window_expired', ?, NULL, ?, NULL, ?)",
            (experiment_id, subject_id, approver_id, reason, now_iso),
        )
        return {"_error": "window_expired", "_commit": True}

    # 4) 无会话或上一会话已过期关闭：首个审批人开启 15 分钟授权窗口
    window_expires_at = (
        now + timedelta(seconds=EMERGENCY_UNBLIND_WINDOW_SECONDS)
    ).isoformat()
    body = {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "operation_id": operation_id,
        "status": "pending_second_approval",
        "first_approver_id": approver_id,
        "opened_at": now_iso,
        "window_expires_at": window_expires_at,
    }
    conn.execute(
        "INSERT INTO emergency_unblind_events "
        "(experiment_id, subject_id, action, approver_id, operation_id, "
        "reason, window_expires_at, created_at) "
        "VALUES (?, ?, 'window_opened', ?, ?, ?, ?, ?)",
        (
            experiment_id,
            subject_id,
            approver_id,
            operation_id,
            reason,
            window_expires_at,
            now_iso,
        ),
    )
    _insert_emergency_operation(
        conn,
        experiment_id,
        operation_id,
        subject_id,
        approver_id,
        reason,
        body,
        now_iso,
    )
    body["_commit"] = True
    return body


def _insert_emergency_operation(
    conn: sqlite3.Connection,
    experiment_id: str,
    operation_id: str,
    subject_id: str,
    approver_id: str,
    reason: str,
    body: dict[str, Any],
    created_at: str,
) -> None:
    """落盘一次紧急揭盲操作的幂等结果（待审批回执 / 首次揭盲序列）。"""
    conn.execute(
        "INSERT INTO emergency_unblind_operations "
        "(experiment_id, operation_id, subject_id, approver_id, reason, "
        "result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            operation_id,
            subject_id,
            approver_id,
            reason,
            json.dumps(body, ensure_ascii=False),
            created_at,
        ),
    )


def _emergency_unblind_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    """审计行转事件字典；审计记录不含任何条件内容。"""
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "subject_id": row["subject_id"],
        "action": row["action"],
        "approver_id": row["approver_id"],
        "operation_id": row["operation_id"],
        "reason": row["reason"],
        "window_expires_at": row["window_expires_at"],
        "created_at": row["created_at"],
    }


def list_emergency_unblind_events(
    experiment_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | None:
    """按时间倒序（最新在前）返回紧急揭盲审计记录。

    审计记录只含审批人、动作与时间等元数据，不含任何条件内容。
    实验不存在时返回 ``None``。
    """
    with closing(get_connection()) as conn:
        experiment_row = conn.execute(
            "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment_row is None:
            return None
        # id 为自增主键，与 created_at 同序；倒序即按时间倒序
        rows = conn.execute(
            "SELECT * FROM emergency_unblind_events "
            "WHERE experiment_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (experiment_id, limit, offset),
        ).fetchall()
    return [_emergency_unblind_event_dict(row) for row in rows]


# 快照周期条目允许输出的字段（白名单候选），输出时按此规范顺序排列
SNAPSHOT_ALLOWED_FIELDS = ("period", "condition", "revealed_at", "completed_at")


def create_result_snapshot(
    experiment_id: str,
    fields: list[str],
    operation_id: str,
) -> dict[str, Any] | None:
    """在单个一致性读事务内固定同一时点，生成不可变的脱敏结果快照。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, operation_id)`` 已存在时，仅当本次字段
       白名单（含顺序）与首次完全一致才返回首次快照；不一致抛出
       :class:`SnapshotOperationIdConflictError`；
    2. 存在性检查：实验不存在时返回 ``None``（404）；
    3. 一致性读：``generated_at`` 在事务内固定一次，名册（按 ``subject_id``
       稳定排序）、已揭盲记录与完成记录均取自同一事务快照，期间无其他写入；
    4. 落盘：快照内容（仅白名单字段、仅已揭盲周期，未完成周期
       ``completed_at`` 为 ``None``）连同其规范化 JSON 的 SHA-256 摘要
       一起写入 ``result_snapshots`` 并提交；快照只增不改。

    全程不读取 ``assignments.sequence`` / ``sequence_index``，未揭盲周期与
    后续安排一律不进入快照。返回首次 / 重放的快照载荷。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _create_snapshot_locked(
                conn, experiment_id, fields, operation_id
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                # 重放（只读）、409、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()


def _create_snapshot_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    fields: list[str],
    operation_id: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：字段白名单（含顺序）一致才返回首次快照，否则 409
    snapshot_row = conn.execute(
        "SELECT * FROM result_snapshots "
        "WHERE experiment_id = ? AND operation_id = ?",
        (experiment_id, operation_id),
    ).fetchone()
    if snapshot_row is not None:
        if json.loads(snapshot_row["fields"]) != list(fields):
            raise SnapshotOperationIdConflictError()
        payload = _snapshot_payload(snapshot_row)
        payload["_replayed"] = True
        return payload

    # 2) 实验必须存在（404 路径不泄露任何信息）
    experiment_row = conn.execute(
        "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment_row is None:
        return None

    # 3) 一致性读：同一事务快照内固定同一时点，期间无其他写入
    generated_at = utc_now().isoformat()
    # 名册只取 subject_id（按 subject_id 稳定排序），不触碰序列与序列下标
    subject_rows = conn.execute(
        "SELECT subject_id FROM assignments "
        "WHERE experiment_id = ? ORDER BY subject_id",
        (experiment_id,),
    ).fetchall()
    reveal_rows = conn.execute(
        "SELECT subject_id, period, condition, revealed_at FROM unblindings "
        "WHERE experiment_id = ? ORDER BY subject_id, period",
        (experiment_id,),
    ).fetchall()
    completion_rows = conn.execute(
        "SELECT subject_id, period, completed_at FROM period_completions "
        "WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchall()

    completed_at_by_key = {
        (row["subject_id"], row["period"]): row["completed_at"]
        for row in completion_rows
    }
    reveals_by_subject: dict[str, list[sqlite3.Row]] = {}
    for row in reveal_rows:
        reveals_by_subject.setdefault(row["subject_id"], []).append(row)

    subjects: list[dict[str, Any]] = []
    for subject_row in subject_rows:
        subject_id = subject_row["subject_id"]
        periods: list[dict[str, Any]] = []
        for reveal in reveals_by_subject.get(subject_id, []):
            # 只输出白名单字段，按规范顺序排列；未完成周期 completed_at 为 None
            entry: dict[str, Any] = {}
            if "period" in fields:
                entry["period"] = reveal["period"]
            if "condition" in fields:
                entry["condition"] = reveal["condition"]
            if "revealed_at" in fields:
                entry["revealed_at"] = reveal["revealed_at"]
            if "completed_at" in fields:
                entry["completed_at"] = completed_at_by_key.get(
                    (subject_id, reveal["period"])
                )
            periods.append(entry)
        subjects.append({"subject_id": subject_id, "periods": periods})

    content = {
        "experiment_id": experiment_id,
        "generated_at": generated_at,
        "subjects": subjects,
    }
    # 摘要：规范化 JSON（键排序、紧凑分隔符、UTF-8）的 SHA-256
    canonical = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    snapshot_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO result_snapshots "
        "(id, experiment_id, operation_id, fields, snapshot, digest, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            experiment_id,
            operation_id,
            json.dumps(list(fields), ensure_ascii=False),
            json.dumps(content, ensure_ascii=False),
            digest,
            generated_at,
        ),
    )

    return {
        "snapshot_id": snapshot_id,
        "experiment_id": experiment_id,
        "operation_id": operation_id,
        "fields": list(fields),
        "digest": digest,
        "created_at": generated_at,
        "snapshot": content,
        "_replayed": False,
    }


def _snapshot_payload(row: sqlite3.Row) -> dict[str, Any]:
    """快照行转响应载荷（内容与摘要均为落盘时的不可变值）。"""
    return {
        "snapshot_id": row["id"],
        "experiment_id": row["experiment_id"],
        "operation_id": row["operation_id"],
        "fields": json.loads(row["fields"]),
        "digest": row["digest"],
        "created_at": row["created_at"],
        "snapshot": json.loads(row["snapshot"]),
    }


def get_result_snapshot(
    experiment_id: str, snapshot_id: str
) -> dict[str, Any] | None:
    """按快照 ID 取回不可变快照内容；实验或快照不存在时返回 ``None``。"""
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM result_snapshots "
            "WHERE experiment_id = ? AND id = ?",
            (experiment_id, snapshot_id),
        ).fetchone()
    if row is None:
        return None
    return _snapshot_payload(row)


def replace_subject(
    experiment_id: str,
    subject_id: str,
    replacement_subject_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any] | None:
    """受试者换号：把隐藏分配与进度原子转移到新编号，原编号留墓碑。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, operation_id)`` 已存在时，仅当本次
       原编号（路径）、新编号与原因均与首次一致才重放首次结果；
       任一不同抛出 :class:`ReplacementOperationIdConflictError`；
    2. 存在性检查：实验不存在返回 ``None``（404）；原编号已退役
       （换号墓碑）抛出 :class:`SubjectReplacedError`；原编号不在册
       返回 ``None``（404）；
    3. 新编号检查：新编号已在册或已退役（不可复用）抛出
       :class:`ReplacementSubjectExistsError`；
    4. 揭盲检查：原编号必须从未发生过任何揭盲——无 ``unblindings``
       记录且无 ``unblinded`` 紧急揭盲审计，否则抛出
       :class:`SubjectAlreadyUnblindedError`；
    5. 转移：``assignments`` 的隐藏分配（序列内容与 ``sequence_index``
       保持不变）与 ``subject_progress`` 进度原子改挂到新编号，
       同事务写入原编号墓碑与换号审计记录，一起提交或一起回滚。

    换号不触碰任何条件内容，审计记录只含编号 / 原因 / 时间等元数据。
    所有失败路径（409）零写入、不消耗 ``operation_id``。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _replace_subject_locked(
                conn,
                experiment_id,
                subject_id,
                replacement_subject_id,
                operation_id,
                reason,
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                # 重放（只读）、409、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()


def _replace_subject_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    replacement_subject_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：原 / 新编号与原因均一致才返回首次结果，否则 409
    operation_row = conn.execute(
        "SELECT * FROM subject_replacements "
        "WHERE experiment_id = ? AND operation_id = ?",
        (experiment_id, operation_id),
    ).fetchone()
    if operation_row is not None:
        if (
            operation_row["original_subject_id"] != subject_id
            or operation_row["replacement_subject_id"] != replacement_subject_id
            or operation_row["reason"] != reason
        ):
            raise ReplacementOperationIdConflictError()
        return _subject_replacement_dict(operation_row, replayed=True)

    # 2) 实验必须存在（404 路径不泄露任何信息）
    experiment_row = conn.execute(
        "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment_row is None:
        return None

    # 3) 原编号已退役（墓碑）→ 409；不在册 → 404
    if _is_tombstoned(conn, experiment_id, subject_id):
        raise SubjectReplacedError()
    assignment_row = conn.execute(
        "SELECT 1 FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    # 4) 新编号必须未被占用：在册编号与已退役墓碑均不可复用
    replacement_assignment = conn.execute(
        "SELECT 1 FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, replacement_subject_id),
    ).fetchone()
    if replacement_assignment is not None or _is_tombstoned(
        conn, experiment_id, replacement_subject_id
    ):
        raise ReplacementSubjectExistsError()

    # 5) 原编号必须从未揭盲：无按周期揭盲记录，也无完成的紧急揭盲
    unblinding_row = conn.execute(
        "SELECT 1 FROM unblindings "
        "WHERE experiment_id = ? AND subject_id = ? LIMIT 1",
        (experiment_id, subject_id),
    ).fetchone()
    if unblinding_row is not None:
        raise SubjectAlreadyUnblindedError()
    emergency_row = conn.execute(
        "SELECT 1 FROM emergency_unblind_events "
        "WHERE experiment_id = ? AND subject_id = ? AND action = 'unblinded' "
        "LIMIT 1",
        (experiment_id, subject_id),
    ).fetchone()
    if emergency_row is not None:
        raise SubjectAlreadyUnblindedError()

    # 6) 原子转移：隐藏分配（序列与下标不变）与进度改挂到新编号
    conn.execute(
        "UPDATE assignments SET subject_id = ? "
        "WHERE experiment_id = ? AND subject_id = ?",
        (replacement_subject_id, experiment_id, subject_id),
    )
    conn.execute(
        "UPDATE subject_progress SET subject_id = ? "
        "WHERE experiment_id = ? AND subject_id = ?",
        (replacement_subject_id, experiment_id, subject_id),
    )
    # 换号只迁移槽位归属；commitment / nonce 均保持创建时的值
    conn.execute(
        "UPDATE allocation_commitments SET subject_id = ? "
        "WHERE experiment_id = ? AND subject_id = ?",
        (replacement_subject_id, experiment_id, subject_id),
    )

    # 7) 同事务落墓碑与审计（同一张表）：原编号自此退役
    created_at = utc_now().isoformat()
    cursor = conn.execute(
        "INSERT INTO subject_replacements "
        "(experiment_id, original_subject_id, replacement_subject_id, "
        "operation_id, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            subject_id,
            replacement_subject_id,
            operation_id,
            reason,
            created_at,
        ),
    )
    inserted = conn.execute(
        "SELECT * FROM subject_replacements WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _subject_replacement_dict(inserted, replayed=False)


def _subject_replacement_dict(
    row: sqlite3.Row, replayed: bool = False
) -> dict[str, Any]:
    """换号审计行转字典；只含编号 / 原因 / 时间等元数据，不含条件内容。"""
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "original_subject_id": row["original_subject_id"],
        "replacement_subject_id": row["replacement_subject_id"],
        "operation_id": row["operation_id"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "_replayed": replayed,
    }


def list_subject_replacements(
    experiment_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | None:
    """按时间倒序（最新在前）返回换号审计记录。

    审计记录只含原 / 新编号、原因与时间等元数据，不含任何条件内容。
    实验不存在时返回 ``None``。
    """
    with closing(get_connection()) as conn:
        experiment_row = conn.execute(
            "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment_row is None:
            return None
        # id 为自增主键，与 created_at 同序；倒序即按时间倒序
        rows = conn.execute(
            "SELECT * FROM subject_replacements "
            "WHERE experiment_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (experiment_id, limit, offset),
        ).fetchall()
    events = []
    for row in rows:
        event = _subject_replacement_dict(row)
        event.pop("_replayed", None)
        events.append(event)
    return events


def list_state_events(
    experiment_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | None:
    """按时间倒序（最新在前）返回实验状态变更审计记录。

    实验不存在时返回 ``None``。
    """
    with closing(get_connection()) as conn:
        experiment_row = conn.execute(
            "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment_row is None:
            return None
        # id 为自增主键，与 created_at 同序；倒序即按时间倒序
        rows = conn.execute(
            "SELECT * FROM experiment_state_events "
            "WHERE experiment_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (experiment_id, limit, offset),
        ).fetchall()
    return [_state_event_dict(row) for row in rows]


def list_commitments(experiment_id: str) -> list[dict[str, Any]] | None:
    """返回当前槽位归属的盲态承诺；绝不返回 condition 或 nonce。

    实验不存在返回 ``None``；实验存在但没有任何创建时承诺（旧实验）抛出
    :class:`CommitmentsUnavailableError`，避免把迁移后补造的值伪装成创建时承诺。
    换号只改变 ``subject_id`` 归属，因此这里自然返回新编号与原承诺。
    """
    with closing(get_connection()) as conn:
        experiment_row = conn.execute(
            "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment_row is None:
            return None
        rows = conn.execute(
            "SELECT subject_id, allocation_slot_id, period, commitment "
            "FROM allocation_commitments "
            "WHERE experiment_id = ? ORDER BY subject_id, period",
            (experiment_id,),
        ).fetchall()
        if not rows:
            raise CommitmentsUnavailableError()
    return [
        {
            "subject_id": row["subject_id"],
            "allocation_slot_id": row["allocation_slot_id"],
            "period": row["period"],
            "commitment": row["commitment"],
        }
        for row in rows
    ]


def get_progress(experiment_id: str, subject_id: str) -> dict[str, Any] | None:
    """查询受试者揭盲进度。

    只返回已揭盲记录（含各自的完成时间）、下一周期以及下一周期最早可
    推进时间 ``next_available_at``，不包含完整序列或后续安排。
    实验或受试者不存在时返回 ``None``；已退役编号（换号墓碑）抛出
    :class:`SubjectReplacedError`（不能查询进度）。
    """
    with closing(get_connection()) as conn:
        experiment_row = conn.execute(
            "SELECT washout_seconds FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        if experiment_row is None:
            return None
        if _is_tombstoned(conn, experiment_id, subject_id):
            raise SubjectReplacedError()
        assignment_row = conn.execute(
            "SELECT sequence FROM assignments "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()
        if assignment_row is None:
            return None
        num_periods = len(json.loads(assignment_row["sequence"]))
        washout = int(experiment_row["washout_seconds"])

        reveal_rows = conn.execute(
            "SELECT period, condition, revealed_at FROM unblindings "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()
        completion_rows = conn.execute(
            "SELECT period, completed_at FROM period_completions "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()
        progress_row = conn.execute(
            "SELECT next_period FROM subject_progress "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()

    next_period = progress_row["next_period"] if progress_row is not None else 1
    completed_at_by_period = {
        row["period"]: row["completed_at"] for row in completion_rows
    }

    # next_available_at 仅由“上一周期完成记录 + 洗脱期”推导；
    # 周期 1、实验已结束或上一周期尚未完成时为 None（不泄露任何安排）。
    next_available_at: str | None = None
    if 1 < next_period <= num_periods:
        prior_completed_at = completed_at_by_period.get(next_period - 1)
        if prior_completed_at is not None:
            next_available_at = (
                datetime.fromisoformat(prior_completed_at)
                + timedelta(seconds=washout)
            ).isoformat()

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "next_period": next_period,
        "num_periods": num_periods,
        "washout_seconds": washout,
        "completed": next_period > num_periods,
        "next_available_at": next_available_at,
        "reveals": [
            {
                "period": row["period"],
                "condition": row["condition"],
                "revealed_at": row["revealed_at"],
                "completed_at": completed_at_by_period.get(row["period"]),
            }
            for row in reveal_rows
        ],
    }


def get_experiment(experiment_id: str) -> dict[str, Any] | None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        assignment_rows = conn.execute(
            "SELECT subject_id FROM assignments "
            "WHERE experiment_id = ? ORDER BY rowid",
            (experiment_id,),
        ).fetchall()
    return _experiment_dict(row, assignment_rows)


def list_experiments() -> list[dict[str, Any]]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM experiments ORDER BY created_at, id"
        ).fetchall()
    return [_experiment_dict(row, None) for row in rows]


def _experiment_dict(
    row: sqlite3.Row, assignment_rows: list[sqlite3.Row] | None
) -> dict[str, Any]:
    conditions = json.loads(row["conditions"])
    experiment: dict[str, Any] = {
        "experiment_id": row["id"],
        "conditions": conditions,
        "seed": row["seed"],
        "group_size": row["group_size"],
        "num_subjects": row["num_subjects"],
        "num_periods": len(conditions),
        "washout_seconds": row["washout_seconds"],
        "status": row["status"],
        "state_version": row["state_version"],
        "created_at": row["created_at"],
    }
    if assignment_rows is not None:
        # 只回传受试者名册，完整条件序列保留在服务端，按周期受控揭盲
        experiment["assignments"] = [
            {"subject_id": a["subject_id"]} for a in assignment_rows
        ]
    return experiment
