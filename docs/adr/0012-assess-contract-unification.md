# ADR-0012：研判契约统一——删除适配层、证据数据面、区域画像与生产方置信度

- 状态：已采纳（2026-11-09）
- 日期：2026-11-09
- 相关：ADR-0001（契约先行边界）、ADR-0002（智能体优先平台降级）、
  ADR-0008（模块边界与 AST 守卫）、ADR-0011（外部智能体模块并入）、
  `contracts/AGENT_INTEGRATION_SPEC.v1.md`

## 背景

ADR-0011 把外部交付的 LangGraph 智能体模块并入了 `backend/src/aegis/agents_ext/`，
但当时只在研判段接了一个动作，并且是**经三层适配层**接的
（`adapter.py` 契约壳 / `contract_face.py` 语义翻译 / `levels.py` 口径对照）。
那条链路有三个硬缺口，加上一个架构问题：

1. **输入层级不对应**：平台研判请求只带 `hits`（规则引擎压缩过的结论），
   外部因子引擎要的是物理因子（`rainfall_mm_h`、`slope_angle_deg` 等）。
   适配层只能把 `hits` 包成事件骨架，补不出从未进入请求的信息。
2. **输出缺 `confidence`**：平台采纳门槛要求 0..1 的置信度，外部结果没有这一格；
   填 `0.0` 是伪造，适配层的处理只能是不产结论。
3. **静态孕灾属性没有数据面**：五个灾种的必需要素普遍包含地形/勘测属性
   （`catchment_area_km2`、`lake_volume_m3`、`snow_depth_cm` 等），平台遥测里不存在。
4. **适配层本身是架构债**：所有字段都要在两套词汇之间"翻译一次"，
   改一条映射要读懂订阅/幂等/超时代码；企业级链路不该长期靠翻译层缝合。

用户已拍板：观测数据**推 + 拉都做**；静态属性**新建区域级 `site_profiles` 数据面**；
**彻底删除适配层**，两端共用一套接口；数据出处**透传到 RiskVerdict / API / 台账**。

## 决策

### 1. 一套 DTO，三个实现共用

`backend/src/aegis/domain/assess.py` 定义 `assess.v1`：

- `AssessRequest`：`region_code`、`window`、`hazard_type`（主灾种）、
  `candidate_hazards`、`hits`、`rule_level`、`intake`、`evidence`、`data_refs`；
- `AssessResponse`：`hazard_type`、`region_code`、`risk_level`（平台 1–5）、
  `confidence`、`confidence_basis`、`rationale`、`evidence_refs`、`assessed_by`、
  `human_review_suggested`、`data_provenance`、`assessment`（引擎信封）；
- `AssessEvidence` / `FactorObservation` / `UnmappedMetric`：输入侧证据包。

全部 `extra="forbid"`、数值拒绝 NaN/Inf、条数与长度有上限。
builtin（`agents/assess.py`）、mock（`agents/mock.py`）、ext（`agents_ext/risk_agent/agent.py`）
三个实现共用这套模型；平台不再认识"某个实现的结果 dict"。
错误码集中为 `E_ASSESS_*`（`E_ASSESS_INSUFFICIENT` / `E_ASSESS_UNCERTAIN` /
`E_ASSESS_FAILED` / `E_ASSESS_HAZARD_UNSUP` / `E_ASSESS_BAD_LEVEL` /
`E_ASSESS_CONF_MISSING` / `E_ASSESS_BAD_INPUT`），旧 `E_EXT_*` 随适配层一起删除。

**为什么 DTO 住 `aegis.domain` 而不是某个实现包**：它现在是平台与实现之间的唯一语言，
放到任一实现包里都会让另一方反向依赖；住 domain 才能让 builtin / mock / ext 平级。

### 2. 证据数据面：平台构造，随请求推送，另开读面

`backend/src/aegis/pipeline/assess_input.py` 的 `AssessEvidenceBuilder`：

- 合并本次触发链读数与存储窗口读数，按 `(station_id, metric, observed_at)` 去重；
- 过滤 `quality_flag != "ok"` 与非有限值，剔除数量写进 `notes`；
- 按**唯一派生表**生成因子：`rain_10min→rainfall_mm_h`（×6）、
  `displacement_mm→displacement_mm_day`（每站斜率×24）、
  `crack_aperture_mm→crack_width_mm`、`air_temperature_c→air_temp_c`、
  `lake_level_m→water_level_ratio`（除以画像里的 `dam_height_m`）；
- **没有同一口径承接的遥测不近似**：`new_snow_cm`、`dam_seepage_turbidity_ntu`、
  `debris_level` 等进 `unmapped_metrics` 并写明原因；
- 区域画像的静态属性以 `quality="site"` 原样透传；
- 自限 48 条观测 / 48KB，超限置 `truncated=True` 并留 note，不静默截断。

研判段在 `_agent_ready` 通过后才构建证据（纯本地部署零额外查询）；
构建异常记一条研判降级并继续派单，不因证据构建炸段。
证据同时经 `GET /api/v1/assess/evidence` 暴露；ext 出进程部署时用 stdlib `urllib`
拉取（`AEGIS_EXT_DATA_API_URL` / `AEGIS_EXT_DATA_API_TOKEN`，只从环境变量读）。
处理顺序：**请求内 evidence → 拉取 → 都没有则 `E_ASSESS_INSUFFICIENT`**。

### 3. 区域级 `site_profiles`

`domain/site.py` 定义 `SiteProfile` / `SiteAttribute`；`storage/site_profiles.py` 是内存实现，
`StoreProtocol` 增 `upsert/get/list` 三个方法，Postgres 走 `persistence/sql/014_site_profiles.sql`
（`region_code` 主键）。导入命令 `backend/scripts/import_site_profiles.py` 沿用
`import_stations.py` 的口径：CSV/JSONL、先全量校验再写、未知列失败、重复 region 失败、
`--dry-run`、默认拒绝内存视图。演示数据 `backend/data/site_profiles.demo.jsonl`
覆盖三个演示区，provenance 标 `demo` / `is_mock=True`。

**边界**：只放静态/慢变勘测属性。雨量、位移速率、湖水位这类动态触发量必须来自遥测，
写进画像就绕过了证据构建器的派生与质量过滤。

### 4. 删除适配层：外部实现直接实现契约

删除 `agents_ext/adapter.py`、`contract_face.py`、`levels.py`。
`risk_agent/agent.py` 的 `RiskAssessAgent` 继承平台 `AgentKit`，直接：

- 校验 `action == assess.hazard`、`AssessRequest.model_validate`；
- 取请求内证据或拉取证据；
- 调 `run_risk_agent(request, evidence) -> AssessResponse`（同步图放 `asyncio.to_thread`）；
- 返回 `Reply(payload=response.model_dump(mode="json"))`。

`runtime.build_ext_agents` 只注册这一个本体（`agent_id="assess.agents_ext"`）。
其余动作**不注册**——网关查不到承接方记 `no_agent`，平台按既有本地降级轨走；
这比注册一个"接了但接不了"的本体诚实。`risk_agent/legacy.py` 保留旧 events 口径，
**只服务未接线的 manager / data_agent**，平台路径不引用，ADR 明记待退役。

**为什么删除而不是保留成薄封装**：适配层的问题是"每个字段翻译一次"的成本与
两个词汇表的分叉。统一 DTO 后，翻译这件事没有存在理由；留下薄封装只会让下一个人
继续在封装里加字段映射。

### 5. 置信度由生产方自产

`agents_ext/risk_agent/confidence.py`：
`confidence = min(0.60, 0.15 + 0.40×coverage + 0.05×validation)`，
`coverage = min(P 组覆盖率, T 组覆盖率)`，`validation` 取验证节点的
`is_valid && evidence_sufficient`。`confidence_basis` 明写四项可复算。
上限 0.60 写死在方法里：数据齐全只能证明"喂饱了引擎"，不能证明"引擎能预警"。

平台**只校验不代填**：`AssessResponse` 要求 `confidence ∈ [0,1]` 且
`confidence_basis` 非空；缺了就拒绝（`E_ASSESS_CONF_MISSING`）。
外部结论继续标 `data_provenance.is_mock=True`、`human_review_suggested=True`。

### 6. 溯源成为一等字段

`RiskVerdict` 增 `data_provenance` / `confidence_basis` / `assessment`；
`as_payload()` 原样带出，`chain_runs.risk` 直接存 payload，
API 与前端 `ReportRiskDto` 加可选字段。读者从台账就能回答
"这条结论的数据是什么出处、置信度依据是什么、引擎信封长什么样"。

### 7. 灾种键统一

`agents_ext/hazard_registry.py` 的标准键直接取平台枚举值
（`HazardType.AVALANCHE.value` / `HazardType.LAKE_OUTBURST.value`）。
旧键 `ice_rock_snow_avalanche` / `glacial_lake_outburst` 只保留在别名表里，
供旧外部调用方兼容。代码里不再维护第二套灾种常量。

### 8. 导入脚本退役

`import_external_agents.py` / `import_external_agents_all.sh` /
`import_external_agents_tests.py` / `postprocess_external_agents*.py` 移到
`archive/external_agents_source_20261109/scripts/`：外部实现现在**在树维护**，
重跑导入会把本次重构冲掉。archive README 写明"仅保留原始交付快照，不再作为更新入口"。

## 后果

**好的**：

- 平台、builtin、mock、ext 说同一套请求/响应语言，所有字段只映射一次；
- 研判输入从"规则命中的压缩结论"变成"可复核的观测证据"，
  数据不足会诚实拒绝，不再出现"拿不到就说没有风险"；
- 静态孕灾属性有了正式数据面与导入命令，不再靠适配层猜；
- 置信度有生产方依据、有上限、有免责声明，台账与 API 能读到；
- 删掉三层翻译代码，链路少一层，字段来源与错误码都只有一处定义。

**要付的代价／保留的限制**：

- **MOCK 权重与原型公式不因接线而变真**：外部结论仍是 demo 级，
  `is_mock=True`、人工核签恒真、置信度封顶 0.6；
- **只有研判段接线**：plan / execute / feedback / perceive 不注册，网关记 `no_agent`；
- **外部的 LLM 仍绕开平台网关**：不进模型台账、不经提示注入围栏（ADR-0011 §5 仍成立）；
- **线程里的 LangGraph 图取消不掉**：deadline 只取消协程，超时后线程继续跑
  （ADR-0011 §3 仍成立）；
- **manager / data_agent 仍在 legacy 口径**：`run_risk_agent_legacy` 只服务未接线 manager，
  平台路径禁止引用；
- **冰湖/雪崩可能长期"数据不足"**：事件标志类因子当前没有遥测来源，
  诚实返回 `E_ASSESS_INSUFFICIENT`，不伪造。

## 验证

- `tests/unit/test_domain_assess_contract.py`：DTO 往返、严格性、边界与错误码；
- `tests/unit/test_assess_evidence_builder.py`：派生、质量过滤、禁止映射、去重、出处、截断；
- `tests/unit/test_site_profiles_store.py` / `test_import_site_profiles.py`：存储与导入口径；
- `tests/unit/agents_ext/test_ext_factor_engine_observations.py` / `test_ext_confidence.py`：
  覆盖率门控与置信度性质；
- `tests/unit/test_agents_ext_service.py`：动作/请求/证据/拉取/异常的错误通道；
- `tests/unit/test_stages_assess_unified.py`：研判段的证据构建、采纳与降级；
- `tests/contract/test_agents_ext_conformance.py`：外部实现过契约 7 项一致性门禁；
- `tests/api/test_api.py::TestAssessDataApi`：证据读面、画像读面、遥测时间窗；
- `tests/integration/test_assess_ext_e2e.py`：内存真栈下研判段走 `mode=agent`，溯源/置信度/台账三键落地；
- 全套 `uv run pytest`、`uv run ruff check src tests`、`uv run mypy src`。
