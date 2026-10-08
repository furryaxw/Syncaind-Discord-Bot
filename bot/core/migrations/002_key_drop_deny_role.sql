-- 一次活动可以额外排除一个角色：只对这一次活动生效，叠加在服务器级黑名单之上。
ALTER TABLE key_drops ADD COLUMN deny_role_id INTEGER;
