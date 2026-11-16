"""复测更正：按样本查看关系，一个表单最多 20 项操作，整单事务提交。

操作类型：
- add_edge   新增复测边 (orig -> retest，可带原因)
- del_edge   删除复测边
- move_order 修改检测单的样本归属

关键保证：
- 所有操作在单个事务里执行（默认 READ COMMITTED）；规则由数据库提交时
  约束触发器把关，应用层不重复实现规则，避免规则双份漂移。
- 提交前按 id 定序预锁涉及的检测单（FOR NO KEY UPDATE，与加锁触发器同序
  同强度），把并发冲突提前到锁等待阶段，并避免与外键 KEY SHARE 自死锁。
- 任何一步失败：显式 ROLLBACK，整单回滚，调用方把违规原因与涉及检测单
  呈现到网页；成功才 COMMIT。
- 不自动改写 latest_retest_* 等既有数据，只执行用户明确提交的操作。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

MAX_OPERATIONS = 20


class CorrectionError(Exception):
    """表单本身不合法（未进入数据库事务）。"""


class CorrectionConflict(Exception):
    """与其他并发事务冲突（锁等待超时/死锁/序列化失败），可重试。"""


@dataclass(frozen=True)
class Operation:
    kind: str  # add_edge | del_edge | move_order
    orig_id: int | None = None
    retest_id: int | None = None
    reason: str | None = None
    order_id: int | None = None
    sample_id: int | None = None


def parse_operations(form: dict[str, Any]) -> list[Operation]:
    """从表单解析操作。表单字段名形如 kind-N/orig-N/...，N 可不连续
    （用户在网页上删除中间行后会留下空号），按实际出现的序号升序处理。"""
    kinds = {
        int(k.split("-", 1)[1]): v
        for k, v in form.items()
        if k.startswith("kind-") and v
    }
    if not kinds:
        raise CorrectionError("未选择任何更正操作。")
    if len(kinds) > MAX_OPERATIONS:
        raise CorrectionError(f"一单最多 {MAX_OPERATIONS} 项操作，当前 {len(kinds)} 项。")

    def val(idx: int, name: str, default: Any = None) -> Any:
        return form.get(f"{name}-{idx}", default)

    def as_int(idx: int, name: str, label: str) -> int:
        raw = val(idx, name)
        if raw in (None, ""):
            raise CorrectionError(f"第 {idx + 1} 项操作缺少{label}。")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise CorrectionError(f"第 {idx + 1} 项操作的{label}必须是整数，收到 {raw!r}。") from None

    ops: list[Operation] = []
    for idx in sorted(kinds):
        kind = kinds[idx]
        if kind == "add_edge":
            orig_id = as_int(idx, "orig", "原检测单 ID")
            retest_id = as_int(idx, "retest", "复测检测单 ID")
            reason = val(idx, "reason") or None
            ops.append(Operation("add_edge", orig_id=orig_id,
                                 retest_id=retest_id, reason=reason))
        elif kind == "del_edge":
            orig_id = as_int(idx, "orig", "原检测单 ID")
            retest_id = as_int(idx, "retest", "复测检测单 ID")
            ops.append(Operation("del_edge", orig_id=orig_id, retest_id=retest_id))
        elif kind == "move_order":
            order_id = as_int(idx, "order", "检测单 ID")
            sample_id = as_int(idx, "sample", "目标样本 ID")
            ops.append(Operation("move_order", order_id=order_id, sample_id=sample_id))
        else:
            raise CorrectionError(f"第 {idx + 1} 项操作类型无法识别：{kind!r}。")
    return ops


def _involved_order_ids(ops: list[Operation]) -> list[int]:
    ids: set[int] = set()
    for op in ops:
        if op.kind in ("add_edge", "del_edge"):
            ids.add(op.orig_id)  # type: ignore[arg-type]
            ids.add(op.retest_id)  # type: ignore[arg-type]
        else:
            ids.add(op.order_id)  # type: ignore[arg-type]
    return sorted(ids)


def _validate_references(conn: psycopg.Connection, ops: list[Operation]) -> None:
    """事务内预校验引用完整性，给出比外键报错更友好的中文提示。"""
    with conn.cursor() as cur:
        order_ids = _involved_order_ids(ops)
        cur.execute(
            "SELECT id FROM test_orders WHERE id = ANY(%s)", (order_ids,)
        )
        existing = {r[0] for r in cur.fetchall()}
        missing = sorted(set(order_ids) - existing)
        if missing:
            raise CorrectionError(f"检测单不存在：{', '.join(map(str, missing))}。")

        sample_ids = sorted({op.sample_id for op in ops if op.kind == "move_order"})
        if sample_ids:
            cur.execute("SELECT id FROM samples WHERE id = ANY(%s)", (sample_ids,))
            existing_s = {r[0] for r in cur.fetchall()}
            missing_s = sorted(set(sample_ids) - existing_s)
            if missing_s:
                raise CorrectionError(f"目标样本不存在：{', '.join(map(str, missing_s))}。")

        # 删除目标不存在的边提前报业务错误，而不是静默成功。
        for op in ops:
            if op.kind != "del_edge":
                continue
            cur.execute(
                "SELECT 1 FROM retest_links WHERE orig_order_id = %s "
                "AND retest_order_id = %s",
                (op.orig_id, op.retest_id),
            )
            if cur.fetchone() is None:
                raise CorrectionError(
                    f"要删除的复测边不存在：检测单 {op.orig_id} -> {op.retest_id}。"
                )


def apply_corrections(conn: psycopg.Connection, ops: list[Operation]) -> dict[str, int]:
    """在单个事务内应用全部更正；失败抛异常并保证整单回滚。"""
    if not ops:
        raise CorrectionError("未选择任何更正操作。")
    if len(ops) > MAX_OPERATIONS:
        raise CorrectionError(f"一单最多 {MAX_OPERATIONS} 项操作，当前 {len(ops)} 项。")

    # 显式开启事务（psycopg 连接默认即 READ COMMITTED，这里不提升隔离级别，
    # 规则本身依赖 READ COMMITTED 提交时刻快照 + 行锁序列化）。
    try:
        conn.rollback()
        # 服务端锁等待上限：并发冲突时等待超过 5 秒即由 PostgreSQL 中止当前语句，
        # 随后被映射为 CorrectionConflict（整单回滚、提示刷新重试）。
        conn.execute("SET SESSION lock_timeout = '5s'")
        _validate_references(conn, ops)

        # 预锁：按 id 全局定序，与 retest_links_lock_trg 的加锁顺序/强度一致。
        # 用 FOR NO KEY UPDATE：与外键的 FOR KEY SHARE 兼容，避免反向边并发自死锁。
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM test_orders WHERE id = ANY(%s) "
                "ORDER BY id FOR NO KEY UPDATE",
                (_involved_order_ids(ops),),
            )

        for op in ops:
            with conn.cursor() as cur:
                if op.kind == "add_edge":
                    # ON CONFLICT 处理重复边：重复提交同一条边视为幂等成功，
                    # 但仍要让规则触发器有机会校验（冲突跳过则沿用已存在边的合法性）。
                    cur.execute(
                        "INSERT INTO retest_links (orig_order_id, retest_order_id, reason) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (op.orig_id, op.retest_id, op.reason),
                    )
                elif op.kind == "del_edge":
                    cur.execute(
                        "DELETE FROM retest_links WHERE orig_order_id = %s "
                        "AND retest_order_id = %s",
                        (op.orig_id, op.retest_id),
                    )
                else:  # move_order
                    cur.execute(
                        "UPDATE test_orders SET sample_id = %s WHERE id = %s",
                        (op.sample_id, op.order_id),
                    )

        # 提交时刻：DEFERRABLE INITIALLY DEFERRED 约束触发器在此执行全量校验。
        conn.commit()
    except CorrectionError:
        conn.rollback()
        raise
    except psycopg.errors.Error as exc:
        conn.rollback()
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate in (psycopg.errors.LockNotAvailable.sqlstate,
                        psycopg.errors.DeadlockDetected.sqlstate,
                        psycopg.errors.SerializationFailure.sqlstate):
            raise CorrectionConflict(
                "检测单正被其他并发更正占用（锁冲突/死锁），本次更正已整单回滚，"
                "请刷新页面确认最新关系后重试。"
            ) from exc
        # 触发器 RAISE EXCEPTION 的 check_violation (23514) 及其他数据库错误
        # 原样上抛，由路由层提取 message/detail 展示涉及检测单。
        raise
    return {"operations": len(ops),
            "orders": len(_involved_order_ids(ops))}
