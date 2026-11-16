-- 复测关系入仓规则（在原始四表结构与演示数据之后创建）：
--   规则 R1：retest_links 每条边的两端检测单必须属于同一样本；
--   规则 R2：复测边构成的有向图不得出现有向环（含自环）。
--
-- 两层触发器实现，且对任意独立 READ COMMITTED SQL 连接的增删改同样生效：
--   1) 即时“加锁”触发器（普通 AFTER ROW）：边插入/更新时立即按 id 定序、逐行
--      锁住两端 test_orders 行（FOR NO KEY UPDATE），让触及同一检测单的并发
--      事务在锁上排队序列化；test_orders.sample_id 的 UPDATE 持 NO KEY UPDATE
--      行锁，与本触发器的锁同级互斥。选 NO KEY UPDATE 而非 FOR UPDATE，是为了
--      与 retest_links 外键自带的 FOR KEY SHARE 兼容，避免反向边并发自死锁。
--   2) 提交时“校验”约束触发器（DEFERRABLE INITIALLY DEFERRED）：事务提交
--      瞬间才执行校验，因此单内暂时违规、最终合法的成批更正可以提交；
--      此时每条语句拿到 READ COMMITTED 的最新快照（含其他事务已提交数据
--      与本事务自身改动），配合第一层行锁，并发事务不可能各自通过却共同
--      提交坏关系——检测到冲突的一方整单回滚并报错。
-- 触发器是数据库目录对象，数据库/容器重启后仍然生效，无需应用侧重建。
-- 本文件只新增规则对象，不修改原有主键/外键，也不改写任何已有关系数据。

-- =====================================================================
-- 规则函数：retest_links 行的提交时校验
-- =====================================================================
CREATE OR REPLACE FUNCTION retest_links_commit_check_fn() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_orig_sample   integer;
    v_retest_sample integer;
    v_orig_no       text;
    v_retest_no     text;
BEGIN
    -- 约束触发器只挂在 INSERT/UPDATE 上；DELETE 不可能制造违规，保留保护分支。
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    -- 最终状态校验：延迟触发器会为事务内每条 INSERT/UPDATE 排队一个事件，
    -- 即使该行随后已被删除，事件仍会在提交时触发。对“加了又删”的净零变更，
    -- 该行已不存在，不可能产生违规，直接跳过；仍存在的行才做完整校验。
    PERFORM 1 FROM retest_links
     WHERE orig_order_id = NEW.orig_order_id
       AND retest_order_id = NEW.retest_order_id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    -- 自环：原单与复测单是同一张检测单。
    IF NEW.orig_order_id = NEW.retest_order_id THEN
        RAISE EXCEPTION
              '复测关系不允许自环：检测单 % 不能以自身作为复测单。',
              NEW.orig_order_id
            USING ERRCODE = 'check_violation',
                  DETAIL  = format('涉及检测单：%s。', NEW.orig_order_id),
                  HINT    = '请删除该复测边或选择不同的检测单后重新提交。';
    END IF;

    SELECT sample_id, order_no INTO v_orig_sample, v_orig_no
      FROM test_orders WHERE id = NEW.orig_order_id;
    SELECT sample_id, order_no INTO v_retest_sample, v_retest_no
      FROM test_orders WHERE id = NEW.retest_order_id;

    -- R1：两端必须属于同一样本。
    IF v_orig_sample IS DISTINCT FROM v_retest_sample THEN
        RAISE EXCEPTION
              '复测边两端必须属于同一样本：检测单 %（%，样本 %）-> 检测单 %（%，样本 %）。',
              NEW.orig_order_id, v_orig_no, v_orig_sample,
              NEW.retest_order_id, v_retest_no, v_retest_sample
            USING ERRCODE = 'check_violation',
                  DETAIL  = format('涉及检测单：%s、%s。',
                                   NEW.orig_order_id, NEW.retest_order_id),
                  HINT    = '请先把两张检测单调整到同一样本，或删除该复测边。';
    END IF;

    -- R2：从复测端沿既有边向前可达原单端，即 NEW 边闭合了一个有向环。
    -- UNION 去重保证即使图中存在环，递归也会终止。
    IF EXISTS (
            WITH RECURSIVE walk(ord) AS (
                SELECT NEW.retest_order_id
                UNION
                SELECT l.retest_order_id
                  FROM walk w
                  JOIN retest_links l ON l.orig_order_id = w.ord
            )
            SELECT 1 FROM walk WHERE ord = NEW.orig_order_id
       ) THEN
        RAISE EXCEPTION
              '复测关系不允许形成有向环：新增边 检测单 % -> 检测单 % 会与既有复测链构成环。',
              NEW.orig_order_id, NEW.retest_order_id
            USING ERRCODE = 'check_violation',
                  DETAIL  = format('涉及检测单：%s、%s。',
                                   NEW.orig_order_id, NEW.retest_order_id),
                  HINT    = '请先断开环上的某条复测边后再提交。';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS retest_links_commit_check ON retest_links;
CREATE CONSTRAINT TRIGGER retest_links_commit_check
    AFTER INSERT OR UPDATE ON retest_links
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION retest_links_commit_check_fn();

-- =====================================================================
-- 加锁函数：retest_links 插入/更新时立即锁住两端检测单
-- =====================================================================
CREATE OR REPLACE FUNCTION retest_links_lock_fn() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_id integer;
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        -- 按 id 全局定序、逐行加锁（FOR NO KEY UPDATE）。
        -- 为什么是 NO KEY UPDATE 而不是 FOR UPDATE：
        --   retest_links 的两个外键在写入时会先对 test_orders 加 FOR KEY SHARE，
        --   且按“原单列、复测列”顺序获取；反向边并发时两事务的 KEY SHARE
        --   获取顺序恰好相反。FOR UPDATE 与 KEY SHARE 冲突会构成 AB-BA 死锁，
        --   而 FOR NO KEY UPDATE 与 KEY SHARE 兼容、只与另一个 NO KEY UPDATE
        --   （sample_id 更新及本触发器）互斥，既消除自死锁，又保留并发序列化。
        -- 逐行定序保证同类事务按完全相同的顺序申请锁，只排队不环等。
        FOR v_id IN
            SELECT id FROM test_orders
             WHERE id IN (NEW.orig_order_id, NEW.retest_order_id)
             ORDER BY id
        LOOP
            PERFORM 1 FROM test_orders WHERE id = v_id FOR NO KEY UPDATE;
        END LOOP;
    END IF;
    RETURN NULL; -- AFTER ROW 触发器，返回值被忽略
END;
$$;

DROP TRIGGER IF EXISTS retest_links_lock_trg ON retest_links;
CREATE TRIGGER retest_links_lock_trg
    AFTER INSERT OR UPDATE ON retest_links
    FOR EACH ROW
    EXECUTE FUNCTION retest_links_lock_fn();

-- =====================================================================
-- 规则函数：test_orders.sample_id 变更的提交时校验
-- =====================================================================
CREATE OR REPLACE FUNCTION test_orders_commit_check_fn() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_bad record;
BEGIN
    IF TG_OP <> 'UPDATE' OR OLD.sample_id IS NOT DISTINCT FROM NEW.sample_id THEN
        RETURN NEW;
    END IF;

    -- 找出所有与本检测单相连、但另一端样本与新样本不一致的边。
    SELECT l.orig_order_id, l.retest_order_id,
           so.sample_id AS orig_sample, sr.sample_id AS retest_sample,
           so.order_no  AS orig_no, sr.order_no  AS retest_no
      INTO v_bad
      FROM retest_links l
      JOIN test_orders so ON so.id = l.orig_order_id
      JOIN test_orders sr ON sr.id = l.retest_order_id
     WHERE (l.orig_order_id = NEW.id OR l.retest_order_id = NEW.id)
       AND so.sample_id IS DISTINCT FROM sr.sample_id
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
              '修改样本归属会造成跨样本复测边：检测单 %（%，新样本 %）关联的边 检测单 %（样本 %）-> 检测单 %（样本 %）两端样本不一致。',
              NEW.id, NEW.order_no, NEW.sample_id,
              v_bad.orig_order_id, v_bad.orig_sample,
              v_bad.retest_order_id, v_bad.retest_sample
            USING ERRCODE = 'check_violation',
                  DETAIL  = format('涉及检测单：%s、%s。',
                                   v_bad.orig_order_id, v_bad.retest_order_id),
                  HINT    = '请把边两端检测单一并调整到同一样本，或先删除该复测边。';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS test_orders_commit_check ON test_orders;
CREATE CONSTRAINT TRIGGER test_orders_commit_check
    AFTER UPDATE OF sample_id ON test_orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (OLD.sample_id IS DISTINCT FROM NEW.sample_id)
    EXECUTE FUNCTION test_orders_commit_check_fn();
