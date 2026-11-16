"""FastAPI 入口：选择检测单，下载可在空库执行的关联数据 SQL。"""
from __future__ import annotations

import os

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from extractor import ExtractError, RootNotFound, extract_order
from sqlgen import build_sql

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://lab:lab@localhost:5432/labdb"
)

app = FastAPI(title="检测单关联数据抽取")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def _error_page(status: int, message: str) -> HTMLResponse:
    html = templates.get_template("error.html").render(status=status, message=message)
    return HTMLResponse(content=html, status_code=status)


@app.exception_handler(psycopg.Error)
def db_error_handler(request: Request, exc: psycopg.Error):
    return _error_page(500, f"数据库访问失败：{exc}")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with _connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        orders = cur.execute(
            """
            SELECT o.id, o.order_no, o.status,
                   s.sample_code, b.batch_no,
                   o.latest_retest_orig, o.latest_retest_retest
            FROM test_orders o
            LEFT JOIN samples s ON s.id = o.sample_id
            LEFT JOIN batches b ON b.id = s.batch_id
            ORDER BY o.id
            """
        ).fetchall()
        counts = {
            t: cur.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"]
            for t in ("batches", "samples", "test_orders", "retest_links")
        }
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"orders": orders, "counts": counts},
    )


@app.get("/export/{order_id}")
def export(order_id: int):
    try:
        with _connect() as conn:
            # 同一已提交视图：REPEATABLE READ 只读事务，MVCC 快照不阻塞源端写入
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            result = extract_order(conn, order_id)
            script = build_sql(result)
    except RootNotFound as exc:
        return _error_page(404, str(exc))
    except (ExtractError, psycopg.Error) as exc:
        # 读取或生成失败：整体报错，不提供任何部分下载
        return _error_page(500, f"抽取或生成失败：{exc}")
    # 全部生成成功后才返回完整文件（内存一次性装配，不做流式输出）
    return Response(
        content=script.encode("utf-8"),
        media_type="application/sql; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; "
            f'filename="test_order_{order_id}_extract.sql"'
        },
    )
