-- 抽奖未送达的补发窗口。为空表示没有待补发项或已结束。
ALTER TABLE key_pool_drops ADD COLUMN retry_until TEXT;
