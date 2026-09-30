-- 001_extensions：空间与向量扩展。
-- postgis 提供 geography 类型与 ST_* 函数，vector 提供 pgvector 的 vector 类型与 HNSW 访问法。
-- 二者均由 deploy/postgres/Dockerfile 在同一镜像内提供（postgresql-17-pgvector），
-- 因此本文件只声明扩展，不做任何"扩展不存在则降级"的分支：缺扩展必须显式失败。
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
