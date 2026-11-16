"""从源库的一致性快照中，抽取指定检测单及其外键可达的全部被引用行。

设计要点：
- 调用方须保证所有查询运行在同一个 REPEATABLE READ 只读事务里，
  因此目录元数据与数据行都来自同一个已提交视图；MVCC 快照不阻塞源端写入。
- 只沿“本表 -> 被引用表”方向递归，不抓取反向引用（子表）中的无关行。
- 复合外键任一列为 NULL 时整体跳过（与 SQL 外键 MATCH SIMPLE 语义一致）。
- 同一行被多次引用时只保留一份（按 (表, 主键值) 去重）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from psycopg import sql
from psycopg.rows import dict_row

SCHEMA = "public"
ROOT_TABLE = "test_orders"


class ExtractError(Exception):
    """抽取失败。调用方应拒绝提供任何部分结果。"""


class RootNotFound(ExtractError):
    """所选检测单不存在。"""


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool


@dataclass
class ForeignKeyMeta:
    name: str
    table: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    update_rule: str
    delete_rule: str
    deferrable: bool
    initially_deferred: bool


@dataclass
class TableMeta:
    name: str
    columns: list[ColumnMeta]
    pk_constraint: str | None
    pk_columns: list[str]
    fks: list[ForeignKeyMeta] = field(default_factory=list)


@dataclass
class ExtractResult:
    tables: dict[str, TableMeta]
    rows: dict[str, dict[tuple, dict]]  # 表名 -> 主键值元组 -> 行
    root_table: str
    root_pk: tuple


_TABLES_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE'
ORDER BY table_name
"""

_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = %(schema)s
ORDER BY table_name, ordinal_position
"""

_PK_SQL = """
SELECT tc.table_name, tc.constraint_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_catalog = tc.constraint_catalog
 AND kcu.constraint_schema  = tc.constraint_schema
 AND kcu.constraint_name    = tc.constraint_name
WHERE tc.constraint_schema = %(schema)s
  AND tc.constraint_type   = 'PRIMARY KEY'
ORDER BY tc.table_name, kcu.ordinal_position
"""

# 复合外键的列配对：借助 key_column_usage.position_in_unique_constraint
# 把外键列与被引用侧唯一约束的列一一对应。
_FK_SQL = """
SELECT kcu1.table_name,
       rc.constraint_name,
       kcu1.column_name,
       kcu2.table_name  AS ref_table,
       kcu2.column_name AS ref_column,
       rc.update_rule,
       rc.delete_rule
FROM information_schema.referential_constraints rc
JOIN information_schema.key_column_usage kcu1
  ON kcu1.constraint_catalog = rc.constraint_catalog
 AND kcu1.constraint_schema  = rc.constraint_schema
 AND kcu1.constraint_name    = rc.constraint_name
JOIN information_schema.key_column_usage kcu2
  ON kcu2.constraint_catalog = rc.unique_constraint_catalog
 AND kcu2.constraint_schema  = rc.unique_constraint_schema
 AND kcu2.constraint_name    = rc.unique_constraint_name
 AND kcu2.ordinal_position   = kcu1.position_in_unique_constraint
WHERE rc.constraint_schema = %(schema)s
ORDER BY kcu1.table_name, rc.constraint_name, kcu1.ordinal_position
"""

# 可延迟属性不在 information_schema 里，从 pg_constraint 取（同名约束可能跨表存在，需带表名）。
_FK_ATTR_SQL = """
SELECT cls.relname AS table_name, con.conname, con.condeferrable, con.condeferred
FROM pg_constraint con
JOIN pg_class cls     ON cls.oid = con.conrelid
JOIN pg_namespace ns  ON ns.oid  = con.connamespace
WHERE ns.nspname = %(schema)s AND con.contype = 'f'
"""


def _load_metadata(cur) -> dict[str, TableMeta]:
    tables: dict[str, TableMeta] = {}
    for r in cur.execute(_TABLES_SQL, {"schema": SCHEMA}):
        tables[r["table_name"]] = TableMeta(
            name=r["table_name"], columns=[], pk_constraint=None, pk_columns=[]
        )
    if not tables:
        raise ExtractError(f"schema {SCHEMA!r} 中没有用户表")

    for r in cur.execute(_COLUMNS_SQL, {"schema": SCHEMA}):
        t = tables.get(r["table_name"])
        if t is not None:
            t.columns.append(
                ColumnMeta(r["column_name"], r["data_type"], r["is_nullable"] == "YES")
            )

    for r in cur.execute(_PK_SQL, {"schema": SCHEMA}):
        t = tables.get(r["table_name"])
        if t is not None:
            t.pk_constraint = r["constraint_name"]
            t.pk_columns.append(r["column_name"])

    fk_attrs = {
        (r["table_name"], r["conname"]): (r["condeferrable"], r["condeferred"])
        for r in cur.execute(_FK_ATTR_SQL, {"schema": SCHEMA})
    }
    fks: dict[tuple[str, str], ForeignKeyMeta] = {}
    for r in cur.execute(_FK_SQL, {"schema": SCHEMA}):
        key = (r["table_name"], r["constraint_name"])
        fk = fks.get(key)
        if fk is None:
            deferrable, initially = fk_attrs.get(key, (False, False))
            fk = ForeignKeyMeta(
                name=r["constraint_name"],
                table=r["table_name"],
                columns=[],
                ref_table=r["ref_table"],
                ref_columns=[],
                update_rule=r["update_rule"],
                delete_rule=r["delete_rule"],
                deferrable=deferrable,
                initially_deferred=initially,
            )
            fks[key] = fk
        fk.columns.append(r["column_name"])
        fk.ref_columns.append(r["ref_column"])

    for fk in fks.values():
        t = tables.get(fk.table)
        if t is not None and fk.ref_table in tables:
            t.fks.append(fk)

    for t in tables.values():
        if not t.pk_columns:
            raise ExtractError(f"表 {t.name!r} 缺少主键，无法按主键去重抽取")
    return tables


def _fetch_row(cur, tmeta: TableMeta, pk: tuple) -> dict | None:
    where = sql.SQL(" AND ").join(
        sql.SQL("{} = %s").format(sql.Identifier(c)) for c in tmeta.pk_columns
    )
    query = sql.SQL("SELECT * FROM {} WHERE {}").format(
        sql.Identifier(tmeta.name), where
    )
    return cur.execute(query, pk).fetchone()


def extract_order(conn, order_id: int) -> ExtractResult:
    """在 conn 当前事务内，抽取检测单 order_id 及外键可达的全部被引用行。"""
    with conn.cursor(row_factory=dict_row) as cur:
        tables = _load_metadata(cur)
        if ROOT_TABLE not in tables:
            raise ExtractError(f"找不到根表 {ROOT_TABLE!r}")
        if len(tables[ROOT_TABLE].pk_columns) != 1:
            raise ExtractError(f"根表 {ROOT_TABLE!r} 的主键应为单列")

        rows: dict[str, dict[tuple, dict]] = {name: {} for name in tables}
        stack = [(ROOT_TABLE, (order_id,))]
        while stack:
            table, pk = stack.pop()
            if pk in rows[table]:
                continue  # 重复引用只保留一份
            row = _fetch_row(cur, tables[table], pk)
            if row is None:
                if table == ROOT_TABLE:
                    raise RootNotFound(f"检测单 id={order_id} 不存在")
                raise ExtractError(
                    f"外键引用了不存在的行 {table}{pk}，源库约束可能已损坏"
                )
            rows[table][pk] = row
            for fk in tables[table].fks:
                values = tuple(row[c] for c in fk.columns)
                if any(v is None for v in values):
                    continue  # 外键任一列为空：不追查
                stack.append((fk.ref_table, values))

    return ExtractResult(
        tables=tables, rows=rows, root_table=ROOT_TABLE, root_pk=(order_id,)
    )
