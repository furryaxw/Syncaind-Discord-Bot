-- 表单提交实际发出的 key；用于幂等重试与审计。
CREATE TABLE IF NOT EXISTS form_key_grants (
    submission_id INTEGER NOT NULL,
    key_id        INTEGER NOT NULL,
    pool_id       INTEGER NOT NULL,
    delivered_at  TEXT    NOT NULL,
    PRIMARY KEY (submission_id, key_id)
);

CREATE INDEX IF NOT EXISTS idx_form_key_grants_submission
    ON form_key_grants (submission_id);
