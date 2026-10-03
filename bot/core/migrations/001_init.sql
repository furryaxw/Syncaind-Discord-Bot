-- 完整 schema：新装一次到位。
--
-- 表按用途分组：服务器配置 → 处罚与案件 → 警告 → GitHub 映射 → 发布订阅 →
-- 权限节点绑定 → 激活码（发放记录、活动、黑名单）→ 自助身份组。

-- ---------------------------------------------------------------- 服务器配置

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id             INTEGER PRIMARY KEY,
    warn_threshold       INTEGER NOT NULL DEFAULT 3,
    warn_action          TEXT    NOT NULL DEFAULT 'timeout',
    warn_timeout_minutes INTEGER NOT NULL DEFAULT 60,
    mod_log_channel_id   INTEGER,
    locale               TEXT,
    updated_at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS module_states (
    guild_id   INTEGER NOT NULL,
    module_id  TEXT    NOT NULL,
    enabled    INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, module_id)
);

-- ---------------------------------------------------------------- 处罚与案件

CREATE TABLE IF NOT EXISTS moderation_actions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    case_number      INTEGER NOT NULL,
    action           TEXT    NOT NULL,
    target_id        INTEGER NOT NULL,
    moderator_id     INTEGER NOT NULL,
    reason           TEXT,
    duration_seconds INTEGER,
    automated        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    revoked_at       TEXT,
    revoked_by       INTEGER,
    revoke_reason    TEXT,
    UNIQUE (guild_id, case_number)
);

CREATE INDEX IF NOT EXISTS idx_moderation_actions_target
    ON moderation_actions (guild_id, target_id, created_at);

CREATE INDEX IF NOT EXISTS idx_moderation_actions_moderator
    ON moderation_actions (guild_id, moderator_id, created_at);

-- ---------------------------------------------------------------- 警告

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT,
    action_id    INTEGER REFERENCES moderation_actions (id),
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_warnings_active
    ON warnings (guild_id, target_id, active);

-- ---------------------------------------------------------------- GitHub 账号映射

CREATE TABLE IF NOT EXISTS github_accounts (
    guild_id        INTEGER NOT NULL,
    discord_user_id INTEGER NOT NULL,
    github_user_id  INTEGER NOT NULL,
    github_login    TEXT    NOT NULL,
    linked_at       TEXT    NOT NULL,
    PRIMARY KEY (guild_id, discord_user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_github_accounts_identity
    ON github_accounts (guild_id, github_user_id);

CREATE INDEX IF NOT EXISTS idx_github_accounts_login
    ON github_accounts (guild_id, github_login);

-- ---------------------------------------------------------------- 发布订阅

-- channel_id 可以指向文字频道，也可以指向帖子。
CREATE TABLE IF NOT EXISTS release_targets (
    guild_id          INTEGER NOT NULL,
    repo              TEXT    NOT NULL,
    channel_id        INTEGER NOT NULL,
    cursor_release_id INTEGER,
    created_by        INTEGER NOT NULL,
    created_at        TEXT    NOT NULL,
    PRIMARY KEY (guild_id, repo)
);

CREATE INDEX IF NOT EXISTS idx_release_targets_channel
    ON release_targets (guild_id, channel_id);

-- 单行表：记监听服务推到哪里了（游标来自服务端，用于去重与断点续传）。
CREATE TABLE IF NOT EXISTS watcher_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    cursor     INTEGER NOT NULL,
    updated_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------- 权限节点绑定

CREATE TABLE IF NOT EXISTS node_role_bindings (
    guild_id     INTEGER NOT NULL,
    node_pattern TEXT    NOT NULL,
    role_id      INTEGER NOT NULL,
    created_by   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, node_pattern, role_id)
);

CREATE INDEX IF NOT EXISTS idx_node_role_bindings_role
    ON node_role_bindings (guild_id, role_id);

-- ---------------------------------------------------------------- 激活码

-- 谁在哪一批里拿到了哪一枚。不存明文码。
CREATE TABLE IF NOT EXISTS key_deliveries (
    guild_id        INTEGER NOT NULL,
    batch_id        TEXT    NOT NULL,
    discord_user_id INTEGER NOT NULL,
    key_id          TEXT    NOT NULL,
    key_prefix      TEXT    NOT NULL,
    delivered_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, batch_id, discord_user_id)
);

CREATE INDEX IF NOT EXISTS idx_key_deliveries_batch
    ON key_deliveries (guild_id, batch_id);

-- 一次发码活动。drop_id 是短随机编号（便于人念、手打）。
CREATE TABLE IF NOT EXISTS key_drops (
    drop_id    TEXT PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    batch_id   TEXT    NOT NULL,
    team_id    TEXT    NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    mode       TEXT    NOT NULL,
    key_count  INTEGER NOT NULL,
    role_id    INTEGER,
    closes_at  TEXT,
    delivered  INTEGER NOT NULL DEFAULT 0,
    status     TEXT    NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_key_drops_due
    ON key_drops (status, closes_at);

CREATE TABLE IF NOT EXISTS key_drop_entries (
    drop_id         TEXT    NOT NULL REFERENCES key_drops (drop_id),
    discord_user_id INTEGER NOT NULL,
    entered_at      TEXT    NOT NULL,
    PRIMARY KEY (drop_id, discord_user_id)
);

-- 持有这些角色的人不能领取激活码。
CREATE TABLE IF NOT EXISTS key_denied_roles (
    guild_id   INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- ---------------------------------------------------------------- 自助身份组

CREATE TABLE IF NOT EXISTS reaction_roles (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    emoji      TEXT    NOT NULL,
    role_id    INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, message_id, emoji)
);

CREATE INDEX IF NOT EXISTS idx_reaction_roles_role
    ON reaction_roles (guild_id, role_id);
