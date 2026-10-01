"""运行配置：环境变量前缀 AEGIS_，指标口径阈值集中在此，供量测与降级策略引用。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BusBackend = Literal["memory", "nats"]
StoreBackend = Literal["memory", "postgres"]
AnalyticsBackend = Literal["off", "clickhouse", "duckdb"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")

    app_name: str = "aegis"
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    http_host: str = "0.0.0.0"
    http_port: int = 8000

    bus_backend: BusBackend = "memory"
    nats_url: str = "nats://127.0.0.1:4222"
    nats_stream_prefix: str = "AEGIS"

    db_url: str = "sqlite+aiosqlite:///./data/aegis.db"

    # 运行态存储面：memory 只保留内存读视图；postgres 在同一读视图前加 PostGIS/pgvector 落库。
    # 选 postgres 但连不上时不降级成另一种实现，而是保留读视图 + 有界写缓冲重试（见 integrations）。
    store_backend: StoreBackend = "memory"
    pg_dsn: str = ""  # 空则回落到 db_url；此时 db_url 必须是 PostgreSQL DSN
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 8
    pg_apply_migrations_on_start: bool = True

    # 分析旁路：write-behind 消费者，off 时链路里完全不出现 OLAP 代码路径。
    analytics_backend: AnalyticsBackend = "off"
    clickhouse_host: str = "127.0.0.1"
    clickhouse_port: int = 8123
    clickhouse_database: str = "aegis"
    clickhouse_username: str = "default"
    clickhouse_password: str = ""
    clickhouse_secure: bool = False
    duckdb_path: str = "./data/edge_analytics.duckdb"
    analytics_apply_schema: bool = False
    analytics_close_grace_ms: int = 2_000

    # 基座层：接入网关
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topic_prefix: str = "field"
    weather_api_base_url: str = ""
    connector_poll_seconds: float = 60.0

    # LLM（可选；无密钥时研判走规则引擎降级路径）
    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"
    llm_timeout_seconds: float = 20.0

    # 交付通道：默认 mock，避免真实短信/北斗凭据缺失时阻塞开发
    delivery_mode: Literal["mock", "http"] = "mock"
    delivery_http_base_url: str = ""

    # 协同框架
    heartbeat_interval_seconds: float = 3.0
    heartbeat_miss_limit: int = 3
    default_request_deadline_ms: int = 10_000
    gateway_allow_unregistered_action: bool = False

    # 课题6 考核指标口径（量测阈值，单位见字段名）
    sla_sync_ms: int = 3_000
    sla_schedule_ms: int = 2_000
    sla_reschedule_ms: int = 10_000
    sla_ingest_seconds: float = 300.0
    sla_warning_gen_seconds: float = 180.0
    sla_reach_seconds: float = 1_200.0
    sla_collaboration_success_rate: float = 0.90
    sla_warning_accuracy: float = 0.80

    # 演示/压测用数据发生器
    simulator_enabled: bool = True
    simulator_interval_seconds: float = 2.0
    simulator_seed: int = 20260929

    hazard_scope: list[str] = Field(
        default_factory=lambda: [
            "landslide",
            "rockfall",
            "debris_flow",
            "avalanche",
            "lake_outburst",
        ]
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
