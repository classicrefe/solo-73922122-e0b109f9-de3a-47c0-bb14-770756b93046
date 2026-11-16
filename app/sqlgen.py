"""把抽取结果生成为可在空库执行的 SQL 脚本。

脚本结构（与 pg_dump 处理循环引用的做法一致）：
  BEGIN -> DROP IF EXISTS -> CREATE TABLE（仅主键）-> INSERT -> ALTER TABLE 补外键 -> COMMIT
外键在数据载入后统一添加并即时校验，因此循环引用可以完整还原，
全程不需要关闭约束（不用 session_replication_role / DISABLE TRIGGER）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from extractor import ExtractError, ExtractResult

# 需求约定源库列类型只有整数与文本
_TYPE_MAP = {"integer": "INTEGER", "text": "TEXT"}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # bool 是 int 子类，先拦住
        raise ExtractError("不支持的列值类型: bool")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise ExtractError(f"不支持的列值类型: {type(value).__name__}")


def _table_order(result: ExtractResult) -> list[str]:
    """被引用表尽量排在前面（可读性）；循环引用按名称打破僵局。

    由于外键在数据载入后才添加，该顺序不影响正确性。
    """
    remaining = {
        t: {fk.ref_table for fk in m.fks if fk.ref_table != t}
        for t, m in result.tables.items()
    }
    order: list[str] = []
    while remaining:
        ready = sorted(t for t, deps in remaining.items() if deps <= set(order))
        if not ready:  # 只剩循环依赖
            ready = [sorted(remaining)[0]]
        for t in ready:
            order.append(t)
            del remaining[t]
    return order


def _column_ddl(table: str, col) -> str:
    try:
        ctype = _TYPE_MAP[col.data_type]
    except KeyError:
        raise ExtractError(
            f"表 {table}.{col.name} 的列类型 {col.data_type!r} 暂不支持（仅 integer/text）"
        ) from None
    null = "" if col.nullable else " NOT NULL"
    return f"    {quote_ident(col.name)} {ctype}{null}"


def _fk_clause(fk) -> str:
    clause = (
        f"ALTER TABLE {quote_ident(fk.table)} ADD CONSTRAINT {quote_ident(fk.name)} "
        f"FOREIGN KEY ({', '.join(quote_ident(c) for c in fk.columns)}) "
        f"REFERENCES {quote_ident(fk.ref_table)} "
        f"({', '.join(quote_ident(c) for c in fk.ref_columns)})"
    )
    if fk.update_rule != "NO ACTION":
        clause += f" ON UPDATE {fk.update_rule}"
    if fk.delete_rule != "NO ACTION":
        clause += f" ON DELETE {fk.delete_rule}"
    if fk.deferrable:
        clause += " DEFERRABLE"
        if fk.initially_deferred:
            clause += " INITIALLY DEFERRED"
    return clause + ";"


def build_sql(result: ExtractResult) -> str:
    order = _table_order(result)
    out: list[str] = []
    out.append("-- 检测单关联数据抽取（可在空库执行）")
    out.append(f"-- 根记录: {result.root_table} 主键 = {result.root_pk[0]}")
    out.append(f"-- 生成时间(UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    out.append("-- 说明: 先建表(仅主键)再载入数据，最后统一补外键并校验；")
    out.append("--       循环引用无需关闭约束即可完整还原。")
    out.append("")
    out.append("BEGIN;")
    out.append("")
    for t in reversed(order):
        out.append(f"DROP TABLE IF EXISTS {quote_ident(t)} CASCADE;")
    out.append("")

    for t in order:
        m = result.tables[t]
        cols = [_column_ddl(t, c) for c in m.columns]
        cols.append(
            f"    CONSTRAINT {quote_ident(m.pk_constraint)} "
            f"PRIMARY KEY ({', '.join(quote_ident(c) for c in m.pk_columns)})"
        )
        out.append(f"CREATE TABLE {quote_ident(t)} (\n" + ",\n".join(cols) + "\n);")
        out.append("")

    for t in order:
        m = result.tables[t]
        rows = result.rows[t]
        if not rows:
            out.append(f"-- 表 {quote_ident(t)} 无关联行")
            out.append("")
            continue
        colnames = ", ".join(quote_ident(c.name) for c in m.columns)
        values = [
            "(" + ", ".join(literal(row[c.name]) for c in m.columns) + ")"
            for _, row in sorted(rows.items())
        ]
        out.append(f"INSERT INTO {quote_ident(t)} ({colnames}) VALUES")
        out.append(",\n".join(values) + ";")
        out.append("")

    for t in order:
        for fk in sorted(result.tables[t].fks, key=lambda f: f.name):
            out.append(_fk_clause(fk))
    out.append("")
    out.append("COMMIT;")
    out.append("")
    return "\n".join(out)
