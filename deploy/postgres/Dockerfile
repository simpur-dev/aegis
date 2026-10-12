# AEGIS 空间数据底座：PostGIS + pgvector 同镜像（均为自由/宽松许可：GPL-2.0-or-later / PostgreSQL License）。
#
# 基座 `postgis/postgis:17-3.5` 是 Debian 11 (bullseye)，PGDG 源是镜像自带的，无需另加。
# 用 postgresql-$PG_MAJOR-pgvector 而非写死 17：BASE_IMAGE 被覆盖时包名随主版本走。
#
# 2026-10-12 实测到的上游变化（这台机器上照旧指令构建会直接失败）：
#   PGDG 撤掉了 bullseye 的当前发布目录 ——
#   `http://apt.postgresql.org/pub/repos/apt/dists/bullseye-pgdg/Release` 现在 404，
#   apt 报 `E: The repository '... bullseye-pgdg Release' does not have a Release file.` ⇒ RUN 退出码 100。
#   同时 `postgis/postgis:17-3.5` 重新拉取后仍是 bullseye 基座（`docker pull` 后 /etc/os-release 实读），
#   所以这不是本地缓存过期，是这条构建路径在源头就断了。
#   包体只在归档镜像上：`http://apt-archive.postgresql.org/pub/repos/apt/dists/bullseye-pgdg/Release` 200。
#
# 因此这里把 PGDG 源改指 apt-archive，而不是换基座升版本：本仓的验收证据钉在
# PostgreSQL 17.5 / PostGIS 3.5.2 / pgvector 0.8.6（见 docs/REPORT.md 的地理与向量能力实测、ADR-0003），
# 换 trixie 基座会得到 PostGIS 3.6.4 / pgvector 0.8.7，那批实测得重跑一遍才许写回文档。
# 归档源是冻结的（不再跟安全更新），这是明知故犯的取舍：宁可挂着这条注释，也不悄悄换掉被量过的数据面。
# 迁移触发条件：上游出 bookworm/trixie 基座的 postgis 官方镜像时，换 BASE_IMAGE 并复跑
# `tests/integration/test_persistence_postgres.py` 与 `test_geo_endpoints_live.py`（真库那两档），
# 数字对得上再改 docs/REPORT.md 里那行版本。
#
# DDL 不在此处执行：建表语句的唯一事实源是 backend/src/aegis/persistence/sql/*.sql，
# 由 postgres.apply_migrations() 按文件名序应用并记校验和台账，避免镜像与代码两套 schema 漂移。
ARG BASE_IMAGE=postgis/postgis:17-3.5
FROM ${BASE_IMAGE}

RUN sed -i 's#http://apt\.postgresql\.org/pub/repos/apt#http://apt-archive.postgresql.org/pub/repos/apt#' /etc/apt/sources.list.d/pgdg.list \
    && apt-get update -o Acquire::Check-Valid-Until=false \
    && apt-get install -y --no-install-recommends postgresql-$PG_MAJOR-pgvector \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 5432
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"
