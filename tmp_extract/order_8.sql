-- 检测单关联数据抽取（可在空库执行）
-- 根记录: test_orders 主键 = 8
-- 生成时间(UTC): 2026-11-15T16:11:19+00:00
-- 说明: 先建表(仅主键)再载入数据，最后统一补外键并校验；
--       循环引用无需关闭约束即可完整还原。

BEGIN;

DROP TABLE IF EXISTS "test_orders" CASCADE;
DROP TABLE IF EXISTS "retest_links" CASCADE;
DROP TABLE IF EXISTS "samples" CASCADE;
DROP TABLE IF EXISTS "batches" CASCADE;

CREATE TABLE "batches" (
    "id" INTEGER NOT NULL,
    "batch_no" TEXT NOT NULL,
    "note" TEXT,
    CONSTRAINT "batches_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "samples" (
    "id" INTEGER NOT NULL,
    "batch_id" INTEGER,
    "sample_code" TEXT NOT NULL,
    CONSTRAINT "samples_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "retest_links" (
    "orig_order_id" INTEGER NOT NULL,
    "retest_order_id" INTEGER NOT NULL,
    "reason" TEXT,
    CONSTRAINT "retest_links_pkey" PRIMARY KEY ("orig_order_id", "retest_order_id")
);

CREATE TABLE "test_orders" (
    "id" INTEGER NOT NULL,
    "sample_id" INTEGER NOT NULL,
    "order_no" TEXT NOT NULL,
    "status" TEXT NOT NULL,
    "latest_retest_orig" INTEGER,
    "latest_retest_retest" INTEGER,
    CONSTRAINT "test_orders_pkey" PRIMARY KEY ("id")
);

-- 表 "batches" 无关联行

INSERT INTO "samples" ("id", "batch_id", "sample_code") VALUES
(5, NULL, 'S-005');

-- 表 "retest_links" 无关联行

INSERT INTO "test_orders" ("id", "sample_id", "order_no", "status", "latest_retest_orig", "latest_retest_retest") VALUES
(8, 5, 'ORD-008', 'done', 99, NULL);

ALTER TABLE "samples" ADD CONSTRAINT "samples_batch_id_fkey" FOREIGN KEY ("batch_id") REFERENCES "batches" ("id");
ALTER TABLE "retest_links" ADD CONSTRAINT "retest_links_orig_fk" FOREIGN KEY ("orig_order_id") REFERENCES "test_orders" ("id");
ALTER TABLE "retest_links" ADD CONSTRAINT "retest_links_retest_fk" FOREIGN KEY ("retest_order_id") REFERENCES "test_orders" ("id");
ALTER TABLE "test_orders" ADD CONSTRAINT "test_orders_latest_retest_orig_latest_retest_retest_fkey" FOREIGN KEY ("latest_retest_orig", "latest_retest_retest") REFERENCES "retest_links" ("orig_order_id", "retest_order_id") DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE "test_orders" ADD CONSTRAINT "test_orders_sample_id_fkey" FOREIGN KEY ("sample_id") REFERENCES "samples" ("id");

COMMIT;
