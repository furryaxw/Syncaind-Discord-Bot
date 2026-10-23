-- 本地 key 池：池、key、活动、入口卡片、抽奖报名、未送达记录。
-- 旧表 key_deliveries / key_drops / key_drop_entries / key_denied_roles 不再使用。

CREATE TABLE IF NOT EXISTS key_pools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    note        TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS key_pool_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    pool_id      INTEGER NOT NULL REFERENCES key_pools (id) ON DELETE CASCADE,
    secret       TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'available',
    added_at     TEXT    NOT NULL,
    reserved_at  TEXT,
    delivered_at TEXT,
    delivered_to INTEGER,
    drop_id      INTEGER,
    voided_at    TEXT,
    UNIQUE (guild_id, secret)
);

CREATE INDEX IF NOT EXISTS idx_key_pool_keys_available
    ON key_pool_keys (guild_id, pool_id, status);

CREATE INDEX IF NOT EXISTS idx_key_pool_keys_drop
    ON key_pool_keys (drop_id, status);

CREATE TABLE IF NOT EXISTS key_pool_drops (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    code          TEXT    NOT NULL,
    mode          TEXT    NOT NULL,
    count_mode    TEXT    NOT NULL,
    key_count     INTEGER,
    role_id       INTEGER,
    deny_role_id  INTEGER,
    channel_id    INTEGER NOT NULL,
    closes_at     TEXT,
    status        TEXT    NOT NULL,
    delivered     INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE (guild_id, code)
);

CREATE INDEX IF NOT EXISTS idx_key_pool_drops_due
    ON key_pool_drops (status, closes_at);

CREATE TABLE IF NOT EXISTS key_pool_drop_pools (
    drop_id INTEGER NOT NULL REFERENCES key_pool_drops (id) ON DELETE CASCADE,
    pool_id INTEGER NOT NULL REFERENCES key_pools (id) ON DELETE CASCADE,
    PRIMARY KEY (drop_id, pool_id)
);

CREATE TABLE IF NOT EXISTS key_pool_drop_cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_id    INTEGER NOT NULL REFERENCES key_pool_drops (id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    status     TEXT    NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_key_pool_drop_cards_drop
    ON key_pool_drop_cards (drop_id, status);

CREATE TABLE IF NOT EXISTS key_pool_drop_entries (
    drop_id    INTEGER NOT NULL REFERENCES key_pool_drops (id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    entered_at TEXT    NOT NULL,
    PRIMARY KEY (drop_id, user_id)
);

CREATE TABLE IF NOT EXISTS key_pool_drop_failures (
    drop_id    INTEGER NOT NULL REFERENCES key_pool_drops (id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    retried_at TEXT,
    PRIMARY KEY (drop_id, user_id)
);
