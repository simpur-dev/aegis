# AEGIS 空间数据底座：PostGIS + pgvector 同镜像（均为自由/宽松许可：GPL-2.0-or-later / PostgreSQL License）。
#
# 包名已在本机对镜像实测确认（2026-09：docker run --rm postgis/postgis:17-3.5 bash -c
#   "apt-get update && apt-cache policy postgresql-17-pgvector"）：
#   PG_MAJOR=17，Candidate = 0.8.6-1.pgdg11+1，
#   来源 = http://apt.postgresql.org/pub/repos/apt bullseye-pgdg/main（镜像自带 PGDG 源，无需另加源）。
# 用 postgresql-$PG_MAJOR-pgvector 而非写死 17：BASE_IMAGE 被覆盖时包名随主版本走。
#
# DDL 不在此处执行：建表语句的唯一事实源是 backend/src/aegis/persistence/sql/*.sql，
# 由 postgres.apply_migrations() 按文件名序应用并记校验和台账，避免镜像与代码两套 schema 漂移。
ARG BASE_IMAGE=postgis/postgis:17-3.5
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-$PG_MAJOR-pgvector \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 5432
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"
