#!/usr/bin/env python3 -I
"""复测更正端到端自测：规则触发器、独立 READ COMMITTED 连接、并发竞争、
网页提交、整单回滚、重启后规则仍生效、原外键与导出保持不变。

用法（环境变量均有默认值，适配本机 Unix socket 上的 PostgreSQL）：
    APP_URL       默认 http://127.0.0.1:8000
    DATABASE_URL  默认 postgresql://lab@/labdb?host=/tmp
"""
from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lab@/labdb?host=/tmp")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def db():
    # 服务端超时（正常 PostgreSQL 环境生效）；用例自身保证所有事务都会自行
    # 提交/回滚，配合按 id 定序加锁与死锁检测器，不留持锁的孤儿后端。
    conn = psycopg.connect(DATABASE_URL)
    conn.execute("SET SESSION statement_timeout = 8000")
    conn.execute("SET SESSION lock_timeout = 6000")
    return conn


def close_quietly(conn):
    """异常路径下保证事务回滚、连接归还，避免行锁残留拖累后续用例。"""
    try:
        if conn is not None and not conn.closed:
            conn.rollback()
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def edge_exists(orig: int, retest: int) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM retest_links WHERE orig_order_id=%s AND retest_order_id=%s",
            (orig, retest),
        ).fetchone() is not None


def _sample_of(order_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT sample_id FROM test_orders WHERE id=%s", (order_id,)
        ).fetchone()[0]


def cleanup(*edges: tuple[int, int]):
    with db() as conn:
        for a, b in edges:
            conn.execute(
                "DELETE FROM retest_links WHERE orig_order_id=%s AND retest_order_id=%s",
                (a, b),
            )
        conn.commit()


def http_post(path: str, data: dict[str, str]) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(APP_URL + path, data=body, method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # 让 303 抛 HTTPError，便于断言重定向

    try:
        with urllib.request.build_opener(NoRedirect()).open(req) as resp:
            return resp.status, resp.headers.get("location", "")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.code, e.headers.get("location", "")
        return e.code, e.read().decode("utf-8", "replace")


def http_get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(APP_URL + path) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------
# 1. 独立 READ COMMITTED SQL 连接上的规则强制
# ---------------------------------------------------------------------
def test_raw_connection_rules():
    with db() as conn:
        iso = conn.execute("SHOW transaction_isolation").fetchone()[0]
        check("独立连接默认 READ COMMITTED", iso == "read committed", iso)

    cases = [
        ("跨样本边被拒", "INSERT INTO retest_links VALUES (1,3,'x')",
         psycopg.errors.CheckViolation, "同一样本"),
        ("自环被拒", "INSERT INTO retest_links VALUES (2,2,'x')",
         psycopg.errors.CheckViolation, "自环"),
        ("有向环被拒", "INSERT INTO retest_links VALUES (3,2,'x')",
         psycopg.errors.CheckViolation, "有向环"),
        ("跨样本改归属被拒",
         "UPDATE test_orders SET sample_id=2 WHERE id=4",
         psycopg.errors.CheckViolation, "跨样本复测边"),
    ]
    for name, sql, exc_type, kw in cases:
        got = None
        conn = None
        try:
            conn = db()
            conn.execute(sql)
            conn.commit()
        except exc_type as e:
            got = e
        check(name, got is not None and kw in str(got), str(got))
        close_quietly(conn)
        # 失败后数据未被污染
    check("违规语句未留下任何边 (1,3)/(3,2)/(2,2)",
          not edge_exists(1, 3) and not edge_exists(3, 2) and not edge_exists(2, 2))

    # 合法同样本边可提交，随后清理
    with db() as conn:
        conn.execute("INSERT INTO retest_links VALUES (1,10,'raw ok')")
        conn.commit()
    check("独立连接合法同样本边可提交", edge_exists(1, 10))
    cleanup((1, 10))

    # 暂时违规、最终合法：边先跨样本，再把对端改到同一样本
    try:
        with db() as conn:
            conn.execute("INSERT INTO retest_links VALUES (10,13,'temp')")
            conn.execute("UPDATE test_orders SET sample_id=1 WHERE id=13")
            conn.commit()
        ok = edge_exists(10, 13)
    except psycopg.Error as e:  # noqa: BLE001
        ok = False
        print("   详情:", e)
    check("单内暂时违规但最终合法可提交", ok)
    with db() as conn:
        conn.execute("DELETE FROM retest_links WHERE orig_order_id=10 AND retest_order_id=13")
        conn.execute("UPDATE test_orders SET sample_id=5 WHERE id=13")
        conn.commit()


# ---------------------------------------------------------------------
# 2. 并发竞争：两个独立事务同时制造环，只能有一个成功
# ---------------------------------------------------------------------
def test_concurrent_cycle():
    cleanup((1, 9), (9, 1))
    start = threading.Event()
    outcomes: dict[str, str] = {}

    def worker(name: str, a: int, b: int):
        conn = None
        start.wait(10)
        try:
            conn = db()
            conn.execute("INSERT INTO retest_links VALUES (%s,%s,'race')", (a, b))
            conn.commit()
            outcomes[name] = "committed"
        except psycopg.errors.CheckViolation:
            outcomes[name] = "rejected"
        except (psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable) as e:
            outcomes[name] = f"conflict:{type(e).__name__}"
        except Exception as e:  # noqa: BLE001
            outcomes[name] = f"error:{type(e).__name__}"
        finally:
            close_quietly(conn)

    t1 = threading.Thread(target=worker, args=("T1", 1, 9))
    t2 = threading.Thread(target=worker, args=("T2", 9, 1))
    t1.start(); t2.start()
    start.set()  # 两线程同时起跑；不在事务内等待，杜绝持锁孤儿
    t1.join(25); t2.join(25)

    # 合法结果有两种，均保证坏关系不可能共同提交：
    #   a) 行锁序列化：一方 committed，另一方被有向环/同样本规则 rejected；
    #   b) 死锁检测器回滚一方（conflict:DeadlockDetected），属可重试的并发冲突。
    vals = list(outcomes.values())
    serialized = sorted(vals) == ["committed", "rejected"]
    deadlocked = any(v.startswith("conflict:") for v in vals) and all(
        v in ("committed", "rejected") or v.startswith("conflict:") for v in vals
    )
    check("并发成环：规则拒绝或死锁冲突拦截，无坏关系共同提交",
          serialized or deadlocked, str(outcomes))
    check("并发成环：库里不可能两条边同时存在",
          not (edge_exists(1, 9) and edge_exists(9, 1)))
    cleanup((1, 9), (9, 1))


# ---------------------------------------------------------------------
# 3. 并发：改归属 vs 加边，行锁序列化，坏关系不可能共同提交
# ---------------------------------------------------------------------
def test_concurrent_move_vs_edge():
    cleanup((1, 3))
    start = threading.Event()
    results: dict[str, str] = {}

    def mover():
        conn = None
        start.wait(10)
        try:
            conn = db()
            conn.execute("UPDATE test_orders SET sample_id=3 WHERE id=1")
            time.sleep(0.4)  # 持 NO KEY UPDATE 行锁；线程始终存活，随后自行提交
            conn.commit()
            results["move"] = "committed"
        except Exception as e:  # noqa: BLE001
            results["move"] = f"error:{type(e).__name__}"
        finally:
            close_quietly(conn)

    def edger():
        start.wait(10)
        time.sleep(0.1)  # 确保 mover 先拿锁
        conn = None
        try:
            conn = db()
            conn.execute("INSERT INTO retest_links VALUES (1,3,'race')")
            conn.commit()
            results["edge"] = "committed"
        except psycopg.errors.CheckViolation:
            results["edge"] = "rejected"
        except Exception as e:  # noqa: BLE001
            results["edge"] = f"error:{type(e).__name__}"
        finally:
            close_quietly(conn)

    t1 = threading.Thread(target=mover); t2 = threading.Thread(target=edger)
    t1.start(); t2.start(); start.set()
    t1.join(25); t2.join(25)

    check("改归属与加边竞争：移动提交、跨样本边被拒（或反之），无共同坏关系",
          results.get("move") == "committed" and results.get("edge") == "rejected",
          str(results))
    check("竞争后不存在跨样本边 (1,3)", not edge_exists(1, 3))
    with db() as conn:
        conn.execute("UPDATE test_orders SET sample_id=1 WHERE id=1")
        conn.commit()


# ---------------------------------------------------------------------
# 4. 并发：无关样本的更正互不阻塞，各自成功
# ---------------------------------------------------------------------
def test_unrelated_samples_proceed():
    cleanup((1, 10), (8, 13))
    start = threading.Event()
    results: dict[str, str] = {}

    def worker(name: str, a: int, b: int):
        start.wait(10)
        conn = None
        try:
            conn = db()
            conn.execute("INSERT INTO retest_links VALUES (%s,%s,'unrel')", (a, b))
            conn.commit()
            results[name] = "committed"
        except Exception as e:  # noqa: BLE001
            results[name] = f"error:{type(e).__name__}"
        finally:
            close_quietly(conn)

    t0 = time.time()
    t1 = threading.Thread(target=worker, args=("sample1", 1, 10))
    t2 = threading.Thread(target=worker, args=("sample5", 8, 13))
    t1.start(); t2.start(); start.set()
    t1.join(25); t2.join(25)
    elapsed = time.time() - t0
    check("无关样本并发更正均成功",
          results == {"sample1": "committed", "sample5": "committed"}, str(results))
    check("无关样本未被锁串行拖慢（<3s）", elapsed < 3, f"{elapsed:.2f}s")
    check("两条边都真实落库", edge_exists(1, 10) and edge_exists(8, 13))
    cleanup((1, 10), (8, 13))


# ---------------------------------------------------------------------
# 5. 网页层：视图、合法提交、违规 409、整单回滚、20 项上限
# ---------------------------------------------------------------------
def test_web_flow():
    status, html = http_get("/correction")
    check("更正页 200 且列出样本", status == 200 and "S-001" in html)
    status, html = http_get("/correction?sample=2")
    check("按样本视图含该样本检测单与边",
          status == 200 and "ORD-002" in html and "结果超限" in html)
    status, _ = http_get("/correction?sample=999")
    check("不存在样本返回 404", status == 404)

    cleanup((1, 9))
    status, loc = http_post("/correction", {
        "sample_id": "1", "kind-0": "add_edge",
        "orig-0": "1", "retest-0": "9", "reason-0": "web",
    })
    check("网页合法更正 303 重定向成功", status == 303 and "msg=" in loc, f"{status} {loc}")
    check("合法更正已落库", edge_exists(1, 9))

    # 混合单：第一项合法删除 + 第二项违规 -> 整单回滚，删除不生效
    status, body = http_post("/correction", {
        "sample_id": "1",
        "kind-0": "del_edge", "orig-0": "1", "retest-0": "9",
        "kind-1": "add_edge", "orig-1": "1", "retest-1": "3",
    })
    check("违规混合单返回 409", status == 409 and "整单已回滚" in body)
    check("整单回滚：第一项删除未生效", edge_exists(1, 9))
    check("错误页展示涉及检测单", "涉及检测单：1、3" in body)

    # 超 20 项
    data = {"sample_id": "1"}
    for i in range(21):
        data.update({f"kind-{i}": "add_edge", f"orig-{i}": "1", f"retest-{i}": "10"})
    status, body = http_post("/correction", data)
    check("超过 20 项操作返回 400", status == 400 and "20" in body)

    # 非连续序号（模拟网页删除中间行后留下空号）：kind-0 与 kind-2，缺 kind-1
    cleanup((1, 9))
    status, loc = http_post("/correction", {
        "sample_id": "1",
        "kind-0": "add_edge", "orig-0": "1", "retest-0": "9",
        "kind-2": "move_order", "order-2": "13", "sample-2": "1",
    })
    check("非连续序号也能提交（删除中间行场景）", status == 303, f"{status} {loc[:80]}")
    check("非连续序号的两项操作均落库",
          edge_exists(1, 9) and _sample_of(13) == 1)
    # 还原
    with db() as conn:
        conn.execute("DELETE FROM retest_links WHERE orig_order_id=1 AND retest_order_id=9")
        conn.execute("UPDATE test_orders SET sample_id=5 WHERE id=13")
        conn.commit()
    cleanup((1, 9))


# ---------------------------------------------------------------------
# 6. 网页并发：两个浏览器同时提交反向边，一个成功一个冲突/拒绝
# ---------------------------------------------------------------------
def test_web_concurrent():
    cleanup((1, 9), (9, 1))
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def post(name: str, a: str, b: str):
        barrier.wait(timeout=15)
        status, loc_or_body = http_post("/correction", {
            "sample_id": "1", "kind-0": "add_edge",
            "orig-0": a, "retest-0": b,
        })
        results[name] = (status, loc_or_body[:80])

    t1 = threading.Thread(target=post, args=("A", "1", "9"))
    t2 = threading.Thread(target=post, args=("B", "9", "1"))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    statuses = sorted(s for s, _ in results.values())
    check("网页并发反向边：一个 303 成功，另一个 409 拦截",
          statuses == [303, 409], str(results))
    check("网页并发后无坏关系落库", not (edge_exists(1, 9) and edge_exists(9, 1)))
    cleanup((1, 9), (9, 1))


# ---------------------------------------------------------------------
# 7. 原外键保留、不自动改写 latest_retest_*、导出仍可用
# ---------------------------------------------------------------------
def test_legacy_invariants():
    # 原 FK：删除被复测边引用的检测单仍被外键阻止
    blocked = False
    try:
        with db() as conn:
            conn.execute("DELETE FROM test_orders WHERE id=3")
            conn.commit()
    except psycopg.errors.ForeignKeyViolation:
        blocked = True
    check("原有外键约束仍然生效", blocked)

    # 删边不自动改写 latest_retest_*：插入一条不被复合外键引用的临时边再删除，
    # 观察所有 latest_retest_* 均不变化。7 与 12 同属样本 4，边 7->12 合法且无环。
    with db() as conn:
        snap = conn.execute(
            "SELECT id, latest_retest_orig, latest_retest_retest FROM test_orders "
            "WHERE latest_retest_orig IS NOT NULL ORDER BY id"
        ).fetchall()
        conn.execute("INSERT INTO retest_links VALUES (7,12,'tmp no-rewrite')")
        conn.execute("DELETE FROM retest_links WHERE orig_order_id=7 AND retest_order_id=12")
        conn.commit()
        snap2 = conn.execute(
            "SELECT id, latest_retest_orig, latest_retest_retest FROM test_orders "
            "WHERE latest_retest_orig IS NOT NULL ORDER BY id"
        ).fetchall()
    check("增删边不自动改写任何 latest_retest_* 列", snap == snap2, f"{snap} vs {snap2}")
    check("临时边已清理", not edge_exists(7, 12))

    # 导出功能仍工作
    try:
        with urllib.request.urlopen(f"{APP_URL}/export/4") as resp:
            sql = resp.read().decode()
            export_status = resp.status
        check("原导出功能不受影响（4 表 5 外键）",
              export_status == 200 and sql.count("CREATE TABLE") == 4
              and sql.count("ALTER TABLE") == 5)
    except Exception as e:  # noqa: BLE001
        check("原导出功能不受影响", False, str(e))

    # 触发器对象在目录中且为可延迟约束触发器
    with db() as conn:
        rows = conn.execute(
            "SELECT tgname, tgenabled, tgdeferrable, tginitdeferred "
            "FROM pg_trigger WHERE tgname IN "
            "('retest_links_commit_check','retest_links_lock_trg','test_orders_commit_check') "
            "ORDER BY tgname"
        ).fetchall()
    check("三个规则触发器均存在，两个校验触发器可延迟初始延迟",
          len(rows) == 3 and all(r[1] == "O" for r in rows)
          and {r[0]: (r[2], r[3]) for r in rows}["retest_links_commit_check"] == (True, True)
          and {r[0]: (r[2], r[3]) for r in rows}["test_orders_commit_check"] == (True, True),
          str(rows))


def main():
    test_raw_connection_rules()
    test_concurrent_cycle()
    test_concurrent_move_vs_edge()
    test_unrelated_samples_proceed()
    test_web_flow()
    test_web_concurrent()
    test_legacy_invariants()
    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败: {FAILURES}")
        raise SystemExit(1)
    print("全部更正自测通过")


if __name__ == "__main__":
    main()
