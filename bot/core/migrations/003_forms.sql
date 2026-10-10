-- 表单：表单本身（字段、用途、通过后动作）写在模块目录的 `bot/modules/forms/definitions.py` 里，
-- 这里只存运行时状态。表按顺序分组：每个表单的绑定 → 发布的卡片 → 分步草稿 → 正式提交。

-- 每个表单在服务器上的绑定。缺项表示「没绑」。审核频道与审核员角色只有申请类用得上；
-- 通知频道既收「有新提交」也收「通过后通知」；发放角色只有声明了 grant_role 的申请类用得上。
CREATE TABLE IF NOT EXISTS form_bindings (
    guild_id           INTEGER NOT NULL,
    form_id            TEXT    NOT NULL,
    review_channel_id  INTEGER,
    reviewer_role_id   INTEGER,
    notify_channel_id  INTEGER,
    grant_role_id      INTEGER,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, form_id)
);

-- 一次发布 = 频道里的一张卡片。按钮按 message_id 反查这一行，所以状态不存在 view 里。
-- capacity 为空表示不限人数；taken 是已提交份数（报名类用它封顶）。
CREATE TABLE IF NOT EXISTS form_publications (
    publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    form_id        TEXT    NOT NULL,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER,
    status         TEXT    NOT NULL,
    capacity       INTEGER,
    taken          INTEGER NOT NULL DEFAULT 0,
    closes_at      TEXT,
    created_by     INTEGER NOT NULL,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_form_publications_message
    ON form_publications (message_id);

CREATE INDEX IF NOT EXISTS idx_form_publications_due
    ON form_publications (status, closes_at);

CREATE INDEX IF NOT EXISTS idx_form_publications_form
    ON form_publications (guild_id, form_id, status);

-- 分步填写的草稿：每提交一步就写一次，所以中途关掉客户端或机器人重启都不会丢。
-- storage_key 是这一步已上传附件的目录名（附件一提交就下载，Discord 给的链接会过期）。
CREATE TABLE IF NOT EXISTS form_drafts (
    guild_id        INTEGER NOT NULL,
    form_id         TEXT    NOT NULL,
    discord_user_id INTEGER NOT NULL,
    step            INTEGER NOT NULL,
    answers         TEXT    NOT NULL,
    storage_key     TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (guild_id, form_id, discord_user_id)
);

-- 正式提交。attachment_dir 唯一，附件按它定位（提交时从草稿目录整体搬过来）。
-- review_message_id 让审核按钮能按消息 id 反查这一行。
CREATE TABLE IF NOT EXISTS form_submissions (
    submission_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    form_id           TEXT    NOT NULL,
    publication_id    INTEGER,
    discord_user_id   INTEGER NOT NULL,
    answers           TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    submitted_at      TEXT    NOT NULL,
    reviewed_by       INTEGER,
    reviewed_at       TEXT,
    decision_reason   TEXT,
    attachment_dir    TEXT    NOT NULL,
    review_channel_id INTEGER,
    review_message_id INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_form_submissions_dir
    ON form_submissions (attachment_dir);

CREATE INDEX IF NOT EXISTS idx_form_submissions_form
    ON form_submissions (guild_id, form_id, submitted_at);

CREATE INDEX IF NOT EXISTS idx_form_submissions_user
    ON form_submissions (guild_id, form_id, discord_user_id, status);

CREATE INDEX IF NOT EXISTS idx_form_submissions_review
    ON form_submissions (review_message_id);
