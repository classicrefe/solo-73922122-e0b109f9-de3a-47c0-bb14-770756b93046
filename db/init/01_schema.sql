-- 四表结构：批次 / 样本 / 检测单 / 复测关联
-- 演示要点：复合主键、复合外键、可空外键、表间循环引用。

CREATE TABLE batches (
    id       INTEGER PRIMARY KEY,
    batch_no TEXT NOT NULL,
    note     TEXT
);

CREATE TABLE samples (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER REFERENCES batches (id),   -- 可空外键
    sample_code TEXT NOT NULL
);

-- 复测关联：复合主键 (原检测单, 复测检测单)
CREATE TABLE retest_links (
    orig_order_id   INTEGER NOT NULL,
    retest_order_id INTEGER NOT NULL,
    reason          TEXT,
    PRIMARY KEY (orig_order_id, retest_order_id)
);

CREATE TABLE test_orders (
    id                   INTEGER PRIMARY KEY,
    sample_id            INTEGER NOT NULL REFERENCES samples (id),
    order_no             TEXT NOT NULL,
    status               TEXT NOT NULL,
    latest_retest_orig   INTEGER,
    latest_retest_retest INTEGER,
    -- 复合外键，与 retest_links 构成循环引用；可延迟以便写入环状数据
    FOREIGN KEY (latest_retest_orig, latest_retest_retest)
        REFERENCES retest_links (orig_order_id, retest_order_id)
        DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE retest_links
    ADD CONSTRAINT retest_links_orig_fk
        FOREIGN KEY (orig_order_id) REFERENCES test_orders (id),
    ADD CONSTRAINT retest_links_retest_fk
        FOREIGN KEY (retest_order_id) REFERENCES test_orders (id);
