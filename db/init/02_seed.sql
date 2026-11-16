-- 演示数据（总量远小于 500 行）。
-- 循环引用数据依赖 test_orders -> retest_links 的可延迟复合外键，
-- 因此整批数据放在同一个事务里写入，提交时再校验该外键。
BEGIN;

INSERT INTO batches (id, batch_no, note) VALUES
    (1, 'B-2026-001', '常规批次'),
    (2, 'B-2026-002', '加急批次'),
    (3, 'B-2026-003', '空批次：无任何样本，用于验证反向引用不被带入');

INSERT INTO samples (id, batch_id, sample_code) VALUES
    (1, 1,    'S-001'),
    (2, 1,    'S-002'),
    (3, 2,    'S-003'),
    (4, 2,    'S-004'),
    (5, NULL, 'S-005'),   -- 可空外键为空：抽取时不应追查批次
    (6, 1,    'S-006');   -- 没有任何检测单：不应出现在任何抽取结果中

INSERT INTO test_orders
        (id, sample_id, order_no, status, latest_retest_orig, latest_retest_retest) VALUES
    (1, 1, 'ORD-001', 'done',     NULL, NULL),
    (2, 2, 'ORD-002', 'retested', 2,    3),
    (3, 2, 'ORD-003', 'done',     NULL, NULL),
    (4, 3, 'ORD-004', 'retested', 4,    5),
    (5, 3, 'ORD-005', 'retested', 5,    6),
    (6, 3, 'ORD-006', 'done',     NULL, NULL),
    (7, 4, 'ORD-007', 'done',     NULL, NULL),
    (8, 5, 'ORD-008', 'done',     99,   NULL);  -- 复合外键一列为空：约束不检查，抽取不追查

INSERT INTO retest_links (orig_order_id, retest_order_id, reason) VALUES
    (2, 3, '结果超限，安排复测'),
    (4, 5, '设备故障，安排复测'),
    (5, 6, '复测仍超限，再次复测');

COMMIT;
