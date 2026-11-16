# 检测单关联数据抽取 / 复测更正（隔离库复现 + 入仓前拦截）

实验室数据工程师排查样本归属问题时，在网页上选择一张检测单，下载一份**可在空库执行**的
SQL：包含四表结构（主键/外键）以及从该检测单出发、沿外键递归可达的全部被引用行，
用于在隔离库中完整复现问题现场。

此外提供**复测更正网页**（首页按钮进入，路由 `/correction`）：按样本查看检测单与复测
关系，一个表单最多 20 项操作（增/删复测边、修改检测单样本归属），整单事务提交；
入仓前在数据库层拦截跨样本复测边与有向环（含自环）。

## 快速开始

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> ，在检测单列表中点击「下载 SQL」，
或点击「复测更正网页」进入入仓前更正界面。

- `db`：PostgreSQL 16，首次启动自动建表并写入演示数据与规则（`db/init/*.sql`，按文件名顺序执行）
- `app`：FastAPI + uvicorn，通过 `DATABASE_URL` 连接数据库

## 复测更正规则（数据库层强制）

规则对**任意独立 READ COMMITTED SQL 连接**对 `test_orders` / `retest_links` 的增删改
都生效，不依赖应用是否在线：

- **R1 同样本**：每条复测边两端的检测单必须属于同一个 `sample_id`。
- **R2 无有向环**：复测边构成的有向图不得出现有向环，自环（原单=复测单）也在禁止之列。
- **整单事务**：一次提交的所有操作在单个事务内执行，任一违规整体回滚，不留部分修改。
- **最终状态判定**：单内暂时违规、最终合法的成批更正（例如先插反向边再删旧边断环、
  先改归属再加边、加了又删的净零变更）可以提交；约束触发器在 COMMIT 瞬间按最终状态校验。

实现采用两层触发器（见 `db/init/03_rules.sql`）：

| 触发器 | 时机/属性 | 职责 |
|---|---|---|
| `retest_links_lock_trg` | 普通 AFTER ROW，即时 | 按 id 定序、逐行对两端检测单加 `FOR NO KEY UPDATE` 行锁，把触及同一检测单的并发事务序列化；选 NO KEY UPDATE 以与外键自带的 `FOR KEY SHARE` 兼容，避免反向边并发自死锁 |
| `retest_links_commit_check` | CONSTRAINT TRIGGER，`DEFERRABLE INITIALLY DEFERRED` | COMMIT 时逐行校验自环、同样本、有向环（递归 CTE） |
| `test_orders_commit_check` | CONSTRAINT TRIGGER，`DEFERRABLE INITIALLY DEFERRED` | COMMIT 时校验 `sample_id` 变更不会产生跨样本边 |

并发语义：READ COMMITTED 提交时刻快照 + 行锁序列化。两个事务同时制造环或一边改归属
一边加边时，先提交者成功，后提交者在 COMMIT 校验时被拒（或因锁超时/死锁回滚），
**不可能各自通过却共同提交坏关系**；触及不相关样本的更正互不阻塞。触发器是数据库
目录对象，**重启后仍然生效**，无需应用重建。原有主键/外键全部保留，应用不自动改写
任何既有关系（包括 `latest_retest_*` 列）。

## 还原到隔离库

```bash
# 例如还原到本机一个空库 isolated_repro
createdb isolated_repro
psql -v ON_ERROR_STOP=1 -d isolated_repro -f test_order_4_extract.sql
```

脚本在单个事务中执行：建表（仅主键）→ 载入数据 → 统一补外键并校验 → 提交。
任一一步失败整体回滚，不会留下半个库。

## 源库模型（演示数据）

| 表 | 说明 | 演示要点 |
|---|---|---|
| `batches` | 批次 | 含一个无任何样本的空批次 |
| `samples` | 样本 | `batch_id` 为**可空外键**；含无检测单的样本 |
| `test_orders` | 检测单 | 复合外键 `(latest_retest_orig, latest_retest_retest)` → `retest_links`，`DEFERRABLE INITIALLY DEFERRED`；含两列全空、以及**仅一列为空**（悬空值 99）的行 |
| `retest_links` | 复测关联 | **复合主键** `(orig_order_id, retest_order_id)`，两个外键指回 `test_orders`，与 `test_orders` 构成**循环引用** |

## 设计要点

- **同一已提交视图**：导出在单个 `REPEATABLE READ` 只读事务内完成（目录元数据与
  数据行同一快照）；MVCC 快照不持有阻塞锁，抽取期间源端可继续增删改。
- **只追被引用行**：从所选检测单沿“本表 → 被引用表”方向递归（BFS），
  不抓取反向引用（如同一批次的其他样本、同一样本的其他检测单）。
- **复合外键任一列为空即不追查**（与 SQL 外键 MATCH SIMPLE 语义一致）；
  同一行被多次引用时按 `(表, 主键)` 去重，只导出一份。
- **主外键识别自数据库目录**：`information_schema`（表/列/主键/外键，复合外键经
  `position_in_unique_constraint` 配对列）+ `pg_constraint`（可延迟属性），
  不在代码里硬编码表结构。
- **循环引用可完整还原**：生成的脚本与 pg_dump 做法一致——先建表（仅主键）、
  载入数据、最后用 `ALTER TABLE` 补外键并即时校验；全程不关闭约束
  （不使用 `session_replication_role` / `DISABLE TRIGGER`），外键的可延迟属性按源库保留。
- **失败不给部分结果**：SQL 在内存中一次性装配完成才返回附件；读取或生成任一
  环节失败只返回错误页（404/500），不产生部分下载。

## 目录结构

```
app/            FastAPI 应用（main.py 入口，extractor.py 抽取，sqlgen.py 生成 SQL，
                correction.py 复测更正事务）
app/templates/  列表页、更正页与错误页
db/init/        建库脚本（按文件名顺序执行）：
                01_schema.sql 四表结构，02_seed.sql 原始演示数据，
                03_rules.sql 复测规则触发器/函数，04_demo_orders.sql 更正页演示数据
scripts/selftest.py             抽取功能端到端自测
scripts/selftest_correction.py  复测规则与并发更正端到端自测（见下）
docker-compose.yml
```

## 自测

`scripts/selftest.py` 覆盖：首页列表、导出内容断言（无反向引用行、复合外键部分
为空不追查、可空外键为空不追查、去重）、空库还原后行数/外键数量/循环引用数据
校验、外键约束真实生效、脚本幂等重放、404/500 失败路径、REPEATABLE READ 快照
与并发写入不互相阻塞。

`scripts/selftest_correction.py` 覆盖：独立 READ COMMITTED 连接上的跨样本/自环/
有向环/跨样本改归属拦截、单内暂时违规但最终合法可提交、并发反向边与改归属竞争
（规则拒绝或死锁拦截、无坏关系共同提交）、无关样本并发互不阻塞、更正网页的
合法提交/违规 409/整单回滚/20 项上限/并发提交、原外键保留、不自动改写
`latest_retest_*`、原导出功能无回归、规则触发器目录对象存在且可延迟。

```bash
# 依赖：psycopg；可选：psql（提供则用真实 psql 执行还原）
APP_URL=http://127.0.0.1:8000 \
DATABASE_URL=postgresql://lab:lab@127.0.0.1:5432/labdb \
ADMIN_URL=postgresql://lab:lab@127.0.0.1:5432/postgres \
PSQL=$(command -v psql) \
python3 scripts/selftest.py

# 复测更正套件（只需应用地址与业务库连接）
APP_URL=http://127.0.0.1:8000 \
DATABASE_URL=postgresql://lab:lab@127.0.0.1:5432/labdb \
python3 scripts/selftest_correction.py
```

## 已知限制

- 列类型按需求仅支持 `integer` / `text`，遇到其他类型会整体报错（不产生部分结果）。
- 还原目标是隔离复现库：脚本不含序列/自增、索引、视图、触发器；四表之外的
  数据库对象不在抽取范围内。
- 根表固定为 `test_orders`（单列主键），见 `app/extractor.py: ROOT_TABLE`。
