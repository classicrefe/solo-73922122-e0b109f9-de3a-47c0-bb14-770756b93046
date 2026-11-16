"""FastAPI 入口：选择检测单下载关联 SQL；复测更正页增删复测边、改样本归属。"""
from __future__ import annotations

import os

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from correction import (
    MAX_OPERATIONS,
    CorrectionConflict,
    CorrectionError,
    apply_corrections,
    parse_operations,
)
from extractor import ExtractError, RootNotFound, extract_order
from sqlgen import build_sql

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://lab:lab@localhost:5432/labdb"
)

app = FastAPI(title="检测单关联数据抽取 / 复测更正")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def _error_page(status: int, message: str, detail: str | None = None,
                context: str = "export") -> HTMLResponse:
    html = templates.get_template("error.html").render(
        status=status, message=message, detail=detail, context=context
    )
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


def _load_correction_view(cur, sample_id: int | None) -> dict:
    samples = cur.execute(
        """
        SELECT s.id, s.sample_code, b.batch_no,
               (SELECT count(*) FROM test_orders o WHERE o.sample_id = s.id) AS order_count
        FROM samples s
        LEFT JOIN batches b ON b.id = s.batch_id
        ORDER BY s.id
        """
    ).fetchall()

    orders = cur.execute(
        """
        SELECT o.id, o.order_no, o.status, o.sample_id, s.sample_code
        FROM test_orders o
        JOIN samples s ON s.id = o.sample_id
        ORDER BY o.sample_id, o.id
        """
    ).fetchall()

    links = cur.execute(
        """
        SELECT l.orig_order_id, l.retest_order_id, l.reason,
               oo.order_no AS orig_no, ro.order_no AS retest_no,
               so.sample_id AS sample_id, s.sample_code
        FROM retest_links l
        JOIN test_orders oo ON oo.id = l.orig_order_id
        JOIN test_orders ro ON ro.id = l.retest_order_id
        JOIN test_orders so ON so.id = l.orig_order_id
        JOIN samples s      ON s.id = so.sample_id
        ORDER BY so.sample_id, l.orig_order_id, l.retest_order_id
        """
    ).fetchall()

    current = None
    if sample_id is not None:
        current = cur.execute(
            "SELECT s.id, s.sample_code, b.batch_no FROM samples s "
            "LEFT JOIN batches b ON b.id = s.batch_id WHERE s.id = %s",
            (sample_id,),
        ).fetchone()
    return {"samples": samples, "orders": orders, "links": links, "current": current}


@app.get("/correction", response_class=HTMLResponse)
def correction_page(request: Request, sample: int | None = None,
                    msg: str | None = None, conflict: int | None = None):
    with _connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        view = _load_correction_view(cur, sample)
    if sample is not None and view["current"] is None:
        return _error_page(404, f"样本 id={sample} 不存在。", context="correction")
    return templates.TemplateResponse(
        request=request,
        name="correction.html",
        context={
            **view,
            "selected": sample,
            "max_ops": MAX_OPERATIONS,
            "flash_msg": msg,
            "flash_conflict": bool(conflict),
        },
    )


@app.post("/correction")
async def correction_submit(request: Request):
    form = (await request.form())
    flat = {k: v for k, v in form.multi_items()}
    try:
        sample_id_raw = flat.get("sample_id")
        sample_id = int(sample_id_raw) if sample_id_raw else None
        ops = parse_operations(flat)
    except (CorrectionError, TypeError, ValueError) as exc:
        return _error_page(400, f"更正表单无效：{exc}", context="correction")

    try:
        with _connect() as conn:
            summary = apply_corrections(conn, ops)
    except CorrectionError as exc:
        return _error_page(400, str(exc), context="correction")
    except CorrectionConflict as exc:
        # 冲突：整单已回滚，带提示重定向回原样本视图，允许重试。
        target = f"/correction?sample={sample_id or ''}&conflict=1&msg={exc}"
        return RedirectResponse(
            url=target, status_code=303,
        )
    except psycopg.Error as exc:
        # 提交时约束触发器抛出的违规：展示规则原因（message）与涉及检测单（detail）。
        detail = getattr(exc, "diag", None)
        detail_text = detail.message_detail if detail is not None else None
        return _error_page(
            409,
            f"更正被规则拦截，整单已回滚：{exc}",
            detail=detail_text,
            context="correction",
        )

    msg = (f"提交成功：共 {summary['operations']} 项操作，"
           f"涉及 {summary['orders']} 张检测单，已在单个事务中提交。")
    url = f"/correction?sample={sample_id or ''}&msg={msg}"
    return RedirectResponse(url=url, status_code=303)
