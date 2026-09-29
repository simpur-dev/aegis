# AEGIS

**Adaptive Emergency Geo-hazard Intelligence System**
西藏山地灾害多智能体协同调控技术与平台 —— 平台侧（后端 + Web）

面向西藏高原 5 类典型山地灾害（滑坡、崩塌危岩、泥石流、冰雪雪崩、冰湖溃决），
构建"感知—研判—决策—执行—反馈"全链条协同调控平台。

- 应用形态：**纯 Web 平台**（响应式，不含原生移动端）
- 分工边界：**平台侧**（L1 接入 / L2 数据 / L4 服务 / L5 应用 / 基座）由本仓库承载；
  **L3 五大智能体**由智能体方按 `contracts/AGENT_INTEGRATION_SPEC.v1.md` 接入
- 架构依据：《课题6_项目架构设计》五层+一基座，融合"一张图 + 一总线 + 四预"扩展
- 理论框架：MA-RIAEW（多智能体协同赋能的突发事件风险情报感知及预警模式）

---

## 1. 目录结构

```
aegis/
├── contracts/                 # 平台与智能体的唯一接口契约（版本化）
│   ├── agent_message.v1.schema.json
│   ├── stu.v1.schema.json
│   └── AGENT_INTEGRATION_SPEC.v1.md
├── backend/
│   ├── src/aegis/
│   │   ├── config.py          # 运行配置与考核指标阈值口径
│   │   ├── logging.py         # 结构化 JSON 日志 + trace_id 上下文
│   │   ├── errors.py          # 类型化错误码（与规范 §5 对齐）
│   │   ├── domain/            # 枚举与契约的 Python 镜像（AgentMessage / STU / 领域记录）
│   │   ├── bus/               # 总线传输抽象、内存实现、NATS JetStream 实现、网关、能力注册中心
│   │   ├── observability/     # 时延账本（分位数/SLA 判定）、Prometheus 导出
│   │   ├── services/          # 触发规则引擎、风险定级、任务拆解、预警生成、靶向触达
│   │   ├── connectors/        # 数据接入：模拟场站、公开气象 API、摄取服务
│   │   ├── storage/           # 运行态存储（遥测/预警/任务/链路）
│   │   ├── pipeline/          # 灾害响应链路（智能体优先、平台降级）
│   │   ├── agents/            # Mock/参考智能体（开发与门禁用）
│   │   ├── container.py       # 平台装配与指标量测出口
│   │   └── api/               # FastAPI HTTP 接口与 SSE
│   ├── tests/
│   │   ├── unit/              # 单元测试与边界测试
│   │   ├── contract/          # 智能体接入规范 §7 一致性门禁
│   │   └── e2e/               # 端到端链路与指标实测
│   └── pyproject.toml
├── deploy/docker-compose.yml  # NATS / TimescaleDB / Neo4j / MinIO / Redis / EMQX / Prometheus
├── docs/                      # 架构说明与 ADR
└── scripts/                   # 演练、压测、演示脚本
```

## 2. 快速开始

```bash
# 1) 配置
cp .env.example backend/.env       # 至少填写 LLM 相关项（可留空走规则降级）

# 2) 安装后端依赖（使用 uv）
cd backend && uv sync --extra dev

# 3) 全量测试（离线可跑，总线使用内存实现）
uv run pytest -q

# 4) 起服务（内存总线，自带模拟场站数据与 Mock 智能体）
uv run python -m aegis.main

# 5) 需要真实基础设施时
docker compose -f deploy/docker-compose.yml up -d
AEGIS_BUS_BACKEND=nats uv run python -m aegis.main
```

接口文档：`http://localhost:8000/docs`　指标：`http://localhost:8000/metrics`

## 3. 一键演练

```bash
cd backend
uv run python -m scripts.drill --scenario surge     # 注入激增监测数据并跑通全链路
uv run python -m scripts.metrics_report            # 打印时延/成功率实测报告（对齐考核指标）
```

## 4. 关键设计约束

| 约束 | 说明 |
| --- | --- |
| 契约先行 | 智能体只经 NATS 总线与 `AgentMessage v1` 交互，不共享代码、不直连数据库 |
| 智能体优先、平台降级 | 每段链路在智能体缺位/超时/产出不合契约时自动回退规则引擎与本地剧本，链路不中断 |
| 指标必须实测 | `latency_report()` 输出运行时埋点的分位数与 SLA 判定，验收数字全部可追溯到样本 |
| 降级留痕 | 降级写入 `ChainResult.degradations`（可归因），致命错误写入 `errors`（判失败） |
| 双语不伪造 | 未接入可信翻译能力时预警仅中文并标记 `translation_pending`，不生成未经审核的藏语文本 |

## 5. 开发规范

- 提交遵循 Conventional Commits（`feat|fix|chore|test|docs|perf|refactor(scope): 描述`），不带 AI 生成尾注
- 门禁：`ruff check` + `mypy` + `pytest` 全绿方可合并（见 `.github/workflows/ci.yml`）
- 契约变更：语义化版本 + 更新一致性测试套件 + 记录 ADR

## 6. 许可

AGPL-3.0（与基线项目 NexusMind 保持一致）。
