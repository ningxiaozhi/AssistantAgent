-- ============================================================
-- 权限兜底脚本：以**超级用户**（通常是 postgres）执行一次
--
--   docker exec -i <容器> psql -U postgres -d assistant < deploy/init-grants.sql
--
-- 解决两类问题：
--   1. POSTGRES_USER 建的用户不是库属主 → 建不了 schema、装不了扩展
--   2. 早期用别的用户建过表 → 新用户能看见但改不了（must be owner of table）
--
-- 用法：把下面 dbuser 换成你 DATABASE_URL 里的用户名
-- 安全性：只转移 app schema 内对象的所有权，不改动任何数据
-- ============================================================

\set app_user 'dbuser'

-- 1) 装 pgvector（pgvector ≥ 0.5 是 trusted，超级用户跑最省事）
CREATE EXTENSION IF NOT EXISTS vector;

-- 2) 库级权限：能建 schema / 表
GRANT CREATE ON DATABASE assistant TO :"app_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"app_user";

-- 3) app schema 若已存在：把属主和内部对象一起转给应用用户
--    （这是 "must be owner of table" 的根治办法）
SELECT format('ALTER SCHEMA app OWNER TO %I', :'app_user')
 WHERE EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'app')
\gexec

DO $do$
DECLARE
    target text := :'app_user';
    r record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'app') THEN
        RETURN;  -- 还没有 app schema，等 first run 建
    END IF;

    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'app' LOOP
        EXECUTE format('ALTER TABLE app.%I OWNER TO %I', r.tablename, target);
    END LOOP;

    FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'app' LOOP
        EXECUTE format('ALTER SEQUENCE app.%I OWNER TO %I', r.sequencename, target);
    END LOOP;

    FOR r IN SELECT viewname FROM pg_views WHERE schemaname = 'app' LOOP
        EXECUTE format('ALTER VIEW app.%I OWNER TO %I', r.viewname, target);
    END LOOP;
END
$do$;

-- 4) public 下未来新建的表/序列自动可用
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";

-- 5) 验证：pgvector 版本 / 目标用户能否建表 / app 里的表属主
SELECT current_database()                                              AS db,
       (SELECT extversion FROM pg_extension WHERE extname = 'vector')  AS pgvector,
       has_database_privilege(:'app_user', current_database(), 'CREATE') AS can_create,
       has_schema_privilege(:'app_user', 'app', 'CREATE')              AS can_create_in_app;

SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'app' ORDER BY tablename;
