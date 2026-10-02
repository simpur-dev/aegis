"""运行配置：环境变量前缀 AEGIS_，指标口径阈值集中在此，供量测与降级策略引用。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BusBackend = Literal["memory", "nats"]
StoreBackend = Literal["memory", "postgres"]
AnalyticsBackend = Literal["off", "clickhouse", "duckdb"]
RetrievalIndexBackend = Literal["local", "seekdb"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    http_host: str = "0.0.0.0"
    http_port: int = 8000

    bus_backend: BusBackend = "memory"
    nats_url: str = "nats://127.0.0.1:4222"
    nats_stream_prefix: str = "AEGIS"

    # 运行态存储面：memory 只保留内存读视图；postgres 在同一读视图前加 PostGIS/pgvector 落库。
    # 选 postgres 但连不上时不降级成另一种实现，而是保留读视图 + 有界写缓冲重试（见 integrations）。
    store_backend: StoreBackend = "memory"
    # 唯一的 PostgreSQL 连接串入口（asyncpg 口径；SQLAlchemy 的 +asyncpg 后缀由 persistence.dsn 收敛）。
    # store_backend=postgres 而这里留空是配置错误，容器构造即报错——静默回落到内存视图会把
    # "以为在落库、其实没有"变成最难查的一类事故。
    pg_dsn: str = ""
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
    # MQTT 推送腿默认关闭：开启需要 `[iot]` extra（aiomqtt）与一个真 broker；
    # 未开启时链路里完全不出现 MQTT 代码路径（与分析旁路同一套"关掉就没有这层"的口径）。
    mqtt_enabled: bool = False
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topic_prefix: str = "field"
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_qos: int = 1
    mqtt_keepalive_seconds: int = 30
    mqtt_client_id: str = "aegis-platform"
    # 单次摄取轮次之间能攒多少条读数：满了丢最旧并计数，绝不无界吃内存
    mqtt_buffer_limit: int = 5_000
    # 公开气象/水文接口（拉取腿）：留空则该腿在链路里完全不出现
    weather_api_base_url: str = ""
    weather_api_path: str = "/observation"
    weather_api_timeout_ms: int = 4_000
    # 工作流节点外呼（api_call / device_control）的主机白名单，逗号分隔。
    # 留空 = 这两类节点在产品形态下不可用（装配桥不注入 http_call，节点响亮失败）；
    # 之所以默认关：URL 由编排画布填写，放开就是把平台变成任意内网地址的代理。
    workflow_http_allowed_hosts: str = ""
    workflow_http_timeout_ms: int = 4_000

    # LLM（可选；无密钥时研判走规则引擎降级路径）
    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"
    llm_timeout_seconds: float = 20.0

    # 契约 Schema 目录：镜像/离线部署把它指到打包位置（后端镜像里是 /contracts）。留空 = 跟着
    # 源码树走（仓库根 contracts/），本机直接跑进程时不需要配。这个键必须真实存在：Dockerfile 里
    # 写过 `ENV AEGIS_CONTRACTS_DIR=/contracts` 而配置面没有它时，`extra="ignore"` 会把它静默丢掉，
    # 容器于是去找源码树相对路径的 contracts，镜像里没有，就在启动期炸成"契约目录不存在"。
    contracts_dir: str = ""

    # 案例知识层：默认纯内存提供者（零外部依赖，16 条内置预案模板，性质见 knowledge/cases.py）。
    # 给了 graphiti URI 才走图谱主路径，并在图谱不可用时自动回落到内存（降级链由 provider 保证）。
    knowledge_graphiti_uri: str = ""
    knowledge_recall_budget_ms: float = 400.0
    # 图谱凭据与嵌入口径都在配置面里声明：只在这里有一份，compose/.env.example 的键才能被测试校验
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    knowledge_embedding_model: str = "text-embedding-v3"
    knowledge_embedding_dim: int = 1024

    # 混合检索：稠密腿依赖 pgvector 连接，词法腿零依赖。默认关闭，开启需要模型与库连接都就位。
    retrieval_enabled: bool = False
    # 权重根目录：由仓库根 scripts/fetch_retrieval_models.py 预置（默认写 backend/data/models），
    # 其下按 bge-m3-int8/ 与 bge-reranker-v2-m3-int8/ 分目录。路径基准是后端进程的工作目录。
    retrieval_model_dir: str = "./data/models"
    # 索引引擎：local = pgvector 密集腿 + 进程内 BM25；seekdb = 两腿都进 seekdb
    # （VECTOR + HNSW 余弦 ANN 与 ngram 中文全文同库同表，见 retrieval/seekdb.py 头注释）。
    retrieval_index_backend: RetrievalIndexBackend = "local"
    seekdb_host: str = "127.0.0.1"
    seekdb_port: int = 2881
    seekdb_user: str = "root"
    # 只在驱动里存在，不进任何对外面（状态行只报 host:port/库/表）
    seekdb_password: str = ""
    seekdb_database: str = "test"
    seekdb_table: str = "aegis_knowledge_doc"
    # 预算取自本机实测（int8 ONNX + CPU ExecutionProvider，2026-10-01，语料为真实案例正文 ~305 字）：
    # 查询侧单条短文本嵌入热态 26ms；交叉编码 5 对 4201ms、10 对 8361ms（代价随序列长度接近平方）。
    # 5000ms 让"取回的 5 条全部重排"能在预算内跑完；相对 180s 的预警口径只占 2.8%。
    # 想压到 1s 量级要把 reranker 的 max_length 从 512 降到 128——那是拿正文后半段的排序质量换时延，
    # 必须由现场显式决定，所以这里不默认这么做。
    retrieval_budget_ms: float = 5_000.0

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
