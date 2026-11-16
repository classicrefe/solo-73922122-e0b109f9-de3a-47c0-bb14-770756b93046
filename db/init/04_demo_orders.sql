-- 复测更正页演示数据：在原始 8 张检测单之外追加，均为合法的同样本关系。
-- 插入发生在 03_rules.sql 建规则之后，因此这批数据本身也会被规则校验。
BEGIN;

INSERT INTO test_orders
        (id, sample_id, order_no, status, latest_retest_orig, latest_retest_retest) VALUES
    (9,  1, 'ORD-009', 'retested', 9,   10),
    (10, 1, 'ORD-010', 'done',     NULL, NULL),
    (11, 4, 'ORD-011', 'retested', 11,  12),
    (12, 4, 'ORD-012', 'done',     NULL, NULL),
    (13, 5, 'ORD-013', 'done',     NULL, NULL);

INSERT INTO retest_links (orig_order_id, retest_order_id, reason) VALUES
    (9, 10, '样本 S-001 复测演示：同一样本内的合法复测边'),
    (11, 12, '样本 S-004 复测演示：同一样本内的合法复测边');

COMMIT;
