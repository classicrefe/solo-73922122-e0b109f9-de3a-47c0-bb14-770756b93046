#!/usr/bin/env python3
"""端到端自测：网页、抽取 SQL 内容、空库还原、快照一致性、失败路径。

用法（环境变量均有默认值）：
    APP_URL       应用地址，默认 http://127.0.0.1:8000
    BAD_APP_URL   指向坏数据库的应用地址（可选，用于验证失败路径）
    DATABASE_URL  源库连接串，默认 postgresql://lab:lab@127.0.0.1:5432/labdb
    ADMIN_URL     管理库连接串，默认 postgresql://lab:lab@127.0.0.1:5432/postgres
    RESTORE_DB    还原测试库名，默认 rtdb（不存在则创建；各用例顺序覆盖执行，
                  依赖生成脚本自带的 DROP TABLE IF EXISTS ... CASCADE）
    PSQL          psql 可执行文件路径（可选；不提供则改用 psycopg 执行脚本）
    WORKDIR       下载文件存放目录，默认 /workspace/tmp_extract
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import psycopg

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
BAD_APP_URL = os.environ.get("BAD_APP_URL", "").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lab:lab@127.0.0.1:5432/labdb")
ADMIN_URL = os.environ.get("ADMIN_URL", "postgresql://lab:lab@127.0.0.1:5432/postgres")
RESTORE_DB = os.environ.get("RESTORE_DB", "rtdb")
PSQL = os.environ.get("PSQL", "")
WORKDIR = os.environ.get("WORKDIR", "/workspace/tmp_extract")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def http_get(url: str):
    """返回 (status, headers, body bytes)；headers 键统一小写；HTTP 错误状态也正常返回。"""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def download(order_id: int) -> tuple[int, dict, bytes]:
    return http_get(f"{APP_URL}/export/{order_id}")


def save_sql(order_id: int, body: bytes) -> str:
    os.makedirs(WORKDIR, exist_ok=True)
    path = os.path.join(WORKDIR, f"order_{order_id}.sql")
    with open(path, "wb") as f:
        f.write(body)
    return path


def db_url(dbname: str) -> str:
    """把 ADMIN_URL 路径部分的数据库名替换为 dbname，保留查询参数。"""
    return re.sub(r"^((?:postgresql|postgres)://[^/]*)/[^/?]*",
                  lambda m: m.group(1) + "/" + dbname, ADMIN_URL)


def ensure_restore_db():
    """还原测试库只建一次；不在测试里 DROP DATABASE（部分环境无权限）。"""
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{RESTORE_DB}"')
    except psycopg.errors.DuplicateDatabase:
        pass


def apply_sql(dbname: str, path: str):
    """用真实客户端把脚本应用到库中；任何错误都视为失败。"""
    if PSQL:
        env = dict(os.environ, PGCONNECT_TIMEOUT="10")
        r = subprocess.run(
            [PSQL, "-v", "ON_ERROR_STOP=1", "-X", "-d", dbname, "-f", path],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            raise RuntimeError(f"psql 执行失败: {r.stderr}\n{r.stdout}")
    else:
        with open(path, encoding="utf-8") as f:
            script = f.read()
        with psycopg.connect(db_url(dbname), autocommit=True) as conn:
            conn.execute(script)


def db_counts(dbname: str) -> dict[str, int]:
    out = {}
    with psycopg.connect(db_url(dbname)) as conn:
        for t in ("batches", "samples", "test_orders", "retest_links"):
            out[t] = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    return out


def test_index():
    status, _, body = http_get(f"{APP_URL}/")
    html = body.decode("utf-8", "replace")
    check("首页返回 200", status == 200, f"status={status}")
    check("首页列出全部 8 张检测单",
          all(f"ORD-00{i}" in html for i in range(1, 9)))
    check("首页显示四表行数", all(
        t in html for t in ("batches", "samples", "test_orders", "retest_links")))


def test_export_content():
    # 检测单 4：循环引用链 4->5->6
    status, headers, body = download(4)
    sql = body.decode("utf-8")
    check("导出 4 返回 200", status == 200, f"status={status}")
    check("导出 4 是附件下载",
          "attachment" in headers.get("content-disposition", "")
          and "test_order_4_extract.sql" in headers.get("content-disposition", ""))
    check("导出 4 含 4 个 CREATE TABLE", sql.count("CREATE TABLE") == 4)
    check("导出 4 含 5 个补外键 ALTER", sql.count("ALTER TABLE") == 5)
    check("导出 4 保留可延迟复合外键",
          "DEFERRABLE" in sql and "INITIALLY DEFERRED" in sql)
    check("导出 4 含循环链上的检测单",
          all(s in sql for s in ("'ORD-004'", "'ORD-005'", "'ORD-006'")))
    check("导出 4 含复测关联行", "'设备故障，安排复测'" in sql and "'复测仍超限，再次复测'" in sql)
    check("导出 4 不带入无关行",
          not any(s in sql for s in ("'ORD-001'", "'ORD-002'", "'ORD-007'", "'ORD-008'",
                                     "'S-001'", "'S-004'", "'S-005'", "'S-006'",
                                     "'B-2026-001'", "'B-2026-003'")))
    save_sql(4, body)

    # 检测单 1：无复测；同批次的其他样本/检测单不应被反向带入
    _, _, body1 = download(1)
    sql1 = body1.decode("utf-8")
    check("导出 1 不含同批次其他样本（无反向引用）",
          "'S-002'" not in sql1 and "'S-006'" not in sql1 and "'ORD-002'" not in sql1)
    check("导出 1 无复测关联行", 'INSERT INTO "retest_links"' not in sql1)
    save_sql(1, body1)

    # 检测单 8：复合外键一列为空 -> 不追查；样本批次为空 -> 不追查
    _, _, body8 = download(8)
    sql8 = body8.decode("utf-8")
    check("导出 8 含自身行", "'ORD-008'" in sql8 and "'S-005'" in sql8)
    check("导出 8 复合外键部分为空不追查", 'INSERT INTO "retest_links"' not in sql8)
    check("导出 8 可空外键为空不追查批次", 'INSERT INTO "batches"' not in sql8)
    save_sql(8, body8)

    # 检测单 2：单级复测
    _, _, body2 = download(2)
    sql2 = body2.decode("utf-8")
    check("导出 2 含原单与复测单", "'ORD-002'" in sql2 and "'ORD-003'" in sql2)
    check("导出 2 不重复导出共享的样本行", sql2.count("'S-002'") == 1)
    save_sql(2, body2)


def test_restore():
    ensure_restore_db()
    # 首个用例在全新空库上执行；后续用例依赖脚本自带的 DROP IF EXISTS 覆盖
    cases = [
        (4, {"batches": 1, "samples": 1, "test_orders": 3, "retest_links": 2}),
        (1, {"batches": 1, "samples": 1, "test_orders": 1, "retest_links": 0}),
        (8, {"batches": 0, "samples": 1, "test_orders": 1, "retest_links": 0}),
        (2, {"batches": 1, "samples": 1, "test_orders": 2, "retest_links": 1}),
    ]
    for order_id, expect in cases:
        path = os.path.join(WORKDIR, f"order_{order_id}.sql")
        try:
            apply_sql(RESTORE_DB, path)
            counts = db_counts(RESTORE_DB)
            check(f"空库还原 order {order_id} 行数正确", counts == expect,
                  f"got {counts}")
        except Exception as e:  # noqa: BLE001
            check(f"空库还原 order {order_id}", False, str(e))

    # 幂等：同一脚本可重复执行
    try:
        apply_sql(RESTORE_DB, os.path.join(WORKDIR, "order_4.sql"))
        check("脚本可在同一库重复执行",
              db_counts(RESTORE_DB)["test_orders"] == 3)
    except Exception as e:  # noqa: BLE001
        check("脚本可重复执行", False, str(e))

    # 循环引用完整还原 + 外键约束真实存在（此时库中是 order 4 的数据）
    try:
        with psycopg.connect(db_url(RESTORE_DB)) as conn:
            row = conn.execute(
                "SELECT latest_retest_orig, latest_retest_retest "
                "FROM test_orders WHERE id = 4"
            ).fetchone()
            check("循环引用数据完整 (order4 -> link(4,5))", row == (4, 5), f"got {row}")
            fk_count = conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE contype = 'f'"
            ).fetchone()[0]
            check("还原库外键数量为 5", fk_count == 5, f"got {fk_count}")
            deferrable = conn.execute(
                """
                SELECT condeferrable AND condeferred FROM pg_constraint
                WHERE contype = 'f' AND conrelid = 'test_orders'::regclass
                  AND array_length(conkey, 1) = 2
                """
            ).fetchone()
            check("复合外键保持 DEFERRABLE INITIALLY DEFERRED", deferrable == (True,))
            # 外键约束真实生效：插入违反外键的行应被拒绝
            violated = False
            try:
                conn.execute(
                    "INSERT INTO samples (id, batch_id, sample_code) "
                    "VALUES (900, 424242, 'BAD')")
            except psycopg.errors.ForeignKeyViolation:
                violated = True
            check("还原库外键约束真实生效", violated)
    except Exception as e:  # noqa: BLE001
        check("循环引用还原校验", False, str(e))


def test_not_found():
    status, headers, body = download(999)
    check("不存在的检测单返回 404", status == 404, f"status={status}")
    check("404 不提供附件下载",
          "attachment" not in headers.get("content-disposition", ""))
    check("404 页面说明未生成部分结果", "未生成任何部分结果" in body.decode("utf-8", "replace"))


def test_snapshot_isolation():
    """REPEATABLE READ 快照：读取期间源端写入不被阻塞，且读不到新数据。"""
    probe = psycopg.connect(DATABASE_URL)
    probe.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    probe.read_only = True
    before = probe.execute("SELECT count(*) FROM test_orders").fetchone()[0]

    writer = psycopg.connect(DATABASE_URL, autocommit=True)
    writer.execute(
        "INSERT INTO test_orders VALUES (9, 6, 'ORD-009', 'done', NULL, NULL)"
    )  # 若快照阻塞写入，这里会挂起或报错
    during = probe.execute("SELECT count(*) FROM test_orders").fetchone()[0]
    check("快照内计数不受并发写入影响", during == before, f"before={before} during={during}")
    writer.execute("DELETE FROM test_orders WHERE id = 9")
    writer.close()
    probe.close()


def test_failure_path():
    if not BAD_APP_URL:
        print("[SKIP] 未提供 BAD_APP_URL，跳过失败路径测试")
        return
    status, headers, _ = http_get(f"{BAD_APP_URL}/export/1")
    check("数据库不可用时导出返回 500", status == 500, f"status={status}")
    check("500 不提供附件下载",
          "attachment" not in headers.get("content-disposition", ""))
    status2, _, _ = http_get(f"{BAD_APP_URL}/")
    check("数据库不可用时首页返回 500 错误页", status2 == 500, f"status={status2}")


def main():
    test_index()
    test_export_content()
    test_restore()
    test_not_found()
    test_snapshot_isolation()
    test_failure_path()
    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败: {FAILURES}")
        sys.exit(1)
    print("全部自测通过")


if __name__ == "__main__":
    main()
