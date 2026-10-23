-- HTML 表单：绑定 key 活动、token 使用记录、提交动作状态。
ALTER TABLE form_bindings ADD COLUMN drop_id INTEGER;

CREATE TABLE IF NOT EXISTS form_tokens (
    jti            TEXT PRIMARY KEY,
    kind           TEXT    NOT NULL,
    guild_id       INTEGER NOT NULL,
    form_id        TEXT,
    publication_id INTEGER,
    user_id        INTEGER NOT NULL,
    subject_id     INTEGER,
    expires_at     TEXT    NOT NULL,
    used_at        TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_form_tokens_expiry
    ON form_tokens (expires_at);

CREATE TABLE IF NOT EXISTS form_actions (
    submission_id INTEGER NOT NULL,
    action        TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (submission_id, action)
);

CREATE TABLE IF NOT EXISTS form_recovery_channels (
    submission_id INTEGER PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    status        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    deleted_at    TEXT
);
